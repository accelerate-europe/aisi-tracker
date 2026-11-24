"""Claims (build step A1 of docs/design/claims-1.1.md): the shape, the structural checks and the
generated institutes[].functions rows, each gate with its mutation."""

import copy
import json
import re
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from scripts import build, claim_checks, rereads
from scripts import published as fact_pipeline
from scripts.display import record_sources
from scripts.references import authored_schema

SCHEMA = json.loads(build.SCHEMA.read_text())
AUTHORED = authored_schema(SCHEMA)
TESTING = "claims-" + "1" * 32
MANDATE = "claims-" + "2" * 32
FACT = "claims-" + "3" * 32
BODY = "claims-" + "4" * 32
STATE_FACT = "claims-" + "5" * 32
VOCABULARY = {
    "model_testing": {"label": "Model testing", "short": "model testing", "definition": "Synthetic test.", "summary": "Explains the function.", "group": "Testing and science"},
    "research": {"label": "Research", "short": "research", "definition": "Synthetic test.", "summary": "Explains the function.", "group": "Testing and science"},
    "old_code": {"label": "Old code", "short": "old code", "definition": "Synthetic test.", "summary": "Explains the function.", "group": "Rules", "deprecated": True, "replaced_by": ["research"]},
    "policy_advice": {"label": "Policy advice", "short": "policy advice", "definition": "Synthetic test.", "summary": "Explains the function.", "group": "Informing government"},
}


def schema_errors(d, schema=AUTHORED):
    validator = Draft202012Validator(schema, format_checker=Draft202012Validator.FORMAT_CHECKER)
    return [e.message for e in validator.iter_errors(d)]


def excerpt(text, pointer):
    return {"text": text, "supports": [pointer]}


@pytest.fixture
def claimed(dataset):
    """Two function claims and a fact on the institute, a body role and a fact on the state, each bound once."""
    d = copy.deepcopy(dataset)
    d["dataset"]["vocabularies"]["function"] = copy.deepcopy(VOCABULARY)
    d["dataset"]["provisional"] = ["institutes[].functions", "vocabularies.function"]
    institute, state = d["institutes"][0], d["jurisdictions"][0]
    institute["claims"] = [
        {"id": TESTING, "kind": "function", "code": "model_testing", "basis": "activity",
         "text": "It ran a pre-deployment test of a named model.", "source_ids": ["gb-source"]},
        {"id": MANDATE, "kind": "function", "code": "model_testing", "basis": "mandate",
         "text": "Its founding text assigns it testing AI models.", "source_ids": ["gb-source"]},
        {"id": FACT, "kind": "fact", "text": "Its first phase runs to 2027.", "source_ids": ["gb-source"]},
    ]
    institute["check"]["functions"] = {"date": "2026-10-17", "codes": ["model_testing", "research"]}
    institute["check"]["log"] = [{"date": "2026-10-17", "text": "Searched in English."}]
    state["claims"] = [
        {"id": BODY, "kind": "function", "code": "research", "basis": "activity", "body_id": "test-regulator",
         "text": "It researches the security of foundation models.", "source_ids": ["gb-source"]},
        {"id": STATE_FACT, "kind": "fact", "text": "Its first team starts by the end of 2026.", "source_ids": ["gb-source"]},
    ]
    d["sources"][0]["excerpts"] += [
        excerpt("The institute ran a pre-deployment test of Synthetic Model 2.", "/institutes/0/claims/0/text"),
        excerpt("The institute shall test AI models.", "/institutes/0/claims/1/text"),
        excerpt("The first phase runs to 2027.", "/institutes/0/claims/2/text"),
        excerpt("Research on the security of foundation models.", "/jurisdictions/0/claims/0/text"),
        excerpt("The first team starts by the end of 2026.", "/jurisdictions/0/claims/1/text"),
    ]
    assert schema_errors(d) == []
    assert build.check_consistency(d, require_roster=True, complete=True) == []
    return d


def test_functions_rows_follow_claims_and_check(claimed):
    rows = build.export_dataset(claimed)["institutes"][0]["functions"]
    assert rows == [
        {"code": "model_testing", "value": "both", "claim_ids": [MANDATE, TESTING], "since": None, "newest": TESTING},
        {"code": "research", "value": "not_found", "claim_ids": [], "since": None, "newest": None},
        {"code": "policy_advice", "value": "not_coded", "claim_ids": [], "since": None, "newest": None},
    ]  # vocabulary order; the retired code gets no row; a jurisdiction's body claim never counts


