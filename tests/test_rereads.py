"""Per-quote re-read states, expiry and alerts (docs/design/claims-1.1.md, section 4), without the private log."""

import json

import pytest

from scripts import monitor_sources as monitor
from scripts import rereads

A, B = "The institute tests models.", "It publishes reports."
QA, QB = rereads.quote_id(A), rereads.quote_id(B)


def source(*texts, sid="s1", retrieved="2026-10-01T08:00:00Z", **extra):
    return {"id": sid, "url": "https://example.org/a", "snapshot": {"retrieved": retrieved},
            "excerpts": [{"text": t, "supports": [], **extra} for t in texts]}


def row(day, klass, route="direct", quotes=(), sid="s1", run="r1", observation=None, **extra):
    observation = observation or ("archive" if route == "wayback" else "live")
    return {"run_id": run, "source_id": sid, "checked": f"{day}T09:00:00Z", "route": route, "class": klass,
            "observation": observation,
            "quotes": [{"id": q, "in_cited_copy": cited, "in_checked_copy": checked} for q, cited, checked in quotes],
            **extra}


def no_archive(url):
    return False


def export(data, rows, is_archive=no_archive):
    return rereads.export(data, rows, is_archive=is_archive)


def replay(rows, *texts, is_archive=no_archive):
    return export({"sources": [source(*(texts or (A,)))]}, rows, is_archive)["sources"]["s1"]


def test_found_moves_last_found_and_clears_failure():
    entry = replay([row("2026-10-10", "blocked"), row("2026-10-20", "trivial", quotes=[(QA, True, True)])])[QA]
    assert entry == {"state": "confirmed", "last_found": "2026-10-20", "since": None, "last_attempt": "2026-10-20"}


def test_access_failure_keeps_its_first_date():
    entry = replay([row("2026-10-10", "blocked"), row("2026-10-17", "failed")])[QA]
    assert entry == {"state": "blocked_or_failed", "last_found": "2026-10-01", "since": "2026-10-10", "last_attempt": "2026-10-17"}


def test_loss_replaces_access_failure_and_survives_later_failures():
    rows = [row("2026-10-10", "blocked"), row("2026-10-12", "gone"), row("2026-10-14", "failed"), row("2026-10-16", "gone")]
    assert replay(rows)[QA]["state"] == "source_gone" and replay(rows)[QA]["since"] == "2026-10-12"


def test_lost_quote_is_per_quote():
    entries = replay([row("2026-10-10", "substantive", quotes=[(QA, True, False), (QB, True, True)])], A, B)
    assert entries[QA]["state"] == "quote_gone" and entries[QB]["state"] == "confirmed"


def test_fallback_loss_counts_only_when_confirmed():
    unconfirmed = row("2026-10-10", "failed", route="jina", quotes=[(QA, True, False), (QB, True, True)])
    entries = replay([unconfirmed], A, B)
    assert entries[QA]["state"] == "blocked_or_failed" and entries[QB]["last_found"] == "2026-10-10"
    confirmed = row("2026-10-10", "substantive", route="headless+jina", quotes=[(QA, True, False), (QB, True, True)],
                    confirmed_quote_ids=[QA])
    entries = replay([confirmed], A, B)
    assert entries[QA]["state"] == "quote_gone" and entries[QB]["last_found"] == "2026-10-10"


def test_wayback_copy_is_not_a_reread():
    entry = replay([row("2026-10-10", "archived", route="wayback", quotes=[(QA, True, True)])])[QA]
    assert entry["state"] == "blocked_or_failed" and entry["last_found"] == "2026-10-01"


def test_skipped_and_undecided_rows_are_no_attempt():
    entry = replay([row("2026-10-10", "skipped"), row("2026-10-12", "trivial", quotes=[(QA, False, False)])])[QA]
    assert entry["last_attempt"] == "2026-10-01"


def test_new_snapshot_after_a_loss_finds_the_quote():
    data = {"sources": [source(A, retrieved="2026-10-20T08:00:00Z")]}
    entry = export(data, [row("2026-10-10", "gone")])["sources"]["s1"][QA]
    assert entry["state"] == "confirmed" and entry["last_found"] == "2026-10-20"


def test_unbound_quote_is_not_replayed_and_falls_back_to_the_copy_date():
    data = {"sources": [source(A, url="https://example.org/other.pdf")], "dataset": {"as_of": "2026-10-05"}}
    assert export(data, [row("2026-10-03", "gone")])["sources"] == {}
    state = rereads.quote_states(data, {"sources": {}})[("s1", QA)]
    assert rereads.effective(state, "2026-10-05") == {"state": "confirmed", "last_found": "2026-10-01", "since": None}
    assert rereads.effective(state, "2026-10-14")["state"] == "confirmed"
    assert rereads.effective(state, "2026-10-15")["state"] == "not_attempted"  # 14 days without an attempt
    # Counted once, never the weekly 14-day alert (hub, 14 Nov: an alert that fires forever teaches people to ignore it).
    assert rereads.alerts(data, {"checked_to": "2026-11-20", "sources": {}}, "2026-11-20") == [
        "1 quote is on a page the monitor does not re-read yet"]


