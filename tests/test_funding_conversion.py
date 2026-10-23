"""Conversion expectations are hand-worked or use an independent float oracle."""

import copy
import csv
import json
import sys

import pytest

from conftest import ROOT, write_json
from scripts import build
from scripts.migrate_facts import migrate
from scripts.migrate_references import migrate as stable
from scripts.published import materialise_facts


@pytest.mark.parametrize(
    ("amount", "currency", "rate", "expected"),
    [
        (60_000_000, "GBP", 1.16714399042, 70_028_600),
        (8_900_000_000, "KRW", 0.000622877438263, 5_543_610),
        (10_500_000, "AUD", 0.570854454948, 5_993_970),
        (10_000_000, "EUR", 999, 10_000_000),
        (0, "GBP", 1.16714399042, 0),
        (1.2345675, "EUR", 999, 1.23457),
        (None, "GBP", 1.16714399042, None),
    ],
)
def test_annual_eur_derivation(amount, currency, rate, expected):
    assert (
        build.annual_eur_estimate(
            {"annual_amount": amount, "currency": currency}, {currency: rate}
        )
        == expected
    )


def funded(dataset):
    funding = dataset["institutes"][0]["funding"][0]
    funding.update(
        amount=8,
        annual_amount=8,
        annual_basis="Stated annual allocation.",
        annual_basis_code="other",
        eur_per_year_est=10,
    )
    dataset["sources"][0]["excerpts"][0]["supports"].append(
        "/institutes/0/funding/0/annual_amount"
    )
    return funding


@pytest.mark.parametrize(
    "kind", ["annual", "multi_year", "one_off", "not_institute_specific"]
)
def test_consistency_checks_all_kinds(dataset, kind):
    funding = funded(dataset)
    funding["kind"] = kind
    assert build.check_consistency(dataset) == []
    funding["eur_per_year_est"] = 11
    assert any(
        "eur_per_year_est: stored 11 disagrees with derived 10" in e for e in build.check_consistency(dataset)
    )


@pytest.mark.parametrize("stored", [None, 9.99, 10.01, 0])
def test_missing_or_unrounded_estimates_fail(dataset, stored):
    funding = funded(dataset)
    funding["eur_per_year_est"] = stored
    with pytest.raises(ValueError, match="eur_per_year_est.*disagrees with derived"):
        materialise_facts(dataset)
    funding.pop("eur_per_year_est")
    assert materialise_facts(dataset)["institutes"][0]["funding"][0]["eur_per_year_est"] == 10


def test_unknown_annual_amount_cannot_have_estimate(dataset):
    funding = dataset["institutes"][0]["funding"][0]
    funding["eur_per_year_est"] = 0
    with pytest.raises(ValueError, match="disagrees with derived None"):
        materialise_facts(dataset)
    funding["eur_per_year_est"] = None
    assert materialise_facts(dataset)["institutes"][0]["funding"][0]["eur_per_year_est"] is None


def test_missing_rate_is_not_silently_omitted(dataset):
    funding = funded(dataset)
    funding["currency"] = "KES"
    with pytest.raises(ValueError, match="no exchange rate for KES"):
        materialise_facts(dataset)
    assert any("missing currencies" in e for e in build.check_consistency(dataset))


def test_conversion_is_pure_and_published_consistently(dataset, monkeypatch):
    funded(dataset)
    reviewed = stable(dataset)
    output = reviewed["institutes"][0]["outputs"][0]
    output["credit"] = {"institute_id": "gb-test", "date": output["date"]}
    primary = migrate(reviewed)
    before = copy.deepcopy(primary)
    assert "eur_per_year_est" not in primary["institutes"][0]["funding"][0]
    one = materialise_facts(primary, strict=True)
    assert one == materialise_facts(primary, strict=True)
    assert one["institutes"][0]["funding"][0]["eur_per_year_est"] == 10
    assert primary == before
    write_json(build.DATA, dataset)
    monkeypatch.setattr(sys, "argv", ["build.py"])
    build.main()
    published = json.loads((build.DIST / "data/aisi.json").read_text())
    assert published["institutes"][0]["funding"][0]["eur_per_year_est"] == 10
    with (build.DIST / "data/funding.csv").open() as stream:
        assert next(csv.DictReader(stream))["eur_per_year_est"] == "10"


def test_conflicting_legacy_table_fails_without_mutation(dataset):
    funded(dataset)
    dataset["dataset"]["exchange_rates"][0]["eur_per_unit"] = 2
    before = copy.deepcopy(dataset)
    with pytest.raises(ValueError, match="stored.*disagrees with derived"):
        materialise_facts(dataset)
    assert dataset == before


def test_real_estimates_against_independent_arithmetic():
    data = build.export_dataset(json.loads((ROOT / "data/aisi.json").read_text()))
    rates = {
        r["currency"]: r["eur_per_unit"] for r in data["dataset"]["exchange_rates"]
    }
    count = 0
    for institute in data["institutes"]:
        for funding in institute.get("funding", []):
            if funding.get("annual_amount") is None:
                assert funding.get("eur_per_year_est") is None
                continue
            # These real figures are away from half-way ties, tested separately above.
            raw = funding["annual_amount"] * (
                1 if funding["currency"] == "EUR" else rates[funding["currency"]]
            )
            assert funding["eur_per_year_est"] == float(format(raw, ".6g"))
            count += 1
    assert count == 17


def test_changed_valid_table_invalidates_stored_estimates(dataset):
    funded(dataset)
    dataset["dataset"]["exchange_rates"][0]["eur_per_unit"] = 2
    dataset["dataset"]["exchange_rates"][0]["units_per_eur"] = 0.5
    assert build.check_exchange_rates(dataset) == []
    assert any(
        "eur_per_year_est: stored 10 disagrees with derived 16" in e for e in build.check_consistency(dataset)
    )



def test_null_currency_fails_derivation_without_mutation(dataset):
    funded(dataset)["currency"] = None
    before = copy.deepcopy(dataset)
    with pytest.raises(ValueError, match="no exchange rate for None"):
        materialise_facts(dataset)
    assert dataset == before
