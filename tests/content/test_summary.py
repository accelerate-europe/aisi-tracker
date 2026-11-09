"""The page's summary on the saved page (test strategy D2, family 4): the hero, legend and developments, the figures
row of medians and counts, and the two-significant-figures rule for shown amounts.

The checks are check_page.py's own, with their expectations from the data; here they read the saved, pre-rendered
page once instead of the hydrated page in the browser gate. Their parity on both pages, today and with planted
defects, was shown before the browser copy went."""

import pytest

from scripts import check_page

FAMILY = {
    "hero, legend and developments": lambda html, data: check_page.check_summary_data(html, data),
    "figures row": lambda html, data: check_page.check_figures_row(check_page.BeautifulSoup(html, "html.parser"), data),
    "two significant figures": lambda html, data: check_page.check_two_significant_figures(check_page.BeautifulSoup(html, "html.parser")),
}


@pytest.mark.parametrize("name", FAMILY)
def test_summary_matches_the_data(name, saved_html, published):
    found = FAMILY[name](saved_html, published)
    assert check_page.blocking(found) == [], f"{name}: {check_page.blocking(found)[:5]}"
