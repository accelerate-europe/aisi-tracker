"""The search-protocol checks (warn-only prototype) on synthetic logs, and on the internal pilot files."""

import json
from datetime import date

import pytest

from scripts import protocol_checks as pc

DOORS = {
    "cards": {
        "staff": {"fields": ["staff"], "doors": ["jobs"], "scan_words": ["staff"]},
    },
    "institutes": {
        "xx-aisi": {"names": {"en": ["X AISI"]}, "research_languages": ["en", "xx"],
                    "doors": [{"id": "xx-jobs", "kinds": ["jobs"], "url": "https://example.org/jobs", "marker": "Jobs"}]},
    },
}
INST = {"id": "xx-aisi", "head": {"state": "stated"}, "national_partners": {"state": "stated"}, "funding": [{"amount": 1, "coverage": "whole_institute"}],
        "staff": [{"total": 3, "technical": 1}], "staffing_model": {"code": "own_staff"}, "outputs": [{}], "agreements": [{}],
        "testing_capacity": {"modes": [{}]}, "model_access": {"access_events": [{}]}, "legal_basis": {"type": "act"}, "host": "Host",
        "reports_to": {"state": "stated"}, "wikidata": "Q1"}


EMPTY = INST | {"staff": []}


def entry(**kw):
    base = {"institute_id": "xx-aisi", "date": "2026-10-24", "pass": "2026-10-24", "field": "staff", "tool": "exa",
            "query": "X AISI staff", "language": "en", "national_or_official": False, "sources_read": [], "result": "none_found"}
    return base | kw


def warnings(entries, inst=None):
    return pc.check({"institutes": [inst or EMPTY]}, DOORS, {"entries": entries}, date(2026, 10, 24))


def test_empty_slot_on_one_engine_without_door_or_language_warns():
    out = warnings([entry(stop="complete")])
    assert any("door xx-jobs" in w for w in out)
    assert any("research language xx" in w for w in out)
    assert any("only one engine" in w for w in out)


def test_full_pass_is_clean():
    out = warnings([
        entry(door="xx-jobs", tool="fetch", query="https://example.org/jobs"),
        entry(language="xx", national_or_official=True),
        entry(tool="web_search", stop="complete"),
    ])
    assert out == []


def test_missing_stop_and_stale_pass_warn():
    assert any("no stop reason" in w for w in warnings([entry(door="xx-jobs", tool="fetch"), entry(language="xx"), entry(tool="web_search")]))
    assert any("no pass within 6 months" in w for w in warnings([entry(**{"pass": "2026-01-01", "date": "2026-01-01", "stop": "complete"})]))


def test_blocked_door_or_open_lead_cannot_close_complete():
    blocked = warnings([entry(door="xx-jobs", tool="fetch", result="blocked"), entry(language="xx"), entry(tool="web_search", stop="complete")])
    assert any("stayed blocked" in w for w in blocked)
    lead = warnings([entry(door="xx-jobs", tool="fetch", result="found", open_lead="owner call"), entry(language="xx"), entry(tool="web_search", stop="complete")])
    assert any("lead is still open" in w for w in lead)


def test_reopened_door_and_pending_items():
    reopened = [entry(door="xx-jobs", tool="fetch", result="blocked"), entry(door="xx-jobs", tool="browser", result="none_found"),
                entry(language="xx"), entry(tool="web_search", stop="complete")]
    assert not any("stayed blocked" in w for w in warnings(reopened))
    pending = {"entries": reopened, "pending": [{"institute_id": "xx-aisi", "fields": ["staff"], "next_check": "2026-11-01", "item": "answer due"}]}
    assert any("lead is still open" in w for w in pc.check({"institutes": [EMPTY]}, DOORS, pending, date(2026, 10, 24)))
    pending["pending"][0]["next_check"] = "2026-10-23"  # overdue leads stay open until resolved
    overdue = pc.check({"institutes": [EMPTY]}, DOORS, pending, date(2026, 10, 24))
    assert any("lead is still open" in w for w in overdue) and any("past due" in w for w in overdue)
    pending["pending"][0]["resolved"] = "2026-10-24"
    assert not any("lead is still open" in w or "past due" in w for w in pc.check({"institutes": [EMPTY]}, DOORS, pending, date(2026, 10, 24)))
    del pending["pending"][0]["resolved"]
    pending["pending"][0]["kind"] = "watch"
    assert not any("lead is still open" in w for w in pc.check({"institutes": [EMPTY]}, DOORS, pending, date(2026, 10, 24)))


