"""Boundary and export checks for canonical short event text."""

import csv
import json
import sys

import pytest

from conftest import event, write_json
from scripts import build
from scripts.method_checks import check_method_rules


@pytest.mark.parametrize("origin", ["timeline", "network_milestones"])
@pytest.mark.parametrize(
    "text",
    [
        None,
        "",
        "   ",
        "A line\nAnother line",
        "A line\n",
        "A line\r\nAnother",
        "A\u2028line",
        "A\u2029line",
        " A line",
        "A line ",
        "one two three four five six seven eight nine ten eleven twelve thirteen",
    ],
)
def test_invalid_event_short(dataset, origin, text):
    record = (
        dataset["jurisdictions"][0]["timeline"][0]
        if origin == "timeline"
        else dataset["dataset"]["network_milestones"][0]
    )
    if text is None:
        record.pop("short")
    else:
        record["short"] = text
    assert any("short must be" in e for e in check_method_rules(dataset))


def test_twelve_words_and_exploring_require_short(dataset):
    j = dataset["jurisdictions"][0]
    j["status"] = "exploring"
    j["timeline"] = [
        event(
            "exploration_started",
            short="one two three four five six seven eight nine ten eleven twelve",
        )
    ]
    assert not any("short must be" in e for e in check_method_rules(dataset))
    j["timeline"][0].pop("short")
    assert any("short must be" in e for e in check_method_rules(dataset))
    j["status"] = "none_identified"
    assert not any("short must be" in e for e in check_method_rules(dataset))
    j["timeline"][0]["short"] = "two\nlines"
    assert any("short must be" in e for e in check_method_rules(dataset))


def test_event_short_exports_and_dictionary(dataset, monkeypatch):
    write_json(build.DATA, dataset)
    monkeypatch.setattr(sys, "argv", ["build.py"])
    build.main()
    out = build.DIST / "data"
    package = json.loads((out / "datapackage.json").read_text())
    resources = {r["name"]: r for r in package["resources"]}
    dataset = build.export_dataset(dataset)
    for name in ("timeline", "events", "network_milestones"):
        with (out / f"{name}.csv").open(newline="") as stream:
            reader = csv.DictReader(stream)
            columns = reader.fieldnames
            rows = list(reader)
        assert columns[columns.index("description") + 1] == "short"
        field = next(
            f for f in resources[name]["schema"]["fields"] if f["name"] == "short"
        )
        assert field["type"] == "string" and "12" in field["description"]
        for row in rows:
            if name == "events":
                origin = row["origin_kind"]
                if origin not in ("timeline", "network_milestones"):
                    assert row["short"] == ""
                    continue
                canonical = dataset
                for component in row["source_pointer"].strip("/").split("/"):
                    canonical = (
                        canonical[int(component)]
                        if isinstance(canonical, list)
                        else canonical[component]
                    )
            elif name == "timeline":
                canonical = next(
                    e
                    for e in dataset["jurisdictions"][0]["timeline"]
                    if e["id"] == row["event_id"]
                )
            else:
                canonical = next(
                    e
                    for e in dataset["dataset"]["network_milestones"]
                    if e["id"] == row["id"]
                )
            assert row["short"] == canonical["short"]
            assert row["description"] == canonical["description"]
            assert row["source_ids"] == "; ".join(canonical["source_ids"])
