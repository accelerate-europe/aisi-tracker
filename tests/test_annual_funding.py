"""Annual-field contract, using the retrieved Japanese FY2025 budget record.

Provenance: Cabinet Office, 7 April 2026, printed page 7:
https://www8.cao.go.jp/cstp/ai/aisi/3kai/renrakukaigi_manager/shiryo1_1.pdf
The nominal supplementary amount is JPY 8.8bn; this is not annual expenditure.
"""

import copy
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

SCHEMA = json.loads(
    (Path(__file__).resolve().parents[1] / "schema/aisi.schema.json").read_text()
)
VALIDATOR = Draft202012Validator({"$defs": SCHEMA["$defs"], "$ref": "#/$defs/funding"})
FY_BUDGET = {
    "kind": "one_off",
    "amount": 8_800_000_000,
    "currency": "JPY",
    "period": "FY2025 supplementary",
    "description": "JPY 8.8bn supplementary funding to strengthen AISI.",
    "source_ids": ["jp-cao-2604"],
    "confidence": "high",
    "commitment": "adopted",
    "coverage": "whole_institute",
    "annual_amount": 8_800_000_000,
    "annual_basis": "Nominal FY2025 supplementary amount, not recurring expenditure.",
    "annual_basis_code": "other",
}


@pytest.mark.parametrize("kind", ["annual", "one_off", "not_institute_specific"])
def test_dated_amount_requires_annual_fields_for_every_scope(kind):
    funding = copy.deepcopy(FY_BUDGET)
    funding["kind"] = kind
    funding["coverage"] = (
        "not_institute_specific"
        if kind == "not_institute_specific"
        else "whole_institute"
    )
    assert not list(VALIDATOR.iter_errors(funding))
    for field in ("annual_amount", "annual_basis", "annual_basis_code"):
        broken = {key: value for key, value in funding.items() if key != field}
        assert list(VALIDATOR.iter_errors(broken)), field


def test_unstated_funding_period_does_not_require_invented_annual_amount():
    funding = {
        key: value
        for key, value in FY_BUDGET.items()
        if key not in {"period", "annual_amount", "annual_basis", "annual_basis_code"}
    }
    assert not list(VALIDATOR.iter_errors(funding))


def test_unknown_amount_with_period_does_not_become_zero():
    funding = {
        key: value for key, value in FY_BUDGET.items() if not key.startswith("annual_")
    }
    funding["amount"] = None
    assert not list(VALIDATOR.iter_errors(funding))
