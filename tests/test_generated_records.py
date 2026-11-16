"""The migrated-record rule (docs/design/claims-1.1.md, section 1, build step B): a jurisdiction with no authored
summary gets its summary, notes, related-body roles and closest-body note generated, and may author none of them;
every other record publishes those four exactly as authored."""

import copy
import json
from pathlib import Path

import pytest

from scripts import build, method_checks, rereads
from scripts import published as published_facts
from scripts.references import authored_schema
from test_claims import SCHEMA, located_errors, schema_errors
from test_claims_development import LEAD, ROLE, lead  # noqa: F401  (the A2 fixture: a closest body with a lead claim)

DATA = Path(__file__).resolve().parent.parent / "data" / "aisi.json"


def test_todays_data_publishes_its_authored_texts():
    """In every record not yet migrated (step F migrates the Europe records batch by batch), the published summary,
    notes, roles and closest notes, and their CSV columns, are the authored values; a migrated one has them generated."""
    authored = json.loads(DATA.read_text(encoding="utf-8"))
    migrated = method_checks.migrated_ids(authored)
    assert {j["id"] for j in authored["jurisdictions"]} - migrated, "a record is left to compare"
    # Institutes with check.log lines and fact claims: an institute's log never reaches its notes.
    logged = {i["jurisdiction_id"] for i in authored["institutes"] if i.get("check", {}).get("log")}
    assert {"FR", "DE", "ES"} <= logged
    assert any(c["kind"] == "fact" for i in authored["institutes"] for c in i.get("claims", []))
    published_institutes = build.export_dataset(authored)["institutes"]
    assert [i.get("notes") for i in published_institutes] == [i.get("notes") for i in authored["institutes"]]
    published = build.export_dataset(authored)
    tables = build.tables(published)
    states = {row["id"]: row for row in tables["jurisdictions"]}
    closest = {row["jurisdiction_id"]: row for row in tables["closest_bodies"]}
    roles = {(row["jurisdiction_id"], row["id"]): row["role"] for row in tables["related_bodies"]}
    for before, after in zip(authored["jurisdictions"], published["jurisdictions"], strict=True):
        if before["id"] in migrated:
            assert after["summary"] == states[before["id"]]["summary"] and after["summary"].strip()
            continue
        assert after["summary"] == before["summary"] == states[before["id"]]["summary"]
        assert after.get("notes") == before.get("notes")
        assert states[before["id"]]["notes"] == before.get("notes", "")
        assert [b["role"] for b in after.get("related_bodies", [])] == [b["role"] for b in before.get("related_bodies", [])]
        for body in before.get("related_bodies", []):
            assert roles[(before["id"], body.get("id", ""))] == body["role"]
        if "closest_body" in before:
            assert after["closest_body"]["note"] == before["closest_body"]["note"] == closest[before["id"]]["note"]


@pytest.fixture
def migrated(lead):  # noqa: F811
    """The A2 fixture with its generated texts removed and the closest body's note excerpt dropped."""
    d = copy.deepcopy(lead)
    state = d["jurisdictions"][0]
    for key in ("summary", "notes"):
        state.pop(key, None)
    state["closest_body"].pop("note")
    for body in state["related_bodies"]:
        body.pop("role")
    for source in d["sources"]:
        for e in source.get("excerpts", []):
            e["supports"] = [p for p in e["supports"] if not p.endswith(("/closest_body/note", "/summary", "/notes")) and "/role" not in p]
        source["excerpts"] = [e for e in source.get("excerpts", []) if e["supports"]]
    d["sources"][0]["excerpts"].append({"text": "The telecoms office is proposed as market-surveillance authority.",
                                        "supports": ["/jurisdictions/0/related_bodies/1/designation_state"]})
    state["check"]["log"] = [{"date": "2026-10-17", "text": "Searched in English."}]
    return d


