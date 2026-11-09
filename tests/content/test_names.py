"""Names on the saved page (test strategy D2, family 6): institute and former names and the none-identified folds,
against the data. The section bar stays in the browser gate: the saved page also holds the page template (the copy
the script hydrates from), whose group rows a parser counts too, so its parity failed (6 group rows against 3).

The checks are check_page.py's own, with their expectations from the data; here they read the saved, pre-rendered
page once instead of the hydrated page in the browser gate. Their parity on both pages, today and with planted
defects, was shown before the browser copy went."""

import pytest

from scripts import check_page

FAMILY = {
    "institute names": lambda html, data: check_page.check_institute_names(html, data),
    "former names": lambda html, data: check_page.check_former_names(html, data),
    "none-identified folds": lambda html, data: check_page.check_nonidentified_folds(html, data),
}


@pytest.mark.parametrize("name", FAMILY)
def test_names_match_the_data(name, saved_html, published):
    found = FAMILY[name](saved_html, published)
    assert check_page.blocking(found) == [], f"{name}: {check_page.blocking(found)[:5]}"
