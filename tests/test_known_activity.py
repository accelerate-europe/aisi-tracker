"""Known activity uses reviewed decisions, not broad catalogue type guesses."""

import copy
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from scripts import build


@pytest.mark.parametrize("kind", ["published_work", "joint_testing", "none"])
def test_explicit_qualification(kind):
    # The same broad type can include an evaluation or a programme report.
    output = {
        "type": "report",
        "attribution": "institutional_author",
        "qualifies_as": kind,
    }
    assert build.output_activity_kind(output) == (None if kind == "none" else kind)


def test_all_catalogue_outputs_have_reviewed_decisions():
    data = json.loads(
        (Path(__file__).resolve().parents[1] / "data/aisi.json").read_text()
    )
    outputs = [o for i in data["institutes"] for o in i.get("outputs", [])]
    assert len(outputs) == 116
    assert all(o["qualification_note"].strip() for o in outputs)
    assert all(
        o["qualifies_as"] in {"published_work", "joint_testing", "none"}
        for o in outputs
    )
    by_key = {
        (i["id"], o["lineage"]): o
        for i in data["institutes"]
        for o in i.get("outputs", [])
    }
    assert (
        by_key["us-caissi", "vision-mission-strategic-goals-2024"]["qualifies_as"]
        == "none"
    )
    assert by_key["jp-aisi", "crosswalk-1"]["qualifies_as"] == "none"
    assert by_key["sg-aisi", "network-pilot-2024"]["qualifies_as"] == "joint_testing"


@pytest.mark.parametrize("field", ["qualifies_as", "qualification_note"])
def test_output_qualification_is_required(dataset, field):
    del dataset["institutes"][0]["outputs"][0][field]
    validator = Draft202012Validator(json.loads(build.SCHEMA.read_text()))
    assert any(field in error.message for error in validator.iter_errors(dataset))


def test_derivation_uses_credit_date_and_kind_order():
    institute = {
        "outputs": [
            {
                "lineage": "later-credit",
                "date": "2023",
                "credited_from": "2026",
                "qualifies_as": "published_work",
                "source_ids": ["late"],
            },
            {
                "lineage": "report",
                "date": "2025",
                "qualifies_as": "published_work",
                "source_ids": ["report"],
            },
            {
                "lineage": "exercise",
                "date": "2024-06",
                "qualifies_as": "joint_testing",
                "source_ids": ["joint"],
            },
            {
                "lineage": "policy",
                "date": "2020",
                "qualifies_as": "none",
                "source_ids": ["policy"],
            },
        ]
    }
    assert build.known_activity(institute) == [
        {
            "kind": "joint_testing",
            "date": "2024-06",
            "lineage": "exercise",
            "source_ids": ["joint"],
        },
        {
            "kind": "published_work",
            "date": "2025",
            "lineage": "report",
            "source_ids": ["report"],
        },
    ]


def test_export_derives_without_mutating_authored_data(dataset):
    before = copy.deepcopy(dataset)
    exported = build.export_dataset(dataset)
    activity = exported["institutes"][0]["operating"][0]
    assert (activity["kind"], activity["date"], activity["lineage"]) == (
        "published_work", "2024-10", "evaluation-report"
    )
    assert activity["source_ids"] == activity["citations"]["date"] == []
    assert dataset == before
    rows = build.tables(exported)
    assert rows["known_activity"][0] == {
        "id": "published_work", "institute_id": "gb-test", "kind": "published_work", "date": "2024-10",
        "lineage": "evaluation-report", "source_ids": "", "source_urls": "",
    }
    assert "operating" not in rows["institutes"][0]
    assert rows["outputs"][0]["qualifies_as"] == "published_work"


def record(kind, day, lineage, source=None, **extra):
    return {
        "qualifies_as": kind,
        "date": day,
        "lineage": lineage,
        "source_ids": [source or lineage],
        **extra,
    }


