"""Funding output on the saved page (test strategy D2, family 1): budgets, funding outside the chart, intervals,
per-person figures, amounts without an exchange rate and euro amounts.

The checks are check_page.py's own, with their independent expectations from the data (never the page's display
fields); here they read the saved, pre-rendered page once instead of the hydrated page in the browser gate. Their
parity on both pages, today and with planted defects, was shown before the browser copy went."""

import pytest

from scripts import check_page

FAMILY = {
    "budget output": lambda html, data: check_page.check_budget_output(html, data),
    "funding outside the chart": lambda html, data: check_page.check_funding_outside(html, data),
    "intervals": lambda html, data: check_page.check_interval_output(html, data, check_page.HAND_WORKED_INTERVALS),
    "per-person figures": lambda html, data: check_page.check_per_person(check_page.BeautifulSoup(html, "html.parser"), data),
    "amounts without a rate": lambda html, data: check_page.check_no_rate_amounts(html, data),
    "euro amounts": lambda html, data: check_page.check_euro_amounts(html, data),
}


@pytest.mark.parametrize("name", FAMILY)
def test_funding_output_matches_the_data(name, saved_html, published):
    found = FAMILY[name](saved_html, published)
    assert check_page.blocking(found) == [], f"{name}: {check_page.blocking(found)[:5]}"
