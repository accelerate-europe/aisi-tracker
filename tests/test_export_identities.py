"""Permanent export identities survive neighbouring insertions and reordering."""

import copy
import hashlib
import json
from pathlib import Path

import pytest

from scripts import build, check
from scripts.references import excerpt_identity
from scripts.source_origins import source_origin_report
from scripts.rule_tables import Report


def test_every_csv_exposes_nonempty_identity_and_no_pointer_ids(dataset):
    exported = build.tables(build.export_dataset(dataset))
    assert set(exported) == set(build.FILES) - {
        "aisi",
        "aisi-schema",
        "shared-claim-coverage",
    }
    for rows in exported.values():
        for row in rows:
            assert row["id"]
            for key, value in row.items():
                if key == "id" or key.endswith("_id"):
                    assert not isinstance(value, str) or not value.startswith("/")
    assert exported["outputs"][0]["id"] == "evaluation-report"
    assert exported["exchange_rates"][0]["id"] == "GBP"
    assert exported["known_activity"][0]["id"] == "published_work"
    assert exported["timeline"][0]["id"] == "plan"


@pytest.mark.parametrize("identity", [None, "", " ", "/institutes/0/funding/0"])
def test_export_identity_guard_rejects_missing_and_positional_ids(identity):
    row = {} if identity is None else {"id": identity}
    with pytest.raises(
        ValueError, match="nonempty stable id|must not contain a JSON pointer"
    ):
        build.check_table_identities({"funding": [row]})


@pytest.mark.parametrize("column", ["institute_id", "origin_id", "source_id"])
def test_export_identity_guard_rejects_pointers_in_any_id_column(column):
    with pytest.raises(ValueError, match=f"{column} must not contain a JSON pointer"):
        build.check_table_identities(
            {"events": [{"id": "stable", column: "prefix:/institutes/0"}]}
        )


def test_export_ids_and_event_meaning_survive_reordering(dataset):
    before = build.tables(build.export_dataset(dataset))
    changed = copy.deepcopy(dataset)
    for institute in changed["institutes"]:
        for key in ("funding", "staff", "people", "names", "outputs", "agreements"):
            institute.get(key, []).reverse()
    changed["dataset"]["exchange_rates"].reverse()
    after = build.tables(build.export_dataset(changed))
    for table, rows in before.items():
        assert sorted(row["id"] for row in rows) == sorted(
            row["id"] for row in after[table]
        )

    def meaning(rows):
        return {
            row["id"]: (row["type"], row["description"], row["origin_id"])
            for row in rows
        }

    assert meaning(before["events"]) == meaning(after["events"])


def test_event_origin_has_no_pointer_fallback(dataset):
    dataset["institutes"][0]["people"][0].pop("id")
    with pytest.raises(ValueError, match="stable origin_id"):
        build.event_rows(dataset)


@pytest.mark.parametrize(
    "origin", ["timeline", "memberships", "people", "agreements", "funding"]
)
def test_event_local_ids_are_scoped_and_sort_without_snapshot_positions(origin):
    data = {
        "dataset": {"as_of": "2026-10-19"},
        "sources": [{"id": "source", "url": "https://example.org/source"}],
        "jurisdictions": [],
        "institutes": [],
    }
    for parent in ("b", "a"):
        jurisdiction = {"id": f"{parent}-state", "name": parent}
        institute = {
            "id": f"{parent}-unit",
            "name": parent,
            "jurisdiction_id": jurisdiction["id"],
        }
        record = {"id": "same-local-id", "source_ids": ["source"]}
        if origin == "timeline":
            record.update(
                date="2025", event="designated", description="Government act."
            )
            jurisdiction[origin] = [record]
        elif origin == "memberships":
            record.update(body="same-body", since="2025")
            jurisdiction[origin] = [record]
        else:
            record.update(
                {
                    "people": {"since": "2025", "name": "Lead", "role": "Director"},
                    "agreements": {
                        "date": "2025",
                        "state": "signed",
                        "counterparty": "Partner",
                    },
                    "funding": {
                        "decision_date": "2025",
                        "description": "Budget decision.",
                    },
                }[origin]
            )
            institute[origin] = [record]
        data["jurisdictions"].append(jurisdiction)
        data["institutes"].append(institute)

    before = build.event_rows(data)
    scope = "state" if origin in {"timeline", "memberships"} else "unit"
    local = "same-body" if origin == "memberships" else "same-local-id"
    assert [row["origin_id"] for row in before] == [
        f"a-{scope}:{local}",
        f"b-{scope}:{local}",
    ]
    assert [row["id"] for row in before] == [
        f"{origin}:a-{scope}:{local}",
        f"{origin}:b-{scope}:{local}",
    ]

    data["jurisdictions"].reverse()
    data["institutes"].reverse()
    after = build.event_rows(data)
    assert [row["source_pointer"] for row in before] != [
        row["source_pointer"] for row in after
    ]
    assert [
        {key: value for key, value in row.items() if key != "source_pointer"}
        for row in before
    ] == [
        {key: value for key, value in row.items() if key != "source_pointer"}
        for row in after
    ]


