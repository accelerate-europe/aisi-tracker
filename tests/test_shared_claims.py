"""Hand fixtures: real pilot/MOU/UK budget values plus deliberate credit-delay variant.

Provenance: data/aisi.json, sg-pilot-2024 (20 Nov 2024), depth-uk-us-mou
(1 Apr 2024), gb-about (GBP66m). US credit delay and staff same-source reuse
are deliberately synthetic counterexamples; they are NOT factual data corrections."""

import copy
import json
import sys

import pytest
from conftest import ROOT, write_json

from scripts import build
from scripts.shared_claims import check_shared_claims, shared_claim_coverage


@pytest.fixture
def claim_data():
    return json.loads((ROOT / "tests/fixtures/shared_claims.json").read_text())


def assert_error(data, phrase):
    errors = check_shared_claims(data)
    assert any(phrase in e for e in errors), errors


def test_valid_and_source_publication_not_instrument_date(claim_data):
    assert check_shared_claims(claim_data) == []


def test_lineage_rename_cannot_hide_shared_document_date_change(claim_data):
    claim_data["institutes"][1]["outputs"][0].update(
        lineage="invented-lineage", date="2024-11-19"
    )
    assert_error(claim_data, "disagrees with document")


def test_document_anchor_missing_fails_for_repeat(claim_data):
    del claim_data["sources"][0]["output_lineage"]
    assert_error(claim_data, "lacks a declared document source")


def test_different_documents_on_same_index_are_not_conflated(claim_data):
    claim_data["sources"].append(
        {
            "id": "other-document",
            "url": "https://example.org/other",
            "output_role": "document",
            "output_lineage": "unrelated",
        }
    )
    claim_data["institutes"][0]["outputs"].append(
        {
            "lineage": "unrelated",
            "date": "2025-01",
            "source_ids": ["index", "other-document"],
        }
    )
    assert check_shared_claims(claim_data) == []


def test_all_outputs_need_anchor_when_document_roles_are_reviewed(claim_data):
    for source in claim_data["sources"]:
        source["output_role"] = (
            "document" if source.get("output_lineage") else "context"
        )
        source["url"] = "https://example.org/" + source["id"]
    claim_data["institutes"][0]["outputs"].append(
        {"lineage": "new", "date": "2025", "source_ids": ["index"]}
    )
    assert_error(claim_data, "lacks a document identity anchor")


def test_removing_every_role_cannot_disable_document_checks(claim_data):
    for source in claim_data["sources"]:
        source.pop("output_role", None)
        source.pop("output_lineage", None)
    assert_error(claim_data, "output citation needs explicit document/context role")
    assert_error(claim_data, "output lacks a document identity anchor")


def test_small_single_output_fragment_still_needs_document_role(claim_data):
    fragment = {
        "institutes": [
            {
                "id": "one",
                "outputs": [{"lineage": "one", "date": "2025", "source_ids": ["one"]}],
            }
        ],
        "sources": [{"id": "one", "url": "https://example.org/one"}],
    }
    errors = check_shared_claims(fragment)
    assert any("needs explicit document/context role" in e for e in errors)
    assert any("lacks a document identity anchor" in e for e in errors)


def test_alias_url_cannot_assign_a_different_lineage(claim_data):
    source = claim_data["sources"][0]
    source.update(output_role="document", url="https://example.org/report")
    claim_data["sources"].append(
        {
            "id": "alias",
            "url": source["url"],
            "output_role": "document",
            "output_lineage": "wrong",
        }
    )
    assert_error(claim_data, "same document URL has conflicting lineages")


def test_first_output_binding_cannot_be_removed(claim_data):
    for jurisdiction in claim_data["jurisdictions"]:
        for event in jurisdiction.get("timeline", []):
            if event.get("event") == "first_output":
                event.pop("output_lineage", None)
    assert_error(claim_data, "needs one explicit output_lineage")


