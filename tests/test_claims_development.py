"""Claims (build step A2 of docs/design/claims-1.1.md): the development kind, the closest body's lead
role claim and gap excerpt, and designation_step; each gate with its mutation."""

import copy

import pytest

from scripts import build, claim_checks
from test_claims import SCHEMA, VOCABULARY, located_errors, schema_errors


def excerpt(text, *pointers):
    return {"text": text, "supports": list(pointers)}


DEVELOPMENT = "claims-" + "6" * 32
LEAD = "claims-" + "7" * 32
ROLE = "claims-" + "8" * 32
DEVELOPMENTS = {
    "public_body": {"label": "Named public body or programme", "definition": "Synthetic test."},
    "testing_duty": {"label": "Testing duty", "definition": "Synthetic test."},
    "old_duty": {"label": "Old duty", "definition": "Synthetic test.", "deprecated": True, "replaced_by": ["testing_duty"]},
}


@pytest.fixture
def lead(dataset):
    """A state with none identified: a closest body with two role claims, one marked lead, a gap excerpt,
    a development, and a proposed AI Act authority with its latest step; each leaf bound once."""
    d = copy.deepcopy(dataset)
    d["dataset"]["exchange_rates"] = []
    d["dataset"]["vocabularies"]["function"] = copy.deepcopy(VOCABULARY)
    d["dataset"]["vocabularies"]["development"] = copy.deepcopy(DEVELOPMENTS)
    d["dataset"]["provisional"] = ["institutes[].functions", "vocabularies.function", "vocabularies.development"]
    state = d["jurisdictions"][0]
    state.update(
        status="none_identified", status_confidence="low", institute_id=None, timeline=[],
        closest_body={"name": "Synthetic model test centre", "body_id": "test-regulator", "stage": "exists", "gap": "designation",
                      "note": "Develops methods for auditing models.", "source_ids": ["gb-source"]},
    )
    state.pop("status_since")
    state["related_bodies"].append({
        "id": "test-authority", "name": "Synthetic telecoms office", "kind": "ai_act_authority", "role": "Proposed market-surveillance authority.",
        "counts_as_aisi": False, "designation_state": "proposed", "designation_basis": "none_yet", "authority_roles": ["market_surveillance"],
        "designation_step": {"text": "bill tabled", "date": "2026-09-24"}, "source_ids": ["gb-source"],
    })
    state["claims"] = [
        {"id": ROLE, "kind": "function", "code": "research", "basis": "activity", "body_id": "test-regulator",
         "text": "It says it researches the security of foundation models.", "source_ids": ["gb-source"]},
        {"id": LEAD, "kind": "function", "code": "model_testing", "basis": "activity", "body_id": "test-regulator", "lead": True,
         "text": "It says it develops \"methods for auditing models\".", "source_ids": ["gb-source"]},
        {"id": DEVELOPMENT, "kind": "development", "code": "testing_duty", "date": "2026-03-05",
         "act": "the government adopted the national AI strategy to 2030",
         "text": "plans national safety-test procedures for algorithms", "source_ids": ["gb-source"]},
    ]
    d["institutes"] = []
    d["sources"][0]["excerpts"] = [
        excerpt("Synthetic centre exists and develops methods for auditing models.",
                *[f"/jurisdictions/0/closest_body/{key}" for key in ("name", "stage", "note")]),
        excerpt("The centre works on its own initiative.", "/jurisdictions/0/closest_body/gap"),
        excerpt("Research on the security of foundation models.", "/jurisdictions/0/claims/0/text"),
        excerpt("Wir entwickeln Methoden zur Prüfung von Modellen.", "/jurisdictions/0/claims/1/text")
        | {"gloss": "We develop Methods for Auditing Models."},
        excerpt("The government adopted the national AI strategy to 2030 today.",
                "/jurisdictions/0/claims/2/act", "/jurisdictions/0/claims/2/date"),
        excerpt("National safety-test procedures for algorithms.", "/jurisdictions/0/claims/2/text"),
        excerpt("The bill was tabled on 24 September 2026.",
                "/jurisdictions/0/related_bodies/1/designation_step/text", "/jurisdictions/0/related_bodies/1/designation_step/date"),
    ]
    assert schema_errors(d) == []
    assert build.check_consistency(d) == []
    return d