@pytest.mark.parametrize("basis", ["mandate", "activity"])
@pytest.mark.parametrize("day", [None, "2024", "2024-11", "2024-11-20"])
def test_one_basis(basis, day):
    institute = {"claims": [{"id": TESTING, "kind": "function", "code": "research", "basis": basis}]}
    rows = claim_checks.function_rows(institute, {"function": VOCABULARY}, {TESTING: {"published": day}})
    assert rows[1] == {"code": "research", "value": basis, "claim_ids": [TESTING], "since": day if basis == "activity" else None, "newest": TESTING if basis == "activity" else None}
    assert rows[0]["value"] == "not_coded"


def test_csv_rows(claimed):
    tables = build.tables(build.export_dataset(claimed))
    assert [(r["institute_id"], r["code"], r["value"], r["claim_ids"]) for r in tables["functions"]] == [
        ("gb-test", "model_testing", "both", f"{MANDATE}; {TESTING}"),
        ("gb-test", "research", "not_found", ""),
        ("gb-test", "policy_advice", "not_coded", ""),
    ]
    rows = {r["id"]: r for r in tables["claims"]}
    assert list(rows) == [BODY, STATE_FACT, TESTING, MANDATE, FACT]
    assert (rows[BODY]["jurisdiction_id"], rows[BODY]["institute_id"], rows[BODY]["body_id"]) == ("GB", "", "test-regulator")
    assert (rows[FACT]["jurisdiction_id"], rows[FACT]["institute_id"], rows[FACT]["code"]) == ("GB", "gb-test", "")
    assert list(rows[TESTING]) == build.EMPTY_COLUMNS["claims"]
    assert list(tables["functions"][0]) == build.EMPTY_COLUMNS["functions"]
    contract = json.loads((Path(__file__).parent / "fixtures/csv_columns.json").read_text())
    assert list(tables["functions"][0]) == contract["functions"]
    assert tables["functions"][0]["since"] is None
    assert tables["functions"][0]["newest"] == TESTING


@pytest.mark.parametrize("undated", [False, True])
def test_function_rows_order_live_evidence_and_preserve_partial_dates(undated):
    claims = [{"id": cid, "kind": "function", "code": "research", "basis": "activity"}
              for cid in ("year", "expired-first", "full", "month", "tied", "mandate", "older", "expired-second", "mandate-second")]
    for c in claims:
        if c["id"].startswith("expired"):
            c["expired"] = True
        if c["id"].startswith("mandate"):
            c["basis"] = "mandate"
    dates = {"year": "2024", "expired-first": "2026", "full": "2024-11-20", "month": "2024-11",
             "tied": "2024-11-20", "mandate": "2020", "older": "2024-03", "expired-second": "2025"}
    if undated:
        claims.insert(0, {"id": "undated", "kind": "function", "code": "research", "basis": "activity"})
    rows = claim_checks.function_rows({"claims": claims}, {"function": VOCABULARY}, {cid: {"published": day} for cid, day in dates.items()})
    row = next(r for r in rows if r["code"] == "research")
    assert row == {"code": "research", "value": "both",
                   "claim_ids": ["mandate", "mandate-second", "full", "tied", "month", "older", "year"] + (["undated"] if undated else []) + ["expired-first", "expired-second"],
                   "since": "2024", "newest": "full"}


def test_function_rows_only_undated_and_expired_activity():
    claims = [{"id": cid, "kind": "function", "code": "research", "basis": "activity", "expired": expired}
              for cid, expired in (("expired", True), ("first", False), ("second", False))]
    sources = {"expired": {"published": "2026-01-01"}}
    row = claim_checks.function_rows({"claims": claims}, {"function": VOCABULARY}, sources)[1]
    assert (row["claim_ids"], row["since"], row["newest"]) == (["first", "second", "expired"], None, "first")
    for c in claims:
        c["expired"] = True
    row = claim_checks.function_rows({"claims": claims}, {"function": VOCABULARY}, sources)[1]
    assert (row["claim_ids"], row["since"], row["newest"]) == (["expired", "first", "second"], None, None)


