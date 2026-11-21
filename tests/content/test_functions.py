"""Functions grid, its summary and both saved forms, against the published data."""

import pytest

from scripts import check_page

FAMILY = {
    "desktop grid and summary": lambda html, data: check_page.functions_structure(html, data),
    "saved forms": lambda html, data: check_page.functions_forms_structure(html, data),
}


@pytest.mark.parametrize("name", FAMILY)
def test_functions_match_the_data(name, saved_html, published):
    found = FAMILY[name](saved_html, published)
    assert found == [], f"{name}: {found[:5]}"