def test_generated_summary_cites_the_leaves_it_words(migrated):
    """A generated summary has no excerpt of its own; the card's finding cites the excerpts of the leaves its pattern
    words (here pattern 2: the closest body's name and stage and its lead claim), and pattern 1 alone cites nothing."""
    from scripts.summaries import summary_leaves
    nothing = {"checked_to": None, "runs": [], "sources": {}}
    state = published_facts.publish(migrated, rereads=nothing)["jurisdictions"][0]
    leaves = summary_leaves(state, None, None)
    lead_index = next(n for n, c in enumerate(state["claims"]) if c["id"] == LEAD)
    assert leaves == [("jurisdiction", "closest_body/name"), ("jurisdiction", "closest_body/stage"), ("jurisdiction", f"claims/{lead_index}/text")]
    assert state["citations"]["finding"]
    assert set(state["citations"]["finding"]) <= set(state["closest_body"]["citations"]["display"]) | set(state["claims"][lead_index]["source_ids"])
    bare = {"id": "XX", "status": "none_identified", "claims": [], "related_bodies": []}
    assert summary_leaves(bare, None, None) == []


def test_closest_activity_follows_vocabulary_and_claim_order(migrated):
    state = migrated["jurisdictions"][0]
    body = state["closest_body"]["body_id"]
    claim = {"kind": "function", "basis": "activity", "body_id": body, "text": "It tests models."}
    state["claims"] = [
        {**claim, "id": "research-first", "code": "research"},
        {**claim, "id": "testing", "code": "model_testing"},
        {**claim, "id": "research-second", "code": "research"},
        {**claim, "id": "mandate", "code": "policy_advice", "basis": "mandate"},
        {**claim, "id": "expired", "code": "policy_advice", "expired": True},
        {**claim, "id": "other", "code": "policy_advice", "body_id": "other-body"},
        {**claim, "id": "fact", "code": "policy_advice", "kind": "fact"},
    ]
    published_facts.word_record(state, {}, migrated["dataset"]["vocabularies"], migrated["dataset"]["as_of"])
    assert state["closest_body"]["active_in"] == [
        {"code": "model_testing", "claim_ids": ["testing"]},
        {"code": "research", "claim_ids": ["research-first", "research-second"]},
    ]
    for c in state["claims"]:
        c["expired"] = True
    published_facts.word_record(state, {}, migrated["dataset"]["vocabularies"], migrated["dataset"]["as_of"])
    assert "active_in" not in state["closest_body"]


def test_closest_activity_is_generated_in_download_page_and_csv(migrated):
    published = build.export_dataset(migrated)
    closest = published["jurisdictions"][0]["closest_body"]
    assert closest["active_in"] == [{"code": "model_testing", "claim_ids": [LEAD]}, {"code": "research", "claim_ids": [ROLE]}]
    assert json.loads(build.page_data(published))["jurisdictions"][0]["closest_body"]["active_in"] == closest["active_in"]
    table = build.tables(published)["closest_bodies"]
    assert table[0]["active_in"] == "model_testing;research"
    assert list(table[0]) == build.EMPTY_COLUMNS["closest_bodies"]
    closest.pop("active_in")
    assert build.tables(published)["closest_bodies"][0]["active_in"] == ""


def test_closest_activity_is_removed_after_reread_expiry(migrated):
    states = {"checked_to": None, "runs": [], "sources": {}}
    for source in migrated["sources"]:
        for excerpt in source.get("excerpts", []):
            if any(p in excerpt["supports"] for p in ("/jurisdictions/0/claims/0/text", "/jurisdictions/0/claims/1/text")):
                states["sources"].setdefault(source["id"], {})[rereads.quote_id(excerpt["text"])] = {
                    "state": "quote_gone", "last_found": "2025-01-01", "since": "2025-01-02",
                }
    published = published_facts.publish(migrated, rereads=states)
    state = published["jurisdictions"][0]
    assert next(c for c in state["claims"] if c["id"] == ROLE)["expired"]
    assert "active_in" not in state["closest_body"]


def test_closest_activity_forbidden_in_authored_input(migrated):
    migrated["jurisdictions"][0]["closest_body"]["active_in"] = [{"code": "research", "claim_ids": [ROLE]}]
    assert any(path == ["jurisdictions", 0, "closest_body"] or "active_in" in path
               for path, _ in located_errors(migrated, authored_schema(SCHEMA, strict=True)))


