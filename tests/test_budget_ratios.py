"""Budget per person and share of GDP (rule budget-ratios), against hand-worked numbers.

Fixture rates are chosen so the arithmetic can be checked on paper:
GBP 0.85 per EUR (EUR 1.17647058824 per GBP at 12 significant figures),
USD 1.25 per EUR (EUR 0.8 per USD), JPY 160 per EUR (EUR 0.00625 per JPY).
"""

import copy
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from scripts.build import check_budget_ratio_inputs, check_consistency, tables
from scripts.display import per_person_label, record_sources
from scripts.published import budget_ratios, materialise_facts, publish
from scripts.references import authored_schema

ROOT = Path(__file__).resolve().parents[1]
RATES = [
    {"currency": "GBP", "units_per_eur": 0.85},
    {"currency": "USD", "units_per_eur": 1.25},
    {"currency": "JPY", "units_per_eur": 160},
]


def record(identity="compared", **extra):
    return {
        "id": identity,
        "kind": "annual",
        "coverage": "whole_institute",
        "annual_period": {"start": "2026-01-01", "end": "2026-12-31"},
        "currency": "EUR",
        "amount": 10_000_000,
        "annual_amount": 10_000_000,
        "commitment": "adopted",
        "annual_basis_code": "other",
        **extra,
    }


def document(funding, population=10_000_000, gdp=None, kind="state"):
    jurisdiction = {"id": "XX", "kind": kind, "summary": "Fixture.", "timeline": []}
    if population is not None:
        jurisdiction["population"] = {"value": population, "year": 2025, "source_ids": ["pop"]}
    if gdp is not None:
        jurisdiction["gdp"] = {"year": 2025, "source_ids": ["gdp"], **gdp}
    return {
        "dataset": {"as_of": "2026-10-20", "exchange_rates": copy.deepcopy(RATES)},
        "jurisdictions": [jurisdiction],
        "institutes": [{"id": "inst", "jurisdiction_id": "XX", "funding": funding}],
        "sources": [],
    }


def compared(doc):
    rows = materialise_facts(doc)["institutes"][0]["funding"]
    return {row["id"]: row for row in rows}


@pytest.mark.parametrize("year", [2024, 2025])
def test_page_ratio_uses_resident_and_the_population_year(year):
    from bs4 import BeautifulSoup
    from scripts import check_page

    data = document([record()])
    data["institutes"][0]["status"] = "established"
    data["jurisdictions"][0]["status"] = "established"
    data["dataset"]["exchange_rates"] = []
    data["jurisdictions"][0]["population"]["year"] = year
    data["institutes"][0]["display"] = {"comparison_funding": "compared", "funding_records": {"compared": record(eur_per_year_est=10_000_000)}}
    html = f'<article class="fund-detail" data-j="XX"><div class="fund-budget-summary"><span class="qualifiers"><abbr class="g per-person val" tabindex="0" aria-description="{check_page.PER_PERSON_TIP}">EUR 1.0 per resident ({year})</abbr></span></div></article>'
    def check(text):
        return check_page.check_per_person(BeautifulSoup(text, "html.parser"), data)

    assert check(html) == []
    assert check(html.replace(f"resident ({year})", "person"))
    assert check(html.replace(f"resident ({year})", "resident (2023)"))
    assert check(html.replace('tabindex="0"', 'tabindex="0" aria-label="EUR 1 per person"'))
    assert check(html.replace(f'aria-description="{check_page.PER_PERSON_TIP}"', ""))
    assert check(html.replace('tabindex="0"', ""))


def test_same_currency_share_never_uses_the_exchange_rate():
    # GBP 60m against GBP 3,000bn GDP is exactly 0.00002, whatever the GBP rate.
    rows = compared(document(
        [record(currency="GBP", amount=60_000_000, annual_amount=60_000_000)],
        population=70_000_000,
        gdp={"value": 3_000_000_000_000, "currency": "GBP"},
    ))
    row = rows["compared"]
    assert row["share_of_gdp_est"] == 2e-05
    # 60m x 1.17647058824 = 70,588,235.2944 EUR; / 70m people = 1.008403361...
    assert row["eur_per_person_est"] == 1.0084
    assert row["ratio_denominators"] == {
        "population": {"value": 70_000_000, "year": 2025},
        "gdp": {"value": 3_000_000_000_000, "currency": "GBP", "year": 2025},
    }


