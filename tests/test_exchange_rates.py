"""Rate checks, with expected values from the retrieved ECB calendar-2025 annual averages.

https://data-api.ecb.europa.eu/service/data/EXR/A.USD+GBP+JPY.EUR.SP00.A?startPeriod=2025&endPeriod=2025&format=csvdata
Published inputs, units per euro: USD 1.1299831372549, GBP 0.8567923137255, JPY 169.0434509803921.
Expected EUR per unit below were calculated separately from the build helper, with fractions.
"""

import copy
import json
from decimal import Decimal

import pytest
from jsonschema import Draft202012Validator

from conftest import ROOT
from scripts import build
from scripts.published import materialise_facts
from scripts.method_checks import check_method_rules


def test_missing_conversion_rate_names_currency_and_preserves_zero():
    for amount in (0, 1):
        with pytest.raises(ValueError, match="^no exchange rate for KES$"):
            build.annual_eur_estimate({"annual_amount": amount, "currency": "KES"}, {})
    assert build.annual_eur_estimate({"annual_amount": 0, "currency": "EUR"}, {}) == 0
    assert build.annual_eur_estimate({"annual_amount": 5, "currency": "EUR"}, {}) == 5
    assert build.annual_eur_estimate({"annual_amount": None, "currency": "KES"}, {}) is None


@pytest.mark.parametrize(
    ("units", "expected"),
    [(1.1299831372549, 0.884968958412), (0.8567923137255, 1.16714399042), (169.0434509803921, 0.00591563881476)],
)
def test_ecb_rates_invert_to_eur_per_unit(units, expected):
    assert build.eur_per_unit(units) == expected
    assert json.loads(json.dumps(expected)) == expected


@pytest.mark.parametrize(
    ("value", "digits", "expected"),
    [("0", 3, 0), ("1.245", 3, 1.25), ("999.5", 3, 1000), ("0.0001235", 3, 0.000124)],
)
def test_decimal_rounding(value, digits, expected):
    assert build.significant(Decimal(value), digits) == expected


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-1"])
def test_invalid_amounts_rejected(value):
    with pytest.raises(ValueError):
        build.significant(Decimal(value), 3)


def test_current_rate_contract():
    data = json.loads((ROOT / "data/aisi.json").read_text())
    assert build.check_exchange_rates(data) == []
    currencies = {r["currency"] for r in data["dataset"]["exchange_rates"]}
    assert {"GBP", "CAD", "JPY", "KRW", "AUD", "NOK", "MYR", "SGD", "USD"} == currencies


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("eur_per_unit", 1.26, "eur_per_unit: stored 1.26 disagrees"),
        ("basis", "Different table", "one period, basis and source set"),
        ("source_ids", ["missing"], "unknown source id"),
        ("eur_per_unit", float("inf"), "eur_per_unit: stored inf disagrees"),
    ],
)
def test_rate_mutations_fail(dataset, field, value, message):
    dataset["dataset"]["exchange_rates"][0][field] = value
    assert any(message in e for e in build.check_consistency(dataset))


def test_duplicate_and_missing_currency(dataset):
    rates = dataset["dataset"]["exchange_rates"]
    rates.append(copy.deepcopy(rates[0]))
    assert "exchange_rates: duplicate currency" in build.check_exchange_rates(dataset)
    rates.pop()
    dataset["institutes"][0]["funding"][0]["currency"] = "KES"
    assert any(
        "missing currencies" in e and "KES" in e
        for e in build.check_exchange_rates(dataset)
    )


@pytest.mark.parametrize(
    "period",
    [
        {"start": "2025-02-30", "end": "2025-12-31"},
        {"start": "2026-01-01", "end": "2025-12-31"},
        {"start": "2025-01-01", "end": "2027-12-31"},
    ],
)
def test_rate_period_is_real_ordered_and_not_future(dataset, period):
    dataset["dataset"]["exchange_rates"][0]["period"] = period
    assert any(
        "invalid or future period" in e for e in build.check_exchange_rates(dataset)
    )


