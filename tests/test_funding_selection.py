"""Hand-worked comparison cases exercise the producer's single selection rule."""

import copy
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from scripts import build
from scripts.published import prepared_facts, materialise_facts
from scripts.method_checks import required_excerpt_pointers
from scripts.shared_claims import resolve

ROOT = Path(__file__).resolve().parents[1]
PAGE = ROOT / "site/index.html"

def funding_selector(institute_id, funding_id, *fields):
    return ["institutes", {"id": institute_id}, "funding", {"id": funding_id}, *fields]


@pytest.fixture
def summed_funding(dataset):
    institute, = dataset["institutes"]
    base, = institute["funding"]
    base.update(annual_basis="Synthetic annual allocation.", annual_basis_code="other")
    first_source, = dataset["sources"]
    second_source = {**first_source, "id": "component-source", "url": "https://example.org/component", "excerpts": []}
    for key in ("output_role", "output_lineage"):
        second_source.pop(key)
    dataset["sources"].append(second_source)
    institute["funding"] = [
        {**copy.deepcopy(base), "id": identity, "amount": amount, "annual_amount": amount,
         "coverage": "component", "confidence": confidence, "source_ids": [source["id"]]}
        for identity, amount, confidence, source in (
            ("component-a", 10, "high", first_source), ("component-b", 20, "medium", second_source)
        )
    ]
    total = {**copy.deepcopy(base), "id": "total", "sum_of": ["component-a", "component-b"],
             "confidence": "medium", "source_ids": [first_source["id"], second_source["id"]]}
    total.pop("amount")
    institute["funding"].append(total)
    for index, source in enumerate((first_source, second_source)):
        source["excerpts"].append({"text": "Synthetic component allocation.", "supports": [
            f"/institutes/0/funding/{index}/{field}" for field in ("description", "amount", "annual_amount")
        ]})
    first_source["excerpts"].append({"text": "Synthetic budget.", "supports": ["/institutes/0/funding/2/description"]})
    return dataset


def select(records, as_of="2026-10-18", picker="comparableFunding"):
    from scripts.display import funding_selection

    comparison, budget = funding_selection(records, as_of)
    index = budget if picker == "pickFunding" else comparison
    return records[index] if index is not None else None


def figure(name, start, end, commitment="adopted", **extra):
    return {
        "id": name,
        "period": name,
        "kind": "annual",
        "eur_per_year_est": 10,
        "coverage": "whole_institute",
        "annual_period": {"start": start, "end": end},
        "commitment": commitment,
        "annual_basis_code": "other",
        **extra,
    }


def test_current_fiscal_dates_inclusive_and_order_independent():
    old = figure("previous", "2025-04-01", "2026-03-31")
    current = figure("current", "2026-04-01", "2027-03-31", "announced")
    future = figure("future", "2027-04-01", "2028-03-31")
    for records in ([old, future, current], [current, old, future]):
        for today in ("2026-04-01", "2027-03-31"):
            assert select(records, today) == current
    assert select([current, old], "2026-03-31") == old


def test_latest_earlier_beats_proposal_even_when_proposal_is_current():
    old = figure("old", "2024-10-01", "2025-09-30")
    latest = figure("latest", "2025-10-01", "2026-09-30")
    proposed = figure("proposal", "2026-10-01", "2027-09-30", "proposed")
    assert select([proposed, old, latest]) == latest
    assert select([old, latest, proposed]) == latest


def test_proposals_never_compared_but_supply_budget_when_nothing_committed():
    proposal = figure("proposal", "2027-01-01", "2027-12-31", "proposed")
    later = figure("later", "2028-01-01", "2028-12-31", "proposed")
    current = figure("current", "2026-01-01", "2026-12-31", "proposed")
    assert select([later, proposal, current]) is None
    funded = [dict(row, amount=10, currency="NOK") for row in (later, proposal)]
    assert select(funded, picker="pickFunding")["id"] == "later"
    assert select([dict(proposal, commitment="announced")]) is None


def test_components_wider_programmes_and_unknowns_never_represent_institute():
    base = figure("current", "2026-01-01", "2026-12-31")
    for changes in (
        {"coverage": "component"},
        {"kind": "not_institute_specific"},
        {"kind": "one_off"},
        {"eur_per_year_est": None},
        {"annual_period": None},
        {"coverage": None},
    ):
        assert select([dict(base, **changes)]) is None
    assert select([dict(base, eur_per_year_est=0)])["eur_per_year_est"] == 0