def test_published_schema_and_csv(lead):
    published = build.export_dataset(lead)
    assert located_errors(published, SCHEMA) == []
    tables = build.tables(published)
    claims = {r["id"]: r for r in tables["claims"]}
    assert (claims[DEVELOPMENT]["act"], claims[DEVELOPMENT]["date"], claims[DEVELOPMENT]["lead"]) == (
        "the government adopted the national AI strategy to 2030", "2026-03-05", "")
    assert claims[LEAD]["lead"] is True and claims[ROLE]["lead"] == ""
    assert tables["closest_bodies"][0]["body_id"] == "test-regulator"
    authority = tables["related_bodies"][1]
    assert (authority["designation_step_text"], authority["designation_step_date"]) == ("bill tabled", "2026-09-24")
    assert list(claims[LEAD]) == build.EMPTY_COLUMNS["claims"]
    assert list(authority) == build.EMPTY_COLUMNS["related_bodies"]
    assert list(tables["closest_bodies"][0]) == build.EMPTY_COLUMNS["closest_bodies"]


def test_rereads_and_expiry_on_a_development(lead):
    """Step C on A2's shape: the claim takes the worst of its text, act and date leaves, and leaves the summary
    more than three years after its date."""
    from scripts import published as fact_pipeline, rereads
    act = rereads.quote_id("The government adopted the national AI strategy to 2030 today.")
    states = {"checked_to": "2026-10-15", "runs": ["r"], "sources": {"gb-source": {act: {
        "state": "blocked_or_failed", "last_found": "2026-09-01", "since": "2026-10-10", "last_attempt": "2026-10-15"}}}}
    development = fact_pipeline.publish(lead, rereads=states)["jurisdictions"][0]["claims"][2]
    assert development["reread"] == {"state": "blocked_or_failed", "last_found": "2026-09-01", "since": "2026-10-10"}
    assert development["expired"] is False
    old = copy.deepcopy(lead)
    old["jurisdictions"][0]["claims"][2]["date"] = "2023-10-16"  # one day outside the window of dataset.as_of 2026-10-17
    assert fact_pipeline.publish(old, rereads=states)["jurisdictions"][0]["claims"][2]["expired"] is True


def test_without_body_id_nothing_new_binds(lead):
    """Until step B: a closest body that names no related body needs no gap excerpt and no lead (today's 11)."""
    d = copy.deepcopy(lead)
    del d["jurisdictions"][0]["closest_body"]["body_id"]
    d["jurisdictions"][0]["claims"][1].pop("lead")
    d["sources"][0]["excerpts"] = [e for e in d["sources"][0]["excerpts"] if e["supports"] != ["/jurisdictions/0/closest_body/gap"]]
    assert build.check_consistency(d) == []


def test_lead_quote_in_english_excerpt_and_curly_marks(lead):
    """With no gloss the span is matched in the excerpt; curly quotation marks count; case is ignored."""
    d = copy.deepcopy(lead)
    d["sources"][0]["excerpts"][3] = excerpt("We develop methods for auditing models.", "/jurisdictions/0/claims/1/text")
    d["jurisdictions"][0]["claims"][1]["text"] = "It says it develops “Methods for auditing models”."
    assert build.check_consistency(d) == []


def test_development_window_is_inclusive(lead):
    d = copy.deepcopy(lead)
    d["jurisdictions"][0]["claims"][2]["date"] = "2023-10-17"  # three years before check.last_checked 2026-10-17
    assert build.check_consistency(d) == []


