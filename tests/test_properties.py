"""D8: bounded, seeded properties complement the hand-worked regressions.

Every property runs 100 cases. Oracles use exact rational arithmetic, including
integer quotient/remainder rounding, never the producer or the gate's answers.
Only ECB inputs are read from the dataset. No build or browser is needed.
"""

import json
import random
import re
import subprocess
from decimal import Decimal, localcontext
from fractions import Fraction
from pathlib import Path

import pytest

from scripts import build, check_page, display, published


ROOT = Path(__file__).resolve().parents[1]
QUALIFIERS = (None, "exact", "about", "at_least", "more_than", "at_most",
              "less_than", "nearly")
PREFIXES = {
    None: "", "exact": "", "about": "about ", "at_least": "at least ",
    "more_than": "more than ", "at_most": "up to ", "less_than": "less than ",
    "nearly": "nearly ",
}
MONTHS = "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split()


def power10(exponent):
    return Fraction(10) ** exponent


def quantum(value, figures):
    """Find a decimal place by rational comparisons, without logs or Decimal.adjusted."""
    decade = Fraction(1)
    while value >= decade * 10:
        decade *= 10
    while value < decade:
        decade /= 10
    return decade / 10 ** (figures - 1)


def rounded(value, figures, qualifier=None):
    if not value:
        return Fraction(0)
    step = quantum(value, figures)
    scaled = value / step
    whole, remainder = divmod(scaled.numerator, scaled.denominator)
    if qualifier in ("at_most", "less_than", "nearly"):
        whole += bool(remainder)
    elif qualifier not in ("at_least", "more_than"):
        whole += 2 * remainder >= scaled.denominator
    return whole * step


def decimal(value):
    # Used only for finite generated inputs and rounded rational rates.
    with localcontext() as context:
        context.prec = 80
        return Decimal(value.numerator) / Decimal(value.denominator)


def assert_rounding(actual, exact, figures, qualifier, case):
    actual = Fraction(str(actual))
    assert actual == rounded(exact, figures, qualifier), case
    if not exact:
        return
    step = quantum(exact, figures)
    if qualifier in ("at_least", "more_than"):
        assert 0 <= exact - actual < step, case
    elif qualifier in ("at_most", "less_than", "nearly"):
        assert 0 <= actual - exact < step, case
    else:
        assert abs(actual - exact) <= step / 2, case


def shown_digits(text):
    """Decimal parsing ignores leading zeros but retains fractional trailing zeros."""
    value = Decimal(text.replace(",", ""))
    if not value:
        return 0
    digits = value.as_tuple().digits
    if value.as_tuple().exponent >= 0:
        return len(str(int(value)).rstrip("0"))
    return len(digits)


def read_euros(label):
    match = re.fullmatch(
        r"(.*?)EUR (\d[\d,]*(?:\.\d+)?)(bn|m|k)?(.*)", label
    )
    assert match, label
    prefix, number, suffix, tail = match.groups()
    scale = {None: 1, "k": 1000, "m": 10**6, "bn": 10**9}[suffix]
    return prefix, Fraction(number.replace(",", "")) * scale, number, tail


@pytest.fixture(scope="module")
def ecb():
    rows = json.loads((ROOT / "data/aisi.json").read_text())["dataset"]["exchange_rates"]
    exact = {row["currency"]: rounded(1 / Fraction(str(row["units_per_eur"])), 12)
             for row in rows}
    exact["EUR"] = Fraction(1)
    return rows, exact, {currency: decimal(rate) for currency, rate in exact.items()}