def test_function_dates_use_text_binding_and_refresh_after_expiry(claimed):
    d = copy.deepcopy(claimed)
    source = d["sources"][0]
    source["published"] = "2024-11"
    claims = d["institutes"][0]["claims"]
    later = "claims-" + "6" * 32
    claims.append({**claims[0], "id": later, "source_ids": ["later", "unbound"]})
    # The newer cited source has no text binding, so it must never supply the claim date.
    d["sources"].extend([
        {**source, "id": "later", "published": "2025-02-03", "excerpts": [excerpt("Later tests.", "/institutes/0/claims/3/text")]},
        {**source, "id": "unbound", "published": "2026", "excerpts": []},
    ])
    empty = {"checked_to": None, "runs": [], "sources": {}}
    row = fact_pipeline.publish(d, rereads=empty)["institutes"][0]["functions"][0]
    assert (row["claim_ids"], row["since"], row["newest"]) == ([MANDATE, later, TESTING], "2024-11", later)
    states = {**empty, "sources": {"later": {rereads.quote_id("Later tests."): {
        "state": "quote_gone", "last_found": "2025-02-03", "since": "2025-02-04",
    }}}}
    published = fact_pipeline.publish(d, rereads=states)
    institute = published["institutes"][0]
    assert next(c for c in institute["claims"] if c["id"] == later)["expired"]
    row = institute["functions"][0]
    assert (row["claim_ids"], row["since"], row["newest"]) == ([MANDATE, TESTING, later], "2024-11", TESTING)
    csv = build.tables(published)["functions"][0]
    assert (csv["since"], csv["newest"]) == ("2024-11", TESTING)


def test_claim_sources_stay_off_the_page(claimed):
    """A1 leaves the page unchanged: a source cited only by claims is in the downloads, not on a card."""
    d = copy.deepcopy(claimed)
    gb = d["sources"][0]
    gb["excerpts"] = [e for e in gb["excerpts"] if e["supports"] != ["/institutes/0/claims/2/text"]]
    d["sources"].append({**{k: gb[k] for k in ("origin", "publisher", "type", "language", "accessed")},
                         "id": "gb-claim", "url": "https://example.org/gb-claim", "title": "Synthetic claim source",
                         "excerpts": [excerpt("The first phase runs to 2027.", "/institutes/0/claims/2/text")]})
    d["institutes"][0]["claims"][2]["source_ids"] = ["gb-claim"]
    assert build.check_consistency(d, require_roster=True, complete=True) == []
    institute = build.export_dataset(d)["institutes"][0]
    assert "gb-claim" not in record_sources(institute)
    assert "gb-claim" not in institute["display"]["source_links"]


def test_page_data_keeps_claims_and_functions(claimed):
    """Cards read claims, including their citations and freshness, without changing the download."""
    published = build.export_dataset(claimed)
    page = json.loads(build.page_data(published))
    assert page["dataset"]["vocabularies"]["function"] == published["dataset"]["vocabularies"]["function"] == VOCABULARY
    assert published["institutes"][0]["functions"] and published["jurisdictions"][0]["claims"]
    for collection in ("institutes", "jurisdictions"):
        for record, original in zip(page[collection], published[collection]):
            assert record.get("claims") == original.get("claims")
            assert record.get("functions") == original.get("functions")
    assert page["jurisdictions"][0]["check"] == {"last_checked": "2026-10-17", "method": "individual"}


def test_no_vocabulary_no_rows(dataset):
    assert "functions" not in build.export_dataset(dataset)["institutes"][0]


@pytest.mark.parametrize("code", VOCABULARY)
def test_short_required_on_every_function_code(claimed, code):
    del claimed["dataset"]["vocabularies"]["function"][code]["short"]
    assert "'short' is a required property" in schema_errors(claimed)


def test_short_is_a_nonempty_string(claimed):
    claimed["dataset"]["vocabularies"]["function"]["research"]["short"] = 7
    assert any("not of type 'string'" in e for e in schema_errors(claimed))


@pytest.mark.parametrize("schema", [SCHEMA, AUTHORED], ids=["published", "authored"])
@pytest.mark.parametrize("value", [None, "", 7], ids=["missing", "empty", "not-string"])
def test_function_summary_is_required_and_nonempty(schema, value):
    entry = copy.deepcopy(VOCABULARY["research"])
    if value is None:
        del entry["summary"]
    else:
        entry["summary"] = value
    shape = schema["properties"]["dataset"]["properties"]["vocabularies"]["properties"]["function"]["additionalProperties"]
    assert list(Draft202012Validator(shape).iter_errors(entry))


def located_errors(d, schema):
    validator = Draft202012Validator(schema, format_checker=Draft202012Validator.FORMAT_CHECKER)
    return [(list(e.absolute_path), e.message) for e in validator.iter_errors(d)]


