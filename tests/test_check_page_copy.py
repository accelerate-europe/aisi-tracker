"""D1: changing owner copy is allowed; empty names, missing tooltips and wrong state still block."""

import json
from types import SimpleNamespace

from bs4 import BeautifulSoup
import pytest

from scripts import check_page


def soup(html):
    return BeautifulSoup(html, "html.parser")


@pytest.fixture
def downloads():
    groups = []
    files = set()
    for kind, names in check_page.DOWNLOAD_GROUPS:
        rows = []
        for name in names:
            files.add("data/" + name)
            rows.append(f'<li><a href="data/{name}"><span class="dl-title">A useful file</span><span class="dl-file">{name}</span></a><span class="d">Description</span></li>')
        groups.append(f'<div class="dlgroup"><h4>Reworded group <span class="label">{kind}</span></h4><ul class="dlist">{"".join(rows)}</ul></div>')
    return soup('<div class="downloads">' + "".join(groups) + '</div>'), files


def test_download_groups_keep_file_contracts_and_nonempty_names(downloads):
    page, files = downloads
    assert check_page.check_download_groups(page, files) == []
    page.select_one(".dl-title").string = " \n "
    assert "download link label is missing or empty" in check_page.blocking(check_page.check_download_groups(page, files))


@pytest.mark.parametrize("replacement", ["", '<h4> <span class="label">CSV</span></h4>'])
def test_download_group_heading_cannot_be_missing_or_only_a_file_type(downloads, replacement):
    page, files = downloads
    page.select_one("h4").replace_with(soup(replacement))
    assert "download group heading is missing or empty" in check_page.blocking(check_page.check_download_groups(page, files))


def test_download_file_cannot_be_changed_while_rewording_labels(downloads):
    page, files = downloads
    page.select_one(".dl-file").string = "another.csv"
    assert any("contract" in error for error in check_page.blocking(check_page.check_download_groups(page, files)))


@pytest.fixture
def network():
    return soup('<table><tbody id="world-rows"><tr data-j="XX"><td data-col="net"><span class="nonmember" data-tip="A reworded explanation" tabindex="0">Not a member</span></td></tr></tbody></table>'), {"jurisdictions": [{"id": "XX", "memberships": []}]}


def test_network_tooltip_allows_rewording_but_keeps_membership(network):
    page, data = network
    assert check_page.check_network_cells(page, data) == []
    data["jurisdictions"][0]["memberships"] = [{"body": "intl-network"}]
    assert any("disagrees" in error for error in check_page.blocking(check_page.check_network_cells(page, data)))


@pytest.mark.parametrize(("attribute", "value"), [("data-tip", " \n "), ("data-tip", None), ("tabindex", None), ("tabindex", "-1")])
def test_network_tooltip_cannot_be_empty_or_unfocusable(network, attribute, value):
    page, data = network
    node = page.select_one(".nonmember")
    if value is None:
        del node[attribute]
    else:
        node[attribute] = value
    assert any("focusable tooltip" in error for error in check_page.blocking(check_page.check_network_cells(page, data)))


@pytest.fixture
def absence():
    values = '<span class="missing" data-tip="Unknown figure explanation" tabindex="0">Not published</span><span class="smark" data-state="not_confirmed" data-tip="Search finding explanation" tabindex="0">None found yet</span>'
    heading = '<div class="statusgroup" data-status="none_identified"><h3><span class="smark" data-tip="Status explanation" tabindex="0">None identified</span></h3></div>'
    dash = '<article class="card"><span class="missing na" data-tip="Structural absence explanation">–</span></article>'
    headers = ''.join(f'<th data-k="{key}"><button data-tip="Column explanation">Column</button></th>' for key in check_page.DASH_COLUMNS)
    return soup(f'<div id="entries">{values}{heading}{dash}</div><div id="world-cards">{values}</div><table class="world"><thead><tr>{headers}</tr></thead></table>')


def test_absence_definitions_allow_rewording_and_retired_variants(absence):
    assert check_page.check_absence_wording(absence) == []
    absence.append(soup('<p>Not available. No data. Unknown. Not stated.</p>'))
    assert check_page.check_absence_wording(absence) == []
    absence.select_one(".missing:not(.na)").string = "Unknown"
    assert any("not Not published" in error for error in check_page.blocking(check_page.check_absence_wording(absence)))


