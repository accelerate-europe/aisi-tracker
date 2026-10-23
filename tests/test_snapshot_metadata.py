"""Public snapshot metadata contains verification fields, never private paths."""

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker

from scripts import build


def validator():
    schema = json.loads(
        (Path(__file__).resolve().parents[1] / "schema/aisi.schema.json").read_text()
    )
    return Draft202012Validator(
        schema["properties"]["sources"]["items"]["properties"]["snapshot"],
        format_checker=FormatChecker(),
    )


def test_snapshot_metadata_contract():
    assert not list(
        validator().iter_errors(
            {"retrieved": "2026-10-17T14:00:00Z", "sha256": "a" * 64}
        )
    )
    assert not list(
        validator().iter_errors(
            {
                "retrieved": "2026-10-17T14:00:00Z",
                "sha256": "a" * 64,
                "text_sha256": "b" * 64,
                "text_scheme": 1,
            }
        )
    )


@pytest.mark.parametrize(
    "value",
    [
        {"retrieved": "2026-10-17", "sha256": "a" * 64},
        {"retrieved": "2026-10-17T14:00:00Z", "sha256": "not-a-hash"},
        {"retrieved": "2026-10-17T14:00:00Z"},
        {"retrieved": "2026-10-17T14:00:00Z", "sha256": "a" * 64, "text_sha256": "b" * 64},
        {"retrieved": "2026-10-17T14:00:00Z", "sha256": "a" * 64, "text_scheme": 1},
        {
            "retrieved": "2026-10-17T14:00:00Z",
            "sha256": "a" * 64,
            "text_sha256": "B" * 64,
            "text_scheme": 1,
        },
        {
            "retrieved": "2026-10-17T14:00:00Z",
            "sha256": "a" * 64,
            "text_sha256": "b" * 64,
            "text_scheme": 0,
        },
        {
            "retrieved": "2026-10-17T14:00:00Z",
            "sha256": "a" * 64,
            "path": "/private/source.pdf",
        },
    ],
)
def test_invalid_or_private_snapshot_metadata_rejected(value):
    assert list(validator().iter_errors(value))


def test_source_csv_exports_only_public_snapshot_metadata(dataset):
    dataset["sources"][0]["snapshot"] = {
        "retrieved": "2026-10-17T14:00:00Z",
        "sha256": "a" * 64,
    }
    row = build.tables(dataset)["sources"][0]
    assert row["snapshot_retrieved"] == "2026-10-17T14:00:00Z"
    assert row["snapshot_sha256"] == "a" * 64
    assert row["snapshot_text_sha256"] == row["snapshot_text_scheme"] == ""
    dataset["sources"][0]["snapshot"].update(text_sha256="b" * 64, text_scheme=1)
    row = build.tables(dataset)["sources"][0]
    assert (row["snapshot_text_sha256"], row["snapshot_text_scheme"]) == ("b" * 64, 1)
    assert not any("path" in key for key in row)
    del dataset["sources"][0]["snapshot"]
    row = build.tables(dataset)["sources"][0]
    assert row["snapshot_retrieved"] == row["snapshot_sha256"] == ""
