"""Record rows on the saved page (test strategy D2, family 3): Head, Reports to and Partners rows, the Testing and
access groups and the Institutional set-up groups, against the data.

The checks are check_page.py's own, with their expectations from the data; here they read the saved, pre-rendered
page once instead of the hydrated page in the browser gate. Their parity on both pages, today and with planted
defects, was shown before the browser copy went."""

import pytest

from scripts import check_page

FAMILY = {
    "leadership rows": lambda html, data: check_page.check_leadership_rows(html, data),
    "access groups": lambda html, data: check_page.check_access_groups(check_page.BeautifulSoup(html, "html.parser"), data),
    "set-up groups": lambda html, data: check_page.check_setup_groups(check_page.BeautifulSoup(html, "html.parser"), data),
}


@pytest.mark.parametrize("name", FAMILY)
def test_record_rows_match_the_data(name, saved_html, published):
    found = FAMILY[name](saved_html, published)
    assert check_page.blocking(found) == [], f"{name}: {check_page.blocking(found)[:5]}"