def test_money_conversion_and_shown_amounts(ecb):
    """100 amounts, 0..1e12 in cents; EUR and all ten dataset ECB currencies.

    AUD/CAD/GBP/INR/JPY/KRW/MYR/NOK/SGD/USD rates have 12 significant
    figures; annual products are exact before 6-figure exports and 2-figure
    page labels. All seven qualifiers and absent qualifiers are exercised.
    No unsupported currencies, missing values or period means in this domain.
    """
    rng = random.Random(0xD801)
    rows, exact_rates, rates = ecb
    currencies = sorted(exact_rates)
    units = {row["currency"]: row["units_per_eur"] for row in rows}
    for n in range(100):
        currency = currencies[n % len(currencies)]
        qualifier = QUALIFIERS[n % len(QUALIFIERS)]
        amount = Fraction(0 if n == 0 else rng.randint(1, 10**14), 100)
        record = {"annual_amount": float(decimal(amount)), "currency": currency,
                  "amount_qualifier": qualifier, "kind": "annual"}
        case = (n, record)
        if currency != "EUR":
            assert_rounding(published.eur_per_unit(units[currency]),
                            1 / Fraction(str(units[currency])), 12, None, case)
        exact = amount * exact_rates[currency]
        assert Fraction(published.annual_eur_amount(record, rates)) == exact, case
        assert Fraction(display.display_annual_amount(record, rates)) == exact, case
        assert_rounding(build.annual_eur_estimate(record, rates), exact, 6,
                        qualifier, case)
        label, _ = display.funding_label(record, annual=True, rates=rates)
        prefix, shown, token, tail = read_euros(label)
        assert_rounding(shown, exact, 2, qualifier, case)
        assert shown_digits(token) <= 2, (case, label)
        mark = "~" if currency != "EUR" or shown != exact else ""
        assert prefix == PREFIXES[qualifier] + ("" if qualifier == "about" else mark), case
        if currency == "EUR":
            assert tail == "", case
        else:
            native = re.fullmatch(r" \((~?)" + currency + r" ([\d,.]+)(bn|m|k)?\)", tail)
            assert native, (case, label)
            mark, token, suffix = native.groups()
            native_shown = Fraction(token.replace(",", "")) * {
                None: 1, "k": 1000, "m": 10**6, "bn": 10**9
            }[suffix]
            assert_rounding(native_shown, amount, 2, qualifier, case)
            assert shown_digits(token) <= 2, (case, label)
            assert mark == ("~" if native_shown != amount else ""), case


@pytest.mark.parametrize("qualifier", QUALIFIERS)
def test_qualified_rounding(qualifier):
    """100 non-negative values per qualifier, 0..1e12, grid as fine as 1e-8.

    Currency-independent two-figure rounding, including generated half ties
    and values just below decade changes; inputs have at most eight figures.
    Exact/about/absent use half up, lower bounds floor, upper bounds ceiling.
    """
    rng = random.Random(0xD802 + QUALIFIERS.index(qualifier))
    for n in range(100):
        if n == 0:
            value = Fraction(0)
        elif n % 5 == 0:
            value = (10 * rng.randint(10, 99) + 5) * power10(rng.randint(-7, 9))
        elif n % 5 == 1:
            value = (10000 - rng.randint(1, 9)) * power10(rng.randint(-8, 8))
        else:
            value = rng.randint(1, 10**8) * power10(rng.randint(-8, 4))
        case = (n, str(decimal(value)), qualifier)
        # Both producer and gate are subjects; neither supplies the expected result.
        for actual in (display.round2(decimal(value), qualifier),
                       check_page.half_up_two(decimal(value), qualifier)):
            assert_rounding(actual, value, 2, qualifier, case)
        token = display.money(display.round2(decimal(value), qualifier))
        number = token.removesuffix("bn").removesuffix("m").removesuffix("k")
        assert shown_digits(number) <= 2, (case, token)
        scale = {"bn": 10**9, "m": 10**6, "k": 10**3}.get(token[len(number):], 1)
        assert Fraction(number) * scale == rounded(value, 2, qualifier), (case, token)  # the text keeps the rounded amount
        assert check_page.two_figures(token) is True, (case, token)


