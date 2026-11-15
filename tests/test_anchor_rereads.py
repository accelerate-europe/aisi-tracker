"""Re-reading an anchored quotation (docs/design/claims-1.1.md, section 4): each anchor is a span with its own hash,
and the excerpt counts as found only when every span is."""

import copy

import pytest

from scripts import monitor_sources as monitor
from scripts import rereads, snapshot_sources
from scripts.references import excerpt_spans, located_spans
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
    lost = {"id": rereads.quote_id("Its tasks:"), "in_cited_copy": True, "in_checked_copy": False}
    # As the check log keeps them (ids only, no texts), with the second excerpt's anchor entry added.
    quotes = [{k: q[k] for k in ("id", "in_cited_copy", "in_checked_copy")} for q in result["quotes"]]
    result = {**result, "quotes": [*quotes, lost]}
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


def cross_copy_data(*, unbound=False):
    published = data(anchors=(LEAD_IN,))
    owner, = published["sources"]
    excerpt, = owner["excerpts"]
    excerpt["anchors"].append({"text": DEFINITION, "kind": "definition", "source_id": "s2"})
    if unbound:
        excerpt["url"] = "https://example.org/another-copy"
    named = {"id": "s2", "url": "https://example.org/unit", "snapshot": {"retrieved": "2026-11-01T08:00:00Z"}}
    published["sources"].append(named)
    return published


@pytest.mark.parametrize("unbound", [False, True])
def test_other_copy_anchor_replays_with_named_source_and_loses_excerpt(unbound):
    published = cross_copy_data(unbound=unbound)
    rows = [row("2026-12-18", "trivial", quotes=[(Q, True, True), (H, True, True), (D, True, True)]),
            {**row("2026-12-04", "substantive", quotes=[(D, True, False)]), "source_id": "s2"}]
    replayed = rereads.export(published, rows, is_archive=lambda url: False)
    assert D not in replayed["sources"].get("s1", {})
    assert replayed["sources"]["s2"][D] == {"state": "quote_gone", "last_found": "2026-11-01", "since": "2026-12-04", "last_attempt": "2026-12-04"}
    rereads.join(published, replayed)
    excerpt = published["sources"][0]["excerpts"][0]
    assert excerpt["reread"] == {"state": "quote_gone", "last_found": "2026-11-01", "since": "2026-12-04"}
    assert published["institutes"][0]["claims"][0]["reread"] == excerpt["reread"]
    assert any("source s2: quote not found in the source since 2026-12-04 (1 quote)" in line
               for line in rereads.alerts(published, replayed, "2026-12-20"))


def test_other_copy_anchor_without_monitor_rows_uses_named_copy_date():
    published = cross_copy_data()
    states = rereads.quote_states(published, {})
    assert states["s2", D]["last_found"] == "2026-11-01"
    assert ("s1", D) not in states


def test_a_quote_without_its_own_state_is_not_confirmed_by_another_sources_anchor():
    published = cross_copy_data()
    published["sources"][0].pop("snapshot")
    rereads.join(published, {"sources": {"s2": {D: entry("confirmed", last_found="2026-12-18")}}})
    assert "reread" not in published["sources"][0]["excerpts"][0]


@pytest.mark.parametrize("unbound", [False, True])
def test_copy_spans_bind_other_copy_anchors_even_when_the_quote_is_unbound(unbound):
    published = cross_copy_data(unbound=unbound)
    owner, named = published["sources"]
    excerpt, = owner["excerpts"]
    assert excerpt_spans(excerpt) == [QUOTE, LEAD_IN]
    assert located_spans(owner["id"], excerpt) == [("s1", QUOTE), ("s1", LEAD_IN), ("s2", DEFINITION)]
    assert snapshot_sources.bound_quotes(owner, published["sources"]) == ([] if unbound else [QUOTE, LEAD_IN])
    assert snapshot_sources.bound_quotes(named, published["sources"]) == [DEFINITION]


