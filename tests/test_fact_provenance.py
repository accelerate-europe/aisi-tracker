"""Independent checks of producer provenance and authored evidence context."""

import copy

import pytest

from scripts import build, published
from scripts.display import derive_display
from scripts.method_checks import required_excerpt_pointers
from scripts.migrate_facts import migrate
from scripts.migrate_references import migrate as stable


def credited_primary(dataset):
    data = copy.deepcopy(dataset)
    output = data["institutes"][0]["outputs"][0]
    output["credit"] = {"institute_id": "gb-test", "date": output["date"]}
    for suffix, field in (("identity", "institute_id"), ("date", "date")):
        source = copy.deepcopy(data["sources"][0])
        source["id"] = f"credit-{suffix}"
        source["url"] = f"https://example.com/credit-{suffix}"
        source["excerpts"] = [
            {
                "text": "A separately reviewed institutional credit.",
                "supports": [f"/institutes/0/outputs/0/credit/{field}"],
            }
        ]
        data["sources"].append(source)
        output["source_ids"].append(source["id"])
    return migrate(stable(data))


def strip_display(value):
    if isinstance(value, dict):
        return {
            k: strip_display(v)
            for k, v in value.items()
            if k not in {"citations", "display"}
        }
    if isinstance(value, list):
        return [strip_display(v) for v in value]
    return value


def test_prepared_and_shipped_credit_provenance_match_before_validation(dataset):
    data = credited_primary(dataset)
    prepared = published.prepared_facts(data, strict=True)
    result = published.publish(data, strict=True)
    institute = prepared["institutes"][0]
    expected = ["credit-identity", "credit-date"]
    assert institute["outputs"][0]["source_ids"] == ["gb-source", *expected]
    assert institute["operating"][0]["source_ids"] == expected
    first = next(
        e
        for e in prepared["jurisdictions"][0]["timeline"]
        if e["event"] == "first_output"
    )
    assert first["source_ids"] == expected
    assert strip_display(result) == prepared
    assert (
        derive_display(prepared)["institutes"][0]["operating"][0]["citations"]["date"]
        == expected
    )


def test_one_output_credit_never_switches_other_authored_status_requirements(dataset):
    before = required_excerpt_pointers(dataset)
    dataset["institutes"][0]["outputs"][0]["credit"] = {
        "institute_id": "gb-test",
        "date": "2024-10",
    }
    after = required_excerpt_pointers(dataset)
    for pointer in (
        "/jurisdictions/0/status",
        "/jurisdictions/0/status_since",
        "/institutes/0/status",
    ):
        assert pointer in before & after
    for field in ("date", "description"):
        assert f"/jurisdictions/0/timeline/2/{field}" in after


def test_evidence_mode_uses_each_authored_record_not_published_status(dataset):
    authored = credited_primary(dataset)
    prepared = published.prepared_facts(authored, strict=True)
    required = required_excerpt_pointers(prepared, authored=authored)
    assert "/jurisdictions/0/status" not in required
    assert "/jurisdictions/0/status_since" not in required
    assert "/institutes/0/status" not in required
    assert "/jurisdictions/0/timeline/1/date" in required
    assert "/jurisdictions/0/timeline/1/event" in required
    assert "/jurisdictions/0/timeline/2/description" not in required
    # One record can retain authored status while its institute uses primary facts.
    authored["jurisdictions"][0]["status"] = "established"
    authored["jurisdictions"][0]["status_since"] = "2024-02-01"
    mixed = required_excerpt_pointers(prepared, authored=authored)
    assert "/jurisdictions/0/status" in mixed
    assert "/institutes/0/status" not in mixed
    assert "/jurisdictions/0/timeline/1/event" in mixed


@pytest.mark.parametrize(
    "consumer", [published.prepared_facts, published.publish, build.export_dataset]
)
def test_consumers_share_dynamic_strict_default(dataset, monkeypatch, consumer):
    monkeypatch.setattr(published, "AUTHORING_STRICT", True)
    with pytest.raises(ValueError):
        consumer(dataset)
    assert consumer(dataset, strict=False)["institutes"]


def test_non_numeric_sum_component_has_actionable_error(dataset):
    data = credited_primary(dataset)
    rows = data["institutes"][0]["funding"]
    rows[:] = [
        {"id": "component", "amount": None, "annual_amount": 2, "currency": "USD"},
        {"id": "second", "amount": 3, "annual_amount": 1, "currency": "USD"},
        {"id": "total", "sum_of": ["component", "second"], "currency": "USD"},
    ]
    with pytest.raises(ValueError, match="sum components require numeric amount"):
        published.materialise_facts(data, strict=True)


@pytest.mark.parametrize("case", ["derived_copy", "legacy_pointer"])
def test_direct_method_validation_uses_the_shared_strict_default(
    dataset, monkeypatch, case
):
    from scripts.method_checks import check_method_rules
    from scripts.references import materialise_references

    data = credited_primary(dataset)
    if case == "derived_copy":
        data["institutes"][0]["funding"][0]["eur_per_year_est"] = None
        message = "derived field must not be authored"
    else:
        data = materialise_references(data)
        message = "authored evidence requires stable selectors"
    monkeypatch.setattr(published, "AUTHORING_STRICT", False)
    assert check_method_rules(data) == []
    monkeypatch.setattr(published, "AUTHORING_STRICT", True)
    assert any(message in error for error in check_method_rules(data))