def test_empty_subfield_needs_a_second_engine_and_total_staff_counts():
    out = warnings([entry(door="xx-jobs", tool="fetch"), entry(result="confirms_existing"), entry(language="xx", stop="complete")],
                   INST | {"staff": [{"total": 3}]})
    assert any("only one engine" in w for w in out)
    assert pc.empty_slots(INST | {"staff": [{"technical": 1}]})["staff"] is True


def test_resumed_pass_is_judged_by_its_last_stop():
    first = [entry(door="xx-jobs", tool="fetch", stop="budget_out")]
    assert any("budget_out" in w for w in warnings(first))
    assert not any("budget_out" in w for w in warnings(first + [entry(language="xx"), entry(tool="web_search", stop="complete")]))


def test_related_funding_or_missing_technical_staff_leave_the_slot_empty():
    related = INST | {"funding": [{"amount": 5, "coverage": "not_institute_specific"}]}
    assert pc.empty_slots(related)["funding"] is True
    assert pc.empty_slots(INST | {"staff": [{"total": 3}]})["staff"] is True
    assert pc.cited_sources({"source_ids": ["a"], "staff": [{"source_ids": ["b"]}]}) == {"a", "b"}


@pytest.mark.skipif(not all(pc.internal_path(name).is_file() for name in ("doors.json", "field_searches.json")),
                    reason="internal search-protocol files missing; set AISI_INTERNAL")
def test_internal_pilot_files_load_and_name_known_institutes():
    data = json.loads(pc.DATA.read_text(encoding="utf-8"))
    doors = json.loads(pc.internal_path("doors.json").read_text(encoding="utf-8"))
    log = json.loads(pc.internal_path("field_searches.json").read_text(encoding="utf-8"))
    ids = {i["id"] for i in data["institutes"]}
    jids = {j["id"] for j in data["jurisdictions"]}
    # A state with no institute (the Netherlands) logs its searches by jurisdiction_id with a null institute_id.
    assert set(doors["institutes"]) <= ids and {e["institute_id"] for e in log["entries"] if e["institute_id"] is not None} <= ids
    assert all(e.get("jurisdiction_id") in jids for e in log["entries"] if e["institute_id"] is None)
    door_ids = {d["id"] for spec in doors["institutes"].values() for d in spec["doors"]} | {d["id"] for d in doors["shared_doors"]}
    institute_rows = [e for e in log["entries"] if e["institute_id"] is not None]  # the door pilot covers institutes only
    assert {e["door"] for e in institute_rows if "door" in e} <= door_ids
    assert all(e["field"] in doors["cards"] or e["field"] == "status" for e in log["entries"])  # status searches (2 Nov; NL, KZ) sit beside the card fields
    pc.check(data, doors, log, date.fromisoformat(data["dataset"]["as_of"]))


@pytest.mark.parametrize("present", [None, "doors.json", "field_searches.json"])
def test_missing_internal_files_are_not_run(tmp_path, monkeypatch, capsys, present):
    monkeypatch.setenv("AISI_INTERNAL", str(tmp_path))
    if present:
        (tmp_path / present).write_text("{}")
    assert pc.main([]) == 3
    error = capsys.readouterr().err
    assert "internal files were not found at" in error and "set AISI_INTERNAL" in error
    for name in ("doors.json", "field_searches.json"):
        assert (str(tmp_path / name) in error) == (name != present)


def test_internal_directory_override_is_read_by_cli(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("AISI_INTERNAL", raising=False)
    assert pc.internal_path("doors.json") == pc.ROOT / "data/internal/doors.json"
    monkeypatch.setenv("AISI_INTERNAL", str(tmp_path))
    (tmp_path / "doors.json").write_text(json.dumps({"institutes": {}, "cards": {}}))
    (tmp_path / "field_searches.json").write_text(json.dumps({"entries": []}))
    monkeypatch.delenv("AISI_SNAPSHOTS", raising=False)
    assert pc.main([]) == 0
    assert "0 warnings" in capsys.readouterr().err