def test_published_schema_accepts_the_export(claimed):
    published = build.export_dataset(claimed)
    assert located_errors(published, SCHEMA) == []


def test_rereads_reach_claims_excerpts_and_csv_never_authored(claimed):
    """Step C: a quote's re-read state joins its excerpt, its claim and claims.csv; authors never write it."""
    quote = "The institute shall test AI models."
    states = {"checked_to": "2026-10-15", "runs": ["r"], "sources": {"gb-source": {rereads.quote_id(quote): {
        "state": "quote_gone", "last_found": "2026-10-01", "since": "2026-10-15", "last_attempt": "2026-10-15"}}}}
    published = fact_pipeline.publish(claimed, rereads=states)
    assert located_errors(published, SCHEMA) == []
    claim = next(c for c in published["institutes"][0]["claims"] if c["id"] == MANDATE)
    assert claim["reread"] == {"state": "quote_gone", "last_found": "2026-10-01", "since": "2026-10-15"} and claim["expired"] is False
    assert "reread" not in next(c for c in published["institutes"][0]["claims"] if c["id"] == TESTING)
    rows = {r["id"]: r for r in build.tables(published)["claims"]}
    assert (rows[MANDATE]["reread_state"], rows[MANDATE]["last_found"]) == ("quote_gone", "2026-10-01")
    assert (rows[TESTING]["reread_state"], rows[TESTING]["last_found"]) == ("", "")
    strict = authored_schema(SCHEMA, strict=True)
    before = located_errors(claimed, strict)
    claimed["sources"][0]["excerpts"][-1]["reread"] = claim["reread"]
    claimed["institutes"][0]["claims"][1]["expired"] = False
    assert len(located_errors(claimed, strict)) == len(before) + 2


def test_digest_alerts_name_a_claim_expired_since_the_last_release(claimed):
    """The monitor joins fresh states into the claims as of today, so the weekly digest lists an expired claim."""
    from scripts import monitor_sources
    gb = claimed["sources"][0]
    quote = rereads.quote_id("The institute shall test AI models.")
    lost = {"run_id": "r", "source_id": gb["id"], "checked": "2026-10-01T09:00:00Z", "route": "direct", "class": "substantive",
            "observation": "live", "quotes": [{"id": quote, "in_cited_copy": True, "in_checked_copy": False}]}
    alerts = monitor_sources.reread_alerts(claimed, [lost], "2026-11-05")
    assert any(a.startswith(f"claim {MANDATE} (/institutes/0/claims/1) left the summary on 2026-11-02") for a in alerts)


def test_functions_are_generated_never_authored(claimed):
    strict = authored_schema(SCHEMA, strict=True)
    before = located_errors(claimed, strict)
    claimed["institutes"][0]["functions"] = []
    after = located_errors(claimed, strict)
    # jsonschema reports a false property schema at its parent, with the rejected value
    assert [e for e in after if e not in before] == [(["institutes", 0], "False schema does not allow []")]


def test_retirement_chain_and_quoted_sentence(claimed):
    """A code retired into one that is retired later keeps its replaced_by; a sentence may end in a closing quote."""
    d = copy.deepcopy(claimed)
    vocabulary = d["dataset"]["vocabularies"]["function"]
    vocabulary["research"].update(deprecated=True, replaced_by=["policy_advice"])
    d["institutes"][0]["check"]["functions"]["codes"] = ["model_testing"]
    d["jurisdictions"][0]["claims"][0]["code"] = "policy_advice"
    d["institutes"][0]["claims"][2]["text"] = "Its plan says: “The first phase runs to 2027.”"
    assert build.check_consistency(d, require_roster=True, complete=True) == []


def unbind(d, pointer):
    for e in d["sources"][0]["excerpts"]:
        e["supports"] = [p for p in e["supports"] if p != pointer]
    d["sources"][0]["excerpts"] = [e for e in d["sources"][0]["excerpts"] if e["supports"]]


