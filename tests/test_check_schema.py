"""Later searches are authored check metadata with a closed, public contract."""

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from scripts.references import authored_schema


@pytest.fixture(params=["published", "authored", "strict_authored"])
def check_validator(request):
    schema = json.loads(
        (Path(__file__).parents[1] / "schema/aisi.schema.json").read_text()
    )
    if request.param != "published":
        schema = authored_schema(schema, strict=request.param == "strict_authored")
    return Draft202012Validator(
        {"$ref": "#/$defs/check", "$defs": schema["$defs"]},
        format_checker=Draft202012Validator.FORMAT_CHECKER,
    )


@pytest.fixture
def search_pass():
    return {
        "date": "2026-11-14",
        "languages": ["pl", "en"],
        "places": ["NASK's site", "the official journals"],
        "fields": [
            "status", "head", "reports_to", "national_partners", "staff",
            "funding", "agreements", "model_access", "testing_capacity",
            "outputs", "closest_body", "related_bodies",
        ],
    }


def check_record(**fields):
    return {"last_checked": "2026-11-01", "method": "individual", **fields}


def test_check_passes_accepts_valid_authored_entry(check_validator, search_pass):
    assert not list(check_validator.iter_errors(check_record(passes=[search_pass])))


def test_check_passes_is_optional_and_may_be_empty(check_validator):
    assert not list(check_validator.iter_errors(check_record()))
    assert not list(check_validator.iter_errors(check_record(passes=[])))


@pytest.mark.parametrize("field", ["date", "languages", "places", "fields"])
def test_check_pass_requires_each_field(check_validator, search_pass, field):
    del search_pass[field]
    errors = list(check_validator.iter_errors(check_record(passes=[search_pass])))
    assert any(e.validator == "required" and field in e.message for e in errors)


@pytest.mark.parametrize(
    "field, value, keyword",
    [
        ("fields", ["unknown"], "enum"),
        ("fields", ["budget"], "enum"),
        ("fields", ["ai_act"], "enum"),
        ("fields", [], "minItems"),
        ("pass_id", "2026-11-14-example", "additionalProperties"),
        ("languages", [], "minItems"),
        ("languages", [""], "minLength"),
        ("places", [], "minItems"),
        ("places", [""], "minLength"),
        ("places", ["x" * 61], "maxLength"),
        ("date", "2026-11-31", "format"),
    ],
)
def test_check_pass_rejects_invalid_entry(
    check_validator, search_pass, field, value, keyword
):
    search_pass[field] = value
    errors = list(check_validator.iter_errors(check_record(passes=[search_pass])))
    assert any(
        e.validator == keyword
        and (field in e.absolute_path or field in e.message)
        for e in errors
    )