@pytest.mark.parametrize("selector", ['#entries .missing:not(.na)', '#world-cards .missing', '#entries .smark[data-state]', '#world-cards .smark', '#entries h3 .smark', '#entries .missing.na', *[f'th[data-k="{key}"] button' for key in check_page.DASH_COLUMNS]])
@pytest.mark.parametrize("value", [None, " \n "])
def test_each_absence_tooltip_must_exist_and_be_nonempty(absence, selector, value):
    node = absence.select_one(selector)
    if value is None:
        del node['data-tip']
    else:
        node['data-tip'] = value
    assert check_page.blocking(check_page.check_absence_wording(absence))


@pytest.mark.parametrize("selector", ['#entries .missing:not(.na)', '#world-cards .missing', '#entries .smark[data-state]', '#world-cards .smark', '#entries h3 .smark'])
def test_absence_terms_remain_focusable(absence, selector):
    del absence.select_one(selector)['tabindex']
    assert check_page.blocking(check_page.check_absence_wording(absence))


def test_source_alignment_missing_tooltip_still_blocks():
    data = {"sources": [], "jurisdictions": [], "institutes": []}
    html = '<span class="missing" data-tip="Reworded explanation">Not published</span>'
    assert check_page.check_source_alignment(html, data) == []
    assert "missing-value tooltip loses the null definition" in check_page.blocking(check_page.check_source_alignment(html.replace('data-tip="Reworded explanation"', 'data-tip=" "'), data))


@pytest.mark.parametrize("key", ["law", "agreed"])
def test_access_glossary_definitions_are_nonempty_and_focusable(key):
    if key == "law":
        access = {"value": "by_law", "legal_power": {"act": "Regulation (EU) 2024/1689", "provision": "Article 92(3)", "citations": {"provision": ["source"], "conditions": []}}}
        agreements = []
        note = '<abbr class="g" data-exp="A reworded law explanation" tabindex="0">AI Act Art. 92(3)</abbr>'
    else:
        access = {"value": "access_agreed", "developer_agreements": [{"agreement_id": "agreement", "citations": {"agreement_id": []}}]}
        agreements = [{"id": "agreement", "scope": "Memorandum of understanding", "counterparty": "Developer", "date": "2026-01", "citations": {"counterparty": ["source"], "date": [], "scope": []}}]
        note = 'Developer <abbr class="g" data-exp="A reworded agreement explanation" tabindex="0">MOU</abbr>, Jan 2026'
    institute = {"jurisdiction_id": "XX", "model_access": access, "agreements": agreements, "testing_capacity": {"state": "not_published"}}
    data = {"institutes": [institute], "jurisdictions": [{"id": "XX", "name": "Example", "status": "established"}]}
    html = f'<div id="c-xx"></div><div id="access"><div class="groups-short"><div class="group-block" data-group="{key}"><h3>Reworded group</h3><div class="marks" role="img" aria-label="1 institute: Example, established"><i class="sw st-established" data-tip="Example" data-tip-sub="Established"></i></div><ul class="group-list"><li data-state="XX"><a href="#c-xx">Example</a><span class="group-note">{note}<span class="cites"><a class="cn" data-source-id="source">1</a></span></span></li></ul></div></div></div>'
    assert check_page.check_access_groups(soup(html), data) == []
    for attribute, value in [("data-exp", " \n "), ("tabindex", "-1")]:
        page = soup(html)
        page.select_one("abbr")[attribute] = value
        assert any("tooltips" in error for error in check_page.blocking(check_page.check_access_groups(page, data)))


def test_privileged_heading_keeps_term_tooltip_and_focus():
    html = '<div id="access"><div class="group-block" data-group="privileged"><h3><abbr class="g" data-exp="Reworded definition" tabindex="0">Early access</abbr></h3></div></div>'
    data = {"institutes": [], "jurisdictions": []}
    errors = lambda page: [error for error in check_page.blocking(check_page.check_access_groups(page, data)) if "heading lacks" in error]  # noqa: E731
    assert errors(soup(html)) == []
    for attribute, value in [("data-exp", " "), ("tabindex", "-1")]:
        page = soup(html)
        page.select_one("abbr")[attribute] = value
        assert errors(page)
    page = soup(html)
    page.select_one("abbr").string = " "
    assert errors(page)