def published(claims, as_of="2026-12-20", **excerpt_supports):
    excerpts = [{"text": t, "supports": excerpt_supports.get(name, [])} for name, t in (("a", A), ("b", B))]
    return {"dataset": {"as_of": as_of, "version": "1.1.0"},
            "changelog": [{"version": "1.0.9", "date": "2026-11-20"}, {"version": "1.1.0", "date": as_of}],
            "sources": [{"id": "s1", "url": "https://example.org/a", "snapshot": {"retrieved": "2026-10-01T08:00:00Z"},
                         "excerpts": excerpts}],
            "jurisdictions": [{"id": "XX", "claims": claims}], "institutes": []}


def file(**entries):
    return {"checked_to": "2026-12-18", "runs": ["r1"], "sources": {"s1": entries}}


def entry(state, since=None, last_found="2026-10-01", last_attempt="2026-12-18"):
    return {"state": state, "last_found": last_found, "since": since, "last_attempt": last_attempt}


def test_claim_takes_the_best_excerpt_of_each_leaf_and_the_worst_leaf():
    claim = {"id": "claims-1", "kind": "fact", "text": "x"}
    data = published([claim], a=["/jurisdictions/0/claims/0/text"], b=["/jurisdictions/0/claims/0/text"])
    rereads.join(data, file(**{QA: entry("quote_gone", "2026-12-10"), QB: entry("confirmed", last_found="2026-12-18")}))
    assert claim["reread"]["state"] == "confirmed" and claim["expired"] is False
    assert data["sources"][0]["excerpts"][0]["reread"] == {"state": "quote_gone", "last_found": "2026-10-01", "since": "2026-12-10"}
    development = {"id": "claims-2", "kind": "development", "text": "x", "act": "y", "date": "2026-01-05"}
    data = published([development], a=["/jurisdictions/0/claims/0/text", "/jurisdictions/0/claims/0/date"],
                     b=["/jurisdictions/0/claims/0/act"])
    rereads.join(data, file(**{QA: entry("confirmed"), QB: entry("blocked_or_failed", "2026-12-01")}))
    assert development["reread"]["state"] == "blocked_or_failed"


def test_expiry_by_reason():
    def expired(state, since, as_of):
        claim = {"id": "claims-1", "kind": "fact", "text": "x"}
        rereads.join(published([claim], as_of, a=["/jurisdictions/0/claims/0/text"]), file(**{QA: entry(state, since)}))
        return claim["expired"]
    assert not expired("quote_gone", "2026-11-20", "2026-12-20")
    assert expired("source_gone", "2026-11-19", "2026-12-20")
    assert not expired("blocked_or_failed", "2026-06-20", "2026-12-20")
    assert expired("blocked_or_failed", "2026-06-19", "2026-12-20")
    old = {"id": "claims-2", "kind": "development", "text": "x", "act": "y", "date": "2023-11"}
    rereads.join(published([old], "2026-12-01"), file())
    assert old["expired"] is True


def test_development_leaves_on_the_level_2_bar_s_own_three_year_rule():
    claim = {"kind": "development", "date": "2023-10-17"}
    assert rereads.claim_expiry(claim, None, "2026-10-17") is None  # exactly three years: still in (inclusive window)
    assert str(rereads.claim_expiry(claim, None, "2026-10-18")) == "2026-10-18"
    leap = {"kind": "development", "date": "2025-02-28"}
    assert rereads.claim_expiry(leap, None, "2028-02-29") is None  # claim_checks.years_before("2028-02-29", 3) is 2025-02-28
    assert str(rereads.claim_expiry(leap, None, "2028-03-01")) == "2028-03-01"
    assert str(rereads.claim_expiry({"kind": "development", "date": "2023-10"}, None, "2026-11-01")) == "2026-11-01"


def test_alerts():
    data = published([{"id": "claims-1", "kind": "fact", "text": "x"}], a=["/jurisdictions/0/claims/0/text"])
    states = {QA: entry("quote_gone", "2026-12-12"), QB: entry("blocked_or_failed", "2026-07-10", last_attempt="2026-12-01")}
    rereads.join(data, file(**states))
    assert rereads.alerts(data, file(**states), "2026-12-20") == [
        "source s1: quote not found in the source since 2026-12-12 (1 quote); triage was due 2026-12-19",
        "source s1: could not be re-read since 2026-07-10 (1 quote); its claims leave summaries after 2027-01-10",
        "1 quote in 1 source had no re-read attempt for 14 days: s1",
    ]
    stale = {**file(**{QA: entry("confirmed")}), "checked_to": "2026-12-11"}
    assert rereads.alerts(published([]), stale, "2026-12-20")[0].startswith("data/rereads.json reflects checks up to 2026-12-11")
    expired = published([{"id": "claims-1", "kind": "fact", "text": "x"}], a=["/jurisdictions/0/claims/0/text"])
    rereads.join(expired, file(**{QA: entry("quote_gone", "2026-11-10")}))
    assert "claim claims-1 (/jurisdictions/0/claims/0) left the summary on 2026-12-11" in rereads.alerts(expired, file(), "2026-12-20")


