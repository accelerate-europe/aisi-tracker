"""D1: changing owner copy is allowed; empty names, missing tooltips and wrong state still block."""


from bs4 import BeautifulSoup
import pytest

from scripts import check_page


def soup(html):
    return BeautifulSoup(html, "html.parser")


@pytest.fixture
def network():
    return soup('<table><tbody id="world-rows"><tr data-j="XX"><td data-col="net"><span class="nonmember" data-tip="A reworded explanation" tabindex="0">Not a member</span></td></tr></tbody></table>'), {"jurisdictions": [{"id": "XX", "memberships": []}]}


def test_network_tooltip_allows_rewording_but_keeps_membership(network):
    page, data = network
    assert check_page.check_network_cells(page, data) == []
    data["jurisdictions"][0]["memberships"] = [{"body": "intl-network"}]
    assert any("disagrees" in error for error in check_page.check_network_cells(page, data))


def test_renamed_card_fields_still_compare_their_citations_with_the_table():
    data = {"sources": [{"id": "one", "url": "https://example.org/one"}, {"id": "two", "url": "https://example.org/two"}], "jurisdictions": [{"id": "XX", "status": "planned"}], "institutes": []}
    cite = '<sup class="cites"><a class="cn" data-source-id="one" href="https://example.org/one">1</a></sup>'
    html = f'<article class="card" data-j="XX"><div class="row" data-track="9"><div class="k">Reworded field</div><div class="v">10<span class="qualifiers">Qualification</span>{cite}</div></div><ol class="srcs"><li data-source-id="one"><a href="https://example.org/one">One</a></li><li data-source-id="two"><a href="https://example.org/two">Two</a></li></ol></article><table><tbody id="world-rows"><tr data-j="XX"><td data-col="status"><span class="smark"><i class="sw"></i>Planned</span></td><td data-col="budget">10{cite}</td><td data-col="op"><span class="missing na"><span aria-hidden="true">–</span><span class="vh">Not applicable</span></span></td></tr></tbody></table>'
    assert check_page.check_source_alignment(html, data) == []
    broken = html.replace(cite, cite.replace('data-source-id="one"', 'data-source-id="two"').replace('example.org/one', 'example.org/two').replace('>1</a>', '>2</a>'), 1)
    errors = check_page.check_source_alignment(broken, data)
    assert 'XX: budget citations differ between card and table' in errors
    assert errors == ['XX: budget citations differ between card and table']



def test_funding_charts_share_only_the_sources_the_institute_cites():
    """Both charts use one records source list; the card keeps its own, longer list."""
    data = {"sources": [{"id": s, "url": f"https://example.org/{s}"} for s in ("one", "two", "three")],
            "jurisdictions": [{"id": "XX", "status": "established"}], "institutes": [{"id": "xx-i", "status": "established", "jurisdiction_id": "XX", "funding": [], "staff": []}]}
    card = ('<article class="card" data-j="XX"><ol class="srcs">' + "".join(f'<li data-source-id="{s}"><a href="https://example.org/{s}">{s}</a></li>' for s in ("one", "two", "three"))
            + '</ol></article>')
    def fund(cited, number, listed, staff_number=None):
        charts = "".join(
            f'<div class="fund-list" data-kind="{kind}"><ul class="fund-chart-rows"><li class="fund-item" data-j="XX" data-institute="xx-i">'
            f'<div class="fund-measure fund-{kind}"><div class="fund-value"><span class="fund-figure">10<span class="cites"><a class="cn" data-source-id="{cited}" href="https://example.org/{cited}">{shown}</a></span></span></div></div></li></ul></div>'
            for kind, shown in (("budget", number), ("staff", number if staff_number is None else staff_number))
        )
        return ('<div id="fund-rows">' + charts
                + '<div id="fund-records"><details class="fund-detail" data-j="XX" data-institute="xx-i"><summary>Funding and staff records</summary><div class="srcs"><ol>'
                + "".join(f'<li data-source-id="{s}"><a href="https://example.org/{s}">{s}</a></li>' for s in listed) + '</ol></div></details></div></div>')
    errors = lambda html: [e for e in check_page.check_source_alignment(html, data) if e.startswith("XX")]  # noqa: E731
    assert errors(card + fund("two", 1, ["two"])) == []
    assert "XX: funding source list is not exactly the sources its funding and staff cite" in errors(card + fund("two", 1, ["one", "two", "three"]))
    assert "XX: citation two differs from card source numbering" in errors(card + fund("two", 2, ["two"]))
    assert "XX: citation two differs from card source numbering" in errors(card + fund("two", 1, ["two"], staff_number=2))