def test_funding_csv_retains_component_ids(dataset):
    funding = dataset["institutes"][0]["funding"]
    funding[0]["sum_of"] = ["component-b", "component-a"]
    row = build.tables(dataset)["funding"][0]
    assert row["sum_of"] == "component-b; component-a"


@pytest.mark.parametrize("qualifier", ["more_than", "at_most", "about"])
def test_funding_csv_keeps_primary_amount_qualifier(dataset, qualifier):
    funding = dataset["institutes"][0]["funding"][0]
    funding.update(amount=10, amount_qualifier=qualifier)
    row = build.tables(dataset)["funding"][0]
    assert (row["amount"], row["amount_qualifier"]) == (10, qualifier)
    descriptions = build.table_descriptions(
        json.loads(build.SCHEMA.read_text()), "funding"
    )
    dictionary = build.table_schema([row], descriptions)
    column = next(
        field for field in dictionary["fields"] if field["name"] == "amount_qualifier"
    )
    assert column["type"] == "string"
    assert "source bound" in column["description"]


def test_rule_report_labels_use_stable_ids_and_lineage(dataset):
    dataset["institutes"][0]["agreements"][0]["scope"] = "Synthetic scope."
    report = Report(dataset, Path("report.md"), [], None)
    text = report.details()
    for label in (
        "gb-test/funding/test-budget",
        "gb-test/agreements/test-agreement",
        "gb-test/outputs/evaluation-report",
    ):
        assert label in text
    assert "gb-test/funding/0" not in text


def test_quote_identity_ignores_bindings_and_copy_url():
    quote = {
        "text": "A quotation.",
        "supports": ["/old"],
        "url": "https://example.org/old",
    }
    expected = "sha256:" + hashlib.sha256(b"A quotation.").hexdigest()
    assert excerpt_identity(quote) == expected
    quote.update(supports=["/new"], url="https://example.org/new")
    assert excerpt_identity(quote) == expected
    quote["text"] += " Corrected."
    assert excerpt_identity(quote) != expected
    quote["id"] = "permanent-quote"
    assert excerpt_identity(quote) == "permanent-quote"


@pytest.mark.parametrize(
    ("collection", "key"),
    [
        ("outputs", "lineage"),
        ("exchange_rates", "currency"),
        ("memberships", "body"),
        ("operating", "kind"),
        ("annual_profile", "id"),
    ],
)
def test_base_comparison_uses_nested_identity(collection, key):
    old = {collection: [{key: "a", "value": 1}, {key: "b", "value": 2}]}
    new = {
        collection: [
            {key: "b", "value": 3},
            {key: "new", "value": 0},
            {key: "a", "value": 1},
        ]
    }
    assert check.diff(old, new) == [
        (f"/{collection}/0/value", 2, 3),
        (f"/{collection}/1", None, {key: "new", "value": 0}),
    ]


def test_source_origin_report_separates_stable_label_from_snapshot_pointer(dataset):
    dataset["sources"][0]["origin"] = "EU"
    before = source_origin_report(dataset)
    changed = copy.deepcopy(dataset)
    changed["institutes"].insert(0, {"id": "neighbour", "jurisdiction_id": "GB"})
    after = source_origin_report(changed)
    old_row = next(
        row
        for row in before["claim_gaps"]
        if row["pointer"] == "/institutes/0/funding/0"
    )
    new_row = next(
        row
        for row in after["claim_gaps"]
        if row["pointer"] == "/institutes/1/funding/0"
    )
    assert (
        old_row["record_id"]
        == new_row["record_id"]
        == "institutes[gb-test]/funding[test-budget]"
    )
