"""Establishment follows surviving designation acts, including after closure."""

import copy

import pytest

from scripts.display import derive_display
from scripts.method_checks import required_excerpt_pointers
from scripts.migrate_facts import migrate
from scripts.published import materialise_facts


def document(*, withdrawn=False, replacement=False, closed=False):
    events = [
        {"id": "plan", "event": "commitment", "date": "2023"},
        {"id": "first", "event": "designated", "date": "2024"},
    ]
    if withdrawn:
        events.append(
            {
                "id": "withdrawal",
                "event": "withdrawn",
                "date": "2025",
                "withdraws": "first",
            }
        )
    if replacement:
        events.append({"id": "replacement", "event": "designated", "date": "2026"})
    if closed:
        events.append({"id": "closure", "event": "closed", "date": "2027"})
    for event in events:
        event.update(description="A government act.", source_ids=[event["id"]])
    return {
        "dataset": {"as_of": "2028-01-01", "exchange_rates": []},
        "jurisdictions": [
            {"id": "GB", "institute_id": "institute", "timeline": events}
        ],
        "institutes": [{"id": "institute", "jurisdiction_id": "GB", "outputs": []}],
        "sources": [
            {
                "id": event["id"],
                "excerpts": [{"supports": [f"/jurisdictions/0/timeline/{n}/date"]}],
            }
            for n, event in enumerate(events)
        ],
    }


def test_withdrawn_designation_does_not_generate_establishment():
    data = document(withdrawn=True)
    result = materialise_facts(data, strict=True)
    assert result["institutes"][0]["status"] == "planned"
    assert "established" not in result["institutes"][0]


def test_withdrawn_designation_is_not_required_establishment_evidence():
    data = document(withdrawn=True)
    required = required_excerpt_pointers(
        materialise_facts(data, strict=True), authored=data
    )
    assert "/jurisdictions/0/timeline/0/date" in required
    assert "/jurisdictions/0/timeline/1/date" not in required
    assert "/jurisdictions/0/timeline/1/event" not in required


def test_withdrawn_designation_does_not_supply_establishment_citations():
    result = derive_display(materialise_facts(document(withdrawn=True), strict=True))
    assert result["institutes"][0]["citations"]["established"] == []


@pytest.mark.parametrize("replacement", [False, True])
def test_closure_preserves_first_surviving_historical_designation(replacement):
    data = document(withdrawn=replacement, replacement=replacement, closed=True)
    facts = materialise_facts(data, strict=True)
    assert facts["institutes"][0]["status"] == "none_identified"
    assert facts["institutes"][0]["established"] == ("2026" if replacement else "2024")
    required = required_excerpt_pointers(facts, authored=data)
    index = 3 if replacement else 1
    assert f"/jurisdictions/0/timeline/{index}/date" in required
    assert f"/jurisdictions/0/timeline/{index}/event" in required
    citations = derive_display(facts)["institutes"][0]["citations"]["established"]
    assert citations == (["replacement"] if replacement else ["first"])


def test_migration_rebinds_establishment_only_to_surviving_designation():
    data = document(withdrawn=True, replacement=True)
    data["institutes"][0]["established"] = "2026"
    data["sources"] = [
        {
            "id": "establishment",
            "excerpts": [
                {
                    "supports": [
                        ["institutes", {"id": "institute"}, "established"],
                    ]
                }
            ],
        }
    ]
    manifest = []
    result = migrate(data, manifest=manifest)
    expected = [
        "jurisdictions",
        {"id": "GB"},
        "timeline",
        {"id": "replacement"},
        "date",
    ]
    assert result["sources"][0]["excerpts"][0]["supports"] == [expected]
    assert manifest[0]["new_selectors"] == [expected]
    assert "established" not in result["institutes"][0]


def test_migration_rejects_establishment_binding_when_no_designation_survives():
    data = document(withdrawn=True)
    data["institutes"][0]["established"] = "2024"
    data["sources"] = [
        {
            "id": "establishment",
            "excerpts": [
                {
                    "supports": [
                        ["institutes", {"id": "institute"}, "established"],
                    ]
                }
            ],
        }
    ]
    before = copy.deepcopy(data)
    manifest = []
    with pytest.raises(
        ValueError, match="established: stored '2024' disagrees with derived None"
    ):
        migrate(data, manifest=manifest)
    assert data == before
    assert manifest == []
