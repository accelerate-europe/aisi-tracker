"""Names and navigation on the saved page (test strategy D2, family 6): the section bar and group rows, institute
and former names, and the none-identified folds, against the data. The page is read without its template's inert
copy (tests/content/page.py); before that, the section bar counted the template's group rows too.

The checks are check_page.py's own, with their expectations from the data; here they read the saved, pre-rendered
page once instead of the hydrated page in the browser gate. Their parity on both pages, today and with planted
defects, was shown before the browser copy went."""

import pytest

from scripts import check_page

FAMILY = {
    "section bar": lambda html, data: check_page.check_section_bar(check_page.BeautifulSoup(html, "html.parser")),
    "institute names": lambda html, data: check_page.check_institute_names(html, data),
    "former names": lambda html, data: check_page.check_former_names(html, data),
    "none-identified folds": lambda html, data: check_page.check_nonidentified_folds(html, data),
}


@pytest.mark.parametrize("name", FAMILY)
def test_names_match_the_data(name, saved_html, published):
    found = FAMILY[name](saved_html, published)
    assert check_page.blocking(found) == [], f"{name}: {check_page.blocking(found)[:5]}"
