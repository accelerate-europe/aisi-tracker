"""Credit migration preserves editorial decisions and requires complete evidence."""

import copy

import pytest
from jsonschema import ValidationError

from scripts.migrate_credit import migrate


@pytest.fixture
def credit_case():
    output = {
        "lineage": "guidance",
        "date": "2025-01-01",
        "attribution": "own_publication",
        "qualifies_as": "none",
        "qualification_note": "Collective guidance does not qualify individually.",
        "source_ids": ["original"],
    }
    source = {
        "id": "original",
        "url": "https://example.org/guidance",
        "title": "Guidance",
        "publisher": "Institute",
        "type": "primary",
        "accessed": "2026-10-18",
        "origin": "GB",
        "excerpts": [
            {"text": "Original title", "supports": ["/institutes/0/outputs/0/lineage"]}
        ],
    }
    new_source = {k: v for k, v in source.items() if k != "excerpts"}
    new_source.update(id="release", url="https://example.org/release")
    assignment = {
        "selector": {"institute_id": "gb-test", "output_lineage": "guidance"},
        "credit": {"institute_id": "gb-test", "date": "2025-02-01"},
        "attribution": "own_publication",
        "qualifies_as": "none",
        "evidence": [],
    }
    for path, quote in [
        ("credit/institute_id", "We published this guidance."),
        ("credit/date", "1 February 2025"),
    ]:
        assignment["evidence"].append(
            {
                "source_id": "release",
                "text": quote,
                "accessed": "2026-10-18",
                "stable_supports": [{**assignment["selector"], "path": path}],
                "verification": {"status": "matched"},
            }
        )
    return (
        {"institutes": [{"id": "gb-test", "outputs": [output]}], "sources": [source]},
        {"new_sources": [new_source], "assignments": [assignment]},
    )


def test_migration_preserves_decisions_adds_membership_and_is_idempotent(credit_case):
    document, evidence = credit_case
    original = copy.deepcopy(document)
    result = migrate(document, evidence)
    output = result["institutes"][0]["outputs"][0]
    assert output["credit"] == {"institute_id": "gb-test", "date": "2025-02-01"}
    assert output["date"] == "2025-01-01"
    assert output["attribution"] == "own_publication"
    assert output["qualifies_as"] == "none"
    assert (
        output["qualification_note"]
        == original["institutes"][0]["outputs"][0]["qualification_note"]
    )
    assert output["source_ids"] == ["original", "release"]
    assert "credit_source_ids" not in output
    assert result["sources"][1]["excerpts"][0]["supports"] == [
        "/institutes/0/outputs/0/credit/institute_id"
    ]
    assert result["sources"][0] == original["sources"][0]
    assert migrate(result, evidence) == result
    assert document == original


def test_authored_selectors_keep_stable_support_format(credit_case):
    document, evidence = credit_case
    document["sources"][0]["excerpts"][0]["supports"] = [
        ["institutes", {"id": "gb-test"}, "outputs", {"lineage": "guidance"}, "lineage"]
    ]
    result = migrate(document, evidence)
    assert result["sources"][1]["excerpts"][1]["supports"] == [
        [
            "institutes",
            {"id": "gb-test"},
            "outputs",
            {"lineage": "guidance"},
            "credit",
            "date",
        ]
    ]


def test_reviewed_context_update_does_not_overwrite_an_unexpected_role(credit_case):
    document, evidence = credit_case
    source = document["sources"][0]
    source.update(output_role="document", output_lineage="guidance")
    evidence["source_context_updates"] = [
        {
            "source_id": "original",
            "expected_role": "document",
            "expected_lineage": "guidance",
        }
    ]
    result = migrate(document, evidence)
    assert result["sources"][0]["output_role"] == "context"
    assert "output_lineage" not in result["sources"][0]
    assert migrate(result, evidence) == result
    source["output_lineage"] = "another-document"
    with pytest.raises(ValueError, match="source role changed since review"):
        migrate(document, evidence)


@pytest.mark.parametrize("field", ["attribution", "qualifies_as"])
def test_rejects_editorial_changes(credit_case, field):
    document, evidence = credit_case
    evidence["assignments"][0][field] = "changed"
    with pytest.raises(ValueError, match="editorial decision changed"):
        migrate(document, evidence)


@pytest.mark.parametrize("change", ["missing", "duplicate", "wrong_selector"])
def test_rejects_incomplete_or_ambiguous_coverage(credit_case, change):
    document, evidence = credit_case
    if change == "missing":
        evidence["assignments"] = []
    elif change == "duplicate":
        evidence["assignments"] *= 2
    else:
        evidence["assignments"][0]["selector"]["institute_id"] = "unknown"
    with pytest.raises(ValueError, match="every output exactly once"):
        migrate(document, evidence)


def test_rejects_unverified_quote_and_missing_leaf(credit_case):
    document, evidence = credit_case
    items = evidence["assignments"][0]["evidence"]
    items[0]["verification"]["status"] = "mismatch"
    with pytest.raises(ValueError, match="has not passed offline verification"):
        migrate(document, evidence)
    items.pop(0)
    with pytest.raises(ValueError, match="both credit leaves need evidence"):
        migrate(document, evidence)


def test_rejects_source_metadata_collision_and_unknown_fields(credit_case):
    document, evidence = credit_case
    evidence["new_sources"][0]["id"] = "original"
    with pytest.raises(ValueError, match="source metadata collision"):
        migrate(document, evidence)
    evidence["new_sources"][0]["local_path"] = "/private/source"
    with pytest.raises(ValidationError, match="Additional properties"):
        migrate(document, evidence)


def test_rejects_evidence_for_a_neighbour_and_existing_credit_conflict(credit_case):
    document, evidence = credit_case
    target = evidence["assignments"][0]["evidence"][0]["stable_supports"][0]
    target["output_lineage"] = "other"
    with pytest.raises(ValueError, match="target does not match"):
        migrate(document, evidence)
    target["output_lineage"] = "guidance"
    document["institutes"][0]["outputs"][0]["credit"] = {
        "institute_id": "gb-test",
        "date": "2025-03-01",
    }
    with pytest.raises(ValueError, match="credit collision"):
        migrate(document, evidence)
