"""Known activity on the saved page (test strategy D2, family 5): each card's and table row's operating finding and
its applicability, against the institutes' outputs and their credited sources.

The check is check_page.py's own, with its expectations from the data; here it reads the saved, pre-rendered page
once instead of the hydrated page in the browser gate. Its parity on both pages, today and with planted defects,
was shown before the browser copy went."""

import pytest

from scripts import check_page

FAMILY = {
    "operating output": lambda html, data: check_page.check_operating_output(html, data),
}


@pytest.mark.parametrize("name", FAMILY)
def test_activity_matches_the_data(name, saved_html, published):
    found = FAMILY[name](saved_html, published)
    assert check_page.blocking(found) == [], f"{name}: {check_page.blocking(found)[:5]}"
