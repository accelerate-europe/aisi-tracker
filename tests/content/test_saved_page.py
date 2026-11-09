"""First content contract: the saved rendering, the authored version, and one card or row per jurisdiction.

Expectations come from the authored data only (never from display fields or the page itself)."""

from collections import Counter


def test_saved_page_is_prerendered_at_the_authored_version(saved_soup, authored):
    assert saved_soup.select_one("html[data-prerendered]") is not None
    expected_version = f"v{authored['dataset']['version']}"
    assert [link.get_text(strip=True) for link in saved_soup.select('#updated a[href="#changes"]')] == [expected_version]


def test_every_jurisdiction_has_its_card_and_its_row(saved_soup, authored):
    # A card for every jurisdiction in Europe's scope and for every one with an institute, exactly once.
    carded = Counter(j["id"] for j in authored["jurisdictions"] if j.get("europe_scope") or j.get("institute_id"))
    cards = Counter(card["data-j"] for card in saved_soup.select("article.card[data-j], .card.mini[data-j]"))
    assert cards == carded
    # One world-table row for every state and territory (the EU, a supranational body, has its own entry instead).
    tabled = Counter(j["id"] for j in authored["jurisdictions"] if j["kind"] != "supranational")
    rows = Counter(row["data-j"] for row in saved_soup.select("#world-rows tr[data-j], #world-rest tr[data-j]"))
    assert rows == tabled
