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
    # The note leaves out the lead the summary quotes (hub default, 15 November); the body's role keeps every sentence.
    assert texts["note"] == "It says it does research."
    assert texts["roles"][body] == "It says it does research. " + entry["claims"][1]["text"]
    assert entry["claims"][1]["text"] in texts["summary"]
    assert set(texts["roles"].values()) - {texts["roles"][body]} == {""}
    assert texts["summary"] == RECORDS["PL"]["summary"]


def test_a_lead_alone_leaves_the_note_as_the_name():
    """The note never repeats the summary's quote: with the lead as the only role claim, the note is the name as a
    sentence (the schema keeps it non-empty) and the page shows no note that is only the name."""
    texts = summaries.generate(copy.deepcopy(RECORDS["PL"]["record"]), None, None)
    assert texts["note"] == "NASK AI Safety Research Centre."
    assert RECORDS["PL"]["record"]["claims"][0]["text"] in texts["summary"]


def test_name_only_closest_body_note_is_its_name():
    """Owner, 15 November: a closest body that never had a quote shows its name alone. The note pins today's output,
    the name as a sentence (hub default, Q1); the page then shows the name twice, a 1.2.0 page fix (worklist)."""
    record = RECORDS["ID"]["record"]
    texts = summaries.generate(record, None, None)
    assert texts["note"] == "AI Safety Institute Indonesia (AISII)."
    assert texts["roles"][record["closest_body"]["body_id"]] == ""
    assert texts["summary"].endswith(" Closest body: AI Safety Institute Indonesia (AISII).")
    assert summaries.closest_warning(record) == (
        "ID: closest body AI Safety Institute Indonesia (AISII) has no live role claim and is shown by name alone; "
        "find a quote in the next sweep")


def expired_lead(state):
    entry = copy.deepcopy(RECORDS["PL"]["record"])
    lead = next(c for c in entry["claims"] if c.get("lead"))
    lead.update(expired=True, reread={"state": state, "since": "2026-11-02", "last_found": "2026-10-14"})
    return entry, lead


@pytest.mark.parametrize("state, mark", [
    ("source_gone", "Source offline since 2 November 2026; last found 14 October 2026."),
    ("blocked_or_failed", "Source could not be re-read since 2 November 2026; last found 14 October 2026.")])
def test_offline_lead_is_kept_marked(state, mark):
    """Owner, 15 November, "Keep last quote, marked": the expired lead stays in the line and the note, its state after it."""
    record, lead = expired_lead(state)
    texts = summaries.generate(record, None, None)
    line = f"Closest body: NASK AI Safety Research Centre. {lead['text']} {mark}"
    assert summaries.pattern_2(record) == line
    assert texts["summary"].endswith(" " + line)
    assert texts["note"] == "NASK AI Safety Research Centre."  # the summary quotes the kept lead; the note does not repeat it
    assert texts["roles"][record["closest_body"]["body_id"]] == f"{lead['text']} {mark}"
    assert summaries.closest_warning(record) == (
        f"PL: closest body NASK AI Safety Research Centre: its lead claim {lead['id']} expired ({state} since 2026-11-02) "
        "and is shown marked; re-check it in the next sweep")


def test_lost_quote_lead_is_not_kept():
    """Hub default: a quote no longer in a readable source leaves; the body falls back to its name alone."""
    record, _ = expired_lead("quote_gone")
    texts = summaries.generate(record, None, None)
    assert summaries.pattern_2(record) == "Closest body: NASK AI Safety Research Centre."
    assert texts["note"] == "NASK AI Safety Research Centre."
    assert "name alone" in summaries.closest_warning(record)


def test_kept_lead_keeps_live_role_claims_and_drops_other_expired_ones():
    record, lead = expired_lead("source_gone")
    body = record["closest_body"]["body_id"]
    record["claims"][:0] = [{"id": "c-live", "kind": "function", "body_id": body, "text": "It says it does research."},
                            {"id": "c-gone", "kind": "function", "body_id": body, "text": "Gone.", "expired": True,
                             "reread": {"state": "source_gone", "since": "2026-11-02", "last_found": "2026-10-14"}}]
    texts = summaries.generate(record, None, None)
    assert texts["note"] == "It says it does research."
    assert texts["roles"][body] == f"It says it does research. {lead['text']} Source offline since 2 November 2026; last found 14 October 2026."
    assert summaries.closest_warning(record) is None  # a live role claim is left


def test_generate_refuses_a_missing_body_id():
    record = copy.deepcopy(RECORDS["PL"]["record"])
    del record["closest_body"]["body_id"]
    with pytest.raises(summaries.GeneratorError, match="body_id"):
        summaries.generate(record, None, None)