def test_two_figures_accepts_only_the_allowed_precision():
    """100 currency-independent decimal tokens, 0..1e9, 0..8 decimal places.

    Includes k/m/bn suffixes and comma grouping; fractional trailing zeros
    count, integer trailing zeros do not. Invalid numeric syntax is excluded.
    """
    rng = random.Random(0xD803)
    for n in range(100):
        if n == 0:
            value = Decimal(0)
        elif n % 4 == 0:
            # Accepted integer zeros, including grouped thousands and millions.
            value = Decimal(rng.randint(1, 99) * 10 ** rng.randint(0, 7))
        elif n % 4 == 1:
            value = Decimal(rng.randint(10, 99)).scaleb(-rng.randint(1, 8))
        elif n % 4 == 2:
            # Rejected fractional zeros: e.g. 0.0100 has four written places.
            value = Decimal(rng.randint(1, 99) * 100).scaleb(-rng.randint(3, 8))
        else:
            value = Decimal(rng.randint(100, 999)).scaleb(-rng.randint(0, 8))
        token = format(value, ",f" if n % 2 else "f")
        text = "EUR " + token + rng.choice(("", "k", "m", "bn"))
        assert check_page.two_figures(text) == (shown_digits(token) <= 2), (n, text)


def test_budget_ratios_from_exact_quotients(ecb):
    """100 budgets, 0..1e12 in cents; annual or total divided by 1..20 years.

    Population: integer 1..1e9; GDP: integer 1e6..1e15. EUR and all ten ECB
    currencies (AUD/CAD/GBP/INR/JPY/KRW/MYR/NOK/SGD/USD), same or different
    budget/GDP currency, 12-figure rates, every qualifier. Raw Decimal results
    allow 1e-48 relative error for at most four 50-digit operations; 6-figure
    exports and 2-figure labels must equal the exact rational rounding.
    Denominators are positive; only a state's single compared record is tested.
    """
    rng = random.Random(0xD804)
    rows, exact_rates, rates = ecb
    currencies = sorted(exact_rates)
    for n in range(100):
        currency = currencies[n % len(currencies)]
        gdp_currency = currency if n % 3 == 0 else rng.choice(currencies)
        qualifier = QUALIFIERS[n % len(QUALIFIERS)]
        amount = Fraction(0 if n == 0 else rng.randint(1, 10**14), 100)
        years = rng.randint(1, 20) if n % 2 else 1
        population, gdp = rng.randint(1, 10**9), rng.randint(10**6, 10**15)
        record = {
            "id": "budget", "kind": "multi_year" if n % 2 else "annual",
            "coverage": "whole_institute", "commitment": "adopted",
            "annual_period": {"start": "2026-01-01", "end": "2026-12-31"},
            "currency": currency, "amount": float(decimal(amount)),
            "amount_qualifier": qualifier,
        }
        if n % 2:
            record.update(years=years, annual_basis_code="period_mean")
        else:
            record["annual_amount"] = record["amount"]
        jurisdiction = {
            "id": "XX", "kind": "state", "timeline": [],
            "population": {"value": population, "year": 2025},
            "gdp": {"value": gdp, "currency": gdp_currency, "year": 2025},
        }
        case = (n, record, jurisdiction)
        annual = amount / years
        exact = {
            "eur_per_person": annual * exact_rates[currency] / population,
            "share_of_gdp": annual / gdp if currency == gdp_currency else
            annual * exact_rates[currency] / (gdp * exact_rates[gdp_currency]),
        }
        raw = published.budget_ratios(record, jurisdiction, rates)
        data = {
            "dataset": {"as_of": "2026-10-20", "exchange_rates": rows},
            "jurisdictions": [jurisdiction],
            "institutes": [{"id": "inst", "jurisdiction_id": "XX", "funding": [record]}],
            "sources": [],
        }
        result = published.materialise_facts(data, strict=True)
        [institute] = result["institutes"]
        [exported] = institute["funding"]
        for key, quotient in exact.items():
            assert abs(Fraction(raw[key]) - quotient) <= abs(quotient) / 10**48, case
            assert_rounding(exported[key + "_est"], quotient, 6, qualifier, case)
        label = display.per_person_label(record, jurisdiction, rates)
        prefix, shown, token, tail = read_euros(label)
        assert_rounding(shown, exact["eur_per_person"], 2, qualifier, case)
        assert shown_digits(token) <= 2 and tail == " per person", (case, label)
        mark = "~" if currency != "EUR" or shown != exact["eur_per_person"] else ""
        assert prefix == PREFIXES[qualifier] + ("" if qualifier == "about" else mark), case