def test_later_local_credit_is_allowed(claim_data):
    claim_data["institutes"][1]["outputs"][0]["credited_from"] = "2024-12-01"
    claim_data["jurisdictions"][1]["timeline"][0]["date"] = "2024-12-01"
    assert check_shared_claims(claim_data) == []


def test_partial_credit_date_uses_existing_method_precision(claim_data):
    claim_data["institutes"][1]["outputs"][0]["credited_from"] = "2024-11"
    claim_data["jurisdictions"][1]["timeline"][0]["date"] = "2024-11"
    assert check_shared_claims(claim_data) == []


def test_first_output_uses_credit_date_not_global_publication(claim_data):
    claim_data["jurisdictions"][1]["timeline"][0]["date"] = "2024-11-20"
    assert_error(claim_data, "bound output eligibility date")


def test_first_output_binding_requires_source_of_selected_output(claim_data):
    claim_data["jurisdictions"][0]["timeline"][0]["source_ids"] = ["gb-about"]
    assert_error(claim_data, "no source shared with its output")


def test_milestone_mutation(claim_data):
    claim_data["dataset"]["network_milestones"][0]["date"] = "2024-11"
    assert_error(claim_data, "pilot-publication: repeated date disagrees")


def test_agreement_date_mutation(claim_data):
    claim_data["institutes"][1]["agreements"][0]["date"] = "2024-04-02"
    assert_error(claim_data, "uk-us-instrument: repeated agreement disagrees")


def test_agreement_state_mutation_not_grouped_away(claim_data):
    claim_data["institutes"][1]["agreements"][0]["state"] = "announced"
    assert_error(claim_data, "uk-us-instrument: repeated agreement disagrees")


def test_missing_one_agreement_date_is_conflict(claim_data):
    del claim_data["institutes"][1]["agreements"][0]["date"]
    assert_error(claim_data, "uk-us-instrument: repeated agreement disagrees")


def test_annual_amount_mutation(claim_data):
    claim_data["institutes"][0]["funding"][0]["annual_amount"] = 66000001
    assert_error(claim_data, "uk-annual: repeated number disagrees")


def test_same_source_different_subject_and_metric_not_equal(claim_data):
    claim_data["institutes"][1]["staff"][0]["total"] = 37
    assert check_shared_claims(claim_data) == []
    assert (
        "/institutes/1/staff/0/total"
        in shared_claim_coverage(claim_data)["unchecked_numeric_leaves"]
    )


def test_source_citation_removed(claim_data):
    claim_data["institutes"][0]["funding"][0]["source_ids"] = ["index"]
    assert_error(claim_data, "does not cite this claim")


def test_unit_changed_cannot_silently_compare_other_currency(claim_data):
    claim_data["institutes"][0]["funding"][0]["currency"] = "EUR"
    assert_error(claim_data, "matched 0 records")


def test_period_changed_cannot_silently_compare_other_year(claim_data):
    claim_data["institutes"][0]["funding"][0]["period"] = "2027"
    assert_error(claim_data, "matched 0 records")


def test_selector_ambiguous_fails(claim_data):
    claim_data["institutes"][0]["funding"].append(
        copy.deepcopy(claim_data["institutes"][0]["funding"][0])
    )
    assert_error(claim_data, "matched 2 records")


def test_reordering_preserves_identity(claim_data):
    claim_data["institutes"].reverse()
    claim_data["jurisdictions"].reverse()
    assert check_shared_claims(claim_data) == []


def test_deleted_target_is_not_silently_skipped(claim_data):
    claim_data["institutes"][0]["funding"] = []
    assert_error(claim_data, "matched 0 records")


def test_duplicate_target_is_not_evidence(claim_data):
    c = claim_data["dataset"]["shared_claims"][2]
    c["targets"][1] = copy.deepcopy(c["targets"][0])
    assert_error(claim_data, "duplicate target")


def test_bool_is_not_number(claim_data):
    claim_data["institutes"][0]["funding"][0]["amount"] = True
    assert_error(claim_data, "is not a number")


