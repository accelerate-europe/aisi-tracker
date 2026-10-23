"""Access-pledge contract using the UK Prime Minister's 12 June 2023 speech.

Source: https://www.gov.uk/government/speeches/pm-london-tech-week-speech-12-june-2023
The speech announces company commitments, not a signed instrument.
"""

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

SCHEMA = json.loads(
    (Path(__file__).resolve().parents[1] / "schema/aisi.schema.json").read_text()
)
AGREEMENT = SCHEMA["properties"]["institutes"]["items"]["properties"]["agreements"][
    "items"
]
VALIDATOR = Draft202012Validator({"$defs": SCHEMA["$defs"], **AGREEMENT})


@pytest.mark.parametrize("state", ["pledged", "announced", "signed"])
def test_agreement_states_preserve_distinct_recorded_acts(state):
    record = {
        "counterparty": "OpenAI",
        "counterparty_type": "developer",
        "state": state,
        "date": "2023-06-12",
        "source_ids": ["gb-ltw-2023"],
        "participation": "recipient" if state == "pledged" else "institute_party",
        "domestic_interagency": False,
    }
    assert not list(VALIDATOR.iter_errors(record))
    record["state"] = "currently_in_force"
    assert list(VALIDATOR.iter_errors(record))


@pytest.mark.parametrize(
    ("state", "participation", "valid"),
    [
        ("pledged", "recipient", True),
        ("pledged", "institute_party", False),
        ("pledged", "named_implementer", False),
        ("signed", "recipient", False),
        ("announced", "recipient", False),
        ("signed", "institute_party", True),
        ("announced", "named_implementer", True),
    ],
)
def test_pledge_recipients_are_distinct_from_agreement_parties(
    state, participation, valid
):
    record = {
        "counterparty": "OpenAI",
        "counterparty_type": "developer",
        "state": state,
        "source_ids": ["gb-ltw-2023"],
        "participation": participation,
        "domestic_interagency": False,
    }
    assert (not list(VALIDATOR.iter_errors(record))) is valid


def test_pledge_requires_explicit_recipient():
    record = {
        "counterparty": "OpenAI",
        "counterparty_type": "developer",
        "state": "pledged",
        "source_ids": ["gb-ltw-2023"],
    }
    assert any(error.validator == "required" for error in VALIDATOR.iter_errors(record))


def test_agreement_download_preserves_participation_and_defines_it(dataset):
    from scripts import build

    agreement = dataset["institutes"][0]["agreements"][0]
    agreement["state"] = "pledged"
    agreement["participation"] = "recipient"
    row = build.tables(dataset)["agreements"][0]
    assert row["state"] == "pledged"
    assert row["participation"] == "recipient"
    assert list(row) == build.EMPTY_COLUMNS["agreements"]
    description = build.table_descriptions(SCHEMA, "agreements")["participation"]
    assert "named beneficiary" in description
    assert "Only pledged records use recipient" in description
