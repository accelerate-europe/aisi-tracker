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

KOREA_FUNDING_IDS = {
    "non_rd": "funding-eb798d93b6345b24b6fb8f1c45ad7e4a",
    "rd": "funding-a4935c40a4e750e8844af6de22176e13",
    "total": "funding-d0f61d15548153b9830f74f6e14ec324",
}


def funding_selector(institute_id, funding_id, *fields):
    return ["institutes", {"id": institute_id}, "funding", {"id": funding_id}, *fields]


def korea_record(data, key):
    return resolve(data, funding_selector("kr-aisi", KOREA_FUNDING_IDS[key]))[0]


def korea_pointer(data, key, field):
    return resolve(data, funding_selector("kr-aisi", KOREA_FUNDING_IDS[key], field))[2]


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


def test_real_selection_matches_expected_selections():
    data = prepared_facts(json.loads((ROOT / "data/aisi.json").read_text()))
    actual = {
        i["jurisdiction_id"]: select(i.get("funding", []), data["dataset"]["as_of"])
        for i in data["institutes"]
    }
    assert {
        country: (f["period"], f["eur_per_year_est"])
        for country, f in actual.items()
        if f
    } == {
        "GB": ("2026/27 to 2029/30", 70_028_600),
        "US": ("FY2026 enacted", 8_849_690),
        "CA": ("from 2024-25", 6_334_220),
        "KR": ("2026 total", 8_019_550),
        "AU": ("2025-26 to 2028-29", 5_993_970),
    }
    assert actual["CA"]["annual_basis_code"] == "period_mean"
    # Norway's 2027 proposal makes no bar but stays its Budget.
    norway = next(i for i in data["institutes"] if i["jurisdiction_id"] == "NO")
    budget = select(norway["funding"], data["dataset"]["as_of"], picker="pickFunding")
    assert (budget["period"], budget["commitment"]) == ("2027 budget proposal", "proposed")
    assert actual["KR"]["annual_amount"] == 8_900_000_000 + 3_975_000_000
    assert actual["KR"]["confidence"] == "low"


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
def test_total_mutations_fail_build(mutation):
    data = prepared_facts(json.loads((ROOT / "data/aisi.json").read_text()))
    assert build.check_funding_totals(data, complete=True) == []
    total = korea_record(data, "total")
    rd = korea_record(data, "rd")
    if mutation in ("amount", "annual_amount"):
        total[mutation] += 1
    elif mutation == "currency":
        rd["currency"] = "USD"
    elif mutation == "period":
        rd["annual_period"]["start"] = "2025-01-01"
    elif mutation == "confidence":
        total["confidence"] = "high"
    elif mutation == "sources":
        total["source_ids"].pop()
    elif mutation == "self":
        total["sum_of"] = [KOREA_FUNDING_IDS["non_rd"], KOREA_FUNDING_IDS["total"]]
    elif mutation == "component":
        rd["coverage"] = "whole_institute"
    if mutation in ("amount", "annual_amount"):
        with pytest.raises(ValueError, match="disagrees with derived"):
            materialise_facts(data)
    else:
        assert build.check_funding_totals(data, complete=True)


def test_removing_total_never_makes_component_comparable():
    data = json.loads((ROOT / "data/aisi.json").read_text())
    only_components = [copy.deepcopy(korea_record(data, key)) for key in ("non_rd", "rd")]
    assert select(only_components) is None


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


def test_derived_total_uses_component_excerpts_and_exports_scope():
    authored = json.loads((ROOT / "data/aisi.json").read_text())
    data = prepared_facts(authored)
    required = required_excerpt_pointers(data, authored=authored)
    for field in ("amount", "annual_amount"):
        assert korea_pointer(data, "total", field) not in required
        for key in ("non_rd", "rd"):
            assert korea_pointer(data, key, field) in required
    assert build.check_consistency(data, require_roster=True, materialised=True, authored=authored) == []
    removed_pointer = korea_pointer(data, "rd", "amount")
    for source in data["sources"]:
        for excerpt in source.get("excerpts", []):
            excerpt["supports"] = [
                p for p in excerpt["supports"] if p != removed_pointer
            ]
    assert any(
        f"required claim has no excerpt: {removed_pointer}" in e
        for e in build.check_consistency(data, materialised=True, authored=authored)
    )
    rows = {
        row["id"]: row for row in build.tables(data)["funding"]
        if row["institute_id"] == "kr-aisi"
    }
    total = rows[KOREA_FUNDING_IDS["total"]]
    assert total["derived_total"] is True
    assert total["coverage"] == "whole_institute"
    assert total["annual_period_start"] == "2026-01-01"
    assert total["annual_period_end"] == "2026-12-31"
    assert all(
        rows[KOREA_FUNDING_IDS[key]]["coverage"] == "component"
        and rows[KOREA_FUNDING_IDS[key]]["derived_total"] is False
        for key in ("non_rd", "rd")
    )


@pytest.mark.parametrize("date", ["2026", "2026-10", "2026-02-30"])
def test_build_rejects_partial_or_invalid_annual_periods(date):
    data = prepared_facts(json.loads((ROOT / "data/aisi.json").read_text()))
    funding = resolve(data, funding_selector("gb-aisi", "funding-9efb56f77c7a5a6c8ca9eee9f9db9191"))[0]
    funding["annual_period"]["end"] = date
    schema = json.loads((ROOT / "schema/aisi.schema.json").read_text())
    validator = Draft202012Validator(
        schema, format_checker=Draft202012Validator.FORMAT_CHECKER
    )
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

# Hand-worked owner expectations, keyed by permanent record identity, not period text.
EXPECTED_BUDGET_IDS = {
    "gb-aisi": "funding-9efb56f77c7a5a6c8ca9eee9f9db9191",
    "fr-inesia": None,
    "de-aisi": None,
    "es-isia": None,
    "no-ki-norge": "funding-0475119a71cc5e2099e293d3a48356b0",
    "eu-ai-office": None,
    "us-caissi": "funding-443aa6a31d205192966af69e21022fb1",
    "ca-caisi": "funding-360a59a37fde5cc8ba0a03ce3a00ea5f",
    "jp-aisi": "funding-7d0b36a48cc65315865a6d6ec83ddeb6",
    "kr-aisi": "funding-d0f61d15548153b9830f74f6e14ec324",
    "sg-aisi": None,
    "au-aisi": "funding-a7c0488859f35293a4b593ba44e40d12",
    "my-aisafe": None,
    "in-aisi": None,
    "ke-ai-safety-institute": None,
}


def test_budget_ids_match_independent_institute_expectations():
    from scripts.published import publish

    authored = json.loads((ROOT / "data/aisi.json").read_text())
    result = publish(authored, strict=True)
    assert {i["id"]: i["display"]["budget"] for i in result["institutes"]} == EXPECTED_BUDGET_IDS
