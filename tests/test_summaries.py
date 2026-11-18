"""The summary patterns of docs/design/claims-1.1.md, section 3 (build step B): the spec's 32 Europe-scope lines and
eleven closest-body lines as expected outputs, and one test per pattern branch the spec lines do not reach."""

import copy
import json
import re
import subprocess
from pathlib import Path

import pytest

from scripts import summaries

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "summary_patterns.json").read_text(encoding="utf-8"))
RECORDS = {r["record"]["id"]: r for r in FIXTURE["records"]}
ROOT = Path(__file__).resolve().parents[1]
AS_OF = "2026-11-15"


def test_fixture_holds_every_spec_line():
    assert sum(r["summary"] is not None for r in FIXTURE["records"]) == 32
    assert sum(r["closest_line"] is not None for r in FIXTURE["records"]) == 11


@pytest.mark.parametrize("jid", sorted(j for j, r in RECORDS.items() if r["summary"]))
def test_europe_lines(jid):
    """Through generate(), with the status event passed as the pipeline passes it."""
    entry = copy.deepcopy(RECORDS[jid])
    basis = entry["record"].pop("status_basis_event", None)
    assert summaries.generate(entry["record"], entry["institute"], basis, as_of=AS_OF)["summary"] == entry["summary"]


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


@pytest.mark.parametrize("stage, line", [("exists", "Closest body: Centre."),
                                         ("planned", "Closest body: Centre, planned and not yet set up."),
                                         ("proposed", "Closest body: Centre, proposed and not yet set up."),
                                         ("under_study", "Closest body: Centre, under study and not yet set up.")])
def test_pattern_2_stage_follows_the_body(stage, line):
    """The stage is the body's, so it follows its name; no stage word stands before "closest body" (owner, 17 Nov)."""
    record = none_identified(closest_body={"name": "Centre", "stage": stage, "body_id": "b"})
    assert summaries.pattern_2(record) == line
    assert not re.search(r"\b(planned|proposed|under study) closest body", line, re.I)


def test_pattern_2_covers_every_stage_value():
    schema = json.loads((ROOT / "schema" / "aisi.schema.json").read_text(encoding="utf-8"))
    stage = schema["properties"]["jurisdictions"]["items"]["properties"]["closest_body"]["properties"]["stage"]
    assert set(stage["enum"]) == set(summaries.STAGE_TAIL)


@pytest.mark.parametrize("n, word", [(1, "one"), (5, "five"), (9, "nine"), (10, "10"), (11, "11")])
def test_counts_in_sentences_are_words_to_nine(n, word):
    assert summaries.count(n) == word


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
        "Zeta is its AI Act market-surveillance authority and notifying authority; two other AI Act bodies are listed below.")
    roleless = [{"id": "x", "name": "Accreditation", "kind": "ai_act_authority", "authority_roles": []}]
    assert summaries.pattern_4(none_identified(related_bodies=roleless)) is None


def test_pattern_4_lead_authority_and_counts():
    bodies = [authority("Zeta", ["market_surveillance", "notifying"]), authority("Alpha", ["notifying"]),
              authority("Beta", ["single_point_of_contact"])]
    assert summaries.pattern_4(none_identified(related_bodies=bodies)) == (
        "Beta is its AI Act single point of contact; two other AI Act bodies are listed below.")
    bodies = [authority("Zeta", ["market_surveillance"]), authority("Alpha", ["notifying"])]
    assert summaries.pattern_4(none_identified(related_bodies=bodies)) == (
        "Alpha is its AI Act notifying authority; one other AI Act body is listed below.")


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


def checked_record(**fields):
    return none_identified(check={"last_checked": "2026-10-16", "method": "individual",
                                  "last_checked_basis": "historical_upper_bound", **fields})


def search_pass(day="2026-11-02", **fields):
    return {"date": day, "languages": ["en", "pl"], "places": ["NASK's site", "the official journals"],
            "fields": ["head", "funding"], **fields}


@pytest.mark.parametrize("basis, when", [("historical_upper_bound", "on or before"), ("recorded_full_check", "on")])
def test_notes_full_check_date_and_distinct_languages(basis, when):
    record = checked_record(last_checked_basis=basis, searches=[{"language": code} for code in ["en", "pl", "pl", "en"]],
                            log=[{"date": "2026-11-02", "text": "A document was read in 2026-11-02-pass."}])
    assert summaries.notes_text(record, AS_OF) == f"Checked in full {when} 16 October 2026, searching in Polish and English."


def test_notes_full_check_without_searches():
    assert summaries.notes_text(checked_record(), AS_OF) == "Checked in full on or before 16 October 2026."
    record = checked_record(searches=[])
    del record["check"]["last_checked_basis"]
    assert summaries.notes_text(record, AS_OF) == "Checked in full on or before 16 October 2026."