@pytest.mark.parametrize(
    ("outputs", "expected"),
    [
        (
            [
                record("published_work", "2025-03", "month"),
                record("published_work", "2025", "year"),
            ],
            [
                {
                    "kind": "published_work",
                    "date": "2025",
                    "lineage": "year",
                    "source_ids": ["year"],
                }
            ],
        ),
        (
            [
                record("published_work", "2025", "z"),
                record("published_work", "2025", "a"),
            ],
            [
                {
                    "kind": "published_work",
                    "date": "2025",
                    "lineage": "a",
                    "source_ids": ["a"],
                }
            ],
        ),
        (
            [
                record("published_work", "2025", "report"),
                record("joint_testing", "2025", "exercise"),
            ],
            [
                {
                    "kind": "joint_testing",
                    "date": "2025",
                    "lineage": "exercise",
                    "source_ids": ["exercise"],
                },
                {
                    "kind": "published_work",
                    "date": "2025",
                    "lineage": "report",
                    "source_ids": ["report"],
                },
            ],
        ),
        (
            [
                record("none", "2020", "guidance"),
                record("published_work", "2025-03", "report"),
            ],
            [
                {
                    "kind": "published_work",
                    "date": "2025-03",
                    "lineage": "report",
                    "source_ids": ["report"],
                }
            ],
        ),
        ([record("none", "2020", "policy")], []),
        ([], []),
    ],
)
def test_hand_worked_selection_edges_in_both_independent_implementations(
    outputs, expected
):
    from scripts.check_page import expected_activity

    for ordered in (outputs, list(reversed(outputs))):
        assert build.known_activity({"outputs": ordered}) == expected
        credited = [
            {**output, "credit": {"date": output.get("credited_from", output["date"])}}
            for output in ordered
        ]
        oracle = {(None, row["lineage"]): row["source_ids"] for row in expected}
        assert expected_activity({"outputs": credited}, oracle) == expected


@pytest.mark.parametrize(
    ("field", "bad"),
    [
        ("date", "2020"),
        ("kind", "joint_testing"),
        ("lineage", "wrong-item"),
        ("source_ids", ["wrong-source"]),
    ],
)
def test_build_command_rejects_stale_generated_claim(
    dataset, monkeypatch, capsys, field, bad
):
    import sys
    from conftest import write_json

    exported = build.export_dataset(dataset)
    exported["institutes"][0]["operating"][0][field] = bad
    write_json(build.DATA, exported)
    monkeypatch.setattr(sys, "argv", ["build.py", "--check", "--require-roster"])
    with pytest.raises(SystemExit) as exit_info:
        build.main()
    assert exit_info.value.code == 1
    assert "False schema does not allow" in capsys.readouterr().err


def test_canonical_data_has_no_stored_activity_and_real_dates_match():
    data = json.loads(
        (Path(__file__).resolve().parents[1] / "data/aisi.json").read_text()
    )
    assert "operating_rule" not in data["dataset"]
    assert all("operating" not in i for i in data["institutes"])
    expected = {
        "gb-aisi": [("published_work", "2024-02-09"), ("joint_testing", "2024-11-19")],
        "us-caissi": [
            ("joint_testing", "2024-11-19"),
            ("published_work", "2025-09-30"),
        ],
        "sg-aisi": [("joint_testing", "2024-11-20")],
        "jp-aisi": [("published_work", "2025-03-31"), ("joint_testing", "2025-06-26")],
        "ke-ai-safety-institute": [("joint_testing", "2025-06-26")],
    }
    assert expected.keys() <= {i["id"] for i in data["institutes"]}
    for institute in data["institutes"]:
        if institute["id"] in expected:
            assert [
                (e["kind"], e["date"]) for e in build.known_activity(institute)
            ] == expected[institute["id"]]


def test_empty_activity_export_keeps_its_dictionary(dataset, monkeypatch):
    import csv
    import sys
    from conftest import write_json

    dataset["institutes"][0]["outputs"][0]["qualifies_as"] = "none"
    write_json(build.DATA, dataset)
    monkeypatch.setattr(sys, "argv", ["build.py"])
    build.main()
    out = build.DIST / "data"
    with (out / "known_activity.csv").open() as stream:
        csv_file = csv.DictReader(stream)
        assert csv_file.fieldnames == [
            "id",
            "institute_id",
            "kind",
            "date",
            "lineage",
            "source_ids",
            "source_urls",
        ]
        assert list(csv_file) == []
    package = json.loads((out / "datapackage.json").read_text())
    fields = next(r for r in package["resources"] if r["name"] == "known_activity")[
        "schema"
    ]["fields"]
    assert all(f.get("description") for f in fields)
    assert all(
        f.get("description")
        for resource in package["resources"]
        for f in resource.get("schema", {}).get("fields", [])
        if f["name"] == "institute_id"
    )
    assert (
        json.loads((out / "aisi.json").read_text())["institutes"][0]["operating"] == []
    )


def test_missing_output_catalogue_is_empty():
    from scripts.check_page import expected_activity

    assert build.known_activity({}) == []
    assert expected_activity({}) == []