MUTATIONS = {
    "unbound text": (lambda d: unbind(d, "/institutes/0/claims/0/text"), "required claim has no excerpt: /institutes/0/claims/0/text"),
    "two quotes": (lambda d: d["sources"][0]["excerpts"].append(excerpt("A second quote.", "/institutes/0/claims/0/text")), "a claim has exactly one quote"),
    "unknown code": (lambda d: d["institutes"][0]["claims"][0].update(code="model_tests"), "unknown function code 'model_tests'"),
    "retired code": (lambda d: d["institutes"][0]["claims"][0].update(code="old_code"), "function code 'old_code' is retired"),
    "body not related": (lambda d: d["jurisdictions"][0]["claims"][0].update(body_id="test-nobody"), "is not a related body of this jurisdiction"),
    "two sentences": (lambda d: d["institutes"][0]["claims"][2].update(text="Its first phase runs to 2027. It has no budget."), "text is one sentence"),
    "no full stop": (lambda d: d["institutes"][0]["claims"][2].update(text="Its first phase runs to 2027"), "text is one sentence"),
    "coded against a retired code": (lambda d: d["institutes"][0]["check"]["functions"]["codes"].append("old_code"), "check.functions code 'old_code' is not a current function code"),
    "functions on a state": (lambda d: d["jurisdictions"][0]["check"].update(functions={"date": "2026-10-17", "codes": ["research"]}), "check.functions is for institutes only"),
    "replaced by itself": (lambda d: d["dataset"]["vocabularies"]["function"]["old_code"].update(replaced_by=["old_code"]), "replaced_by 'old_code' is not another code in the list"),
    "replaced by a missing code": (lambda d: d["dataset"]["vocabularies"]["function"]["old_code"].update(replaced_by=["gone"]), "replaced_by 'gone' is not another code in the list"),
    "provisional path unknown": (lambda d: d["dataset"]["provisional"].append("institutes[].functionz"), "'institutes[].functionz' names no field in the schema"),
    "claim id reused": (lambda d: d["jurisdictions"][0]["claims"][1].update(id=FACT), "claim id is used twice"),
    "source not cited": (lambda d: d["institutes"][0]["claims"][0].update(source_ids=["no-such-source"]), "no-such-source"),
}


@pytest.mark.parametrize("name", MUTATIONS)
def test_mutation_fails(claimed, name):
    mutate, message = MUTATIONS[name]
    d = copy.deepcopy(claimed)
    mutate(d)
    errors = build.check_consistency(d, require_roster=True, complete=True)
    assert any(message in e for e in errors), errors


def test_fact_needs_a_status():
    d = {
        "dataset": {"vocabularies": {"function": VOCABULARY}},
        "sources": [],
        "institutes": [],
        "jurisdictions": [{"id": "XX", "status": "none_identified", "check": {}, "claims": [
            {"id": FACT, "kind": "fact", "text": "A quoted fact.", "source_ids": ["x"]}]}],
    }
    assert any("a fact claim sits only on an institute" in e for e in claim_checks.check_claims(d))
    d["jurisdictions"][0]["status"] = "planned"
    assert claim_checks.check_claims(d) == []


SHAPES = {
    "function without basis": (lambda d: d["institutes"][0]["claims"][0].pop("basis")),
    "function without code": (lambda d: d["institutes"][0]["claims"][0].pop("code")),
    "fact with a code": (lambda d: d["institutes"][0]["claims"][2].update(code="research")),
    "body_id on an institute": (lambda d: d["institutes"][0]["claims"][0].update(body_id="test-regulator")),
    "state function without body_id": (lambda d: d["jurisdictions"][0]["claims"][0].pop("body_id")),
    "id not permanent form": (lambda d: d["institutes"][0]["claims"][0].update(id="claims-1")),
    "unknown kind": (lambda d: d["institutes"][0]["claims"][0].update(kind="opinion")),
    "empty check.functions": (lambda d: d["institutes"][0]["check"]["functions"].update(codes=[])),
    "group missing": (lambda d: d["dataset"]["vocabularies"]["function"]["research"].pop("group")),
    "retired without successor": (lambda d: d["dataset"]["vocabularies"]["function"]["old_code"].pop("replaced_by")),
}


@pytest.mark.parametrize("name", SHAPES)
def test_shape_fails(claimed, name):
    d = copy.deepcopy(claimed)
    SHAPES[name](d)
    assert schema_errors(d)


def test_real_function_summaries_are_short_reader_sentences():
    real = json.loads(build.DATA.read_text())
    for code, entry in real["dataset"]["vocabularies"]["function"].items():
        summary = entry["summary"]
        assert isinstance(summary, str) and summary.strip(), code
        assert summary.endswith(".") and not any(mark in summary[:-1] for mark in ".?!"), code
        assert len(summary.split()) <= 18, code
        assert not any(mark in summary for mark in ";()"), code
        # the EU AI Office is one of the rows, so no sentence may tie a function to a nation (owner's hub, 18 Nov 2026)
        assert not re.search(r"\b(nationa\w*|countr\w*|at home)\b", summary, re.I), code
        assert summary != entry["definition"], code