def test_notes_broad_search_is_an_upper_bound():
    record = checked_record(method="broad_search", searches=[{"language": "en"}])
    assert summaries.notes_text(record, AS_OF) == "Covered by a search across many states by 16 October 2026."


def test_notes_passes_newest_first_with_authored_ties_places_and_topics():
    record = checked_record(passes=[search_pass(), search_pass("2026-11-04", places=["gov.ie"], languages=["en"],
                                                             fields=["status", "staff", "outputs"]),
                                   search_pass("2026-11-04", places=["the register"], languages=["fr", "de", "en", "fr"],
                                               fields=["related_bodies"])])
    assert summaries.notes_text(record, AS_OF).split("\n\n") == [
        "Checked in full on or before 16 October 2026.",
        "On 4 November 2026 we searched gov.ie in English, for information on status, staff and published work.",
        "On 4 November 2026 we searched the register in French, German and English, for information on AI Act authorities and related bodies.",
        "On 2 November 2026 we searched NASK's site and the official journals in Polish and English, for information on head and budget.",
    ]
    assert record["check"]["passes"][0]["date"] == "2026-11-02"  # no mutation of authored order


@pytest.mark.parametrize("day", ["2026-10-15", "2026-10-16"])
def test_notes_pass_on_or_before_full_check_is_omitted(day):
    assert summaries.notes_text(checked_record(passes=[search_pass(day)]), AS_OF) == summaries.notes_text(checked_record(), AS_OF)


@pytest.mark.parametrize("day, shown, warned", [("2026-11-16", True, False), (AS_OF, False, False), ("2026-11-14", False, True)])
def test_notes_next_check_and_release_warning(day, shown, warned):
    from scripts.published import generate_texts, next_check_warnings

    record = checked_record()
    record["next_check"] = {"date": day}
    authored = {"dataset": {"as_of": AS_OF}, "jurisdictions": [record]}
    generated = copy.deepcopy(authored)
    assert generate_texts(generated) == []
    assert generated["jurisdictions"][0]["notes"] == (
        "Checked in full on or before 16 October 2026." + ("\n\nNext check due by 16 November 2026." if shown else ""))
    assert next_check_warnings(authored, generated) == ([f"XX: next_check {day} is before as_of"] if warned else [])


@pytest.mark.parametrize("check_fields", [{"searches": [{"language": "zz"}]},
                                        {"passes": [search_pass("2026-10-01", languages=["zz"])]}])
def test_unknown_language_names_the_record_even_when_search_is_not_shown(check_fields):
    from scripts.published import generate_texts

    record = checked_record(method="broad_search", **check_fields)
    with pytest.raises(summaries.GeneratorError, match="XX: unknown check language code: zz"):
        summaries.notes_text(record, AS_OF)
    # Legacy jurisdictions and institutes also fail the publishing/build boundary.
    record["summary"] = "Legacy summary."
    for collection in ("jurisdictions", "institutes"):
        assert generate_texts({collection: [record]}) == ["XX: unknown check language code: zz"]


def test_language_names_cover_page_and_dataset_and_topics_cover_schema():
    page = (ROOT / "site/index.html").read_text()
    literal = re.search(r"var QUOTE_LANGUAGES = \{([^}]+)\};", page).group(1)
    names = dict(re.findall(r'([a-z]+): "([^"]+)"', literal))
    assert names and all(summaries.LANGUAGE_NAMES.get(code) == name for code, name in names.items())
    data = json.loads((ROOT / "data/aisi.json").read_text())
    for record in data["jurisdictions"] + data["institutes"]:
        summaries.check_language_names(record)
    schema = json.loads((ROOT / "schema/aisi.schema.json").read_text())
    assert set(summaries.CHECK_TOPICS) == set(schema["$defs"]["check"]["properties"]["passes"]["items"]["properties"]["fields"]["items"]["enum"])


@pytest.mark.parametrize("place, kind", [("PDF pages 2 and 3", "workshop words"),
                                        ("the register of 2 Nov 2026", "short month")])
def test_search_pass_places_are_linted_as_prose(place, kind):
    from scripts.reader_text import lint_errors

    data = {"dataset": {"as_of": AS_OF}, "jurisdictions": [checked_record(passes=[search_pass(places=[place])])]}
    errors = lint_errors(data, {"XX"})
    assert any(f"/check/passes/0/places/0: {kind}" in error for error in errors)


