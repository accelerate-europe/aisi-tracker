"""Staff totals on the saved page (test strategy D2, family 7): each chart row's staff figure against the hand-worked
entries in tests/fixtures/staff_figures.json, read from the supporting quotations, never from display.total_staff.

The check is check_page.py's own; here it reads the saved, pre-rendered page once instead of the hydrated page in
the browser gate. Its parity on both pages, today and with planted defects, was shown before the browser copy went
(see the commit)."""

import pytest

from scripts import check_page

FAMILY = {
    "staff totals": lambda html, data: check_page.check_staff_figures(check_page.BeautifulSoup(html, "html.parser"), data),
}


@pytest.mark.parametrize("name", FAMILY)
def test_staff_totals_match_the_reviewed_figures(name, saved_html, published):
    found = FAMILY[name](saved_html, published)
    assert check_page.blocking(found) == [], f"{name}: {check_page.blocking(found)[:5]}"