@pytest.mark.parametrize("tip", ["Reworded minimum-width explanation", "", None, " \n "])
def test_funding_bar_tooltip_is_required_without_fixed_words(tip):
    results = iter([{"key": ["adopted", "announced"], "clear": True, "solid": ["solid", "solid"], "hatch": ["hatch", "hatch"], "tips": [tip]}, []])
    page = SimpleNamespace(evaluate=lambda script: next(results))
    assert bool(check_page.blocking(check_page.check_funding_key(page))) == (not (tip or "").strip())


@pytest.mark.parametrize("mini", [False, True])
def test_state_download_names_and_tips_allow_rewording_but_keep_contract(tmp_path, mini):
    path = tmp_path / 'api/v1/jurisdictions/XX.json'
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"record": {"id": "XX"}}))
    data = {"dataset": {"version": "1.0.0"}}
    html = f'<article class="card {"mini" if mini else ""}" data-j="XX"><div class="{"foot" if mini else "top"}"><a class="state-dl i-dl" href="api/v1/jurisdictions/XX.json" download="XX.json" data-tip="Download this record"><span class="vh">Get this record</span></a></div></article>'
    assert check_page.check_state_download_buttons(html, tmp_path, data) == []
    for old, new in [('Download this record', ' '), ('Get this record', ' '), ('download="XX.json"', 'download="YY.json"'), ('i-dl', 'other')]:
        assert check_page.blocking(check_page.check_state_download_buttons(html.replace(old, new), tmp_path, data))


@pytest.mark.parametrize("active", ["light", "dark"])
@pytest.mark.parametrize("system", [False, True])
def test_theme_name_says_the_chosen_mode_in_any_wording(active, system):
    scheme, mode = ("light dark", "system") if system else (active, active)
    paint = check_page.GROUNDS[active]
    assert check_page.theme_state_matches([scheme, paint, f"Currently {mode.upper()}"], scheme, active, mode)
    if system:  # today's page: system mode, painted in whatever the system chose
        assert check_page.theme_state_matches([scheme, paint, "Theme: follows system"], scheme, active, mode)
    for label in (None, "", "light" if mode == "dark" else "dark"):
        assert not check_page.theme_state_matches([scheme, paint, label], scheme, active, mode)
    assert not check_page.theme_state_matches([scheme, "wrong paint", mode], scheme, active, mode)
    assert not check_page.theme_state_matches(["wrong scheme", paint, mode], scheme, active, mode)



def test_renamed_card_fields_still_compare_their_citations_with_the_table():
    data = {"sources": [{"id": "one", "url": "https://example.org/one"}, {"id": "two", "url": "https://example.org/two"}], "jurisdictions": [{"id": "XX", "status": "planned"}], "institutes": []}
    cite = '<sup class="cites"><a class="cn" data-source-id="one" href="https://example.org/one">1</a></sup>'
    html = f'<article class="card" data-j="XX"><div class="row" data-track="9"><div class="k">Reworded field</div><div class="v">10<span class="qualifiers">Qualification</span>{cite}</div></div><ol class="srcs"><li data-source-id="one"><a href="https://example.org/one">One</a></li><li data-source-id="two"><a href="https://example.org/two">Two</a></li></ol></article><table><tbody id="world-rows"><tr data-j="XX"><td data-col="status"><span class="smark"><i class="sw"></i>Planned</span></td><td data-col="budget">10{cite}</td><td data-col="op"><span class="missing na"><span aria-hidden="true">–</span><span class="vh">Not applicable</span></span></td></tr></tbody></table>'
    assert check_page.check_source_alignment(html, data) == []
    broken = html.replace(cite, cite.replace('data-source-id="one"', 'data-source-id="two"').replace('example.org/one', 'example.org/two').replace('>1</a>', '>2</a>'), 1)
    errors = check_page.blocking(check_page.check_source_alignment(broken, data))
    assert 'XX: budget citations differ between card and table' in errors
    assert errors == ['XX: budget citations differ between card and table']



@pytest.mark.parametrize("label, mode, ok", [
    ("Theme: light", "light", True), ("Theme: daylight", "light", False),  # a whole word, not part of one
    ("Theme: system / light / dark", "dark", True),  # matches here; the cycle check rejects names that say every mode
])
def test_theme_mode_is_a_whole_word(label, mode, ok):
    paint = check_page.GROUNDS["dark" if mode == "dark" else "light"]
    scheme = "dark" if mode == "dark" else "light"
    assert check_page.theme_state_matches([scheme, paint, label], scheme, "dark" if mode == "dark" else "light", mode) is ok