def how_checked_tree(record, *, show_how=True):
    """Run the page's real helpers in Node with a small DOM, as in test_render_only.py; no browser needed."""
    source = (ROOT / "site/index.html").read_text()
    helpers = source[source.index("  var HOW_WORDS ="):source.index("  // Status definitions, short:")]
    helpers += source[source.index("  var SEARCHED ="):source.index("  // Each card field has a fixed track")]
    helpers += source[source.index("  function sourceFooter("):source.index("  // The repository's correction issue form")]
    stub = """
function el(tag,className,text) {
  return {tag,className,text:text||'',children:[],appendChild(c){this.children.push(c);return c;},
    get lastElementChild(){return this.children[this.children.length-1];}};
}
const dated=t=>el('time',null,t), separated=parts=>({tag:'fragment',children:parts});
const fieldCitations=(j,fields)=>j.citations[fields[0]], cite=ids=>({tag:'sup',className:'cites',ids});
const correctionLink=()=>el('a','correction','Suggest a correction');
"""
    script = stub + helpers + "\nconst j=" + json.dumps(record) + ";\n"
    script += "const how=howWeChecked(j,cite);\nconsole.log(JSON.stringify({how,foot:sourceFooter(j,{as_of:'2026-11-15',repository:'repo'},[],{},{},"
    script += ("how" if show_how else "null") + ")}));"
    return json.loads(subprocess.run(["node"], input=script, text=True, capture_output=True, check=True).stdout)


def node_text(node):
    return node.get("text", "") + "".join(node_text(c) for c in node.get("children", []))


def test_generated_notes_render_three_paragraphs_and_a_closed_fold_without_citations():
    record = checked_record(passes=[search_pass(f"2026-11-0{n}") for n in (1, 2, 3)])
    record["next_check"] = {"date": "2026-11-16"}
    record["notes"] = summaries.notes_text(record, AS_OF)
    record["citations"] = {"notes": ["must-not-be-cited"]}
    tree = how_checked_tree(record)
    heading, *children = tree["how"]["children"]
    assert heading["text"] == "How we checked"
    assert [p["text"] for p in children[:3]] == [
        "Checked in full on or before 16 October 2026.",
        "Next check due by 16 November 2026.",
        "On 3 November 2026 we searched NASK's site and the official journals in Polish and English, for information on head and budget.",
    ]
    fold = children[3]
    assert fold["tag"] == "details" and not fold.get("open")
    assert [node_text(c) for c in fold["children"]] == [
        "2 earlier searches",
        "On 2 November 2026 we searched NASK's site and the official journals in Polish and English, for information on head and budget.",
        "On 1 November 2026 we searched NASK's site and the official journals in Polish and English, for information on head and budget.",
    ]
    assert "cites" not in json.dumps(tree["how"])
    assert "Checked" not in node_text(tree["foot"])
    assert "Suggest a correction" in node_text(tree["foot"])
    # Institute cards have no How block and must retain their date.
    assert "Checked 2026-10-16" in node_text(how_checked_tree(record, show_how=False)["foot"])


@pytest.mark.parametrize("notes", ["", "\n \n", "Checked in full on 16 October 2026."])
def test_generated_notes_render_without_an_unnecessary_fold(notes):
    record = checked_record()
    record["notes"] = notes
    tree = how_checked_tree(record)
    if not notes.strip():
        assert tree["how"] is None
        assert "Checked" in node_text(tree["foot"])
    else:
        assert [n["tag"] for n in tree["how"]["children"]] == ["p", "p"]


def test_legacy_notes_keep_their_search_rewrite_citations_and_footer_date():
    record = checked_record()
    del record["claims"]
    record.update(notes="Searched in Polish and English for a body its government has tasked with testing general-purpose or frontier AI models or named its AI safety or security institute.\n\nA document was read.",
                  citations={"notes": ["source"]})
    tree = how_checked_tree(record)
    assert [n["text"] for n in tree["how"]["children"]] == ["How we checked", "Searched in Polish and English.", "A document was read."]
    assert tree["how"]["children"][-1]["children"] == [{"tag": "sup", "className": "cites", "ids": ["source"]}]
    assert "Checked 2026-10-16" in node_text(tree["foot"])


def test_how_checked_gate_rejects_lost_paragraphs_open_folds_citations_and_duplicate_dates():
    from scripts.check_page import check_how_we_checked

    record = checked_record()
    record["notes"] = "Checked in full on or before 16 October 2026.\n\nSecond.\n\nThird.\n\nFourth."
    data = {"jurisdictions": [record]}
    html = '<details class="card mini" id="c-xx"><div class="how"><p class="gl">How we checked</p><p>Checked in full on or before 16 October 2026.</p><p>Second.</p><p>Third.</p><details><summary>1 earlier searches</summary><p>Fourth.</p></details></div><div class="foot"></div></details>'
    assert check_how_we_checked(html, data) == []
    for before, after in [("<p>Third.</p>", ""), ("<details>", "<details open>"), ("1 earlier", "2 earlier"),
                          ("Fourth.", 'Fourth.<sup class="cites">1</sup>'), ('class="foot">', 'class="foot"><p class="source-after">Checked 16 October 2026</p>')]:
        assert check_how_we_checked(html.replace(before, after), data)


