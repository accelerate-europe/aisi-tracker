"""Claim sentences and their closed quote disclosures on the saved page."""

import pytest

from scripts import check_page

FAMILY = {"card claims": lambda html, data: check_page.check_claim_cards(html, data)}


@pytest.mark.parametrize("name", FAMILY)
def test_claims_match_the_data(name, saved_html, published):
    assert FAMILY[name](saved_html, published) == []