def test_development_needs_last_checked():
    record = {"id": "XX", "status": "none_identified", "check": {}}
    claim = {"code": "testing_duty", "date": "2026-03-05", "act": "the government adopted it", "text": "plans tests"}
    errors = claim_checks.check_development("XX claim", record, claim, {"development": DEVELOPMENTS}, {})
    assert errors == ["XX claim: a development needs check.last_checked, which its three-year window counts back from"]


def test_years_before_leap_day():
    assert claim_checks.years_before("2028-02-29", 3) == "2025-02-28"


def unbind(d, pointer):
    for e in d["sources"][0]["excerpts"]:
        e["supports"] = [p for p in e["supports"] if p != pointer]
    d["sources"][0]["excerpts"] = [e for e in d["sources"][0]["excerpts"] if e["supports"]]


def add_source(d, **fields):
    gb = d["sources"][0]
    d["sources"].append({**{k: gb[k] for k in ("origin", "publisher", "type", "language", "accessed")},
                         "id": "other-source", "url": "https://example.org/other", "title": "Synthetic other source", **fields,
                         "excerpts": [excerpt("The government adopted the strategy.", "/jurisdictions/0/claims/2/act")]})
    unbind(d, "/jurisdictions/0/claims/2/act")  # the date stays bound to the first excerpt
    d["jurisdictions"][0]["claims"][2]["source_ids"].append("other-source")


def claims(d):
    return d["jurisdictions"][0]["claims"]


MUTATIONS = {
    "act unbound": (lambda d: unbind(d, "/jurisdictions/0/claims/2/act"), "required claim has no excerpt: /jurisdictions/0/claims/2/act"),
    "date unbound": (lambda d: unbind(d, "/jurisdictions/0/claims/2/date"), "required claim has no excerpt: /jurisdictions/0/claims/2/date"),
    "development text unbound": (lambda d: unbind(d, "/jurisdictions/0/claims/2/text"), "required claim has no excerpt: /jurisdictions/0/claims/2/text"),
    "act quoted twice": (lambda d: d["sources"][0]["excerpts"].append(excerpt("Adopted.", "/jurisdictions/0/claims/2/act")), "act has 2 bound excerpts"),
    "gap unbound": (lambda d: unbind(d, "/jurisdictions/0/closest_body/gap"), "required claim has no excerpt: /jurisdictions/0/closest_body/gap"),
    "step text unbound": (lambda d: unbind(d, "/jurisdictions/0/related_bodies/1/designation_step/text"),
                          "required claim has no excerpt: /jurisdictions/0/related_bodies/1/designation_step/text"),
    "step date unbound": (lambda d: unbind(d, "/jurisdictions/0/related_bodies/1/designation_step/date"),
                          "required claim has no excerpt: /jurisdictions/0/related_bodies/1/designation_step/date"),
    "unknown development code": (lambda d: claims(d)[2].update(code="public_bodies"), "unknown development code 'public_bodies'"),
    "function code on a development": (lambda d: claims(d)[2].update(code="model_testing"), "unknown development code 'model_testing'"),
    "retired development code": (lambda d: claims(d)[2].update(code="old_duty"), "development code 'old_duty' is retired"),
    "development replaced by a missing code": (lambda d: d["dataset"]["vocabularies"]["development"]["old_duty"].update(replaced_by=["gone"]),
                                               "dataset.vocabularies.development.old_duty: replaced_by 'gone'"),
    "two developments": (lambda d: claims(d).append({**claims(d)[2], "id": "claims-" + "9" * 32}), "at most one development claim"),
    "development on a state with a status": (lambda d: d["jurisdictions"][0].update(status="planned"),
                                             "a development sits only on a jurisdiction with none identified"),
    "development text a sentence": (lambda d: claims(d)[2].update(text="Plans safety tests."), "a development's text is the clause"),
    "development text two sentences": (lambda d: claims(d)[2].update(text="plans safety tests. It also funds a lab"), "a development's text is the clause"),
    "development older than three years": (lambda d: claims(d)[2].update(date="2023-10-16"), "within 3 years before check.last_checked (2026-10-17), not 2023-10-16"),
    "development after the check": (lambda d: claims(d)[2].update(date="2026-10-18"), "not 2026-10-18"),
    "act from a secondary source": (lambda d: add_source(d, type="secondary"), "the act's excerpt is from other-source, not a primary source of GB"),
    "act from another state's source": (lambda d: add_source(d, origin="FR"), "the act's excerpt is from other-source, not a primary source of GB"),
    "body_id not related": (lambda d: d["jurisdictions"][0]["closest_body"].update(body_id="test-nobody"),
                            "closest_body.body_id 'test-nobody' is not a related body"),
    "no lead": (lambda d: claims(d)[1].pop("lead"), "the closest body has role claims, so one of them is marked lead"),
    "two leads": (lambda d: claims(d)[0].update(lead=True), "at most one claim is marked lead"),
    "lead on another body": (lambda d: claims(d)[1].update(body_id="test-authority"), "lead marks a role claim of the closest body"),
    "lead quote not in gloss": (lambda d: claims(d)[1].update(text="It says it develops \"methods for testing models\"."), "the lead claim's text quotes"),
    "lead quote from the original, not the gloss": (lambda d: claims(d)[1].update(text="It says it develops \"Methoden zur Prüfung\"."),
                                                    "the lead claim's text quotes"),
    "lead without a quotation": (lambda d: claims(d)[1].update(text="It says it develops methods for auditing models."), "the lead claim's text quotes"),
}


