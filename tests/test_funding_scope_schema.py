"""The funding contract keeps amount bounds distinct from currency estimates."""

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from scripts.published import materialise_facts
from test_funding_selection import summed_funding  # noqa: F401


def real_data():
    return json.loads((Path(__file__).parents[1] / "data/aisi.json").read_text())


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


@pytest.mark.parametrize("component_id", ["component-a", "component-b"])
@pytest.mark.parametrize("qualifier", ["about", "at_most", "more_than"])
def test_sum_rejects_any_qualified_component(summed_funding, component_id, qualifier):  # noqa: F811
    data = summed_funding
    rows = {row["id"]: row for row in materialise_facts(data)["institutes"][0]["funding"]}
    assert rows["total"]["amount"] == 30
    component = next(row for row in data["institutes"][0]["funding"] if row["id"] == component_id)
    component["amount_qualifier"] = qualifier
    with pytest.raises(
        ValueError, match=f"sum_of cannot combine qualified component {component_id}"
    ):
        materialise_facts(data)