@pytest.mark.parametrize(
    ("gdp", "share"),
    [
        # USD 10m = EUR 8m; GDP EUR 1,000bn.
        ({"value": 1_000_000_000_000, "currency": "EUR"}, 8e-06),
        # GDP JPY 500,000bn = EUR 3,125bn; 8m / 3,125bn.
        ({"value": 500_000_000_000_000, "currency": "JPY"}, 2.56e-06),
    ],
)
def test_different_currency_share_converts_both_at_the_same_rates(gdp, share):
    rows = compared(document([record(currency="USD")], gdp=gdp))
    assert rows["compared"]["share_of_gdp_est"] == share
    assert rows["compared"]["eur_per_person_est"] == 0.8


def test_ratios_use_unrounded_amounts_not_the_six_figure_export():
    # EUR 1,234,564.9 a year exports as 1,234,560; over 3 people that would give 411,520.
    rows = compared(document([record(amount=1_234_564.9, annual_amount=1_234_564.9)], population=3))
    row = rows["compared"]
    assert row["eur_per_year_est"] == 1_234_560
    assert row["eur_per_person_est"] == 411_522  # 411,521.633...
    exact = budget_ratios(row, {"population": {"value": 3}}, {})["eur_per_person"]
    assert str(exact).startswith("411521.63333333333333333333333333")


def test_period_mean_is_recomputed_not_read_from_its_float_export():
    # AUD 100 over 3 years, upper bound, at EUR 3 per AUD: exactly EUR 100 a year. The float
    # annual_amount 33.333333333333336 x 3 would round up to 100.001 and 0.100001.
    mean = record(currency="AUD", kind="multi_year", amount=100, years=3, annual_basis_code="period_mean", amount_qualifier="at_most")
    del mean["annual_amount"]  # derived from amount and years
    doc = document([mean], population=1, gdp={"value": 1000, "currency": "EUR"})
    doc["dataset"]["exchange_rates"].append({"currency": "AUD", "units_per_eur": 0.3333333333333333})
    row = compared(doc)["compared"]
    assert row["annual_amount"] == 33.333333333333336
    assert (row["eur_per_person_est"], row["share_of_gdp_est"]) == (100, 0.1)
    assert per_person_label(row, {"population": {"value": 1}}, {"AUD": 3}) == "up to ~EUR 100 per person"


@pytest.mark.parametrize(
    ("qualifier", "expected", "label"),
    [
        # EUR 10,000,041 / 10m people = 1.0000041: half up 1, an upper bound rounds up.
        (None, 1, "~EUR 1.0 per person"),
        ("at_most", 1.00001, "up to ~EUR 1.1 per person"),
        ("at_least", 1, "at least ~EUR 1.0 per person"),
        ("about", 1, "about EUR 1.0 per person"),
    ],
)
def test_bounds_round_the_way_that_keeps_them_true(qualifier, expected, label):
    extra = {"amount_qualifier": qualifier} if qualifier else {}
    row = compared(document([record(amount=10_000_041, annual_amount=10_000_041, **extra)]))["compared"]
    assert row["eur_per_person_est"] == expected
    assert per_person_label(row, {"population": {"value": 10_000_000}}, {}) == label


@pytest.mark.parametrize(
    ("amount", "export", "label"),
    [
        (12_345_650, 1.23457, "~EUR 1.2 per person"),  # half up at the sixth figure
        (1_250_000, 0.125, "~EUR 0.13 per person"),  # half up at the second figure
        (10_000_000, 1, "EUR 1.0 per person"),  # exact EUR figure: no ~, two figures kept
        (0, 0, "EUR 0 per person"),  # a zero budget stays zero
        (99_600_000, 9.96, "~EUR 10 per person"),  # rounds into the next decade, still two figures
    ],
)
def test_rounding_boundaries(amount, export, label):
    row = compared(document([record(amount=amount, annual_amount=amount)]))["compared"]
    assert row["eur_per_person_est"] == export
    assert per_person_label(row, {"population": {"value": 10_000_000}}, {}) == label


