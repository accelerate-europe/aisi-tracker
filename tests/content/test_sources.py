"""Sources on the saved page (test strategy D2, family 2): each card's source list against the records' sources, and
the generic-source wording that names what a source is.

The checks are check_page.py's own, with their expectations from the data; here they read the saved, pre-rendered
page once instead of the hydrated page in the browser gate. Their parity on both pages, today and with planted
defects, was shown before the browser copy went."""

import pytest

from scripts import check_page

FAMILY = {
    "source alignment": lambda html, data: check_page.check_source_alignment(html, data),
    "generic sources": lambda html, data: check_page.check_generic_sources(html, data),
}


@pytest.mark.parametrize("name", FAMILY)
def test_sources_match_the_data(name, saved_html, published):
    found = FAMILY[name](saved_html, published)
    assert check_page.blocking(found) == [], f"{name}: {check_page.blocking(found)[:5]}"