def test_allocation_before_mean_then_adopted_for_equal_periods():
    allocation = figure("allocation", "2026-04-01", "2027-03-31", "announced")
    mean = figure(
        "mean",
        "2024-04-01",
        "2029-03-31",
        annual_basis_code="period_mean",
        kind="multi_year",
    )
    assert select([mean, allocation]) == allocation
    adopted = dict(allocation, commitment="adopted")
    assert select([allocation, adopted]) == adopted


@pytest.mark.parametrize(
    "mutation",
    [
        "amount",
        "annual_amount",
        "currency",
        "period",
        "confidence",
        "sources",
        "self",
        "component",
    ],
)
def test_total_mutations_fail_build(summed_funding, mutation):
    data = prepared_facts(summed_funding)
    assert build.check_funding_totals(data, complete=True) == []
    records = {f["id"]: f for f in data["institutes"][0]["funding"]}
    total = records["total"]
    component = records["component-b"]
    if mutation in ("amount", "annual_amount"):
        total[mutation] += 1
    elif mutation == "currency":
        component["currency"] = "USD"
    elif mutation == "period":
        component["annual_period"]["start"] = "2025-01-01"
    elif mutation == "confidence":
        total["confidence"] = "high"
    elif mutation == "sources":
        total["source_ids"].pop()
    elif mutation == "self":
        total["sum_of"] = ["component-a", "total"]
    elif mutation == "component":
        component["coverage"] = "whole_institute"
    if mutation in ("amount", "annual_amount"):
        with pytest.raises(ValueError, match="disagrees with derived"):
            materialise_facts(data)
    else:
        assert build.check_funding_totals(data, complete=True)


def test_decision_dates_and_latest_decision_tie_break():
    base = figure("current", "2026-01-01", "2026-12-31")
    old = dict(base, decision_date="2026-01-01")
    new = dict(base, decision_date="2026-10-18")
    future = dict(base, decision_date="2026-10-19")
    assert select([future, old, new]) == new
    assert select([new, old, future]) == new
    assert select([future]) is None
    for partial in ("2026", "2026-10"):
        assert select([dict(base, decision_date=partial)]) is not None
    for partial in ("2027", "2026-11"):
        assert select([dict(base, decision_date=partial)]) is None


def test_derived_total_uses_component_excerpts_and_exports_scope(summed_funding):
    authored = summed_funding
    data = prepared_facts(authored)
    institute, = data["institutes"]
    def pointer(key, field):
        return resolve(data, funding_selector(institute["id"], key, field))[2]

    required = required_excerpt_pointers(data, authored=authored)
    for field in ("amount", "annual_amount"):
        assert pointer("total", field) not in required
        for key in ("component-a", "component-b"):
            assert pointer(key, field) in required
    assert build.check_consistency(data, require_roster=True, materialised=True, authored=authored) == []
    removed_pointer = pointer("component-b", "amount")
    for source in data["sources"]:
        for excerpt in source.get("excerpts", []):
            excerpt["supports"] = [
                p for p in excerpt["supports"] if p != removed_pointer
            ]
    assert any(
        f"required claim has no excerpt: {removed_pointer}" in e
        for e in build.check_consistency(data, materialised=True, authored=authored)
    )


@pytest.mark.parametrize("date", ["2026", "2026-10", "2026-02-30"])
def test_build_rejects_partial_or_invalid_annual_periods(dataset, date):
    data = prepared_facts(dataset)
    funding, = data["institutes"][0]["funding"]
    schema = json.loads((ROOT / "schema/aisi.schema.json").read_text())
    validator = Draft202012Validator(
        schema, format_checker=Draft202012Validator.FORMAT_CHECKER
    )
    assert not list(validator.iter_errors(data))
    funding["annual_period"]["end"] = date
    assert any("annual_period" in e.absolute_path for e in validator.iter_errors(data))


def test_card_and_table_picker_excludes_components_and_amountless_future_figures():
    """A future announced figure is eligible by date; it fails here only for lacking an amount."""
    component = figure("part", "2026-01-01", "2026-12-31", coverage="component")
    future = figure("future", "2027-01-01", "2027-12-31", commitment="announced")
    for records in ([component], [future], [component, future]):
        assert select(records, picker="pickFunding") is None
    funded_future = dict(future, amount=10, currency="USD")
    assert select([component, funded_future], picker="pickFunding") == funded_future
    published_only = dict(component, coverage="whole_institute", kind="one_off", amount=10, currency="USD")
    assert (
        select([component, future, published_only], picker="pickFunding")
        == published_only
    )