@pytest.mark.parametrize("name", MUTATIONS)
def test_mutation_fails(lead, name):
    mutate, message = MUTATIONS[name]
    d = copy.deepcopy(lead)
    mutate(d)
    errors = build.check_consistency(d)
    assert any(message in e for e in errors), errors


SHAPES = {
    "development without act": (lambda d: claims(d)[2].pop("act")),
    "development without date": (lambda d: claims(d)[2].pop("date")),
    "development with a basis": (lambda d: claims(d)[2].update(basis="mandate")),
    "development with body_id": (lambda d: claims(d)[2].update(body_id="test-regulator")),
    "development marked lead": (lambda d: claims(d)[2].update(lead=True)),
    "function with an act": (lambda d: claims(d)[0].update(act="the government adopted it")),
    "lead false": (lambda d: claims(d)[1].update(lead=False)),
    "step on a designated authority": (lambda d: d["jurisdictions"][0]["related_bodies"][1].update(designation_state="designated")),
    "step on another body": (lambda d: d["jurisdictions"][0]["related_bodies"][0].update(designation_step={"text": "bill tabled", "date": "2026-09-24"})),
    "step without a date": (lambda d: d["jurisdictions"][0]["related_bodies"][1]["designation_step"].pop("date")),
    "development code group": (lambda d: d["dataset"]["vocabularies"]["development"]["public_body"].update(group="Rules")),
}


@pytest.mark.parametrize("name", SHAPES)
def test_shape_fails(lead, name):
    d = copy.deepcopy(lead)
    SHAPES[name](d)
    assert schema_errors(d)


def test_institute_claims_have_no_development_or_lead(dataset):
    d = copy.deepcopy(dataset)
    d["institutes"][0]["claims"] = [{"id": DEVELOPMENT, "kind": "development", "code": "testing_duty", "date": "2026-03-05",
                                     "act": "the government adopted it", "text": "plans tests", "source_ids": ["gb-source"]}]
    assert schema_errors(d)
    d["institutes"][0]["claims"] = [{"id": LEAD, "kind": "function", "code": "research", "basis": "activity", "lead": True,
                                     "text": "It says it does \"research\".", "source_ids": ["gb-source"]}]
    assert schema_errors(d)
