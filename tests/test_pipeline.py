"""Hand-worked primary facts exercise the shared consumer boundary."""

import copy
import json
from pathlib import Path

import pytest

from scripts.build import event_rows, export_dataset, tables
from scripts import build
from jsonschema import Draft202012Validator
from scripts.migrate_facts import migrate
from scripts.migrate_references import migrate as stable
from scripts.method_checks import required_excerpt_pointers
from scripts.published import publish
from scripts.references import authored_schema
from scripts.shared_claims import shared_claim_coverage


def primary_fixture(dataset):
    reviewed = stable(dataset)
    reviewed["institutes"][0]["outputs"][0]["credit"] = {
        "institute_id": "gb-test",
        "date": "2024-10",
    }
    data = migrate(reviewed)
    output = data["institutes"][0]["outputs"][0]
    output["date"] = "2023-01"
    output["credit"]["date"] = "2024-10"
    data["sources"][0]["excerpts"][0]["supports"].extend(
        [
            "institutes",
            {"id": "gb-test"},
            "outputs",
            {"lineage": "evaluation-report"},
            "credit",
            leaf,
        ]
        for leaf in ("institute_id", "date")
    )
    return data


def test_one_pipeline_drives_json_csv_events_and_resolved_evidence(dataset):
    data = primary_fixture(dataset)
    before = copy.deepcopy(data)
    published = publish(data, strict=True)
    assert export_dataset(data, strict=True) == published
    output = published["institutes"][0]["outputs"][0]
    activity = published["institutes"][0]["operating"][0]
    first = next(
        e
        for e in published["jurisdictions"][0]["timeline"]
        if e["event"] == "first_output"
    )
    assert output["date"] == "2023-01"
    assert output["credited_from"] == activity["date"] == first["date"] == "2024-10"
    assert activity["source_ids"] == first["source_ids"] == ["gb-source"]
    flat = tables(published)
    assert flat["outputs"][0]["credit_institute_id"] == "gb-test"
    assert flat["outputs"][0]["credit_date"] == "2024-10"
    for collection in ("funding", "staff", "people", "agreements"):
        assert (
            flat[collection][0]["id"] == published["institutes"][0][collection][0]["id"]
        )
    assert (
        flat["related_bodies"][0]["id"]
        == published["jurisdictions"][0]["related_bodies"][0]["id"]
    )
    assert flat["known_activity"][0]["date"] == "2024-10"
    assert flat["known_activity"][0]["source_ids"] == "gb-source"
    row = next(e for e in event_rows(published) if e["type"] == "first_output")
    assert row["date"] == "2024-10"
    assert row["source_pointer"] == "/jurisdictions/0/timeline/2"
    assert row["source_ids"] == "gb-source"
    assert data == before


def test_strict_authored_schema_accepts_primary_and_rejects_generated_leaves(dataset):
    data = primary_fixture(dataset)
    schema = authored_schema(json.loads(build.SCHEMA.read_text()), strict=True)
    validator = Draft202012Validator(schema)
    assert list(validator.iter_errors(data)) == []
    published = export_dataset(data, strict=True)
    assert list(validator.iter_errors(published))
    for collection, key in (
        ("jurisdictions", "status"),
        ("institutes", "operating"),
        ("institutes", "announced"),
    ):
        broken = copy.deepcopy(data)
        broken[collection][0][key] = published[collection][0][key]
        assert list(validator.iter_errors(broken))
        with pytest.raises(ValueError, match="derived field must not be authored"):
            export_dataset(broken, strict=True)
    broken = copy.deepcopy(data)
    broken["institutes"][0]["names"][0]["from"] = published["institutes"][0]["names"][0]["from"]
    assert list(validator.iter_errors(broken))
    with pytest.raises(ValueError, match="names/test-old-name/from: derived field must not be authored"):
        export_dataset(broken, strict=True)


def test_excluded_output_still_needs_credit_evidence(dataset):
    data = primary_fixture(dataset)
    data["institutes"][0]["outputs"][0]["qualifies_as"] = "none"
    published = publish(data, strict=True)
    assert published["institutes"][0]["operating"] == []
    first = next(
        e
        for e in published["jurisdictions"][0]["timeline"]
        if e["event"] == "first_output"
    )
    assert first["citations"]["date"] == ["gb-source"]
    required = required_excerpt_pointers(published, authored=data)
    assert "/institutes/0/outputs/0/credit/institute_id" in required
    assert "/institutes/0/outputs/0/credit/date" in required
    assert not any("/timeline/2/" in pointer for pointer in required)


