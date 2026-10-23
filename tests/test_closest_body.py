"""Closest-body contract; stages never turn a negative finding into a status."""

import copy
import csv
import json
import sys

import pytest
from jsonschema import Draft202012Validator

from conftest import write_json
from scripts import build


@pytest.fixture
def closest(dataset):
    dataset["dataset"]["exchange_rates"] = []
    j = dataset["jurisdictions"][0]
    j.update(
        status="none_identified",
        status_confidence="low",
        institute_id=None,
        timeline=[],
        closest_body={
            "name": "Synthetic model test centre",
            "stage": "exists",
            "gap": "remit",
            "note": "Tests models for certification.",
            "source_ids": ["gb-source"],
        },
    )
    j.pop("status_since")
    dataset["institutes"] = []
    dataset["sources"][0]["excerpts"] = [
        {
            "text": "Synthetic centre exists and tests models for certification.",
            "supports": [
                f"/jurisdictions/0/closest_body/{key}"
                for key in ("name", "stage", "note")
            ],
        }
    ]
    return dataset


def schema_errors(data):
    return list(
        Draft202012Validator(json.loads(build.SCHEMA.read_text())).iter_errors(data)
    )


@pytest.mark.parametrize(
    ("stage", "gap"),
    [
        ("exists", "remit"),
        ("planned", "remit"),
        ("proposed", "remit"),
        ("under_study", "remit"),
        ("exists", "designation"),
        ("planned", "designation"),
        ("proposed", "designation"),
        ("under_study", "designation"),
    ],
)
def test_closest_body_valid_codes(closest, stage, gap):
    closest["jurisdictions"][0]["closest_body"].update(stage=stage, gap=gap)
    assert schema_errors(closest) == []
    assert build.check_consistency(closest) == []
    assert build.tables(closest)["jurisdictions"][0]["status"] == "none_identified"


@pytest.mark.parametrize("status", ["established", "planned", "proposed", "exploring"])
def test_closest_body_requires_negative_jurisdiction(closest, status):
    closest["jurisdictions"][0]["status"] = status
    assert any(
        list(error.absolute_path) == ["jurisdictions", 0, "status"]
        and error.validator == "const"
        for error in schema_errors(closest)
    )
    del closest["jurisdictions"][0]["closest_body"]
    assert schema_errors(closest) == []


@pytest.mark.parametrize("field", ["name", "stage", "gap", "note", "source_ids"])
def test_closest_body_fields_required(closest, field):
    del closest["jurisdictions"][0]["closest_body"][field]
    assert schema_errors(closest)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("stage", "established"),
        ("gap", "unknown"),
        ("gap", ["remit", "designation"]),
        ("gap", "both"),
        ("gap", "act"),
        ("name", "  "),
        ("note", ""),
        ("source_ids", []),
        ("extra", "not allowed"),
    ],
)
def test_closest_body_rejects_invalid_values(closest, field, value):
    closest["jurisdictions"][0]["closest_body"][field] = value
    assert schema_errors(closest)


def test_closest_body_is_one_object(closest):
    j = closest["jurisdictions"][0]
    j["closest_body"] = [j["closest_body"]]
    assert schema_errors(closest)


def test_closest_body_unknown_source_fails(closest):
    closest["jurisdictions"][0]["closest_body"]["source_ids"] = ["missing"]
    assert any(
        "closest_body: unknown source" in e for e in build.check_consistency(closest)
    )


def test_closest_body_excerpt_cannot_borrow_parent_citation(closest):
    source = copy.deepcopy(closest["sources"][0])
    source.update(id="body-only", url="https://example.org/body-only", excerpts=[])
    closest["sources"].append(source)
    closest["jurisdictions"][0]["closest_body"]["source_ids"] = ["body-only"]
    assert any(
        "nearest cited record does not cite" in e for e in build.check_excerpts(closest)
    )
    source["excerpts"] = closest["sources"][0].pop("excerpts")
    assert build.check_consistency(closest) == []


@pytest.mark.parametrize("field", ["name", "stage", "note"])
def test_closest_body_needs_exact_excerpt_leaves(closest, field):
    pointers = closest["sources"][0]["excerpts"][0]["supports"]
    pointers.remove(f"/jurisdictions/0/closest_body/{field}")
    pointers.append("/jurisdictions/0/closest_body")
    assert any(
        f"required claim has no excerpt: /jurisdictions/0/closest_body/{field}" == e
        for e in build.check_consistency(closest)
    )


def test_closest_body_dangling_excerpt_fails(closest):
    closest["sources"][0]["excerpts"][0]["supports"].append(
        "/jurisdictions/0/closest_body/missing"
    )
    assert any("does not resolve" in e for e in build.check_excerpts(closest))


def test_closest_body_download(closest, monkeypatch):
    write_json(build.DATA, closest)
    monkeypatch.setattr(sys, "argv", ["build.py"])
    build.main()
    out = build.DIST / "data"
    with (out / "closest_bodies.csv").open(newline="") as stream:
        rows = list(csv.DictReader(stream))
    assert rows == [
        {
            "id": "GB",
            "jurisdiction_id": "GB",
            "name": "Synthetic model test centre",
            "stage": "exists",
            "gap": "remit",
            "note": "Tests models for certification.",
            "source_ids": "gb-source",
            "source_urls": closest["sources"][0]["url"],
        }
    ]
    package = json.loads((out / "datapackage.json").read_text())
    resource = next(r for r in package["resources"] if r["name"] == "closest_bodies")
    fields = {f["name"]: f for f in resource["schema"]["fields"]}
    assert "not jurisdiction status" in fields["stage"]["description"]
    assert "remit takes precedence" in fields["gap"]["description"]
    assert list(rows[0]) == build.EMPTY_COLUMNS["closest_bodies"]
    assert (
        json.loads((out / "aisi.json").read_text())["jurisdictions"][0]["closest_body"]
        == build.export_dataset(closest)["jurisdictions"][0]["closest_body"]
    )