def test_legacy_closest_body_has_no_generated_activity(lead):  # noqa: F811
    assert "active_in" not in build.export_dataset(lead)["jurisdictions"][0]["closest_body"]


def test_real_generated_status_summaries_are_cited():
    """The Netherlands and Sweden (a status, no institute): their card shows the generated summary as its finding."""
    authored = json.loads(DATA.read_text(encoding="utf-8"))
    published = published_facts.publish(authored)
    migrated = method_checks.migrated_ids(authored)
    for j in published["jurisdictions"]:
        if j["id"] in migrated and j["status"] != "none_identified":
            assert j["citations"]["finding"], j["id"]


def test_migrated_record_is_generated(migrated):
    assert schema_errors(migrated) == []
    assert build.check_consistency(migrated) == []
    published = build.export_dataset(migrated)
    assert located_errors(published, SCHEMA) == []
    state = published["jurisdictions"][0]
    claims = {c["id"]: c["text"] for c in state["claims"]}
    month = build.export_dataset(migrated)["jurisdictions"][0]["check"]["last_checked"][:7]
    assert state["summary"].startswith("No qualifying institute identified as of ")
    assert month.split("-")[0] in state["summary"]
    assert state["summary"].endswith(" Closest body: Synthetic model test centre. " + claims[LEAD])
    assert state["notes"] == "Checked in full on or before 17 October 2026."
    assert state["closest_body"]["note"] == claims[ROLE]  # the lead is in the summary, not said twice
    roles = {b["id"]: b["role"] for b in state["related_bodies"]}
    assert roles == {"test-regulator": f"{claims[ROLE]} {claims[LEAD]}", "test-authority": ""}
    tables = build.tables(published)
    assert tables["jurisdictions"][0]["summary"] == state["summary"]
    assert tables["closest_bodies"][0]["note"] == state["closest_body"]["note"]


def lead_lost(migrated, state):
    """Publish the fixture before and after the lead claim's quote takes the re-read state `state` since 2025."""
    nothing = {"checked_to": None, "runs": [], "sources": {}}
    before = published_facts.publish(migrated, rereads=nothing)
    lead_index = next(i for i, c in enumerate(before["jurisdictions"][0]["claims"]) if c["id"] == LEAD)
    gone = {s["id"]: {rereads.quote_id(e["text"]): {"state": state, "last_found": "2025-01-01", "since": "2025-01-02",
                                                       "last_attempt": before["dataset"]["as_of"]}
                      for e in s.get("excerpts", []) if f"/jurisdictions/0/claims/{lead_index}/text" in e["supports"]}
            for s in before["sources"]}
    after = published_facts.publish(migrated, rereads={"checked_to": before["dataset"]["as_of"], "runs": ["r1"],
                                                        "sources": {k: v for k, v in gone.items() if v}})
    return before, after


def test_expired_claim_leaves_the_generated_texts(migrated):
    """Step C marks claims expired after the texts are first worded; the published texts are worded without them.
    A lead claim whose quote is gone from a readable source leaves too (hub default)."""
    before, after = lead_lost(migrated, "quote_gone")
    state = after["jurisdictions"][0]
    claims = {c["id"]: c["text"] for c in state["claims"]}
    assert {c["id"]: c["expired"] for c in state["claims"]}[LEAD] is True
    assert claims[LEAD] in before["jurisdictions"][0]["summary"]
    assert claims[LEAD] not in state["summary"] and claims[LEAD] not in state["closest_body"]["note"]
    assert state["closest_body"]["note"] == claims[ROLE]
    assert located_errors(after, SCHEMA) == []
    assert published_facts.closest_body_warnings(migrated, after) == []  # a live role claim is left


