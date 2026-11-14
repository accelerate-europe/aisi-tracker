"""Re-reading an anchored quotation (docs/design/claims-1.1.md, section 4): each anchor is a span with its own hash,
and the excerpt counts as found only when every span is."""

from scripts import monitor_sources as monitor
from scripts import rereads, snapshot_sources
from tests.test_monitor_sources import OLD, no_requests, setup  # noqa: F401 - the autouse fixture applies here too
from tests.test_rereads import entry, file, row

QUOTE = "monitoring the emergence of unforeseen risks stemming from general-purpose AI models"
LEAD_IN = "The Office shall perform the following tasks stemming from the forthcoming Regulation"
DEFINITION = "The European Artificial Intelligence Office (the ‘Office’) is established."
Q, H, D = (rereads.quote_id(t) for t in (QUOTE, LEAD_IN, DEFINITION))
CLAIM = "/institutes/0/claims/0/text"


def data(as_of="2026-12-20", anchors=(LEAD_IN, DEFINITION)):
    excerpt = {"text": QUOTE, "supports": [CLAIM],
               "anchors": [{"text": t, "kind": k} for t, k in zip(anchors, ("heading", "definition"))]}
    return {"dataset": {"as_of": as_of, "version": "1.1.0"},
            "changelog": [{"version": "1.0.9", "date": "2026-11-20"}, {"version": "1.1.0", "date": as_of}],
            "sources": [{"id": "s1", "url": "https://example.org/a", "snapshot": {"retrieved": "2026-10-01T08:00:00Z"},
                         "excerpts": [excerpt]}],
            "jurisdictions": [], "institutes": [{"id": "eu", "claims": [{"id": "claims-1", "kind": "function", "text": "x"}]}]}


def joined(entries, **kwargs):
    published = data(**kwargs)
    rereads.join(published, file(**entries))
    return published["sources"][0]["excerpts"][0]["reread"], published["institutes"][0]["claims"][0]


def test_export_replays_every_span():
    rows = [row("2026-12-10", "substantive", quotes=[(Q, True, True), (H, True, False), (D, True, True)])]
    entries = rereads.export(data(), rows, is_archive=lambda url: False)["sources"]["s1"]
    assert set(entries) == {Q, H, D}
    assert (entries[Q]["state"], entries[H]["state"], entries[D]["state"]) == ("confirmed", "quote_gone", "confirmed")


def test_confirmed_only_when_every_span_is_found():
    found = entry("confirmed", last_found="2026-12-18")
    reread, claim = joined({Q: found, H: found, D: found})
    assert reread["state"] == "confirmed" and claim["reread"]["state"] == "confirmed"


def test_only_the_anchor_gone_is_quote_gone_from_the_anchor_s_loss():
    """The quotation is found, the lead-in is not: the excerpt no longer shows whom it is about."""
    reread, claim = joined({Q: entry("confirmed", last_found="2026-12-18"), D: entry("confirmed", last_found="2026-12-18"),
                            H: entry("quote_gone", "2026-12-04", last_found="2026-11-27")})
    assert reread == {"state": "quote_gone", "last_found": "2026-11-27", "since": "2026-12-04"}
    assert claim["reread"]["state"] == "quote_gone" and claim["expired"] is False
    _, later = joined({Q: entry("confirmed", last_found="2027-01-10", last_attempt="2027-01-10"),
                       D: entry("confirmed", last_found="2027-01-10", last_attempt="2027-01-10"),
                       H: entry("quote_gone", "2026-12-04", last_found="2026-11-27", last_attempt="2027-01-10")}, as_of="2027-01-10")
    assert later["expired"] is True  # the same one-month deadline as a lost quotation


def test_two_losses_take_the_earlier_start():
    reread, _ = joined({Q: entry("quote_gone", "2026-12-10", last_found="2026-12-03"), D: entry("confirmed"),
                        H: entry("quote_gone", "2026-12-04", last_found="2026-11-27")})
    assert reread == {"state": "quote_gone", "last_found": "2026-11-27", "since": "2026-12-04"}


def test_an_anchor_without_a_monitor_row_takes_the_copy_date():
    """An anchor added after the last monitor run has only the cited copy, like a new quotation."""
    reread, _ = joined({Q: entry("confirmed", last_found="2026-12-18")})
    assert reread == {"state": "not_attempted", "last_found": "2026-10-01", "since": None}


def test_a_lost_anchor_alerts_like_a_lost_quote():
    published = data()
    lines = rereads.alerts(published, file(**{Q: entry("confirmed"), D: entry("confirmed"),
                                              H: entry("quote_gone", "2026-12-04")}), "2026-12-20")
    assert any("source s1: quote not found in the source since 2026-12-04 (1 quote)" in line for line in lines)