def test_registry_cannot_shrink_to_one_target(claim_data):
    claim_data["dataset"]["shared_claims"][2]["targets"].pop()
    assert_error(claim_data, "needs at least two targets")


def test_source_same_date_distinct_event_not_assumed_equal(claim_data):
    claim_data["dataset"]["network_milestones"].append(
        {"id": "meeting", "date": "2024-11-21", "source_ids": ["sg-pilot-2024"]}
    )
    assert check_shared_claims(claim_data) == []


@pytest.mark.parametrize(
    ("key", "value"), [("unit", "EUR"), ("period", "2027"), ("subject", "us-caissi")]
)
def test_numeric_claim_identity_itself_is_enforced(claim_data, key, value):
    claim_data["dataset"]["shared_claims"][2][key] = value
    assert_error(claim_data, f"claim {key}")


@pytest.mark.parametrize("mutation", [None, "unit", "period"])
def test_staff_identity_uses_people_and_observation_date(claim_data, mutation):
    record = claim_data["institutes"][1]["staff"][0]
    record.update(id="headcount", as_of="2025-01", technical=30, target=30)
    claim_data["dataset"]["shared_claims"].append(
        {
            "id": "staff",
            "kind": "number",
            "subject": "us-caissi",
            "metric": "staff",
            "unit": "people",
            "period": "2025-01",
            "scope": "synthetic equal headcounts",
            "source_ids": ["gb-about"],
            "targets": [
                ["institutes", {"id": "us-caissi"}, "staff", {"id": "headcount"}, field]
                for field in ["total", "technical", "target"]
            ],
        }
    )
    assert check_shared_claims(claim_data) == []
    if mutation:
        claim_data["dataset"]["shared_claims"][-1][mutation] = (
            "GBP" if mutation == "unit" else "2025-02"
        )
        errors = check_shared_claims(claim_data)
        for field in ("total", "technical", "target"):
            assert any(f"/staff/0/{field}: claim {mutation}" in e for e in errors), errors


@pytest.mark.parametrize("missing", ["currency", "period"])
def test_missing_numeric_context_cannot_bypass_identity(claim_data, missing):
    claim = claim_data["dataset"]["shared_claims"][2]
    for selector in claim["targets"]:
        selector[3] = {"amount": 66000000}
    del claim_data["institutes"][0]["funding"][0][missing]
    assert_error(claim_data, "lacks unit or period context")


def test_numeric_target_cannot_move_to_an_unchecked_field(claim_data):
    record = claim_data["institutes"][0]["funding"][0]
    record["years"] = record["amount"]
    claim_data["dataset"]["shared_claims"][2]["targets"][0][-1] = "years"
    assert_error(claim_data, "not a supported funding amount field")


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("context_lineage", "context source cannot anchor"),
        ("document_without_lineage", "document source needs output_lineage"),
        ("duplicate_id", "duplicate shared claim ID"),
        ("unknown_source", "missing or unknown source identity"),
    ],
)
def test_invalid_registry_and_source_roles(claim_data, mutation, message):
    if mutation == "context_lineage":
        claim_data["sources"][0]["output_role"] = "context"
    elif mutation == "document_without_lineage":
        del claim_data["sources"][0]["output_lineage"]
    elif mutation == "duplicate_id":
        claim_data["dataset"]["shared_claims"].append(
            copy.deepcopy(claim_data["dataset"]["shared_claims"][0])
        )
    else:
        claim_data["dataset"]["shared_claims"][0]["source_ids"] = ["unknown"]
    assert_error(claim_data, message)


def test_first_output_cannot_bind_another_institutes_only_lineage(claim_data):
    claim_data["sources"].append(
        {
            "id": "other",
            "url": "https://example.org/other",
            "output_role": "document",
            "output_lineage": "other",
        }
    )
    claim_data["institutes"][1]["outputs"].append(
        {"lineage": "other", "date": "2025", "source_ids": ["other"]}
    )
    claim_data["jurisdictions"][0]["timeline"][0]["output_lineage"] = "other"
    assert_error(claim_data, "first_output needs one explicit output_lineage")