def test_offline_lead_claim_is_kept_marked(migrated):
    """Owner, 15 November, "Keep last quote, marked": an expired lead whose source is offline stays, with its state."""
    _, after = lead_lost(migrated, "source_gone")
    state = after["jurisdictions"][0]
    claims = {c["id"]: c["text"] for c in state["claims"]}
    mark = "Source offline since 2 January 2025; last found 1 January 2025."
    assert {c["id"]: c["expired"] for c in state["claims"]}[LEAD] is True
    assert state["summary"].endswith(f" Closest body: Synthetic model test centre. {claims[LEAD]} {mark}")
    assert state["closest_body"]["note"] == claims[ROLE]  # the kept lead is in the summary, marked
    assert {b["id"]: b["role"] for b in state["related_bodies"]}["test-regulator"] == f"{claims[ROLE]} {claims[LEAD]} {mark}"
    assert located_errors(after, SCHEMA) == []


@pytest.mark.parametrize("leaf", ["notes", "role", "note"])
def test_migrated_record_authors_no_generated_text(migrated, leaf):
    state = migrated["jurisdictions"][0]
    {"notes": state, "role": state["related_bodies"][0], "note": state["closest_body"]}[leaf][leaf] = "Hand-written."
    errors = build.check_consistency(migrated)
    assert any("must not be authored" in e for e in errors), errors


def test_legacy_record_still_authors_role_and_note(lead):  # noqa: F811
    """authored_schema leaves role and note optional for migrated records; a record with a summary still needs them."""
    del lead["jurisdictions"][0]["related_bodies"][0]["role"]
    del lead["jurisdictions"][0]["closest_body"]["note"]
    for e in lead["sources"][0]["excerpts"]:
        e["supports"] = [p for p in e["supports"] if p != "/jurisdictions/0/closest_body/note"]
    assert schema_errors(lead) == []
    errors = build.check_consistency(lead)
    assert any("role is required while the record authors its summary" in e for e in errors), errors
    assert any("note is required while the record authors its summary" in e for e in errors), errors


def test_summary_stays_required_in_the_download():
    for strict in (False, True):
        jurisdictions = authored_schema(SCHEMA, strict=strict)["properties"]["jurisdictions"]["items"]
        assert "summary" not in jurisdictions["required"] and jurisdictions["properties"]["summary"] is not False
    assert "summary" in SCHEMA["properties"]["jurisdictions"]["items"]["required"]


@pytest.fixture
def undisclosed_funding():
    authored = json.loads(DATA.read_text(encoding="utf-8"))
    institute = next(i for i in authored["institutes"] if i["id"] == "fr-inesia")
    funding = next(f for f in institute["funding"] if f["id"] == "funding-5900b7d819c0501a83196d3281da8527")
    assert "description" not in funding
    return authored, funding


@pytest.mark.parametrize("coverage, expected", [
    ("whole_institute", "No budget published for the institute as a whole."),
    ("component", "No budget published for this part of the institute."),
    ("not_institute_specific", "No budget published for this wider funding."),
])
def test_undisclosed_funding_description_is_generated_by_coverage(undisclosed_funding, coverage, expected):
    authored, funding = undisclosed_funding
    funding["coverage"] = coverage
    published = build.export_dataset(authored)
    institute = next(i for i in published["institutes"] if i["id"] == "fr-inesia")
    assert next(f for f in institute["funding"] if f["id"] == funding["id"])["description"] == expected
    assert "description" not in funding  # publication never writes into the authored input


def test_real_undisclosed_funding_migration_and_legacy_descriptions(undisclosed_funding):
    authored, _ = undisclosed_funding
    migrated = method_checks.migrated_ids(authored)
    published = build.export_dataset(authored)
    descriptions = {f["id"]: f["description"] for i in published["institutes"] for f in i.get("funding", [])}
    flagged = {f["id"] for i in published["institutes"] for f in i.get("funding", []) if f["display"].get("generated_description")}
    assert flagged == {f["id"] for i in authored["institutes"] if i["jurisdiction_id"] in migrated
                       for f in i.get("funding", []) if f["kind"] == "none_disclosed"}  # the page hides exactly these statements
    generated = set()
    legacy = set()
    for institute in authored["institutes"]:
        for funding in institute.get("funding", []):
            if institute["jurisdiction_id"] in migrated and funding["kind"] == "none_disclosed":
                generated.add(institute["id"])
                assert "description" not in funding
                assert descriptions[funding["id"]] == "No budget published for the institute as a whole."
            else:
                assert descriptions[funding["id"]] == funding["description"]
                if funding["kind"] == "none_disclosed":
                    legacy.add(institute["id"])
    assert generated == {"fr-inesia", "de-aisi"}
    assert legacy == {"eu-ai-office", "my-aisafe", "in-aisi"}
    assert schema_errors(authored, authored_schema(SCHEMA, strict=True)) == []
    assert located_errors(published, SCHEMA) == []


