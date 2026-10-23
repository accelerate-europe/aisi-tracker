import csv
import json
import sys

import pytest
from jsonschema import Draft202012Validator

from conftest import write_json
from scripts import build


def test_source_titles_survive_json_and_csv_export(dataset, monkeypatch):
    # Document language does not determine whether its recorded title needs translation.
    source = dataset["sources"][0]
    source.update(
        title="Plan Nacional de Inteligencia Artificial",
        title_en="National Artificial Intelligence Plan",
        language="en",
    )
    dataset["sources"].append(
        {
            **source,
            "id": "english-title",
            "url": "https://example.org/english-title",
            "title": "AI Research Overview",
            "language": "ja",
        }
    )
    dataset["sources"][-1].pop("title_en")
    dataset["sources"][-1].pop("excerpts", None)
    dataset["jurisdictions"][0]["source_ids"].append("english-title")
    write_json(build.DATA, dataset)
    monkeypatch.setattr(sys, "argv", ["build.py"])
    build.main()
    exported = json.loads((build.DIST / "data/aisi.json").read_text())
    assert exported["sources"] == dataset["sources"]
    with (build.DIST / "data/sources.csv").open(newline="", encoding="utf-8") as stream:
        rows = list(csv.DictReader(stream))
    assert rows[0]["title"] == source["title"]
    assert rows[0]["title_en"] == source["title_en"]
    assert rows[1]["title"] == "AI Research Overview"
    assert rows[1]["title_en"] == ""
    package = json.loads((build.DIST / "data/datapackage.json").read_text())
    resource = next(r for r in package["resources"] if r["name"] == "sources")
    field = next(f for f in resource["schema"]["fields"] if f["name"] == "title_en")
    assert field["type"] == "string"
    assert field["description"] == build.FIELD_DOCS["title_en"]


@pytest.mark.parametrize("value", ["", "   ", "\t\n", None, 7])
def test_source_title_translation_rejects_blank_or_nontext(dataset, value):
    dataset["sources"][0]["title_en"] = value
    validator = Draft202012Validator(json.loads(build.SCHEMA.read_text()))
    assert any(
        list(e.path) == ["sources", 0, "title_en"]
        for e in validator.iter_errors(dataset)
    )


def test_source_title_translation_is_optional(dataset):
    validator = Draft202012Validator(json.loads(build.SCHEMA.read_text()))
    assert not list(validator.iter_errors(dataset))
    dataset["sources"][0]["title_en"] = "Translated title"
    assert not list(validator.iter_errors(dataset))