def month_lengths(year):
    leap = year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)
    return (31, 28 + leap, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31)


def ordinal(year, month, day):
    prior = year - 1
    return (365 * prior + prior // 4 - prior // 100 + prior // 400
            + sum(month_lengths(year)[:month - 1]) + day)


def generated_date(rng, n):
    year = rng.choice((1900, 2000, 2100)) if n % 10 == 0 else rng.randint(1900, 2100)
    month = 2 if n % 10 == 0 else rng.randint(1, 12)
    day = rng.randint(1, month_lengths(year)[month - 1])
    return year, month, day


def iso(parts, precision):
    year, month, day = parts
    return f"{year:04d}-{month:02d}-{day:02d}"[:precision]


def ordinal_bounds(parts, precision, cap):
    year, month, day = parts
    if precision == 10:
        return ordinal(year, month, day), ordinal(year, month, day)
    first_month, last_month = (1, 12) if precision == 4 else (month, month)
    first = ordinal(year, first_month, 1)
    last = ordinal(year, last_month, month_lengths(year)[last_month - 1])
    return first, last if cap is None else max(first, min(last, ordinal(*cap)))


def test_partial_dates_keep_display_precision():
    """100 Gregorian dates in 1900..2100, cycling YYYY/YYYY-MM/YYYY-MM-DD.

    English abbreviated months, unpadded day, preserved year/month precision;
    includes century years and February. No currencies or invalid dates.
    The real page formatter runs in Node once, without a DOM or browser.
    """
    rng = random.Random(0xD805)
    inputs, expected = [], []
    for n in range(100):
        parts = generated_date(rng, n)
        year, month, day = parts
        precision = (4, 7, 10)[n % 3]
        value = iso(parts, precision)
        words = [str(year)]
        if precision >= 7:
            words.insert(0, MONTHS[month - 1])
        if precision == 10:
            words.insert(0, str(day))
        label = " ".join(words)
        inputs.append(value)
        expected.append(label)
        assert build.valid_partial_date(value), (n, value)
        if precision == 10:
            assert display.card_date(value) == label, (n, value)
    source = (ROOT / "site/index.html").read_text()
    helpers = source[source.index("  var MON ="):source.index("  function dated(")]
    result = subprocess.run(
        ["node", "-"], input=helpers + "\nconsole.log(JSON.stringify("
        + json.dumps(inputs) + ".map(fmtDate)));", capture_output=True,
        text=True, check=True, timeout=30,  # room for a busy shared host; a passing run takes well under a second
    )
    actual = json.loads(result.stdout)
    assert len(actual) == 100
    for value, shown, wanted in zip(inputs, actual, expected, strict=True):
        assert shown == wanted, (value, shown, wanted)


def test_partial_date_bounds_and_signed_intervals():
    """100 pairs of Gregorian dates in 1900..2100 at year/month/day precision.

    All nine precision pairs; as_of absent or in 1900..2100, including before,
    inside and after the recorded period. Exact Gregorian integer ordinals
    determine bounds, ordering and signed whole-day interval extrema.
    No currency, missing milestone or invalid calendar date is generated.
    """
    rng = random.Random(0xD806)
    for n in range(100):
        start = generated_date(rng, n)
        end = start if n % 5 == 0 else generated_date(rng, n + 1)
        cap = None if n % 3 == 0 else start if n % 3 == 1 else generated_date(rng, n + 2)
        precisions = ((4, 7, 10)[n % 3], (4, 7, 10)[n // 3 % 3])
        expected, actual = [], []
        for parts, precision in zip((start, end), precisions, strict=True):
            day, as_of = iso(parts, precision), iso(cap, 10) if cap else None
            bounds = published.date_bounds(day, as_of)
            wanted = ordinal_bounds(parts, precision, cap)
            assert tuple(value.toordinal() for value in bounds) == wanted, (day, as_of)
            actual.append(bounds)
            expected.append(wanted)
        differences = [b - a for a in expected[0] for b in expected[1]]
        span = published.interval(*actual)
        assert span == {"days_min": min(differences), "days_max": max(differences)}, (
            start, end, precisions, cap
        )