@pytest.mark.parametrize("description", ["Authored negative.", "No budget published for the institute as a whole."])
def test_migrated_undisclosed_funding_refuses_an_authored_description(undisclosed_funding, description):
    authored, funding = undisclosed_funding
    funding["description"] = description
    with pytest.raises(ValueError, match="generated in a migrated record; must not be authored"):
        build.export_dataset(authored)


@pytest.mark.parametrize("strict", [False, True])
def test_only_undisclosed_funding_can_omit_description_in_authored_schema(undisclosed_funding, strict):
    from jsonschema import Draft202012Validator

    _, funding = undisclosed_funding
    for schema, missing_allowed in ((authored_schema(SCHEMA, strict=strict), True), (SCHEMA, False)):
        validator = Draft202012Validator({"$ref": "#/$defs/funding", "$defs": schema["$defs"]})
        assert validator.is_valid(funding) is missing_allowed
        for kind in ("annual", "multi_year", "one_off", "not_institute_specific"):
            errors = list(validator.iter_errors({**funding, "kind": kind}))
            assert any(error.message == "'description' is a required property" for error in errors)


def test_legacy_undisclosed_funding_still_requires_description(undisclosed_funding):
    authored, _ = undisclosed_funding
    institute = next(i for i in authored["institutes"] if i["id"] == "my-aisafe")
    funding = next(f for f in institute["funding"] if f["kind"] == "none_disclosed")
    del funding["description"]
    with pytest.raises(ValueError, match="description is required while the record authors its summary"):
        build.export_dataset(authored)


def test_legacy_undisclosed_funding_may_not_repeat_a_generated_line(undisclosed_funding):
    # The generated lines are what marks a description as generated (display.generated_description), so a legacy
    # record that authored one word for word would lose its source statement on the page.
    authored, _ = undisclosed_funding
    institute = next(i for i in authored["institutes"] if i["id"] == "my-aisafe")
    funding = next(f for f in institute["funding"] if f["kind"] == "none_disclosed")
    funding["description"] = "No budget published for the institute as a whole."
    with pytest.raises(ValueError, match="may not repeat a generated line"):
        build.export_dataset(authored)


@pytest.mark.parametrize("bound", [False, True])
def test_generated_funding_description_needs_no_quote_but_allows_evidence(undisclosed_funding, bound):
    authored, funding = undisclosed_funding
    source = next(s for s in authored["sources"] if s["id"] == "fr-sgdsn-aisi")
    if bound:
        source["excerpts"].append({"text": "Synthetic non-disclosure evidence.", "supports": [
            ["institutes", {"id": "fr-inesia"}, "funding", {"id": funding["id"]}, "description"],
        ]})
    published = build.export_dataset(authored)
    institute = next(i for i in published["institutes"] if i["id"] == "fr-inesia")
    result = next(f for f in institute["funding"] if f["id"] == funding["id"])
    assert result["description"] == "No budget published for the institute as a whole."
    assert result["citations"].get("description", []) == ([source["id"]] if bound else [])
    items = method_checks.unquoted_items(authored)
    assert not any("institutes[].funding[].description: fr-inesia" in item for _, _, item in items)
    funding["notes"] = "An unquoted funding fact."
    assert any("institutes[].funding[].notes: fr-inesia" in item for _, _, item in method_checks.unquoted_items(authored))