def test_converted_figure_is_marked_approximate():
    row = compared(document([record(currency="USD")]))["compared"]
    assert per_person_label(row, {"population": {"value": 10_000_000}}, {"USD": 0.8}) == "~EUR 0.80 per person"


@pytest.mark.parametrize(
    ("population", "gdp", "per_person", "share"),
    [
        (None, None, None, None),
        (10_000_000, None, 1, None),
        (None, {"value": 1_000_000_000_000, "currency": "EUR"}, None, 1e-05),
        (0, {"value": 0, "currency": "EUR"}, None, None),
        # A GDP currency with no ECB rate, different from the budget's, gives no share.
        (10_000_000, {"value": 1_000_000_000_000, "currency": "KES"}, 1, None),
    ],
)
def test_missing_or_unusable_denominators_give_null(population, gdp, per_person, share):
    row = compared(document([record()], population=population, gdp=gdp))["compared"]
    assert row["eur_per_person_est"] == per_person
    assert row["share_of_gdp_est"] == share
    if per_person is None:
        assert per_person_label(row, {"population": {"value": population}} if population is not None else {}, {}) is None


def test_only_the_compared_record_of_a_state_gets_ratios():
    funding = [
        record("compared"),
        record("proposal", commitment="proposed"),
        record("one-off", kind="one_off"),
        record("component", coverage="component"),
    ]
    rows = compared(document(funding, gdp={"value": 1_000_000_000_000, "currency": "EUR"}))
    assert "eur_per_person_est" in rows["compared"]
    for identity in ("proposal", "one-off", "component"):
        assert not {"eur_per_person_est", "share_of_gdp_est", "ratio_denominators"} & rows[identity].keys()
    for kind in ("supranational", "territory"):
        other = compared(document([record()], gdp={"value": 1_000_000_000_000, "currency": "EUR"}, kind=kind))
        assert not {"eur_per_person_est", "share_of_gdp_est", "ratio_denominators"} & other["compared"].keys(), kind


@pytest.mark.parametrize(
    ("amount", "qualifier", "export"),
    [
        (1_234_561, None, 1_234_560),  # half up
        (1_234_561, "at_most", 1_234_570),  # "up to": rounds up
        (1_234_561, "nearly", 1_234_570),
        (1_234_569, "at_least", 1_234_560),  # "at least": rounds down
        (1_234_569, "more_than", 1_234_560),
        (1_234_569, "about", 1_234_570),  # not a bound: half up
        (1_234_560, "at_most", 1_234_560),  # already six figures: unchanged
    ],
)
def test_eur_export_rounds_bounds_outward(amount, qualifier, export):
    extra = {"amount_qualifier": qualifier} if qualifier else {}
    row = compared(document([record(amount=amount, annual_amount=amount, **extra)]))["compared"]
    assert row["eur_per_year_est"] == export


def test_ratios_are_generated_never_authored():
    doc = document([record(eur_per_person_est=1)])
    with pytest.raises(ValueError, match="eur_per_person_est: derived field must not be authored"):
        materialise_facts(doc, strict=True)
    doc = document([record(), record("other", commitment="proposed", share_of_gdp_est=0.1)])
    with pytest.raises(ValueError, match="share_of_gdp_est: generated for the compared figure only"):
        materialise_facts(doc)
    schema = json.loads((ROOT / "schema/aisi.schema.json").read_text(encoding="utf-8"))
    funding = authored_schema(schema, strict=True)["$defs"]["funding"]
    validator = Draft202012Validator({"$defs": schema["$defs"], **funding})
    authored = {**record(), "description": "A budget.", "source_ids": ["s"], "confidence": "high", "annual_basis": "Stated."}
    assert not list(validator.iter_errors(authored))
    for key in ("eur_per_person_est", "share_of_gdp_est", "ratio_denominators"):
        assert list(validator.iter_errors({**authored, key: 1})), key


@pytest.fixture(scope="module")
def published():
    return publish(json.loads((ROOT / "data/aisi.json").read_text(encoding="utf-8")))


def test_real_ratios_match_hand_worked_values(published):
    rows = {
        institute["jurisdiction_id"]: institute["display"]["funding_records"][institute["display"]["comparison_funding"]]
        for institute in published["institutes"]
        if institute["display"]["comparison_funding"]
    }
    assert all(row.get("eur_per_person_est") is not None and row.get("share_of_gdp_est") is not None for row in rows.values())