def test_profile_calculation_requires_primary_amounts_not_computed_copy(dataset):
    funding = dataset["institutes"][0]["funding"][0]
    funding.update(
        annual_basis_code="allocation_current_year",
        amount=30,
        annual_amount=11,
        annual_profile=[
            {"amount": 11, "start": "2025-01-01", "end": "2025-12-31"},
            {"amount": 19, "start": "2026-01-01", "end": "2026-12-31"},
        ],
    )
    required = required_excerpt_pointers(dataset)
    prefix = "/institutes/0/funding/0"
    assert f"{prefix}/amount" in required
    assert f"{prefix}/annual_amount" not in required
    assert f"{prefix}/annual_profile/0/amount" in required
    assert f"{prefix}/annual_profile/1/amount" in required


@pytest.mark.parametrize(
    "mutation,message",
    [
        ("missing", "explicit institutional credit is required"),
        ("foreign", "credit institute must match"),
        ("early", "cannot precede lineage publication"),
    ],
)
def test_explicit_credit_mutations_fail(dataset, mutation, message):
    data = primary_fixture(dataset)
    output = data["institutes"][0]["outputs"][0]
    if mutation == "missing":
        output.pop("credit")
    elif mutation == "foreign":
        output["credit"]["institute_id"] = "other-institute"
    else:
        output["credit"]["date"] = "2022"
    with pytest.raises(ValueError, match=message):
        publish(data, strict=True)


def test_generated_selection_indices_are_not_numeric_research_claims(dataset):
    before = shared_claim_coverage(dataset)
    after = shared_claim_coverage(export_dataset(dataset))
    assert after["numeric_leaves"] == before["numeric_leaves"]
    assert after["unchecked_numeric_leaves"] == before["unchecked_numeric_leaves"]


def test_legacy_pointers_are_stabilised_before_generated_event_insertion(dataset):
    data = copy.deepcopy(dataset)
    data["jurisdictions"][0]["timeline"].pop()  # New first output precedes designation.
    data["institutes"][0]["outputs"][0]["date"] = "2022"
    data["sources"][0]["excerpts"][0]["supports"] = ["/jurisdictions/0/timeline/1/date"]
    published = publish(data)
    supports = published["sources"][0]["excerpts"][0]["supports"]
    assert supports == ["/jurisdictions/0/timeline/2/date"]
    assert published["jurisdictions"][0]["timeline"][2]["id"] == "act"


def test_authored_funding_schema_requires_only_primary_annual_amount(dataset):
    data = primary_fixture(dataset)
    funding = data["institutes"][0]["funding"][0]
    funding.update(
        annual_basis="Average of the announced allocation.",
        annual_basis_code="period_mean",
        amount=120,
        years=3,
    )
    validator = Draft202012Validator(
        authored_schema(json.loads(build.SCHEMA.read_text()), strict=True)
    )
    assert list(validator.iter_errors(data)) == []
    funding["annual_amount"] = 40
    assert any("False schema" in error.message for error in validator.iter_errors(data))
    funding["annual_basis_code"] = "other"
    assert list(validator.iter_errors(data)) == []
    funding.pop("annual_amount")
    assert any(
        "annual_amount" in error.message for error in validator.iter_errors(data)
    )


def test_singapore_designation_does_not_supply_an_announcement_date():
    data = json.loads(
        (Path(__file__).resolve().parents[1] / "data/aisi.json").read_text()
    )
    raw = next(i for i in data["institutes"] if i["id"] == "sg-aisi")
    assert "announced" not in raw
    published = {i["id"]: i for i in publish(data)["institutes"]}
    institute = published["sg-aisi"]
    assert institute["established"] == "2024-05-22"
    assert institute["announced"] is None
    assert institute["citations"]["announced"] == []
    # Same situation as Singapore: designation recorded, no commitment or proposal.
    for identity in ("fr-inesia", "my-aisafe"):
        assert published[identity]["announced"] is None
    us = published["us-caissi"]
    assert (us["announced"], us["citations"]["announced"]) == ("2023-11-01", ["us-commerce-2023"])