def test_legacy_undisclosed_funding_description_still_needs_a_quote(undisclosed_funding):
    authored, _ = undisclosed_funding
    institute = next(i for i in authored["institutes"] if i["id"] == "my-aisafe")
    funding = next(f for f in institute["funding"] if f["kind"] == "none_disclosed")
    selector = ["institutes", {"id": institute["id"]}, "funding", {"id": funding["id"]}, "description"]
    for source in authored["sources"]:
        for excerpt in source.get("excerpts", []):
            excerpt["supports"] = [p for p in excerpt["supports"] if p != selector]
        source["excerpts"] = [e for e in source.get("excerpts", []) if e["supports"]]
    assert any("institutes[].funding[].description: my-aisafe" in item for _, _, item in method_checks.unquoted_items(authored))


def test_migrated_closest_body_needs_gap_excerpt_and_body_id(migrated):
    d = copy.deepcopy(migrated)
    for source in d["sources"]:
        source["excerpts"] = [e for e in source.get("excerpts", []) if e["supports"] != ["/jurisdictions/0/closest_body/gap"]]
    assert "required claim has no excerpt: /jurisdictions/0/closest_body/gap" in build.check_consistency(d)
    d = copy.deepcopy(migrated)
    del d["jurisdictions"][0]["closest_body"]["body_id"]
    assert any("closest_body.body_id" in e for e in build.check_consistency(d))


def test_migrated_closest_body_without_role_claim_shows_its_name_and_warns(migrated):
    """Owner, 15 November: never quoted, the name alone; the build warns for the next sweep instead of failing."""
    d = copy.deepcopy(migrated)
    d["jurisdictions"][0]["claims"] = []  # all, so no excerpt pointer shifts
    for source in d["sources"]:
        source["excerpts"] = [e for e in source.get("excerpts", []) if not any("/claims/" in p for p in e["supports"])]
    assert build.check_consistency(d) == []
    published = build.export_dataset(d)
    assert located_errors(published, SCHEMA) == []
    state = published["jurisdictions"][0]
    assert state["closest_body"]["note"] == "Synthetic model test centre."
    assert state["summary"].endswith(" Closest body: Synthetic model test centre.")
    assert published_facts.closest_body_warnings(d, published) == [
        f"{state['id']}: closest body Synthetic model test centre has no live role claim and is shown by name alone; "
        "find a quote in the next sweep"]


def test_migrated_body_needs_a_role_claim(migrated):
    d = copy.deepcopy(migrated)
    d["jurisdictions"][0]["related_bodies"].append({"id": "test-other", "name": "Synthetic lab", "kind": "other", "counts_as_aisi": False,
                                                    "source_ids": [d["sources"][0]["id"]]})
    assert any("has a role claim or is an AI Act authority" in e for e in build.check_consistency(d))


def test_migrated_idless_body_without_closest_needs_a_role_claim(migrated):
    d = copy.deepcopy(migrated)
    del d["jurisdictions"][0]["closest_body"]
    d["jurisdictions"][0]["claims"][1].pop("lead")
    d["sources"][0]["excerpts"] = [e for e in d["sources"][0]["excerpts"] if not any("/closest_body/" in p for p in e["supports"])]
    assert build.check_consistency(d) == []
    d["jurisdictions"][0]["related_bodies"].append({"name": "Synthetic lab", "kind": "other", "counts_as_aisi": False, "source_ids": ["gb-source"]})
    errors = build.check_consistency(d)
    assert len([e for e in errors if "a related body of a migrated record has a role claim" in e]) == 1, errors


@pytest.mark.parametrize("migrate", [True, False])
def test_unbound_free_text_is_an_error_only_in_a_migrated_record(dataset, migrate):
    """An institute's field notes with no excerpt: an error when its jurisdiction is migrated, a warning otherwise,
    and nothing once an excerpt binds it."""
    d = copy.deepcopy(dataset)
    if migrate:
        d["jurisdictions"][0].pop("summary")
    d["institutes"][0]["funding"][0]["notes"] = "Unbound synthetic note."
    item = "institutes[].funding[].notes: gb-test (0 checkable sentences)"
    errors = method_checks.migrated_unquoted_errors(d)
    warned = method_checks.unquoted_claims(d)["free text with no excerpt"]
    assert (any(item in e for e in errors), item in warned) == ((True, False) if migrate else (False, True))
    d["sources"][0]["excerpts"].append({"text": "Synthetic note.", "supports": ["/institutes/0/funding/0/notes"]})
    assert not any(item in e for e in method_checks.migrated_unquoted_errors(d))
    assert item not in method_checks.unquoted_claims(d)["free text with no excerpt"]