@pytest.mark.parametrize("field", ["units_per_eur", "period/start", "period/end"])
def test_rate_input_needs_exact_excerpt(dataset, field):
    pointer = f"/dataset/exchange_rates/0/{field}"
    dataset["sources"][0]["excerpts"][0]["supports"].remove(pointer)
    assert f"required claim has no excerpt: {pointer}" in check_method_rules(dataset)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("units_per_eur", 0),
        ("eur_per_unit", -1),
        ("source_ids", []),
        ("currency", "gbp"),
    ],
)
def test_rate_schema_rejects_invalid_fields(dataset, field, value):
    dataset["dataset"]["exchange_rates"][0][field] = value
    schema = json.loads(build.SCHEMA.read_text())
    assert list(Draft202012Validator(schema).iter_errors(dataset))


def test_valid_but_mixed_periods_fail(dataset):
    dataset["dataset"]["exchange_rates"][0]["period"] = {
        "start": "2024-01-01",
        "end": "2024-12-31",
    }
    assert build.check_exchange_rates(dataset) == [
        "exchange_rates: all entries must use one period, basis and source set",
        "exchange_rates: period must be calendar 2025, the latest complete year before as_of",
    ]


def test_distinct_primary_source_sets_fail(dataset):
    dataset["sources"].append({**dataset["sources"][0], "id": "another-primary"})
    dataset["dataset"]["exchange_rates"][0]["source_ids"] = ["another-primary"]
    assert build.check_exchange_rates(dataset) == [
        "exchange_rates: all entries must use one period, basis and source set"
    ]


def test_secondary_rates_fail(dataset):
    dataset["sources"][0]["type"] = "secondary"
    assert all(
        "rates need primary sources" in e for e in build.check_exchange_rates(dataset)
    )
    assert len(build.check_exchange_rates(dataset)) == 2


def test_rate_input_change_invalidates_stored_inverse(dataset):
    dataset["dataset"]["exchange_rates"][1]["units_per_eur"] = 0.5
    assert build.check_exchange_rates(dataset) == []
    with pytest.raises(ValueError, match="eur_per_unit.*disagrees with derived 2"):
        materialise_facts(dataset)


def test_missing_rate_for_a_funding_currency_is_reported(dataset):
    dataset["dataset"]["exchange_rates"] = [
        r for r in dataset["dataset"]["exchange_rates"] if r["currency"] != "GBP"
    ]
    assert build.check_exchange_rates(dataset) == [
        "exchange_rates: missing currencies ['GBP']"
    ]


def test_eur_has_no_row(dataset):
    rates = dataset["dataset"]["exchange_rates"]
    rates.append({**rates[0], "currency": "EUR"})
    assert "exchange_rates: EUR is the base and has no row" in build.check_exchange_rates(dataset)


def test_partial_rate_dates_fail(dataset):
    dataset["dataset"]["exchange_rates"][0]["period"] = {"start": "2025", "end": "2025"}
    assert any(
        "invalid or future period" in e for e in build.check_exchange_rates(dataset)
    )


def test_method_rate_year_must_match_the_rate_period(dataset):
    dataset["dataset"]["method"].append({"id": "currency-rounding", "text": "Converted at the ECB's annual-average rates for 2024."})
    assert any("must say 'annual-average rates for 2025'" in e for e in build.check_exchange_rates(dataset))


def test_method_reworded_without_the_year_still_fails(dataset):
    dataset["dataset"]["method"].append({"id": "currency-rounding", "text": "Converted at the ECB's 2025 annual-average rates."})
    assert any("must say" in e for e in build.check_exchange_rates(dataset))


def test_real_method_states_the_rate_year():
    data = json.loads((ROOT / "data/aisi.json").read_text())
    year = data["dataset"]["exchange_rates"][0]["period"]["start"][:4]
    assert any(f"annual-average rates for {year}" in t["text"] for t in data["dataset"]["method"])
    assert build.check_exchange_rates(data) == []


def test_year_end_as_of_still_uses_the_year_before(dataset):
    # The ECB publishes a year's annual average only in January, so as_of 2026-12-31 still needs 2025 rates.
    dataset["dataset"]["as_of"] = "2026-12-31"
    assert build.check_exchange_rates(dataset) == []