def test_real_vocabulary_is_the_spec_list():
    real = json.loads(build.DATA.read_text())
    assert all(claim_checks.schema_node(p) for p in real["dataset"]["provisional"])


HELD_VOCABULARY = {code: {**VOCABULARY["research"], "label": code} for code in
                   ("model_testing", "evaluation_methods", "standards_guidance", "international_cooperation", "research")}


def held_row(code: str, held: dict, entry_day: str = "2025-06") -> dict:
    institute = {"claims": [{"id": "entry", "kind": "function", "code": code, "basis": "activity"}], **held}
    rows = claim_checks.function_rows(institute, {"function": HELD_VOCABULARY}, {"entry": {"published": entry_day}})
    return next(r for r in rows if r["code"] == code)


HELD_PAIRS = [("joint_testing", "model_testing"), ("joint_testing", "international_cooperation"), ("evaluation", "model_testing"),
              ("tool", "evaluation_methods"), ("dataset", "evaluation_methods"), ("guidance", "standards_guidance")]


def test_held_map_is_the_approved_one():
    assert sorted((kind, code) for kind, codes in claim_checks.OUTPUT_FUNCTIONS.items() for code in codes) == sorted(HELD_PAIRS)


@pytest.mark.parametrize("kind, code", HELD_PAIRS)
def test_older_credited_output_withholds_since(kind, code):
    older = {"outputs": [{"type": kind, "date": "2024-11-20", "credit": {"institute_id": "x", "date": "2024-11-20"}}]}
    newer = {"outputs": [{"type": kind, "date": "2026-01", "credit": {"institute_id": "x", "date": "2026-01"}}]}
    assert held_row(code, older)["since"] is None
    assert held_row(code, newer)["since"] == "2025-06"


def test_older_signed_agreement_withholds_cooperation_since():
    def agreement(state="signed", party="government", day="2024-04-01"):
        return {"agreements": [{"state": state, "counterparty_type": party, "date": day}]}
    assert held_row("international_cooperation", agreement())["since"] is None
    assert held_row("international_cooperation", agreement(party="institute"))["since"] is None
    assert held_row("international_cooperation", agreement(state="announced"))["since"] == "2025-06"
    assert held_row("international_cooperation", agreement(state="pledged"))["since"] == "2025-06"
    assert held_row("international_cooperation", agreement(day="2025"))["since"] == "2025-06"  # same year, coarser: not older
    assert held_row("international_cooperation", agreement(party="developer"))["since"] == "2025-06"
    assert held_row("international_cooperation", agreement(day="2025-07"))["since"] == "2025-06"
    assert held_row("model_testing", agreement())["since"] == "2025-06"


def test_reports_and_uncredited_outputs_never_withhold_since():
    report = {"outputs": [{"type": "report", "date": "2020", "credit": {"institute_id": "x", "date": "2020"}}]}
    uncredited = {"outputs": [{"type": "joint_testing", "date": "2020"}]}
    assert held_row("research", report)["since"] == "2025-06"
    assert held_row("model_testing", uncredited)["since"] == "2025-06"
    # Same month at a coarser precision is not older: "2025-06" pads before "2025-06-01" only within a prefix.
    same = {"outputs": [{"type": "evaluation", "date": "2025-06-15", "credit": {"institute_id": "x", "date": "2025-06-15"}}]}
    assert held_row("model_testing", same)["since"] == "2025-06"


def test_held_output_uses_credit_date_then_output_date_and_shared_precision():
    credited_later = {"outputs": [{"type": "evaluation", "date": "2024-01", "credit": {"institute_id": "x", "date": "2026-01"}}]}
    assert held_row("model_testing", credited_later)["since"] == "2025-06"  # the institute's credit starts after its entry
    no_credit_date = {"outputs": [{"type": "evaluation", "date": "2024-01", "credit": {"institute_id": "x"}}]}
    assert held_row("model_testing", no_credit_date)["since"] is None
    coarse = {"outputs": [{"type": "evaluation", "date": "2025-06", "credit": {"institute_id": "x", "date": "2025-06"}}]}
    assert held_row("model_testing", coarse, entry_day="2025-06-15")["since"] == "2025-06-15"
    assert held_row("model_testing", coarse, entry_day="2025-07-01")["since"] is None