def test_required_search_log_notes_need_no_quote_in_a_migrated_record(dataset):
    """Hub default (spec section 5): a row note that row_checks requires as a dated search log (a row not published,
    or partners stated with no cyber agency) is left out of the quote rule in a migrated record and listed instead;
    any other row note is still an error."""
    d = copy.deepcopy(dataset)
    d["jurisdictions"][0].pop("summary")
    institute = d["institutes"][0]
    institute["head"] = {"state": "not_published", "notes": "Searched the founding text on 2026-10-19."}
    institute["national_partners"] = {"state": "stated", "notes": "Cyber agency searched: none, 2026-10-19.",
                                      "entries": [{"id": "np-x", "name": "Partner", "relation": "works_with", "how": "Works with it.",
                                                   "source_ids": [d["sources"][0]["id"]]}]}
    institute["reports_to"] = {"state": "not_applicable", "notes": "Synthetic note on a row with no required log."}

    def errors_for(field):
        return [e for e in method_checks.migrated_unquoted_errors(d) if f"institutes[].{field}.notes" in e]

    assert errors_for("head") == [] and errors_for("national_partners") == []
    assert len(errors_for("reports_to")) == 1
    assert method_checks.search_log_notes(d) == [
        "search-log note, no quote required: gb-test head.notes (not_published): Searched the founding text on 2026-10-19.",
        "search-log note, no quote required: gb-test national_partners.notes (no_cyber_agency): Cyber agency searched: none, 2026-10-19."]
    institute["national_partners"]["entries"][0]["cyber_agency"] = True
    institute["head"]["state"] = "not_applicable"
    assert len(errors_for("head")) == 1 and len(errors_for("national_partners")) == 1
    assert method_checks.search_log_notes(d) == []
    d["jurisdictions"][0]["summary"] = "Synthetic."  # not migrated: nothing listed, warnings as before
    assert method_checks.search_log_notes(d) == []


def test_generated_first_output_description_is_not_unquoted():
    """The first_output event's description is generated from the output's title, so it is never an unquoted item
    (the UK and France have one; before the fix a migrated UK record failed on it)."""
    authored = json.loads(DATA.read_text(encoding="utf-8"))
    published = build.export_dataset(authored)
    owners = {j["id"] for j in published["jurisdictions"] if any(e["event"] == "first_output" for e in j.get("timeline", []))}
    assert {"GB", "FR"} <= owners
    items = method_checks.unquoted_claims(authored)["free text with no excerpt"]
    assert not [i for i in items if i.startswith("jurisdictions[].timeline[].description: ") and i.split(": ")[1].split()[0] in owners]
    migrated_gb = copy.deepcopy(authored)
    next(j for j in migrated_gb["jurisdictions"] if j["id"] == "GB").pop("summary", None)  # GB may already be migrated
    assert not [e for e in method_checks.migrated_unquoted_errors(migrated_gb) if "timeline[].description" in e]


def test_migrated_unquoted_free_text_is_an_error(migrated):
    """Its generated leaves (summary, notes, roles, note) are never unquoted items; anything else is an error."""
    d = copy.deepcopy(migrated)
    assert [item for item in method_checks.unquoted_items(d) if item[1] == d["jurisdictions"][0]["id"]] == []
    d["jurisdictions"][0]["related_bodies"][0]["source_ids"] = []
    errors = method_checks.migrated_unquoted_errors(d)
    assert any("related body with no source in a migrated record" in e for e in errors), errors
    assert all(d["jurisdictions"][0]["id"] not in w for w in method_checks.unquoted_warnings(method_checks.unquoted_claims(d)))