def test_the_monitor_compares_each_anchor(tmp_path):
    """The cited copy carries quotation and lead-in; the new copy keeps the quotation and drops the lead-in."""
    cited = OLD.replace(b"<p>The institute was", b"<p>Its tasks: the institute was")
    anchors = [{"text": "Its tasks:", "kind": "heading"}]
    result = setup(tmp_path / "kept", cited, cited=cited, anchors=anchors)
    assert result["class"] == "unchanged"
    new = cited.replace(b"Its tasks: ", b"").replace(b"publishes reports.", b"publishes reports. Updated.")
    result = setup(tmp_path / "lost", new, cited=cited, anchors=anchors)
    assert (result["class"], result["reason"]) == ("substantive", "quote_lost")
    spans = {q["text"]: (q["in_cited_copy"], q["in_checked_copy"]) for q in result["quotes"]}
    assert spans["founded in 2024"] == (True, True) and spans["Its tasks:"] == (True, False)
    source = {"id": "src", "title": "News", "excerpts": [{"text": "founded in 2024", "supports": [], "anchors": anchors}]}
    text = monitor.markdown(report_for(result), {"src": source}, {})
    assert '- anchor "Its tasks:" of "founded in 2024"' in text


def report_for(result):
    return {"sources": [{**result, "source_id": "src"}], "counts": {}, "started": "2026-12-01", "seconds": 1.0,
            "dataset_version": "1.1.0", "live_unchecked": 0, "by_route": {}, "unreached": [], "next_checks": [], "discovery": []}


def test_a_shared_lost_anchor_is_reported_for_each_excerpt(tmp_path):
    """Two quotations under one lead-in: losing it is reported once against each, never twice against the first."""
    cited = OLD.replace(b"<p>The institute was", b"<p>Its tasks: the institute was")
    anchors = [{"text": "Its tasks:", "kind": "heading"}]
    new = cited.replace(b"Its tasks: ", b"").replace(b"publishes reports.", b"publishes reports. Updated.")
    result = setup(tmp_path, new, cited=cited, anchors=anchors)
    second = {"text": "national security risks", "supports": [["institutes", {"id": "xx-aisi"}, "mandate"]], "anchors": anchors}
    lost = {"id": rereads.quote_id("Its tasks:"), "text": "Its tasks:", "in_cited_copy": True, "in_checked_copy": False}
    result = {**result, "quotes": [*result["quotes"], lost]}  # the monitor's entry for the second excerpt's anchor
    source = {"id": "src", "title": "News", "excerpts": [
        {"text": "founded in 2024", "supports": [["jurisdictions", {"id": "XX"}, "name"]], "anchors": anchors}, second]}
    lines = [line for line in monitor.markdown(report_for(result), {"src": source}, {}).splitlines() if 'anchor "Its tasks:"' in line]
    assert lines == ['    - anchor "Its tasks:" of "founded in 2024" supports jurisdictions › XX › name',
                     '    - anchor "Its tasks:" of "national security risks" supports institutes › xx-aisi › mandate']


def test_rows_from_before_the_anchor_leave_it_not_attempted():
    """Check-log rows written before an anchor existed carry no entry for it: the anchor keeps the copy date, and
    after 14 days without an attempt the excerpt reads not_attempted, never confirmed by the quotation's rows."""
    rows = [row("2026-12-10", "trivial", quotes=[(Q, True, True)])]
    entries = rereads.export(data(), rows, is_archive=lambda url: False)["sources"]["s1"]
    assert entries[Q]["last_found"] == "2026-12-10"
    assert entries[H] == {"state": "confirmed", "last_found": "2026-10-01", "since": None, "last_attempt": "2026-10-01"}
    published = data()
    rereads.join(published, {"checked_to": "2026-12-10", "runs": ["r1"], "sources": {"s1": entries}})
    assert published["sources"][0]["excerpts"][0]["reread"] == {"state": "not_attempted", "last_found": "2026-10-01", "since": None}


def test_a_new_copy_without_an_anchor_does_not_replace_the_cited_copy():
    source = {"url": "https://example.org/a", "excerpts": [{"text": QUOTE, "supports": [], "anchors": [{"text": LEAD_IN, "kind": "heading"}]},
                                                            {"text": "elsewhere", "supports": [], "url": "https://example.org/other"}]}
    assert snapshot_sources.bound_quotes(source) == [QUOTE, LEAD_IN]
