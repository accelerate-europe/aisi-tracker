"""The funding contract keeps amount bounds distinct from currency estimates."""

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from scripts.published import materialise_facts


KOREA_TOTAL_ID = "funding-d0f61d15548153b9830f74f6e14ec324"
KOREA_COMPONENT_IDS = (
    "funding-eb798d93b6345b24b6fb8f1c45ad7e4a",
    "funding-a4935c40a4e750e8844af6de22176e13",
)


def real_data():
    return json.loads((Path(__file__).parents[1] / "data/aisi.json").read_text())


def korea_funding(data):
    institute = next(row for row in data["institutes"] if row["id"] == "kr-aisi")
    return {row["id"]: row for row in institute["funding"]}


def validator():
    schema = json.loads(
        (Path(__file__).parents[1] / "schema/aisi.schema.json").read_text()
    )
    return Draft202012Validator({"$ref": "#/$defs/funding", "$defs": schema["$defs"]})


def record(**fields):
    return {
        "kind": "one_off",
        "description": "Published allocation",
        "source_ids": ["official"],
        "confidence": "high",
        "commitment": "adopted",
        "coverage": "whole_institute",
        **fields,
    }


def test_related_funding_is_representable_but_unknown_scope_is_not():
    assert validator().is_valid(record(coverage="not_institute_specific"))
    assert not validator().is_valid(record(coverage="unknown"))


@pytest.mark.parametrize(
    "qualifier", ["about", "at_most", "at_least", "nearly", "more_than", "less_than"]
)
def test_amount_bounds_require_a_primary_numeric_amount(qualifier):
    assert validator().is_valid(record(amount=10, amount_qualifier=qualifier))
    assert not validator().is_valid(record(amount_qualifier=qualifier))
    assert not validator().is_valid(record(amount=None, amount_qualifier=qualifier))
    total = record(amount=10, sum_of=["component-a", "component-b"])
    assert validator().is_valid(total)  # Only adding the qualifier makes it invalid.
    assert not validator().is_valid({**total, "amount_qualifier": qualifier})


def test_exactness_is_not_inferred_from_an_unqualified_figure():
    assert validator().is_valid(record(amount=10))
    assert not validator().is_valid(record(amount=10, amount_qualifier="exact"))


def test_all_real_funding_records_require_explicit_coverage():
    data = materialise_facts(real_data(), strict=True)
    records = {
        (institute["id"], funding["id"]): funding
        for institute in data["institutes"]
        for funding in institute.get("funding", [])
    }
    assert len(records) == 24
    funding_validator = validator()
    for identity, funding in records.items():
        assert funding_validator.is_valid(funding), identity
        assert funding["coverage"] in {
            "whole_institute",
            "component",
            "not_institute_specific",
        }, identity
        missing = {key: value for key, value in funding.items() if key != "coverage"}
        errors = list(funding_validator.iter_errors(missing))
        assert any(
            error.validator == "required" and "coverage" in error.message
            for error in errors
        ), identity


def test_real_korea_unqualified_components_keep_the_hand_calculated_total():
    data = real_data()
    funding = korea_funding(data)
    assert set(funding[KOREA_TOTAL_ID]["sum_of"]) == set(KOREA_COMPONENT_IDS)
    assert all("amount_qualifier" not in funding[key] for key in KOREA_COMPONENT_IDS)
    total = korea_funding(materialise_facts(data, strict=True))[KOREA_TOTAL_ID]
    assert total["amount"] == total["annual_amount"] == 12_875_000_000


@pytest.mark.parametrize("component_id", KOREA_COMPONENT_IDS)
@pytest.mark.parametrize("qualifier", ["about", "at_most", "more_than"])
def test_real_korea_sum_rejects_any_qualified_component(component_id, qualifier):
    data = real_data()
    korea_funding(data)[component_id]["amount_qualifier"] = qualifier
    with pytest.raises(
        ValueError, match=f"sum_of cannot combine qualified component {component_id}"
    ):
        materialise_facts(data, strict=True)