def test_ratios_and_gdp_stay_off_page_source_lists(published):
    jurisdictions = {jurisdiction["id"]: jurisdiction for jurisdiction in published["jurisdictions"]}
    for jurisdiction in published["jurisdictions"]:
        if "gdp" in jurisdiction:
            gdp_ids = set(jurisdiction["gdp"]["source_ids"])
            assert not gdp_ids & set(jurisdiction["display"]["source_links"])
            assert not gdp_ids & set(jurisdiction["citations"]["display"])
            assert not gdp_ids & set(record_sources(jurisdiction))
    for institute in published["institutes"]:
        gdp_ids = set(jurisdictions[institute["jurisdiction_id"]].get("gdp", {}).get("source_ids", []))
        for row in institute.get("funding", []):
            if row.get("share_of_gdp_est") is not None:
                assert gdp_ids and gdp_ids <= set(row["citations"]["share_of_gdp_est"])
                assert not gdp_ids & set(row["citations"]["display"])
                assert not gdp_ids & set(row["display"]["budget_citations"])
        assert not gdp_ids & set(institute["display"]["source_links"])


def test_funding_csv_carries_ratios_with_denominator_year_and_sources(published):
    jurisdictions = {jurisdiction["id"]: jurisdiction for jurisdiction in published["jurisdictions"]}
    institutes = {institute["id"]: institute for institute in published["institutes"]}
    funding = tables(published)["funding"]
    rows = [row for row in funding if row["eur_per_person_est"] != "" or row["share_of_gdp_est"] != ""]
    assert rows
    for row in rows:
        jurisdiction = jurisdictions[institutes[row["institute_id"]]["jurisdiction_id"]]
        for denominator in ("population", "gdp"):
            assert row[f"{denominator}_year"] == jurisdiction[denominator]["year"]
            source_ids = row[f"{denominator}_source_ids"]
            assert source_ids and set(source_ids.split(";")) == set(jurisdiction[denominator]["source_ids"])
    others = [row for row in funding if row["eur_per_person_est"] == ""]
    assert all(row["gdp_source_ids"] == row["population_source_ids"] == "" for row in others)


def test_compared_state_budgets_have_both_denominators():
    """Each way of losing a denominator fails with the fixture state named."""
    data = document([record()], gdp={"value": 1_000_000_000, "currency": "EUR"})
    assert check_budget_ratio_inputs(materialise_facts(data)) == []
    for mutate, denominator in (
        (lambda j: j.pop("gdp"), "gdp"),
        (lambda j: j.pop("population"), "population"),
        (lambda j: j["gdp"].update(currency="KES"), "gdp"),
        (lambda j: j["gdp"].update(value=0), "gdp"),
    ):
        mutated = copy.deepcopy(data)
        jurisdiction, = mutated["jurisdictions"]
        mutate(jurisdiction)
        errors = check_budget_ratio_inputs(materialise_facts(mutated))
        message = f"jurisdiction {jurisdiction['id']} needs a positive {denominator}"
        assert len(errors) == 1 and message in errors[0], errors


def test_budget_ratio_inputs_skip_non_states():
    doc = materialise_facts(document([record()], population=None, kind="supranational"))
    assert check_budget_ratio_inputs(doc) == []
    doc = materialise_facts(document([record()], population=None))
    assert len(check_budget_ratio_inputs(doc)) == 2


def test_production_validation_names_a_compared_state_without_population(dataset):
    """The fixture goes through check_consistency, the path build.py validation runs."""
    funding, = dataset["institutes"][0]["funding"]
    funding.update(annual_amount=0, eur_per_year_est=0, annual_basis="Synthetic annual allocation.", annual_basis_code="other")
    dataset["sources"][0]["excerpts"][0]["supports"].append("/institutes/0/funding/0/annual_amount")
    assert check_consistency(dataset) == []
    jurisdiction, = dataset["jurisdictions"]
    institute, = dataset["institutes"]
    jurisdiction["population"]["value"] = 0
    errors = check_consistency(dataset)
    assert any(f"institute {institute['id']}" in e and f"{jurisdiction['id']} needs a positive population" in e for e in errors), errors
