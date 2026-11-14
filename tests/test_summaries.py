"""The summary patterns of docs/design/claims-1.1.md, section 3 (build step B): the spec's 32 Europe-scope lines and
eleven closest-body lines as expected outputs, and one test per pattern branch the spec lines do not reach."""

import copy
import json
from pathlib import Path

import pytest

from scripts import summaries

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "summary_patterns.json").read_text(encoding="utf-8"))
RECORDS = {r["record"]["id"]: r for r in FIXTURE["records"]}


def test_fixture_holds_every_spec_line():
    assert sum(r["summary"] is not None for r in FIXTURE["records"]) == 32
    assert sum(r["closest_line"] is not None for r in FIXTURE["records"]) == 11


@pytest.mark.parametrize("jid", sorted(j for j, r in RECORDS.items() if r["summary"]))
def test_europe_lines(jid):
    """Through generate(), with the status event passed as the pipeline passes it."""
    entry = copy.deepcopy(RECORDS[jid])
    basis = entry["record"].pop("status_basis_event", None)
    assert summaries.generate(entry["record"], entry["institute"], basis)["summary"] == entry["summary"]


@pytest.mark.parametrize("jid", sorted(j for j, r in RECORDS.items() if r["closest_line"]))
def test_closest_body_lines(jid):
    assert summaries.pattern_2(RECORDS[jid]["record"]) == RECORDS[jid]["closest_line"]


def none_identified(**extra):
    return {"id": "XX", "status": "none_identified", "check": {"last_checked": "2026-10-16"}, "related_bodies": [],
            "claims": [], **extra}


def authority(name, roles, state="designated", **extra):
    return {"id": f"related_bodies-{name}", "name": name, "kind": "ai_act_authority", "authority_roles": roles,
            **({"designation_state": state} if state else {}), **extra}


def test_pattern_1_alone_and_month():
    assert summaries.summary_text(none_identified(check={"last_checked": "2027-01-03"})) == (
        "No qualifying institute identified as of January 2027.")


@pytest.mark.parametrize("stage, prefix", [("exists", "Closest body"), ("planned", "Planned closest body"),
                                           ("proposed", "Proposed closest body"), ("under_study", "Closest body under study")])
def test_pattern_2_stage_prefix(stage, prefix):
    record = none_identified(closest_body={"name": "Centre", "stage": stage, "body_id": "b"})
    assert summaries.pattern_2(record) == f"{prefix}: Centre."


def test_pattern_2_skips_expired_lead_and_other_bodies_claims():
    claims = [{"kind": "function", "body_id": "other", "lead": True, "text": "Other body."},
              {"kind": "function", "body_id": "b", "lead": True, "expired": True, "text": "Expired."}]
    record = none_identified(closest_body={"name": "Centre", "stage": "exists", "body_id": "b"}, claims=claims)
    assert summaries.pattern_2(record) == "Closest body: Centre."


def test_pattern_order_closest_then_development_then_authority():
    development = {"kind": "development", "date": "2026-03-05", "act": "the government adopted a plan", "text": "funds a lab"}
    bodies = [authority("Office", ["notifying"])]
    record = none_identified(related_bodies=bodies, claims=[development])
    assert summaries.summary_text(record).endswith(" On 5 March 2026 the government adopted a plan, which funds a lab.")
    record["closest_body"] = {"name": "Centre", "stage": "exists", "body_id": "b"}
    assert summaries.summary_text(record).endswith(" Closest body: Centre.")  # pattern 2 before a live pattern 3
    del record["closest_body"]
    record["claims"][0]["expired"] = True
    assert summaries.summary_text(record).endswith(" Office is its AI Act notifying authority.")


def test_pattern_2_name_with_its_own_full_stop():
    record = none_identified(closest_body={"name": "Centre Ltd.", "stage": "exists", "body_id": "b"})
    assert summaries.pattern_2(record) == "Closest body: Centre Ltd."
    record["closest_body"]["name"] = "Centre (Dept.)"
    assert summaries.pattern_2(record) == "Closest body: Centre (Dept.)."


def test_pattern_4_most_roles_without_contact_point_and_roleless_entries():
    bodies = [authority("Alpha", ["notifying"]), authority("Zeta", ["market_surveillance", "notifying"]),
              {"id": "x", "name": "Accreditation", "kind": "ai_act_authority"}]
    assert summaries.pattern_4(none_identified(related_bodies=bodies)) == (
        "Zeta is its AI Act market-surveillance authority and notifying authority; 2 other AI Act bodies are listed below.")
    roleless = [{"id": "x", "name": "Accreditation", "kind": "ai_act_authority", "authority_roles": []}]
    assert summaries.pattern_4(none_identified(related_bodies=roleless)) is None