def test_expiry_on_the_day_after_a_release_is_alerted():
    claim = {"id": "claims-1", "kind": "fact", "text": "x"}
    data = published([claim], "2026-12-21", a=["/jurisdictions/0/claims/0/text"])
    data["changelog"][0]["date"] = "2026-12-20"
    rereads.join(data, file(**{QA: entry("quote_gone", "2026-11-20")}))
    assert claim["expired"] is True
    assert "claim claims-1 (/jurisdictions/0/claims/0) left the summary on 2026-12-21" in rereads.alerts(data, file(), "2026-12-21")


def test_leaf_and_claim_choices_do_not_depend_on_excerpt_order():
    early, late = entry("quote_gone", "2026-11-01"), entry("quote_gone", "2026-12-01")
    for pair in ([early, late], [late, early]):
        assert rereads.best([rereads.effective(e, "2026-12-20") for e in pair])["since"] == "2026-12-01"
        assert rereads.worst([rereads.effective(e, "2026-12-20") for e in pair])["since"] == "2026-11-01"


def test_a_quote_neither_copy_carries_is_no_evidence_in_any_class():
    for klass, route in (("failed", "jina"), ("blocked", "direct"), ("archived", "wayback"), ("trivial", "direct")):
        entry_ = replay([row("2026-10-10", klass, route=route, quotes=[(QA, False, False)])])[QA]
        assert (entry_["state"], entry_["last_attempt"]) == ("confirmed", "2026-10-01"), klass


def test_an_archive_observation_confirms_only_a_source_whose_own_url_is_a_capture():
    rows = [row("2026-10-05", "gone"), row("2026-10-10", "unchanged", observation="archive", quotes=[(QA, True, True)])]
    entry_ = replay(rows)[QA]
    assert (entry_["state"], entry_["last_found"]) == ("source_gone", "2026-10-01")
    entry_ = replay(rows[1:], is_archive=lambda url: True)[QA]
    assert (entry_["state"], entry_["last_found"]) == ("confirmed", "2026-10-10")


@pytest.mark.parametrize("klass", ["substantive", "trivial", "unchanged", "failed", "blocked", "archived"])
@pytest.mark.parametrize("route", ["direct", "headless", "jina", "headless+jina", "wayback"])
@pytest.mark.parametrize("flags", [(True, True), (True, False), (False, True)])
@pytest.mark.parametrize("confirmed_ids", [None, [QA]])
def test_an_archive_observation_never_confirms_or_loses_a_quote(klass, route, flags, confirmed_ids):
    extra = {} if confirmed_ids is None else {"confirmed_quote_ids": confirmed_ids}
    archive = row("2026-10-10", klass, route=route, observation="archive", quotes=[(QA, *flags)], **extra)
    assert rereads.evidence(archive, QA) == "blocked_or_failed"
    if route == "wayback":  # a Wayback route is never a re-read, even of a source whose own URL is a capture
        assert rereads.evidence(archive, QA, own_archive=True) == "blocked_or_failed"


def test_an_unconfirmed_fallback_loss_beside_a_confirmed_one_is_an_access_failure():
    mixed = row("2026-10-10", "substantive", route="headless+jina", quotes=[(QA, True, False), (QB, True, False)],
                confirmed_quote_ids=[QA])
    entries = replay([mixed], A, B)
    assert entries[QA]["state"] == "quote_gone" and entries[QB]["state"] == "blocked_or_failed"


def test_monitor_writes_the_file(tmp_path):
    data = tmp_path / "aisi.json"
    data.write_text(json.dumps({"sources": [source(A)]}), encoding="utf-8")
    checks = tmp_path / "checks"
    checks.mkdir()
    (checks / "checks.jsonl").write_text(json.dumps(row("2026-10-10", "trivial", quotes=[(QA, True, True)])) + "\n", encoding="utf-8")
    out = tmp_path / "rereads.json"
    assert monitor.main(["--data", str(data), "--directory", str(tmp_path / "store"), "--checks", str(checks),
                         "--rereads", "--rereads-out", str(out)]) == 0
    assert json.loads(out.read_text())["sources"]["s1"][QA]["last_found"] == "2026-10-10"