def test_source_publication_target_proves_only_its_own_identity(claim_data):
    claim = claim_data["dataset"]["shared_claims"][0]
    source = claim_data["sources"][0]
    source["published"] = "2024-11-20"
    claim["targets"][0] = ["sources", {"id": source["id"]}, "published"]
    assert check_shared_claims(claim_data) == []
    source["published"] = "2024-11-21"
    assert_error(claim_data, "repeated date disagrees")
    source["published"] = "2024-11-20"
    claim["source_ids"] = ["index"]
    assert_error(claim_data, "does not cite this claim")


def test_consistency_entrypoint_enforces_shared_registry(dataset):
    dataset["institutes"][0]["funding"][0]["period"] = "2026"
    dataset["dataset"]["shared_claims"] = [
        {
            "id": "annual",
            "kind": "number",
            "subject": "gb-test",
            "metric": "annual funding",
            "unit": "EUR",
            "period": dataset["institutes"][0]["funding"][0]["period"],
            "scope": "synthetic annual allocation",
            "source_ids": ["gb-source"],
            "targets": [
                ["institutes", {"id": "gb-test"}, "funding", {"currency": "GBP"}, field]
                for field in ["amount", "annual_amount"]
            ],
        }
    ]
    dataset["institutes"][0]["funding"][0].update(
        annual_amount=dataset["institutes"][0]["funding"][0]["amount"],
        annual_basis="Synthetic annual allocation",
        annual_basis_code="other",
        eur_per_year_est=0,
    )
    dataset["sources"][0]["excerpts"][0]["supports"].append(
        "/institutes/0/funding/0/annual_amount"
    )
    assert any("claim unit" in e for e in build.check_consistency(dataset))
    dataset["dataset"]["shared_claims"][0]["unit"] = "GBP"
    assert build.check_consistency(dataset) == []


def test_normal_build_exports_coverage_resource(dataset, monkeypatch):
    write_json(build.DATA, dataset)
    monkeypatch.setattr(sys, "argv", ["build.py"])
    build.main()
    package = json.loads((build.DIST / "data/datapackage.json").read_text())
    resource = next(
        r for r in package["resources"] if r["name"] == "shared-claim-coverage"
    )
    coverage = json.loads((build.DIST / "data" / resource["path"]).read_text())
    assert coverage == shared_claim_coverage(dataset)
    assert coverage["output_rows"] == 1


def test_real_registry_resolves_and_obeys_numeric_identity():
    data = json.loads((ROOT / "data/aisi.json").read_text())
    assert data["dataset"]["shared_claims"]
    assert check_shared_claims(data) == []


@pytest.mark.parametrize("mutation", ["missing_annual_period", "wrong_profile_window"])
def test_current_allocation_period_identity_fails_for_missing_or_wrong_window(
    claim_data, mutation
):
    record = claim_data["institutes"][0]["funding"][0]
    record.update(
        kind="multi_year",
        period="2025–2027",
        annual_amount=66000000,
        annual_basis_code="allocation_current_year",
        annual_period={"start": "2026-01-01", "end": "2026-12-31"},
        annual_profile=[
            {"start": "2026-01-01", "end": "2026-12-31", "amount": 66000000}
        ],
    )
    claim = claim_data["dataset"]["shared_claims"][2]
    claim["period"] = "2026-01-01/2026-12-31"
    root = ["institutes", {"id": "gb-aisi"}, "funding", {"currency": "GBP"}]
    claim["targets"] = [
        root + ["annual_amount"],
        root + ["annual_profile", {"amount": 66000000}, "amount"],
    ]
    assert check_shared_claims(claim_data) == []
    if mutation == "missing_annual_period":
        del record["annual_period"]
        assert_error(claim_data, "current annual amount needs an annual_period")
    else:
        record["annual_profile"][0]["end"] = "2027-12-31"
        assert_error(claim_data, "claim period")