def test_pattern_4_lead_authority_and_counts():
    bodies = [authority("Zeta", ["market_surveillance", "notifying"]), authority("Alpha", ["notifying"]),
              authority("Beta", ["single_point_of_contact"])]
    assert summaries.pattern_4(none_identified(related_bodies=bodies)) == (
        "Beta is its AI Act single point of contact; 2 other AI Act bodies are listed below.")
    bodies = [authority("Zeta", ["market_surveillance"]), authority("Alpha", ["notifying"])]
    assert summaries.pattern_4(none_identified(related_bodies=bodies)) == (
        "Alpha is its AI Act notifying authority; 1 other AI Act body is listed below.")


def test_pattern_4_proposed_step_and_unconfirmed():
    step = authority("Office", ["single_point_of_contact", "market_surveillance"], "proposed",
                     designation_step={"text": "bill tabled", "date": "2026-09-24"})
    assert summaries.pattern_4(none_identified(related_bodies=[step])) == (
        "Office is proposed as its AI Act single point of contact and market-surveillance authority"
        " (bill tabled on 24 September 2026).")
    listed = authority("Office", ["market_surveillance"], None, name_en="Data Office")
    assert summaries.pattern_4(none_identified(related_bodies=[listed])) == (
        "Data Office is listed as its AI Act market-surveillance authority, but no national designation is confirmed.")


@pytest.mark.parametrize("form, host, words", [
    ("new_body", "Ministry", "as a new body"),
    ("university_centre", "University", "as a university centre"),
    ("unit_in_existing_body", None, "as a unit within an existing body"),
])
def test_pattern_5_forms(form, host, words):
    institute = {"name": "Institute", "established": "2025-07", "legal_form": form, **({"host": host} if host else {})}
    record = {"id": "XX", "status": "established"}
    assert summaries.pattern_5(record, institute) == f"Institute was set up in July 2025 {words}."


def test_pattern_5_unknown_form_and_missing_basis():
    institute = {"name": "Institute", "short_name": "Inst", "established": "2025", "legal_form": "unknown"}
    assert summaries.pattern_5({"id": "XX", "status": "established"}, institute) == "Inst was set up in 2025."
    with pytest.raises(summaries.GeneratorError):
        summaries.pattern_5({"id": "XX", "status": "planned", "status_since": "2026-01-01"}, None)


def test_pattern_6_absent_without_mandate_short():
    record = {"id": "XX", "status": "exploring", "status_since": "2026-06-01", "status_basis_event": {"short": "Study."}}
    assert summaries.summary_text(record, {"name": "Institute"}) == "Exploring since 1 June 2026: Study."


def test_notes_from_check_log_in_date_order():
    record = none_identified(check={"last_checked": "2026-10-16", "log": [
        {"date": "2026-10-17", "text": "Second."}, {"date": "2026-10-01", "text": "First."},
        {"date": "2026-10-17", "text": "Third."}]})
    assert summaries.notes_text(record) == "First.\n\nSecond.\n\nThird."
    assert summaries.notes_text(none_identified()) == ""


def test_generate_roles_and_closest_note():
    entry = copy.deepcopy(RECORDS["PL"]["record"])
    body = entry["closest_body"]["body_id"]
    entry["claims"].insert(0, {"kind": "function", "body_id": body, "text": "It says it does research."})
    texts = summaries.generate(entry, None, None)
    assert texts["note"] == "It says it does research. " + entry["claims"][1]["text"]
    assert texts["roles"][body] == texts["note"]
    assert set(texts["roles"].values()) - {texts["note"]} == {""}
    assert texts["summary"] == RECORDS["PL"]["summary"]


def test_generate_refuses_an_empty_closest_note_and_a_missing_body_id():
    with pytest.raises(summaries.GeneratorError, match="no role claim"):
        summaries.generate(RECORDS["ID"]["record"], None, None)
    record = copy.deepcopy(RECORDS["PL"]["record"])
    del record["closest_body"]["body_id"]
    with pytest.raises(summaries.GeneratorError, match="body_id"):
        summaries.generate(record, None, None)