def test_monitor_checks_incoming_anchor_and_digest_names_both_sources(tmp_path):
    text = "Its tasks:"
    excerpt = {"text": "Models are tested.", "url": "https://example.org/other-copy", "supports": [["institutes", {"id": "lab"}, "mandate"]],
               "anchors": [{"kind": "membership", "text": text, "source_id": "src"}]}
    owner = {"id": "owner", "title": "Lab programme", "excerpts": [excerpt]}
    # An identical quote and anchor must still be reported as an anchor belonging to another source.
    second = {**copy.deepcopy(excerpt), "text": text}
    owner["excerpts"].append(second)
    cited = OLD.replace(b"<p>The institute was", b"<p>Its tasks: the institute was")
    kept = setup(tmp_path / "kept", cited, cited=cited, excerpts=False, sources=[owner])
    assert all(q["in_checked_copy"] for q in kept["quotes"])
    result = setup(tmp_path / "lost", OLD, cited=cited, excerpts=False, sources=[owner])
    assert result["class"] == "substantive" and result["reason"] == "quote_lost"
    assert all(q["id"] == rereads.quote_id(text) and q["supports"] == ["institutes › lab › mandate"] for q in result["quotes"])
    result["quotes"] = [{k: q[k] for k in ("id", "in_cited_copy", "in_checked_copy")} for q in result["quotes"]]
    digest = monitor.markdown(report_for(result), {"src": {"id": "src", "title": "Membership source"}, "owner": owner}, {})
    assert "Membership source" in digest and "`src`" in digest
    assert '- anchor "Its tasks:" of "Models are tested." from source `owner` (Lab programme)' in digest
    assert '- anchor "Its tasks:" of "Its tasks:" from source `owner` (Lab programme)' in digest


def test_snapshot_replacement_checks_the_named_sources_incoming_anchor(tmp_path):
    from tests.test_snapshot_sources import FakeFetcher

    named = {"id": "unit", "url": "https://example.org/unit", "title": "Institute unit membership", "excerpts": []}
    owner = {"id": "owner", "url": "https://example.org/owner", "excerpts": [{"text": QUOTE, "supports": [],
             "url": "https://example.org/other-copy", "anchors": [{"kind": "membership", "text": "founded in 2024", "source_id": "unit"}]}]}
    fetcher = FakeFetcher(tmp_path / "old-cache")
    fetcher.add(named["url"], OLD)
    first = snapshot_sources.process_source(named, fetcher, tmp_path / "private", tmp_path / "fallback", sources=[owner, named])
    snapshot_sources.apply_result(named, first)
    newer = FakeFetcher(tmp_path / "new-cache")
    newer.add(named["url"], OLD.replace(b"founded in 2024", b"founded in 2025"), retrieved="2026-10-19T00:00:00Z")
    result = snapshot_sources.process_source_safe(named, newer, tmp_path / "private", tmp_path / "fallback", sources=[owner, named])
    assert result["change"] == "bound_quote_missing"
    assert result["bound_quotes"] == {"total": 1, "lost_in_new_copy": 1, "gained_in_new_copy": 0, "in_neither": 0}


@pytest.mark.parametrize("route", ["direct", "headless", "jina", "wayback"])
def test_monitor_routes_include_anchors_from_an_unselected_owner(tmp_path, monkeypatch, route):
    from tests.test_monitor_checks import Run

    run = Run(tmp_path, monkeypatch, excerpts=[])
    owner = {"id": "owner", "url": "https://example.org/owner", "title": "Owner text", "excerpts": [
        {"text": QUOTE, "url": "https://example.org/elsewhere", "supports": [["institutes", {"id": "lab"}, "name"]],
         "anchors": [{"text": "founded in 2024", "kind": "membership", "source_id": "src0"}]}]}
    run.data["sources"].append(owner)
    run.write_data()
    run.responses[route] = OLD
    assert run.run("--only", "src0") == 0
    checked, = run.rows()
    span, = checked["quotes"]
    assert span["id"] == rereads.quote_id("founded in 2024")
    assert span["in_cited_copy"] and span["in_checked_copy"]
    assert checked["route"] == route