@pytest.mark.parametrize("residue", ["A document was read.", "Two documents were read.", "2026-11-02-pass"])
def test_how_checked_gate_rejects_reading_logs_and_pass_ids_only_after_migration(residue):
    from scripts.check_page import check_how_we_checked

    record = checked_record()
    record["notes"] = residue
    html = f'<details class="card mini" id="c-xx"><div class="how"><p class="gl">How we checked</p><p>{residue}</p></div></details>'
    assert any("document read or a research pass id" in e for e in check_how_we_checked(html, {"jurisdictions": [record]}))
    del record["claims"]
    assert check_how_we_checked(html, {"jurisdictions": [record]}) == []


def test_generate_roles_and_closest_note():
    entry = copy.deepcopy(RECORDS["PL"]["record"])
    body = entry["closest_body"]["body_id"]
    entry["claims"].insert(0, {"kind": "function", "body_id": body, "text": "It says it does research."})
    texts = summaries.generate(entry, None, None, as_of=AS_OF)
    # The note leaves out the lead the summary quotes (hub default, 15 November); the body's role keeps every sentence.
    assert texts["note"] == "It says it does research."
    assert texts["roles"][body] == "It says it does research. " + entry["claims"][1]["text"]
    assert entry["claims"][1]["text"] in texts["summary"]
    assert set(texts["roles"].values()) - {texts["roles"][body]} == {""}
    assert texts["summary"] == RECORDS["PL"]["summary"]


def test_a_lead_alone_leaves_the_note_as_the_name():
    """The note never repeats the summary's quote: with the lead as the only role claim, the note is the name as a
    sentence (the schema keeps it non-empty) and the page shows no note that is only the name."""
    texts = summaries.generate(copy.deepcopy(RECORDS["PL"]["record"]), None, None, as_of=AS_OF)
    assert texts["note"] == "NASK AI Safety Research Centre."
    assert RECORDS["PL"]["record"]["claims"][0]["text"] in texts["summary"]


def test_name_only_closest_body_note_is_its_name():
    """Owner, 15 November: a closest body that never had a quote shows its name alone. The note pins today's output,
    the name as a sentence (hub default, Q1); the page then shows the name twice, a 1.2.0 page fix (worklist)."""
    record = RECORDS["ID"]["record"]
    texts = summaries.generate(record, None, None, as_of=AS_OF)
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
    ("source_gone", "Source offline since 2 November 2026; last read 14 October 2026."),
    ("blocked_or_failed", "Source could not be re-read since 2 November 2026; last read 14 October 2026.")])
def test_offline_lead_is_kept_marked(state, mark):
    """Owner, 15 November, "Keep last quote, marked": the expired lead stays in the line and the note, its state after it."""
    record, lead = expired_lead(state)
    texts = summaries.generate(record, None, None, as_of=AS_OF)
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
    texts = summaries.generate(record, None, None, as_of=AS_OF)
    assert summaries.pattern_2(record) == "Closest body: NASK AI Safety Research Centre."
    assert texts["note"] == "NASK AI Safety Research Centre."
    assert "name alone" in summaries.closest_warning(record)


def test_kept_lead_keeps_live_role_claims_and_drops_other_expired_ones():
    record, lead = expired_lead("source_gone")
    body = record["closest_body"]["body_id"]
    record["claims"][:0] = [{"id": "c-live", "kind": "function", "body_id": body, "text": "It says it does research."},
                            {"id": "c-gone", "kind": "function", "body_id": body, "text": "Gone.", "expired": True,
                             "reread": {"state": "source_gone", "since": "2026-11-02", "last_found": "2026-10-14"}}]
    texts = summaries.generate(record, None, None, as_of=AS_OF)
    assert texts["note"] == "It says it does research."
    assert texts["roles"][body] == f"It says it does research. {lead['text']} Source offline since 2 November 2026; last read 14 October 2026."
    assert summaries.closest_warning(record) is None  # a live role claim is left


def test_generate_refuses_a_missing_body_id():
    record = copy.deepcopy(RECORDS["PL"]["record"])
    del record["closest_body"]["body_id"]
    with pytest.raises(summaries.GeneratorError, match="body_id"):
        summaries.generate(record, None, None, as_of=AS_OF)
