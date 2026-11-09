import re
import subprocess
import sys
import json
from collections import Counter
from pathlib import Path

from bs4 import BeautifulSoup
import pytest

from conftest import ROOT, SOURCE_URL
from scripts import check_page


def test_definition_link_allows_rewording_but_requires_one_reachable_target():
    data = {"dataset": {"definition": "Example.", "status_definitions": {}}}
    html = '<div id="headline"></div><p id="definition-link"><a href="#definition">Read the definition</a></p><section id="method-sec"><div class="section-body"><p id="definition">Example. <a href="method.html#qualifying-body">What counts</a></p></div></section>'
    assert check_page.check_supporting_text(BeautifulSoup(html, "html.parser"), data) == []
    for broken in (
        html.replace('id="definition"', 'id="gone"'),
        html.replace('href="#definition"', 'href="#gone"'),
        html.replace('Read the definition</a>', 'Read the definition</a><a href="#definition">Again</a>'),
        html.replace('id="definition-link"', 'id="gone"'),
        html.replace('Read the definition</a>', ' \n </a>'),
    ):
        assert any("one link to an existing #definition" in error for error in check_page.blocking(check_page.check_supporting_text(BeautifulSoup(broken, "html.parser"), data)))


def test_definition_rule_link_allows_rewording_but_requires_text_and_target():
    data = {"dataset": {"definition": "Example.", "status_definitions": {}}}
    html = '<div id="headline"></div><p id="definition-link"><a href="#definition">Read more</a></p><section id="method-sec"><div class="section-body"><p id="definition">Example. <a href="method.html#qualifying-body">See the criteria</a></p></div></section>'
    assert check_page.check_supporting_text(BeautifulSoup(html, "html.parser"), data) == []
    for broken in (
        html.replace("See the criteria", " \n "),
        html.replace('<a href="method.html#qualifying-body">See the criteria</a>', ""),
        html.replace("method.html#qualifying-body", "method.html#other"),
    ):
        assert any("link does not go" in error for error in check_page.blocking(check_page.check_supporting_text(BeautifulSoup(broken, "html.parser"), data)))


def test_section_headings_allow_rewording_but_not_missing_or_empty_headings():
    html = "".join(f'<h2 id="{key}">Reworded heading</h2>' for key in check_page.SECTION_HEADINGS)

    def errors(markup):
        return [e for e in check_page.blocking(check_page.check_section_bar(BeautifulSoup(markup, "html.parser"))) if e.startswith("heading #")]

    assert errors(html) == []
    for key in check_page.SECTION_HEADINGS:
        for replacement in ("", f'<h2 id="{key}"> \n </h2>'):
            assert errors(html.replace(f'<h2 id="{key}">Reworded heading</h2>', replacement)) == [f"heading #{key} is missing or empty"]


@pytest.mark.parametrize("key", ["privileged", "law", "agreed", "none"])
def test_access_group_headings_allow_rewording_but_not_missing_or_empty_headings(key):
    data = {"institutes": [], "jurisdictions": []}
    html = f'<div id="access"><div class="group-block" data-group="{key}"><h3>Reworded heading</h3></div></div>'

    def errors(markup):
        return [e for e in check_page.blocking(check_page.check_access_groups(BeautifulSoup(markup, "html.parser"), data)) if "heading is missing or empty" in e]

    assert errors(html) == []
    for replacement in ("", "<h3> \n </h3>"):
        assert errors(html.replace("<h3>Reworded heading</h3>", replacement)) == [f"access group {key} heading is missing or empty"]


def test_legend_definitions_allow_rewording_but_require_every_status_once():
    statuses = ("established", "planned", "proposed", "exploring", "none_identified")
    data = {"dataset": {"status_definitions": dict.fromkeys(statuses, "Full definition."), "definition": "Example.", "version": "1.0.0"}, "jurisdictions": [], "institutes": [], "changelog": [{"date": "2026-11-09"}]}
    entries = {status: f'<div class="legend-entry" data-status="{status}"><i class="sw st-{status}"></i><abbr class="g legend-name" data-exp="Reworded definition.">{status}</abbr></div>' for status in statuses}
    html = '<div id="legend">' + "".join(entries.values()) + '</div>'

    def errors(markup):
        return [e for e in check_page.blocking(check_page.check_summary_data(markup, data)) if e.startswith("legend")]

    assert errors(html) == []
    for status, entry in entries.items():
        for replacement in ("", entries["planned" if status == "established" else "established"], entry.replace(f'data-status="{status}"', 'data-status="unknown"')):
            assert "legend must have one entry per dataset status" in errors(html.replace(entry, replacement))
        for replacement in ('data-exp=" \n "', ""):
            assert errors(html.replace(entry, entry.replace('data-exp="Reworded definition."', replacement))) == [f"legend name lacks a non-empty definition tooltip: {status}"]


@pytest.fixture
def rendered(tmp_path):
    dist = tmp_path / "dist"
    (dist / "data").mkdir(parents=True)
    (dist / "data/aisi.json").write_text("{}")
    (dist / "data/jurisdictions.csv").write_text("id\nGB\n")
    data = {"jurisdictions": [
        {"id": "GB", "kind": "state", "europe_scope": True, "status": "established"},
        {"id": "EU", "kind": "supranational", "europe_scope": False, "status": "planned"},
        {"id": "FR", "kind": "state", "europe_scope": True, "status": "none_identified"},
        {"id": "AU", "kind": "state", "europe_scope": False, "status": "none_identified"},
    ], "sources": [{"id": "source", "url": SOURCE_URL}]}
    html = f"""<!doctype html><html><body>
      <nav><a href="#world">Worldwide</a></nav><h2 id="world">Worldwide</h2>
      <article class="card" data-j="GB"><span class="smark"><i class="st-established"></i>Established</span></article>
      <article class="card" data-j="EU"><span class="smark"><i class="st-planned"></i>Planned</span></article>
      <details class="card mini" data-j="FR">None identified</details>
      <input id="map-search" placeholder="Find a state" aria-label="Find a state"><ul id="map-search-list" aria-label="States and territories"></ul>
      <input id="world-filter" placeholder="Search state or institute" aria-label="Search state or institute">
      <span id="world-count">Showing 1 of 3</span>
      <table class="world"><tbody id="world-rows"><tr><td>GB</td></tr></tbody></table>
      <span class="cites"><a class="cn" href="{SOURCE_URL}">1</a></span>
      <ul class="dlist" id="dl-a"><li><a href="data/jurisdictions.csv">CSV</a></li></ul>
      <ul class="dlist" id="dl-b"><li><a href="data/aisi.json">JSON</a></li></ul>
      <footer><div id="foot">Code: MIT; data, page text and documentation: CC BY 4.0
      <p>Quotations from sources remain with their rights holders and are reproduced as quotations; the licence covers our own compilation, text and data.</p></div></footer>
      <script>var value = undefined; var other = NaN;</script><style>.undefined {{ color: red; }}</style>
      </body></html>"""
    return BeautifulSoup(html, "html.parser"), data, dist


def errors_for(rendered):
    soup, data, dist = rendered
    return check_page.check_dom(str(soup), data, dist, "http://127.0.0.1:8000/")


def test_rendered_invariants_pass(rendered):
    assert errors_for(rendered) == []


@pytest.mark.parametrize(("selector", "message"), [
    ("article.card", "status cards"), (".card.mini", "mini-cards"),
    (".smark", "status mark"), ("#world-rows tr", "world table"),
    (".cn", "citation marks missing"), ("nav", "navigation links missing"),
    ("#dl-b li", "downloads"),
    ("footer", "footer: missing quotations licence notice"),
])
def test_missing_elements_fail(rendered, selector, message):
    rendered[0].select_one(selector).decompose()
    assert any(message in e for e in errors_for(rendered))


def test_an_accessible_name_from_a_referenced_label_counts(rendered):
    soup = rendered[0]
    box = soup.select_one("#map-search-list")
    box.attrs.pop("aria-label", None)
    box["aria-labelledby"] = "list-label"
    label = soup.new_tag("span", id="list-label", attrs={"aria-label": "Places"})  # named by its attribute, no text
    box.insert_before(label)
    assert errors_for(rendered) == []
    label["aria-label"] = ""
    image = soup.new_tag("img", alt="Places")
    label.append(image)  # named by an image's alt text
    assert errors_for(rendered) == []


def test_a_label_names_only_form_controls(rendered):
    soup = rendered[0]
    box = soup.select_one("#map-search-list")
    for key in ("aria-label", "aria-labelledby", "title"):
        box.attrs.pop(key, None)
    label = soup.new_tag("label", attrs={"for": "map-search-list"})
    label.string = "Places"
    box.insert_before(label)
    assert "#map-search-list: no accessible name" in check_page.blocking(errors_for(rendered))  # a <label> cannot name a list
    field = soup.select_one("#world-filter")
    for key in ("aria-label", "aria-labelledby", "title"):
        field.attrs.pop(key, None)
    wrapper = soup.new_tag("label")
    wrapper.string = "Search"
    field.wrap(wrapper)
    assert "#world-filter: no accessible name" not in errors_for(rendered)  # a wrapping label names an input


def test_search_inputs_need_a_name_and_a_placeholder_not_fixed_wording(rendered):
    box = rendered[0].select_one("#world-filter")
    box["placeholder"] = "Search country or institute"
    assert errors_for(rendered) == []  # rewording is reviewed, not pinned
    box["placeholder"] = " "
    assert check_page.blocking(errors_for(rendered)) == ["#world-filter: no placeholder"]


def test_listbox_requires_an_accessible_name_without_fixed_wording(rendered):
    soup = rendered[0]
    box = soup.select_one("#map-search-list")
    box["aria-label"] = "Matching places"
    assert errors_for(rendered) == []
    for value in ("", " \n "):
        box["aria-label"] = value
        assert check_page.blocking(errors_for(rendered)) == ["#map-search-list: no accessible name"]
    del box["aria-label"]
    box["aria-labelledby"] = "list-label"
    label = soup.new_tag("span", id="list-label")
    label.string = "Places"
    box.insert_before(label)
    assert errors_for(rendered) == []
    label.decompose()
    assert check_page.blocking(errors_for(rendered)) == ["#map-search-list: no accessible name"]
    box["title"] = "Places"
    assert errors_for(rendered) == []
    box.decompose()
    assert check_page.blocking(errors_for(rendered)) == ["#map-search-list: no accessible name"]


@pytest.mark.parametrize(("selector", "message"), [
    ("article.card", "status cards"), (".card.mini", "mini-cards"),
    ("#world-rows tr", "world table"), ("#dl-b li", "downloads"),
])
def test_duplicate_elements_fail(rendered, selector, message):
    soup = rendered[0]
    node = soup.select_one(selector)
    node.insert_after(BeautifulSoup(str(node), "html.parser"))
    assert any(message in e for e in errors_for(rendered))


@pytest.mark.parametrize(("selector", "href", "message"), [
    (".cn", "#world", "not a source URL"),
    ("nav a", "#missing", "navigation target"),
    ("nav a", SOURCE_URL, "navigation target"),
    ("#dl-b a", "data/absent.json", "downloads"),
    ("#dl-b a", SOURCE_URL, "downloads"),
])
def test_broken_links_fail(rendered, selector, href, message):
    rendered[0].select_one(selector)["href"] = href
    assert any(message in e for e in errors_for(rendered))


def test_missing_citation_href_fails(rendered):
    del rendered[0].select_one(".cn")["href"]
    assert any("not a source URL" in e for e in errors_for(rendered))


@pytest.mark.parametrize("text", ["undefined", "NaN", "Budget: NaN million"])
def test_bad_rendered_text_fails(rendered, text):
    rendered[0].body.append(text)
    assert any("invalid rendered text" in e for e in errors_for(rendered))


def test_swapped_jurisdiction_cannot_hide_behind_card_count(rendered):
    rendered[0].select_one("article.card")["data-j"] = "AU"
    assert any("status cards" in e for e in errors_for(rendered))


def test_extra_download_file_needs_entry(rendered):
    (rendered[2] / "data/datapackage.json").write_text("{}")
    assert any("downloads" in e for e in errors_for(rendered))


def test_empty_dom_fails(rendered):
    _, data, dist = rendered
    assert len(check_page.check_dom("", data, dist, "http://127.0.0.1/")) >= 5


def test_chrome_is_the_locked_playwright_chromium_or_missing(monkeypatch, tmp_path):
    """Only the locked Playwright's own Chromium counts, never a browser on PATH (a snap browser escapes a job's memory cap)."""
    import sys
    import types
    from contextlib import contextmanager

    chrome = tmp_path / "chromium-1243" / "chrome"

    @contextmanager
    def sync_playwright():
        yield types.SimpleNamespace(chromium=types.SimpleNamespace(executable_path=str(chrome)))

    module = types.ModuleType("playwright.sync_api")
    module.sync_playwright = sync_playwright
    monkeypatch.setitem(sys.modules, "playwright", types.ModuleType("playwright"))
    monkeypatch.setitem(sys.modules, "playwright.sync_api", module)
    monkeypatch.setenv("PATH", str(tmp_path))
    (tmp_path / "chromium").write_text("#!/bin/sh\n")
    (tmp_path / "chromium").chmod(0o755)
    assert check_page.find_chrome() is None  # not installed: a Chromium on PATH does not stand in
    chrome.parent.mkdir()
    chrome.write_text("#!/bin/sh\n")
    chrome.chmod(0o755)
    assert check_page.find_chrome() == str(chrome)


def test_build_gate_and_capture_lock_one_playwright():
    """prerender.py (the build) and check_page.py (gate and contact sheet) pin the same Playwright, so one browser."""
    import tomllib

    pins = {}
    for lock in sorted((ROOT / "scripts").glob("*.py.lock")):
        packages = {p["name"]: p["version"] for p in tomllib.loads(lock.read_text(encoding="utf-8")).get("package", [])}
        if "playwright" in packages:
            pins[lock.name] = packages["playwright"]
    assert {"check_page.py.lock", "prerender.py.lock"} <= set(pins)
    assert len(set(pins.values())) == 1, pins


@pytest.mark.parametrize("ci", [None, "true"])
def test_missing_chrome_is_not_run_never_a_pass(monkeypatch, capsys, ci):
    if ci is None:
        monkeypatch.delenv("CI", raising=False)
    else:
        monkeypatch.setenv("CI", ci)
    monkeypatch.setattr(check_page, "find_chrome", lambda: None)
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: pytest.fail("No browser means no subprocess"))
    assert check_page.main() == check_page.NOT_RUN != 0
    assert "NOT RUN:" in capsys.readouterr().err


def test_missing_build_fails_and_never_builds(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(check_page, "find_chrome", lambda: "chrome")
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: pytest.fail("The page check never builds"))
    assert check_page.main(tmp_path / "absent") == 1
    assert "build first" in capsys.readouterr().err
    assert not (tmp_path / "absent").exists()


@pytest.mark.parametrize("failure", [
    subprocess.CalledProcessError(1, ["chrome"], stderr="Browser failed"),
    subprocess.TimeoutExpired(["chrome"], 45),
])
def test_browser_failure_is_not_a_skip(rendered, monkeypatch, capsys, failure):
    _, data, dist = rendered
    (dist / "data/aisi.json").write_text(json.dumps(data))
    (dist / "index.html").write_text("Built page")
    monkeypatch.setattr(check_page, "find_chrome", lambda: "chrome")
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: pytest.fail("The page check never builds"))

    def fail(*args):
        raise failure

    monkeypatch.setattr(check_page, "render", fail)
    assert check_page.main(dist) == 1
    assert "error:" in capsys.readouterr().err


def test_render_reads_the_hydrated_page_and_reports_browser_errors(tmp_path, monkeypatch):
    """render() reads the page once its script has taken over and the network is quiet, in a session that serves
    only this dist; a browser error is reported as a page-check failure (RuntimeError), never a crash or a skip."""
    import sys
    import types
    from contextlib import contextmanager

    class PlaywrightError(Exception):
        pass

    module = types.ModuleType("playwright.sync_api")
    module.Error = PlaywrightError
    monkeypatch.setitem(sys.modules, "playwright", types.ModuleType("playwright"))
    monkeypatch.setitem(sys.modules, "playwright.sync_api", module)
    calls = []

    class Page:
        def goto(self, url):
            calls.append(("goto", url))

        def wait_for_function(self, script, timeout):
            calls.append(("hydrated", script))

        def wait_for_load_state(self, state):
            calls.append(("load", state))

        def content(self):
            return "<html>Rendered</html>"

    @contextmanager
    def session(chrome, dist):
        calls.append(("session", chrome, dist))
        yield types.SimpleNamespace(new_page=Page), "http://127.0.0.1:1234/"

    monkeypatch.setattr(check_page, "chrome_session", session)
    assert check_page.render("chrome", tmp_path) == ("<html>Rendered</html>", "http://127.0.0.1:1234/")
    assert calls[0] == ("session", "chrome", tmp_path) and ("load", "networkidle") in calls
    assert any(c[0] == "hydrated" and "data-prerendered" in c[1] for c in calls)

    def broken(self, url):
        raise PlaywrightError("Target closed")

    monkeypatch.setattr(Page, "goto", broken)
    with pytest.raises(RuntimeError, match="rendering the page failed: Target closed"):
        check_page.render("chrome", tmp_path)
    assert RuntimeError in check_page.CAUGHT


def test_every_browser_starts_without_a_session_bus(monkeypatch):
    """Chromium with a session bus moves its browser process into a systemd scope of its own, out of a job's memory cap."""
    monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", "unix:path=/run/user/1000/bus")
    assert check_page.browser_env()["DBUS_SESSION_BUS_ADDRESS"] == "disabled:"
    source = (ROOT / "scripts/check_page.py").read_text(encoding="utf-8") + (ROOT / "scripts/prerender.py").read_text(encoding="utf-8")
    launches = re.findall(r"chromium\.launch(?:_persistent_context)?\((?:[^()]|\([^()]*\))*\)", source, flags=re.S)
    assert len(launches) == 3 and all("env=browser_env()" in call for call in launches), launches



def test_budget_oracle_rejects_wrong_id_and_missing_value_structure():
    data = {"jurisdictions": [{"id": "JP", "status": "established"}], "institutes": [{"id": "jp-aisi", "jurisdiction_id": "JP", "funding": []}]}
    identity = "funding-7d0b36a48cc65315865a6d6ec83ddeb6"
    value = f'<span class="budget-value" data-funding-id="{identity}"><span class="budget-amount">~EUR 52m (JPY 8.8bn)</span><span class="budget-qualifiers">one-off · FY2025 supplementary · adopted</span></span>'
    html = f'<article class="card" data-j="JP"><div class="row" data-track="9"><div class="k">Budget</div><div class="v">{value}</div></div></article><table><tbody id="world-rows"><tr data-j="JP"><td data-col="budget">{value}</td></tr></tbody></table>'
    assert check_page.check_budget_output(html, data) == []
    assert any("wrong funding ID" in error for error in check_page.check_budget_output(html.replace(identity, "older-record"), data))
    assert any("separate amount and qualifiers" in error for error in check_page.check_budget_output(html.replace("budget-qualifiers", "removed"), data))
    assert any("latest own JPY 8.8bn" in error for error in check_page.check_budget_output(html.replace("~EUR 52m (JPY 8.8bn)", "~EUR 2.2m (JPY 380m)"), data))
    assert any("latest own JPY 8.8bn" in error for error in check_page.check_budget_output(html.replace("~EUR 52m (JPY 8.8bn)", "JPY 8.8bn"), data))


def test_budget_oracle_covers_institutes_before_establishment():
    """Norway's proposed institute shows its selected NOK 30m record; Not published there is an error."""
    identity = check_page.EXPECTED_BUDGET_IDS["no-ki-norge"]
    data = {"jurisdictions": [{"id": "NO", "status": "proposed"}], "institutes": [{"id": "no-ki-norge", "jurisdiction_id": "NO", "funding": []}]}
    value = f'<span class="budget-value" data-funding-id="{identity}"><span class="budget-amount">NOK 30m</span><span class="budget-qualifiers">a year · 2027 budget proposal</span></span>'
    html = f'<article class="card" data-j="NO"><div class="row" data-track="9"><div class="k">Budget</div><div class="v">{value}</div></div></article><table><tbody id="world-rows"><tr data-j="NO"><td data-col="budget">{value}</td></tr></tbody></table>'
    assert check_page.check_budget_output(html, data) == []
    missing = html.replace(value, '<span class="missing">Not published</span>')
    assert any("wrong funding ID" in error for error in check_page.check_budget_output(missing, data))
    assert any("separate amount and qualifiers" in error for error in check_page.check_budget_output(html.replace("budget-amount", "removed"), data))


def test_setup_groups_cover_institutes_before_establishment():
    """Institutional set-up lists every institute whatever its status, each square in its state's status colour."""
    data = {
        "jurisdictions": [{"id": "GB", "name": "United Kingdom", "status": "established"}, {"id": "NO", "name": "Norway", "status": "proposed"}],
        "institutes": [
            {"id": "gb", "jurisdiction_id": "GB", "legal_form": "new_body", "host": None, "citations": {"legal_form": [], "host": []}},
            {"id": "no", "jurisdiction_id": "NO", "legal_form": "new_body", "host": None, "citations": {"legal_form": [], "host": []}},
        ],
    }
    def page(states):
        marks = "".join(f'<i class="sw st-{status}"></i>' for _, status in states)
        items = "".join(f'<li data-state="{jid}"><a href="#c-{jid.lower()}">x</a><span class="group-note"><span class="missing">Not published</span></span></li>' for jid, _ in states)
        cards = "".join(f'<article id="c-{jid.lower()}"></article>' for jid, _ in states)
        return BeautifulSoup(f'<h2 id="set-up">Institutional set-up</h2><div id="setup"><div class="groups-short"></div><div class="groups-long"><div class="group-block" data-form="new_body"><div class="marks">{marks}</div><ul class="group-list">{items}</ul></div></div></div>{cards}', "html.parser")
    assert check_page.check_setup_groups(page([("GB", "established"), ("NO", "proposed")]), data) == []
    renamed = page([("GB", "established"), ("NO", "proposed")])
    renamed.select_one("#set-up").string = "How institutes are organised"
    assert check_page.check_setup_groups(renamed, data) == []
    renamed.select_one("#set-up").string = " \n "
    assert check_page.blocking(check_page.check_setup_groups(renamed, data)) == ["set-up heading is missing or empty"]
    renamed.select_one("#set-up").decompose()
    assert check_page.blocking(check_page.check_setup_groups(renamed, data)) == ["set-up heading is missing or empty"]
    assert any("lists" in error for error in check_page.check_setup_groups(page([("NO", "proposed"), ("GB", "established")]), data))  # status on the ladder first, then name
    assert any("lists" in error for error in check_page.check_setup_groups(page([("GB", "established")]), data))
    assert any("marks differ" in error for error in check_page.check_setup_groups(page([("GB", "established"), ("NO", "established")]), data))


def test_uncited_values_need_a_citation_link_except_absence_and_known_gaps(monkeypatch):
    from bs4 import BeautifulSoup

    monkeypatch.setattr(check_page, "KNOWN_UNCITED", {("net", "US"): "no quotation yet"})
    html = """<table><tbody id="world-rows">
      <tr data-j="GB"><td data-col="net">Member<span class="cites"><a class="cn" href="#">1</a></span></td><td data-col="budget"><span class="missing">Not published</span></td></tr>
      <tr data-j="US"><td data-col="net">Member</td><td data-col="op">None found yet</td></tr>
      <tr data-j="FR"><td data-col="op"><span class="activity-kind">Joint testing<span class="cites"><a class="cn" href="#">1</a></span></span><span class="activity-kind">Published work</span></td></tr>
    </tbody></table>
    <article class="card" data-j="KR"><div class="row" data-track="12"><div class="k">Related funding</div><div class="v"><span class="related-funding-item" data-funding-id="f1">KRW 20.1bn<span class="cites"></span></span></div></div></article>"""
    found = check_page.uncited_values(BeautifulSoup(html, "html.parser"))
    assert found == ["card KR: Related funding", "related funding f1", "table FR: op"]
    monkeypatch.setattr(check_page, "KNOWN_UNCITED", {("net", "US"): "", ("net", "GB"): ""})
    assert "KNOWN_UNCITED entry ('net', 'GB') is stale: remove it" in check_page.uncited_values(BeautifulSoup(html, "html.parser"))


@pytest.mark.parametrize(
    "phrase",
    [
        "rendered text from r.jina.ai is used",
        "one Jina fallback failed",
        "read through a reader proxy",
        "direct requests returned HTTP 403",
        "Round 6 review (17 October 2026): text",
        "the record ID decides",
        "the earlier entry breaks ties",
        "The build lists records for review",
        "The about code represents the approximation",
    ],
)
def test_internal_vocabulary_is_reported(phrase):
    found = check_page.internal_vocabulary(BeautifulSoup(f"<main><p>{phrase}.</p></main>", "html.parser"))
    assert found


def test_internal_vocabulary_exempts_version_history_and_plain_words():
    html = """<main><p>The second funding round 2 opened. Ties keep a fixed order.</p>
      <details id="changes"><p>Round 6 review: direct requests returned HTTP 403.</p></details></main>"""
    assert check_page.internal_vocabulary(BeautifulSoup(html, "html.parser")) == []


def test_glyph_check_allows_status_swatches_only():
    good = BeautifulSoup('<span class="smark"><i class="sw st-planned"></i>Planned</span><span class="smark value" data-state="confirmed">Joint testing</span>', "html.parser")
    assert check_page.check_glyphs(good) == []
    for bad in (
        '<span class="smark value" data-state="confirmed"><i class="sw state-confirmed"></i>Joint testing</span>',
        '<p id="operating-legend">■ Published work</p>',
        '<i class="sw st-planned st-extra"></i>',
    ):
        assert check_page.check_glyphs(BeautifulSoup(bad, "html.parser")), bad


def _names_data():
    return {
        "jurisdictions": [
            {"id": "GB", "name": "United Kingdom", "kind": "state"},
            {"id": "ES", "name": "Spain", "kind": "state"},
            {"id": "FR", "name": "France", "kind": "state"},
        ],
        "institutes": [
            {"id": "gb-aisi", "name": "AI Security Institute", "short_name": "UK AISI", "jurisdiction_id": "GB"},
            {"id": "es-isia", "name": "AI Security Institute (planned)", "short_name": "Spain AISI", "jurisdiction_id": "ES"},
            {"id": "fr-inesia", "name": "National Institute for AI Evaluation and Security", "short_name": "INESIA", "jurisdiction_id": "FR"},
        ],
    }


def _names_html(gb="AI Security Institute (United Kingdom)", gb_short="UK AISI", fr_short="INESIA"):
    def card(jid, name):
        return f'<article class="card" id="c-{jid.lower()}"><div class="row" data-track="2"><div class="k">Institute</div><div class="v">{name}</div></div></article>'
    def funding(jid, name):
        return f'<article class="fund-item" data-j="{jid}"><div class="fund-identity"><h3>x</h3><span class="smark"></span><span>{name}</span></div></article>'
    def row(jid, short, full):
        return f'<tr data-j="{jid}"><td data-col="inst"><abbr class="g" data-exp="{full}">{short}</abbr></td></tr>'
    def region(jid, short, full):
        return f'<li><button class="pick" data-pick="{jid}"></button><span class="small"><abbr class="g" data-exp="{full}" tabindex="0">{short}</abbr></span></li>'
    return (
        card("GB", gb) + card("ES", "AI Security Institute (Spain)") + card("FR", "National Institute for AI Evaluation and Security")
        + "<table class='world'><tbody>"
        + row("GB", gb_short, "AI Security Institute (United Kingdom)") + row("ES", "Spain AISI", "AI Security Institute (Spain)")
        + row("FR", fr_short, "National Institute for AI Evaluation and Security")
        + "</tbody></table>"
        + '<div id="fund-rows">' + funding("ES", "AI Security Institute (Spain)") + "</div>"
        + '<div id="regions"><ul class="region-rows">' + region("GB", gb_short, "AI Security Institute (United Kingdom)")
        + region("ES", "Spain AISI", "AI Security Institute (Spain)") + region("FR", fr_short, "National Institute for AI Evaluation and Security") + "</ul></div>"
    )


def test_institute_names_pass_and_fail_on_each_rule():
    data = _names_data()
    assert check_page.check_institute_names(_names_html(), data) == []
    # Same-named bodies without their country.
    assert any("card names its institute" in e for e in check_page.check_institute_names(_names_html(gb="AI Security Institute"), data))
    # The acronym preference is report-only; a name that differs from data still blocks.
    findings = check_page.check_institute_names(_names_html(gb_short="AISI"), data)
    assert any("bare generic acronym" in e and check_page.is_report_line(e) for e in findings)
    assert not any("bare generic acronym" in e for e in check_page.blocking(findings))
    assert any("does not show the short form" in e for e in check_page.blocking(findings))
    data["institutes"][0]["short_name"] = "AISI"
    assert check_page.blocking(check_page.check_institute_names(_names_html(gb_short="AISI"), data)) == []
    data["institutes"][0]["short_name"] = "UK AISI"
    # The table shows the full name instead of the recorded short form.
    assert any("does not show the short form" in e for e in check_page.check_institute_names(_names_html(fr_short="National Institute"), data))
    # A short form that does not give the full name on hover.
    html = _names_html().replace('data-exp="AI Security Institute (Spain)"', 'data-exp="Spain"')
    assert any("on hover" in e for e in check_page.check_institute_names(html, data))
    # A card or table row that disappears, (the funding row cases follow).
    assert any("card has no Institute row" in e for e in check_page.check_institute_names(_names_html().replace('id="c-es"', 'id="c-xx"'), data))
    assert any("ES table cell" in e for e in check_page.check_institute_names(_names_html().replace('<tr data-j="ES">', '<tr data-j="XX">'), data))
    # A funding row that names its institute in short form, or with the new-tab note the page adds to links.
    bad = _names_html().replace("AI Security Institute (Spain)</span></div></article>", "Spain AISI</span></div></article>")
    assert any("funding row names its institute" in e for e in check_page.check_institute_names(bad, data))
    tab = _names_html().replace("AI Security Institute (Spain)</span></div></article>", 'AI Security Institute (Spain)<span class="vh"> Opens elsewhere</span></span></div></article>')
    assert check_page.check_institute_names(tab, data) == []
    # No funding rows at all is a failure, not a pass.
    assert any("no funding rows" in e for e in check_page.check_institute_names(_names_html().replace('id="fund-rows"', 'id="x"'), data))


def _fold_data():
    return {
        "jurisdictions": [{
            "id": "BG",
            "related_bodies": [
                {"name": "Комисия за финансов надзор", "name_en": "Financial Supervision Commission", "kind": "ai_act_authority",
                 "authority_roles": ["market_surveillance"], "designation_state": "proposed", "source_ids": ["s1"], "role": "Proposed market authority."},
                {"name": "INSAIT (Sofia University)", "kind": "other", "source_ids": ["s1"]},
            ],
        }],
        "dataset": {},
    }


def _fold_html(status='<p class="basis">Proposed, not yet designated.</p>', det='<span class="det">Proposed market authority.</span>', original=True, label="AI Act market surveillance"):
    orig = '<span class="orig">Комисия за финансов надзор</span>' if original else ""
    return (
        '<details class="card mini" id="c-bg" data-j="BG"><summary><span class="name">Bulgaria</span></summary>'
        f'<div class="body-group"><p class="gl">{label}</p>{status}<ul><li><span class="body-name">Financial Supervision Commission<span class="cites"><a class="cn">1</a></span></span>{orig}{det}</li></ul></div>'
        '<div class="body-group"><p class="gl">Other bodies</p><ul><li><span class="body-name">INSAIT (Sofia University)<span class="cites"><a class="cn">1</a></span></span></li></ul></div>'
        "</details>"
    )


def test_nonidentified_folds_pass_and_fail_on_each_rule():
    data = _fold_data()
    assert check_page.check_nonidentified_folds(_fold_html(), data) == []
    # The original name must sit beneath an English name.
    assert any("original name must sit beneath" in e for e in check_page.check_nonidentified_folds(_fold_html(original=False), data))
    # A body under the wrong function heading.
    assert any("fold-out groups" in e for e in check_page.check_nonidentified_folds(_fold_html(label="AI Act notifying"), data))
    # A body without its citation.
    assert any("no inline citation" in e for e in check_page.check_nonidentified_folds(_fold_html().replace('<a class="cn">1</a>', "", 1), data))
    # A role text that is nowhere in the entry.
    assert any("role text is nowhere" in e for e in check_page.check_nonidentified_folds(_fold_html(det=""), data))
    # A designation status that is not shown; an entry that did not render; a non-Latin name standing alone.
    assert any("designation status" in e for e in check_page.check_nonidentified_folds(_fold_html(status=""), data))
    gone = {"jurisdictions": [{"id": "BG", "status": "none_identified", "europe_scope": True}], "dataset": {}}
    assert any("entry is missing" in e for e in check_page.check_nonidentified_folds("<ul id='rules'></ul>", gone))
    alone = _fold_html(original=False).replace("Financial Supervision Commission<", "Комисия за финансов надзор<")
    assert check_page.blocking(check_page.check_nonidentified_folds(alone, data))  # wrong rendered name still blocks
    data["jurisdictions"][0]["related_bodies"][0].pop("name_en")
    assert any("non-Latin script" in e for e in check_page.blocking(check_page.check_nonidentified_folds(alone, data)))


def test_nonidentified_fold_accepts_structured_closest_candidate():
    data = _fold_data()
    (jurisdiction,) = data["jurisdictions"]
    jurisdiction["closest_body"] = {
        "name": "INSAIT (Sofia University)",
        "source_ids": ["s1"],
    }
    page = _fold_html().replace(">Other bodies<", ">Closest candidate<")
    assert check_page.check_nonidentified_folds(page, data) == []


@pytest.mark.parametrize("closest", [False, True])
def test_fold_owner_headings_allow_rewording_but_need_text(closest):
    data = _fold_data()
    if closest:
        data["jurisdictions"][0]["closest_body"] = {"name": "INSAIT (Sofia University)", "source_ids": ["s1"]}
    html = _fold_html().replace(">Other bodies<", ">A different heading<")
    assert check_page.check_nonidentified_folds(html, data) == []
    for broken in (html.replace("A different heading", " \n "), html.replace('<p class="gl">A different heading</p>', "")):
        assert any("heading is empty" in error for error in check_page.blocking(check_page.check_nonidentified_folds(broken, data)))


def test_generic_sources_stay_out_of_card_lists_and_in_method():
    data = {
        "sources": [{"id": "eurostat", "url": "https://e.example/"}, {"id": "own", "url": "https://o.example/"}],
        "jurisdictions": [{"id": "GB", "source_ids": ["own"], "population": {"source_ids": ["eurostat"]}}], "institutes": [],
    }
    good = f'<article class="card" data-j="GB"><div class="srcs"><ol><li data-source-id="own"></li></ol></div></article><ul><li id="generic-sources">{check_page.GENERIC_LEAD}<a href="https://e.example/">Eurostat</a>{check_page.GENERIC_TAIL}</li></ul>'
    assert check_page.check_generic_sources(good, data) == []
    assert "Method generic sources line differs from its copy" in check_page.check_generic_sources(good.replace(check_page.GENERIC_TAIL, "."), data)
    padded = good.replace('<li data-source-id="own"></li>', '<li data-source-id="own"></li><li data-source-id="eurostat"></li>')
    assert any("repeats generic source eurostat" in e for e in check_page.check_generic_sources(padded, data))
    funding = padded.replace('<article class="card" data-j="GB"><div class="srcs">', '<div class="fund-item" data-j="GB"><div class="srcs">').replace("</article>", "</div>")
    assert any("repeats generic source eurostat" in e for e in check_page.check_generic_sources(funding, data))
    assert any("no source lists found" in e for e in check_page.check_generic_sources("<ul id='generic-sources'><li><a href='https://e.example/'></a></li></ul>", data))
    assert any("Method does not name generic source eurostat" in e for e in check_page.check_generic_sources(good.replace("https://e.example/\">", "https://x/\">"), data))


def _archive_data():
    return {
        "sources": [
            {"id": "kept", "url": "https://k.example/", "archive_url": "https://web.archive.org/web/1/https://k.example/"},
            {"id": "live", "url": "https://l.example/"},
        ],
        "dataset": {"display": {"generic_sources": ["kept"]}, "other_overviews": [{"display": {"source_links": ["live"]}}]},
    }


def _archive_html(kept_archive=True, live_archive=False):
    copy = lambda url: f' <span class="archive-copy">(<a class="archive" href="{url}">archived copy<span class="vh"> (opens in a new tab)</span></a>)</span>'  # noqa: E731
    kept = copy("https://web.archive.org/web/1/https://k.example/") if kept_archive else ""
    live = copy("https://l.example/") if live_archive else ""
    return (
        f'<article class="card" data-j="GB"><div class="srcs"><ol><li data-source-id="kept"><a class="standalone" href="https://k.example/">K</a>{kept}, Publisher</li>'
        f'<li data-source-id="live"><a class="standalone" href="https://l.example/">L</a>{live}, Publisher</li></ol></div>'
        f'<p>Value<sup><a class="cn" data-source-id="kept" href="https://k.example/">1</a></sup></p></article>'
        f'<ul><li id="generic-sources"><span class="inline-links"><a href="https://k.example/">K</a>{kept}</span></li>'
        f'<li><span class="inline-links"><a href="https://l.example/">L</a>{live}</span>: note</li></ul>'
    )


def test_archive_links_follow_archived_sources_only():
    data = _archive_data()
    assert check_page.check_archive_links(_archive_html(), data) == []
    missing = check_page.check_archive_links(_archive_html(kept_archive=False), data)
    assert "GB source list: source kept lacks its archived copy link" in missing
    assert "Method: source kept lacks its archived copy link" in missing
    extra = check_page.check_archive_links(_archive_html(live_archive=True), data)
    assert "GB source list: source live has no archive_url but shows an archived copy link" in extra
    assert "Method: source live has no archive_url but shows an archived copy link" in extra
    wrong = _archive_html().replace("web/1/", "web/2/", 1)
    assert "GB source list: source kept archived copy link differs from data" in check_page.check_archive_links(wrong, data)
    unbracketed = _archive_html().replace("</a>)</span>", "</a></span>", 1)
    assert "(opens in a new tab)" in unbracketed
    assert "GB source list: source kept archived copy is not one bracketed phrase after its link" in check_page.check_archive_links(unbracketed, data)
    extra_citation = _archive_html().replace(">1</a></sup>", '>1</a><span class="archive-copy">(<a class="archive" href="x">archived copy</a>)</span></sup>')
    assert "3 archived copy links on the page, 2 expected from source lists and Method" in check_page.check_archive_links(extra_citation, data)
    as_citation = _archive_html().replace('class="archive" href="https://web.archive.org', 'class="archive cn" href="https://web.archive.org', 1)
    assert check_page.check_archive_links(as_citation, data) == ["a superscript citation carries an archived copy link"]


def test_archive_links_allow_rewording_but_require_visible_link_text():
    data = _archive_data()
    html = _archive_html().replace("archived copy", "Saved source").replace("(opens in a new tab)", "Opens separately")
    assert check_page.check_archive_links(html, data) == []
    for label in ("", " \n "):
        errors = check_page.blocking(check_page.check_archive_links(html.replace("Saved source", label), data))
        assert any("archived copy link differs" in error for error in errors)


def test_former_names_are_marked_as_former():
    data = {"institutes": [{
        "id": "us-x", "name": "Center for Advancing Innovation and Standards for Super Intelligence", "acronym": "CAISSI", "short_name": "CAISSI",
        "jurisdiction_id": "US", "names": [{"name": "US AI Safety Institute"}, {"name": "Center for AI Standards and Innovation (CAISI)"},
                                           {"name": "Center for Advancing Innovation and Standards for Super Intelligence (CAISSI)"}],
    }], "jurisdictions": [{"id": "US", "name": "United States"}, {"id": "CA", "name": "Canada"}]}
    assert check_page.former_forms(data["institutes"][0]) == {"US AI Safety Institute", "Center for AI Standards and Innovation", "CAISI"}

    def page(text, j="US"):
        return f'<article id="c-{j.lower()}" data-j="{j}"><div class="v">{text}</div><details class="srcs"><ol><li>CAISI Evaluation</li></ol></details></article>'

    ok = ["USD 27m requested for CAISI (now CAISSI).", "Chaired by the then U.S. AI Safety Institute.",
          "Acting Director of the Center for AI Standards and Innovation (CAISI, the institute's former name)", "Run by CAISSI."]
    for text in ok:
        assert check_page.check_former_names(page(text), data) == [], text
    assert check_page.check_former_names(page("USD 27m requested for CAISI."), data)
    assert check_page.check_former_names(page("Chaired by the U.S. AI Safety Institute."), data)
    # Text beside a nested qualifier, sentence-initial markers, a marker after a semicolon.
    assert check_page.check_former_names(page('CAISI leads.<span class="meta-item">2026</span>'), data)
    for text in ["Formerly called CAISI, it led the work.", "Then known as CAISI, it led the work.", "CAISI leads; now CAISSI."]:
        assert check_page.check_former_names(page(text), data) == [], text
    # The state's own qualifier does not exempt a former name; another body's name does.
    gb = {"institutes": [{"id": "gb", "name": "AI Security Institute", "short_name": "UK AISI", "jurisdiction_id": "GB", "names": [{"name": "AI Safety Institute"}]}],
          "jurisdictions": [{"id": "GB", "name": "United Kingdom"}]}
    assert check_page.check_former_names(page("The UK AI Safety Institute leads this work.", j="GB"), gb)
    gb["institutes"].append({"id": "jp", "name": "Japan AI Safety Institute", "short_name": "J-AISI", "jurisdiction_id": "JP"})
    assert check_page.check_former_names(page("Signed with the Japan AI Safety Institute.", j="GB"), gb) == []
    # A row that carries data-j itself, an ordinary capitalised word, markup inside a sentence, 'Now', other sentence ends.
    assert check_page.check_former_names('<li class="development-row" data-j="US">CAISI leads the work.</li>', data)
    assert check_page.check_former_names(page("Contact CAISI for details."), data)
    assert check_page.check_former_names(page('CAISI <span class="meta-item">(now CAISSI)</span> leads.'), data) == []
    assert check_page.check_former_names(page("CAISI (Now CAISSI) leads."), data) == []
    assert check_page.check_former_names(page("CAISI leads; now CAISSIX helps."), data)
    assert check_page.check_former_names(page("CAISI leads! The office was formerly elsewhere."), data)
    # Prose with a link is checked; a source entry led by its link is not.
    assert check_page.check_former_names('<ul data-j="US"><li>CAISI leads the work; <a class="ext" href="x">read more</a>.</li></ul>', data)
    assert check_page.check_former_names('<ul data-j="US"><li><a class="standalone ext" href="x">CAISI Evaluation</a>, NIST</li></ul>', data) == []
    # Another state's text, and source titles, are out of scope.
    assert check_page.check_former_names(page("Canadian AI Safety Institute (CAISI) leads.", j="CA"), data) == []


def test_institute_names_two_institutes_need_a_decision():
    data = _names_data()
    assert check_page.check_institute_names(_names_html(), data) == []
    data["institutes"].append(
        {
            "id": "es-second",
            "name": "Second Institute",
            "short_name": "Second",
            "jurisdiction_id": "ES",
        }
    )
    errors = check_page.check_institute_names(_names_html(), data)
    assert "ES has several institutes; the table cell rule needs a decision" in errors
    assert "ES funding row: the institute name rule needs a decision" in errors


def interval_case(credit="2024-02-09", lineage_date=None, kind="published_work"):
    """A GB-shaped record: commitment 26 Oct 2023, designation 2 Nov 2023, one credited output."""
    jurisdiction = {
        "id": "GB", "status": "established", "status_since": "2023-11-02",
        "citations": {"since": ["designation"]},
        "timeline": [
            {"id": "c", "event": "commitment", "date": "2023-10-26", "citations": {"date": ["speech"]}},
            {"id": "d", "event": "designated", "date": "2023-11-02", "citations": {"date": ["designation"]}},
        ],
    }
    institute = {"id": "gb", "outputs": [
        {"lineage": "first", "qualifies_as": kind, "date": lineage_date or credit, "credit": {"date": credit}},
    ]}
    jurisdiction["institute_id"] = "gb"
    return {"dataset": {"as_of": "2026-10-20"}, "jurisdictions": [jurisdiction], "institutes": [institute], "sources": []}


def interval_html(status="Announced on 26 Oct 2023, 7 days earlier", activity="Published work began about 3 months after it was established", cites=("designation", "speech"),
                  caveat="It does not measure speed or quality.", table="",
                  activity_caveat="It is not a measure of speed or quality."):
    links = "".join(f'<a class="cn" data-source-id="{sid}">{n}</a>' for n, sid in enumerate(cites, 1))
    head, comma, amount = (status or "").partition(", ")
    amount, _, tail = amount.rpartition(" ")
    gap = (f'{head}, <abbr class="g interval" data-exp="{caveat}">{amount}</abbr> <span class="cite-glue">{tail}' if comma else f'<span class="cite-glue">{head}') if status else ""
    line = f'<span class="small qualifiers activity-interval">{activity}</span>' if activity else ""
    return (
        '<article class="card" data-j="GB">'
        f'<div class="row" data-track="3"><div class="k">Status</div><div class="v"><span class="qualified"><span class="value-line">Established since 2 Nov 2023</span>'
        f'<span class="small qualifiers">{gap}<span class="cites">{links}</span>{"</span>" if status else ""}</span></span></div></div>'
        f'<div class="row" data-track="11"><div class="k">Known activity</div><div class="v"><span class="smark value activity" title="{activity_caveat}"><span class="activity-kinds">'
        '<span class="qualified activity-kind"><span class="value-line activity-clause" data-kind="published_work">Published work</span>'
        f'<span class="small qualifiers activity-since">since 9 Feb 2024</span>{line}</span></span></span></div></div></article>'
        f'<table><tbody id="world-rows"><tr data-j="GB"><td>{table}</td></tr></tbody></table>'
    )


def test_interval_oracle_accepts_exact_intervals_with_caveat_and_citations():
    hand = {"GB": ("Announced on 26 Oct 2023, 7 days earlier", "about 3 months after it was established")}
    assert check_page.check_interval_output(interval_html(), interval_case(), hand) == []


@pytest.mark.parametrize(
    ("html", "data", "message"),
    [
        (interval_html(status="Proposed on 26 Oct 2023, 7 days earlier"), interval_case(), "Status interval"),
        (interval_html(status="Announced on 26 Oct 2023, 8 days earlier"), interval_case(), "Status interval"),
        (interval_html(activity="Published work began about 4 months after it was established"), interval_case(), "Known activity interval"),
        (interval_html(activity=None), interval_case(), "Known activity interval"),
        (interval_html(cites=("designation",)), interval_case(), "citations do not cover the announcement"),
        (interval_html(caveat="Counted from the dates."), interval_case(), "caveat"),
        (interval_html(caveat="It is not a measure of speed or quality."), interval_case(), "caveat"),
        (interval_html(activity_caveat="Counted from the dates."), interval_case(), "caveat"),
        (interval_html(table='<span class="activity-interval">x</span>'), interval_case(), "table shows an interval"),
        # Option A: an output that does not qualify never ends the interval.
        (interval_html(), interval_case(kind="none"), "Known activity interval"),
        # A month-only credit date gives a range, which never reaches the card.
        (interval_html(), interval_case(credit="2024-02"), "Known activity interval"),
    ],
)
def test_interval_oracle_rejects_mutations(html, data, message):
    assert any(message in error for error in check_page.check_interval_output(html, data))


def test_interval_oracle_counts_to_the_institute_credit_date_not_the_lineage_date():
    data = interval_case(credit="2024-05-01", lineage_date="2024-02-09")
    html = interval_html(activity="Published work began about 6 months after it was established").replace("since 9 Feb 2024", "since 1 May 2024")
    assert check_page.check_interval_output(html, data) == []


def test_interval_oracle_keeps_negative_intervals_and_pins_hand_worked_wording():
    since, line, counted = check_page.expected_intervals(*interval_case(credit="2023-09-07")["jurisdictions"], interval_case(credit="2023-09-07")["institutes"][0], "2026-10-20")
    assert (since, line, counted) == ("Announced on 26 Oct 2023, 7 days earlier", {"kind": "published_work", "text": "56 days before it was established"}, [0, 1])
    errors = check_page.check_interval_output(interval_html(), interval_case(), {"GB": ("Announced on 26 Oct 2023, 7 days earlier", None)})
    assert any("differs from the hand-worked" in error for error in errors)


@pytest.mark.parametrize("status", ["Announced on Oct 2023, 7 days earlier", "Announced on 2023, 7 days earlier", "Announced on 26 Oct 2023"])
def test_status_qualifier_rejects_inexact_or_missing_interval_wording(status):
    assert any("Status interval" in e for e in check_page.check_interval_output(interval_html(status=status), interval_case()))


@pytest.mark.parametrize("status", ["Announced on Mar 2025", "Announced on 2025", "Proposed on 26 Oct 2023"])
def test_status_qualifier_must_be_absent_when_none_is_expected(status):
    data = interval_case()
    data["jurisdictions"][0]["timeline"] = data["jurisdictions"][0]["timeline"][1:]  # no announcement: no qualifier
    html = interval_html(status=status, cites=("designation",))
    assert any("Status interval" in e and "differs from None" in e for e in check_page.check_interval_output(html, data))


def test_interval_oracle_cites_every_tied_designation():
    data = interval_case()
    data["jurisdictions"][0]["timeline"].append({"id": "d2", "event": "designated", "date": "2023-11-02", "citations": {"date": ["second"]}})
    assert check_page.check_interval_output(interval_html(cites=("designation", "speech", "second")), data) == []
    assert any("citations do not cover" in e for e in check_page.check_interval_output(interval_html(), data))


def test_every_theme_is_set_through_the_theme_helpers():
    """Principle Theme: the page ignores the system scheme, so a page opened with color_scheme alone is silently
    light. Only the helpers and the theme checks may name a colour scheme; every loop over both themes opens its
    pages only through themed_page (no raw new_page or new_context inside it) or switches them with set_theme."""
    import ast

    tree = ast.parse(Path(check_page.__file__).read_text(encoding="utf-8"))
    allowed = {"themed_page", "set_theme", "check_theme_default"}
    offenders, loops = [], []
    for function in (n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)):
        for node in ast.walk(function):
            if isinstance(node, ast.keyword) and node.arg == "color_scheme" and function.name not in allowed:
                offenders.append(function.name)
            if (isinstance(node, ast.For) and isinstance(node.iter, ast.Tuple)
                    and {getattr(e, "value", None) for e in node.iter.elts} == {"light", "dark"}):
                calls = [c for c in ast.walk(node) if isinstance(c, ast.Call)]
                names = {getattr(c.func, "id", None) for c in calls}
                raw = [c.lineno for c in calls if getattr(c.func, "attr", None) in ("new_page", "new_context")]
                if function.name not in allowed and (raw or not names & {"themed_page", "set_theme"}):
                    loops.append(f"{function.name} (raw page at lines {raw})" if raw else function.name)
    assert not offenders, f"colour scheme set outside the theme helpers in {offenders}"
    assert not loops, f"a loop over both themes does not use themed_page or set_theme in {loops}"


def test_cite_is_the_citation_with_its_doi_link():
    doi = "10.5281/zenodo.1234567"
    url = "https://doi.org/" + doi
    dataset = {"citation": "Tracker, version 1.0.0. " + url, "doi": doi}
    good = BeautifulSoup(f'<p id="cite">Tracker, version 1.0.0. <a href="{url}">{url}<span class="vh"> (opens in a new tab)</span></a></p>', "html.parser")
    assert check_page.check_cite(good, dataset) == []
    plain = BeautifulSoup(f'<p id="cite">Tracker, version 1.0.0. {url}</p>', "html.parser")
    assert "expected the DOI link" in check_page.check_cite(plain, dataset)[0]
    wrong = BeautifulSoup('<p id="cite">Tracker, version 1.0.0.</p>', "html.parser")
    assert check_page.check_cite(wrong, dataset) == ["Cite as is not dataset.citation verbatim"]
    no_doi = {"citation": "Tracker, version 1.0.0."}
    assert check_page.check_cite(wrong, no_doi) == []


KEYED_PAGE = BeautifulSoup("""
<table><tbody id="world-rows">
  <tr data-j="GB"><td data-col="budget">1</td></tr>
  <tr data-j="FR"></tr>
  <tr data-j="FR"><td data-col="budget">2</td></tr>
</tbody><tbody id="world-rest"><tr data-j="DE"><td data-col="budget">3</td></tr></tbody></table>
<article class="card" data-j="GB"><div class="row"><span class="v">a</span></div>
  <article class="card" data-j="IT"><div class="row"><span class="v">b</span></div></article></article>
<div id="c-gb"><div class="row" data-track="2"><span class="v">c</span></div></div>
""", "html.parser")


@pytest.mark.parametrize("selector, attr, value, rest", [
    ("#world-rows tr[data-j]", "data-j", "GB", ""),
    ("#world-rows tr[data-j]", "data-j", "FR", ""),  # two rows: the first
    ("#world-rows tr[data-j]", "data-j", "FR", '[data-col="budget"]'),  # the first row has none: the second's
    ("#world-rows tr[data-j], #world-rest tr[data-j]", "data-j", "DE", ""),
    ("#world-rows tr[data-j], #world-rest tr[data-j]", "data-j", "FR", "td"),
    ("#world-rows tr[data-j]", "data-j", "NL", ""),  # absent
    (".card[data-j]", "data-j", "GB", ".row .v"),  # a nested card's row is inside the outer card too
    (".card[data-j]", "data-j", "IT", ".row .v"),
    ("[id]", "id", "c-gb", ".row[data-track='2'] .v"),
    ("#world-rows tr[data-j]", "data-j", "G\\42", ""),  # not a plain name: CSS reads the escape
])
def test_keyed_finds_what_the_plain_query_finds(selector, attr, value, rest):
    keyed = check_page.Keyed(KEYED_PAGE, selector, attr)
    plain = ", ".join(f'{arm.strip()}[{attr}="{value}"] {rest}'.rstrip() for arm in selector.split(","))
    assert keyed.one(value, rest) is KEYED_PAGE.select_one(plain)
    if rest:
        assert keyed.all(value, rest) == KEYED_PAGE.select(plain)


def staged_job(stage, check, args):
    """A stand-in browser job for run_stages: ("ok", errors) returns them, ("raise", message) raises, ("mark", (file,
    errors)) creates the file and returns them, ("after", (file, errors)) returns them once the file exists, ("after_line",
    (file, job, errors)) once the file holds a complete timings line for that job, ("stuck", pid file) ignores SIGTERM,
    then records its process id and never returns, ("die", None) kills its own process."""
    import os
    import signal
    import time

    kind, payload = args
    if kind == "raise":
        raise RuntimeError(payload)
    if kind == "mark":
        Path(payload[0]).touch()
        return list(payload[1])
    if kind == "after_line":
        deadline = time.monotonic() + 60
        while not any(line.get("job") == payload[1] for line in check_page.read_timings(Path(payload[0]))):
            if time.monotonic() > deadline:
                raise TimeoutError(f"{payload[0]} never held a line for {payload[1]}")
            time.sleep(0.05)
        return list(payload[2])
    if kind == "after":
        deadline = time.monotonic() + 60
        while not Path(payload[0]).exists():
            if time.monotonic() > deadline:
                raise TimeoutError(f"{payload[0]} never appeared")
            time.sleep(0.05)
        return list(payload[1])
    if kind == "die":
        os.kill(os.getpid(), signal.SIGKILL)
    if kind == "stuck":
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        Path(payload + ".tmp").write_text(str(os.getpid()))
        Path(payload + ".tmp").rename(payload)  # appears complete
        time.sleep(600)
    return list(payload)


@pytest.mark.parametrize("jobs", [1, 2])
def test_run_stages_reports_like_the_sequential_gate(jobs, tmp_path):
    def job(stage, kind, payload):  # a real job's check: a partial of module functions, as browser_jobs builds them
        return (stage, check_page.partial(check_page.prefixed, "access: ", check_page.check_access_fixes), (kind, payload))

    seeds = {"late": lambda: ["seed"]}
    done = tmp_path / "l2-done"
    l1 = job("late", "after", (str(done), ["l1"])) if jobs > 1 else job("late", "ok", ["l1"])  # in parallel, l2 finishes first
    work = [job("early", "ok", []), l1, job("early", "ok", []), job("late", "mark", (str(done), ["l2"]))]
    assert check_page.run_stages(staged_job, work, seeds, jobs) == ["seed", "l1", "l2"]
    # A stage is reached only when everything before it passed: the later failure is never reported.
    assert check_page.run_stages(staged_job, [job("early", "ok", ["e1"]), job("late", "raise", "late")], {}, jobs) == ["e1"]
    # The first exception in report order ends the run, whichever finishes first.
    work = [job("early", "ok", []), job("late", "ok", ["l1"]), job("late", "raise", "first"), job("late", "raise", "second")]
    with pytest.raises(RuntimeError, match="^first$"):
        check_page.run_stages(staged_job, work, {}, jobs)


@pytest.mark.parametrize("jobs", [1, 2])
def test_report_lines_are_reported_in_order_but_stop_no_stage(jobs):
    def job(stage, payload):
        return (stage, check_page.check_access_fixes, ("ok", payload))

    work = [job("early", [check_page.report_line("spacing")]), job("late", ["l1", check_page.report_line("weight")]), job("later", ["never"])]
    assert check_page.run_stages(staged_job, work, {}, jobs) == [check_page.report_line("spacing"), "l1", check_page.report_line("weight")]
    assert check_page.blocking([check_page.report_line("spacing"), "1000px dark: " + check_page.report_line("gap"), "l1"]) == ["l1"]  # a prefix keeps the mark


def test_only_the_mark_makes_a_report_line_not_the_text():
    quoted = "missing evidence for 'report line: example'"  # page text or a message that happens to read like one
    assert check_page.blocking([quoted]) == [quoted]
    assert check_page.printed_report_line("390px: " + check_page.report_line("gap")) == "report line: 390px: gap"


@pytest.mark.parametrize("planted", ["data/page.json:\\u001F", "index.html:\x1f", "bad\x1fname.txt:\x1f", "bad\x1fname.png:clean"])
def test_the_report_mark_is_reserved_in_the_built_site(rendered, monkeypatch, capsys, planted):
    _, data, dist = rendered
    (dist / "data/aisi.json").write_text(json.dumps(data))
    (dist / "index.html").write_text("Built page")
    assert check_page.check_reserved_mark(dist, "<p>page</p>") == []
    name, text = planted.split(":")
    (dist / name).parent.mkdir(parents=True, exist_ok=True)
    (dist / name).write_text(f'"name {text}"')
    found = check_page.check_reserved_mark(dist, "")
    assert name.replace("\x1f", "\\x1f") in found[0] and check_page.blocking(found) == found  # the guard's own finding is never a report line
    monkeypatch.setattr(check_page, "find_chrome", lambda: "chrome")
    monkeypatch.setattr(check_page, "render", lambda *args: ("<html></html>", "http://127.0.0.1:1/"))
    monkeypatch.setattr(check_page, "check_dom", lambda *args: (_ for _ in ()).throw(AssertionError("checked after the mark was found")))
    assert check_page.main(dist) == 1 and "reserved report-line mark" in capsys.readouterr().err


def test_a_shard_exception_fails_whatever_its_message_says(tmp_path, monkeypatch, capsys):
    behaviour = [("hydration", ("raise", RuntimeError("report line: " + check_page.REPORT_MARK + "boom")))]
    run_round_trip(tmp_path, monkeypatch, behaviour)
    assert "1 findings" in capsys.readouterr().out


@pytest.fixture
def quarantine_file(tmp_path, monkeypatch):
    path = tmp_path / "quarantine.json"
    path.write_text("[]\n")
    monkeypatch.setattr(check_page, "QUARANTINE", path)
    monkeypatch.setattr(check_page, "today", lambda: check_page.date(2026, 11, 9))
    return path


def quarantine_entry(**changes):
    return {"job": "hydration/hydration_paint", "owner": "Test owner", "issue": "#123",
            "expires": "2026-11-09", "reason": "Demonstrated harness flake; evidence in the issue.", **changes}


@pytest.mark.parametrize("expires", ["2026-11-09", "2026-12-09"])
def test_active_quarantine_runs_and_reports_findings_through_local_shards_and_merge(quarantine_file, tmp_path, monkeypatch, expires):
    entry = quarantine_entry(job="hydration/fake_hydration/0", expires=expires)
    quarantine_file.write_text(json.dumps([entry]))
    behaviour = [("hydration", ("errors", ["h1", check_page.report_line("h2")])), ("interactions", ("errors", ["i1"]))]
    merged, local = run_round_trip(tmp_path, monkeypatch, behaviour)
    prefix = f"quarantined until {expires} (owner Test owner, #123): "
    assert merged == local == ([check_page.report_line(prefix + "h1"), check_page.report_line(prefix + "h2"), "seed", "i1"], None)
    lines = check_page.read_timings(tmp_path / "timings.jsonl")
    assert all(check_page.timing_ok({key: value for key, value in line.items() if key != "shard"}) for line in lines)
    assert {line["job"]: line["errors"] for line in lines} == {"hydration/fake_hydration/0": 1, "interactions/fake_interaction/1": 1}  # raw
    # The merge applies the quarantine on its own date: after the expiry the same shard results block again.
    monkeypatch.setattr(check_page, "today", lambda: check_page.date.fromisoformat(expires) + check_page.date.resolution)
    assert check_page.blocking(check_page.check_merged(tmp_path / "site", {}, tmp_path / "gate")) == ["h1"]


def test_expired_quarantine_blocks_and_warns(quarantine_file, tmp_path, monkeypatch, capsys):
    quarantine_file.write_text(json.dumps([quarantine_entry(job="hydration/fake_hydration/0", expires="2026-11-08")]))
    merged, local = run_round_trip(tmp_path, monkeypatch, [("hydration", ("errors", ["h1"]))])
    assert merged == local == (["h1"], None)
    assert "warning: expired quarantine hydration/fake_hydration/0 (expired 2026-11-08, owner Test owner, #123)" in capsys.readouterr().err


def test_quarantine_uses_the_coordinator_run_date(tmp_path, monkeypatch):
    path = tmp_path / "quarantine.json"
    path.write_text(json.dumps([quarantine_entry(expires="2026-11-30")]))
    monkeypatch.setattr(check_page, "QUARANTINE", path)
    monkeypatch.setenv("AISI_RUN_DATE", "2026-11-30")
    check_page.today.cache_clear()
    try:
        assert "hydration/hydration_paint" in check_page.load_quarantine()
        monkeypatch.setenv("AISI_RUN_DATE", "2026-12-01")
        check_page.today.cache_clear()
        assert check_page.load_quarantine() == {}
    finally:
        check_page.today.cache_clear()


@pytest.mark.parametrize(("entries", "message"), [
    ({}, "expected a list"),
    ([None], "requires job"),
    *[([{key: value for key, value in quarantine_entry().items() if key != missing}], "requires job")
      for missing in ("job", "owner", "issue", "expires", "reason")],
    *[([quarantine_entry(**{field: value})], "non-empty text")
      for field, value in (("owner", " "), ("reason", None), ("issue", 123), ("owner", "bad\nowner"))],
    ([quarantine_entry(expires="2026-02-30")], "bad expires date"),
    ([quarantine_entry(expires="20261109")], "bad expires date"),
    ([quarantine_entry(job="hydration/unknown")], "unknown job"),
    ([quarantine_entry(), quarantine_entry()], "duplicate job"),
    ([quarantine_entry(expires="2026-12-10")], "more than 30 days"),
    ([quarantine_entry(issue="not an issue")], "issue must be"),
])
@pytest.mark.parametrize("mode", ["local", "shard", "merge"])
def test_bad_quarantine_fails_main_before_browser_access(quarantine_file, tmp_path, monkeypatch, capsys, entries, message, mode):
    quarantine_file.write_text(json.dumps(entries))
    monkeypatch.setattr(check_page, "find_chrome", lambda: pytest.fail("browser access before quarantine validation"))
    kwargs = {"shard": (1, 1), "results": tmp_path / "result.json"} if mode == "shard" else {"merge": tmp_path} if mode == "merge" else {}
    assert check_page.main(tmp_path, **kwargs) == 1
    error = capsys.readouterr().err
    assert error.startswith("error: quarantine ") and message in error


def test_unreadable_quarantine_fails_clearly(quarantine_file):
    quarantine_file.write_text("[broken")
    with pytest.raises(ValueError, match="quarantine"):
        check_page.load_quarantine()


@pytest.mark.parametrize("issue", ["#1", "https://example.org/issues/123"])
def test_quarantine_issue_formats(quarantine_file, issue):
    quarantine_file.write_text(json.dumps([quarantine_entry(issue=issue)]))
    assert check_page.load_quarantine()["hydration/hydration_paint"]["issue"] == issue


def test_quarantined_exception_still_blocks_and_is_timed(quarantine_file, tmp_path, monkeypatch, capsys):
    quarantine_file.write_text(json.dumps([quarantine_entry(job="hydration/fake_hydration/0")]))
    merged, local = run_round_trip(tmp_path, monkeypatch, [("hydration", ("raise", RuntimeError("harness exploded")))])
    assert merged == local == ("caught", "harness exploded")
    assert "1 findings" in capsys.readouterr().out
    (line,) = check_page.read_timings(tmp_path / "timings.jsonl")
    assert check_page.timing_ok({key: value for key, value in line.items() if key != "shard"})
    assert line["exception"] == "RuntimeError" and line["errors"] is None


@pytest.mark.parametrize("crashes", [False, True])
def test_quarantine_main_exit_and_printed_findings(quarantine_file, tmp_path, monkeypatch, capsys, crashes):
    quarantine_file.write_text(json.dumps([quarantine_entry()]))
    dist = tmp_path / "dist"
    (dist / "data").mkdir(parents=True)
    (dist / "index.html").write_text("built page")
    (dist / "data/aisi.json").write_text('{"jurisdictions": []}')
    monkeypatch.setattr(check_page, "find_chrome", lambda: "chrome")
    monkeypatch.setattr(check_page, "render", lambda *args: ("<html></html>", "http://localhost/"))
    for name in STATIC_CHECKS:
        monkeypatch.setattr(check_page, name, lambda *args: [])
    kind, payload = ("raise", "harness exploded") if crashes else ("ok", ["drift"])
    monkeypatch.setattr(check_page, "browser_jobs", lambda *args: [("hydration", check_page.hydration_paint, (kind, payload))])
    monkeypatch.setattr(check_page, "run_browser_job", lambda chrome, dist, stage, check, args: staged_job(stage, check, args))
    assert check_page.main(dist) == int(crashes)
    output = capsys.readouterr()
    if crashes:
        assert "error: page check failed: harness exploded" in output.err
    else:
        assert "report line: quarantined until 2026-11-09 (owner Test owner, #123): drift" in output.out
        assert "error:" not in output.err


def test_checker_digest_does_not_depend_on_the_quarantine_or_the_date(tmp_path, monkeypatch):
    # Shards record raw findings and the merge applies the quarantine on its own date, so no shard result goes stale
    # across midnight or when the register changes.
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    monkeypatch.setattr(check_page, "__file__", str(scripts / "check_page.py"))
    register = scripts / "quarantine.json"
    register.write_text("[]")
    empty = check_page.checker_digest()
    register.write_text(json.dumps([quarantine_entry()]))
    assert check_page.checker_digest() == empty


def test_a_job_with_only_report_lines_is_timed_without_errors(tmp_path):
    timings = tmp_path / "t.jsonl"
    check_page.timed_job(timings, 0.0, staged_job, "early", check_page.check_access_fixes, ("ok", [check_page.report_line("x"), "e"]))
    assert [line["errors"] for line in check_page.read_timings(timings)] == [1]


def test_main_prints_report_lines_and_passes_on_them_alone(rendered, monkeypatch, capsys):
    _, data, dist = rendered
    (dist / "data/aisi.json").write_text(json.dumps(data))
    (dist / "index.html").write_text("Built page")
    monkeypatch.setattr(check_page, "find_chrome", lambda: "chrome")
    monkeypatch.setattr(check_page, "render", lambda *args: ("<html></html>", "http://127.0.0.1:1/"))
    for name in STATIC_CHECKS:
        monkeypatch.setattr(check_page, name, lambda *args: [])
    monkeypatch.setattr(check_page, "check_dom", lambda *args: [check_page.report_line("static drift")])
    monkeypatch.setattr(check_page, "check_browser", lambda *args: ["390px: " + check_page.report_line("browser drift")])
    assert check_page.main(dist) == 0  # a report line does not stop the browser jobs either
    out = capsys.readouterr()
    assert out.out.splitlines()[:2] == ["report line: static drift", "report line: 390px: browser drift"] and "error:" not in out.err
    monkeypatch.setattr(check_page, "check_browser", lambda *args: ["broken", check_page.report_line("browser drift")])
    assert check_page.main(dist) == 1
    out = capsys.readouterr()
    assert "report line: browser drift" in out.out and "error: broken" in out.err and "error: report line" not in out.err


def test_check_reads_the_report_lines_the_gate_prints():
    from scripts import check

    assert check.REPORT_LINE == check_page.REPORT_LINE


@pytest.mark.parametrize("jobs", [1, 2])
def test_timed_jobs_write_one_line_per_job_that_ends(jobs, tmp_path):
    import time

    def job(stage, kind, payload):
        return (stage, check_page.check_access_fixes, (kind, payload))

    timings = tmp_path / "timings.jsonl"
    run = check_page.partial(check_page.timed_job, timings, time.monotonic(), staged_job)
    if jobs == 1:  # a sequential run stops at the failed stage: the later job never runs
        work, expected = [job("early", "ok", ["e1"]), job("late", "ok", ["never reported"])], {("early/check_access_fixes", 1)}
    else:  # in parallel, the early failure waits for the late job's line, so the discarded result is timed before the pool stops
        work = [job("early", "after_line", (str(timings), "late/check_access_fixes", ["e1"])), job("late", "ok", ["never reported"])]
        expected = {("early/check_access_fixes", 1), ("late/check_access_fixes", 1)}
    assert check_page.run_stages(run, work, {}, jobs) == ["e1"]
    lines = check_page.read_timings(timings)
    assert all(check_page.timing_ok(line) for line in lines)
    assert {(line["job"], line["errors"]) for line in lines} == expected and len(lines) == len(expected)
    timings.unlink()
    with pytest.raises(RuntimeError):
        check_page.run_stages(run, [job("early", "raise", "boom")], {}, jobs)
    assert [(line["errors"], line["exception"]) for line in check_page.read_timings(timings)] == [(None, "RuntimeError")]


def test_a_timings_file_that_cannot_be_written_changes_no_outcome(tmp_path, capsys):
    unwritable = tmp_path / "missing-dir" / "timings.jsonl"
    assert check_page.timed_job(unwritable, 0.0, staged_job, "early", check_page.check_access_fixes, ("ok", ["x"])) == ["x"]
    with pytest.raises(RuntimeError, match="^boom$"):  # the job's own exception, not the failed write's
        check_page.timed_job(unwritable, 0.0, staged_job, "early", check_page.check_access_fixes, ("raise", "boom"))
    assert "timings not written" in capsys.readouterr().err


def test_a_damaged_timings_file_loses_only_timings(tmp_path, monkeypatch, capsys):
    timings = tmp_path / "t.jsonl"
    timings.write_text('{"job": "a", "started": 0, "seconds": 1, "errors": 0, "exception": null}\n{"job": "b", "sta\n{"job": 3}\n')
    assert [line["job"] for line in check_page.read_timings(timings)] == ["a", 3]  # the cut line is dropped; merge validates the rest
    monkeypatch.setattr(Path, "read_text", lambda self, *a, **k: (_ for _ in ()).throw(PermissionError("denied")))
    assert check_page.read_timings(timings) == [] and "not read" in capsys.readouterr().err
    monkeypatch.undo()
    timings.write_bytes(b"\xff\xfe not utf-8\n")
    assert check_page.read_timings(timings) == [] and "not read" in capsys.readouterr().err


def test_a_working_file_that_cannot_be_removed_only_warns(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(Path, "unlink", lambda self, missing_ok=False: (_ for _ in ()).throw(PermissionError("denied")))
    check_page.drop_timings(tmp_path / "x.timings")
    assert "not removed" in capsys.readouterr().err


def test_load_shards_hands_over_the_timings_it_validated(tmp_path):
    site, results = shard_files(tmp_path, ["a", "b"], {1: {0: OK}, 2: {1: OK}})
    lines = []
    check_page.load_shards(results, site, ["a", "b"], lines)
    assert [(t["job"], t["shard"]) for t in lines] == [("a", "1/2"), ("b", "2/2")]


def test_a_shard_that_cannot_make_its_timings_file_runs_untimed(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(check_page.tempfile, "mkstemp", lambda **k: (_ for _ in ()).throw(PermissionError("denied")))
    behaviour = [("hydration", ("errors", [])), ("interactions", ("errors", ["i1"]))]
    merged, local = run_round_trip(tmp_path, monkeypatch, behaviour)
    assert merged == local and "not timed" in capsys.readouterr().err
    assert check_page.read_timings(tmp_path / "timings.jsonl") == []


def test_main_clears_timings_of_an_earlier_run_first(tmp_path, monkeypatch):
    timings = tmp_path / "timings.jsonl"
    timings.write_text('{"job": "old"}\n')
    monkeypatch.setattr(check_page, "find_chrome", lambda: None)
    assert check_page.main(tmp_path, timings=timings) == check_page.NOT_RUN
    assert timings.read_text() == ""


def test_without_a_timings_file_a_job_runs_untimed():
    assert check_page.timed_job(None, 0.0, staged_job, "early", check_page.check_access_fixes, ("ok", ["x"])) == ["x"]


def test_only_named_jobs_run_in_their_stages(monkeypatch):
    seen = {}
    monkeypatch.setattr(check_page, "run_stages", lambda run, work, seeds, jobs: seen.setdefault("ids", [check_page.job_id(*j) for j in work]) and [])
    chosen = ["interactions/check_touch_tips", "hydration/hydration_paint"]
    check_page.check_browser("chrome", Path("site"), {}, only=chosen)
    assert seen["ids"] == ["hydration/hydration_paint", "interactions/check_touch_tips"]  # report order, not the order asked


def test_a_subset_runs_through_the_real_stages(monkeypatch):
    ran = []
    monkeypatch.setattr(check_page, "run_browser_job", lambda chrome, dist, stage, check, args: ran.append(check_page.job_id(stage, check, args)) or [])
    monkeypatch.setattr(check_page, "browser_seeds", lambda dist: {"interactions": lambda: ran.append("seed") or ["seed error"]})
    # Only an interactions job: the empty earlier stages pass, the stage's seed runs, nothing else does.
    assert check_page.check_browser("chrome", Path("site"), {}, only=["interactions/check_menu_touch"]) == ["seed error"]
    assert ran == ["seed", "interactions/check_menu_touch"]
    ran.clear()
    # Mixed stages: a hydration finding stops the run before the interactions stage, as in the full gate.
    monkeypatch.setattr(check_page, "run_browser_job", lambda chrome, dist, stage, check, args: ran.append(check_page.job_id(stage, check, args)) or (["h"] if stage == "hydration" else []))
    assert check_page.check_browser("chrome", Path("site"), {}, only=["interactions/check_menu_touch", "hydration/hydration_edges"]) == ["h"]
    assert ran == ["hydration/hydration_edges"]


def test_job_ids_must_exist():
    assert check_page.job_ids("hydration/hydration_paint, interactions/interaction_pass/390/dark") == [
        "hydration/hydration_paint", "interactions/interaction_pass/390/dark"]
    assert check_page.job_ids("origins/check_origins,origins/check_origins") == ["origins/check_origins"]  # counted as run
    for text in ("", "hydration/nope", "hydration/hydration_paint,nope"):
        with pytest.raises(check_page.argparse.ArgumentTypeError, match="unknown browser job"):
            check_page.job_ids(text)
    assert "interactions/interaction_pass/1440/light" in check_page.all_job_ids() and len(check_page.all_job_ids()) == len(set(check_page.all_job_ids()))


def test_a_stuck_job_is_stopped_by_its_watchdog_and_named(tmp_path, capfd):
    """A job past its limit ends its own process: the gate reports WorkerCrashed instead of waiting, stderr names the
    job and where it was, and its timings line says JobTimeout."""
    import os
    import time

    pid_file, timings = tmp_path / "stuck.pid", tmp_path / "timings.jsonl"
    run = check_page.partial(check_page.timed_job, timings, time.monotonic(), staged_job, limit=3)
    work = [("early", check_page.check_access_fixes, ("stuck", str(pid_file)))]
    start = time.monotonic()
    with pytest.raises(check_page.WorkerCrashed, match="early/check_access_fixes has no result"):
        check_page.run_stages(run, work, {}, jobs=2)
    assert time.monotonic() - start < 300  # the job sleeps 600 s; slow process start on a loaded host has room
    with pytest.raises(ProcessLookupError):
        os.kill(int(pid_file.read_text()), 0)
    assert "browser job early/check_access_fixes ran over 3 s and is stopped" in capfd.readouterr().err
    [line] = check_page.read_timings(timings)
    assert (line["job"], line["errors"], line["exception"]) == ("early/check_access_fixes", None, "JobTimeout")
    assert line["seconds"] >= 3


def test_a_job_that_ends_in_time_disarms_its_watchdog(tmp_path, monkeypatch):
    """A fake timer, so a slow scheduler cannot fire a real os._exit in the test process; real expiry is covered by
    test_a_stuck_job_is_stopped_by_its_watchdog_and_named."""
    timings, timers = tmp_path / "timings.jsonl", []

    class Timer:
        daemon = False
        def __init__(self, seconds, function):
            self.seconds, self.started, self.cancelled = seconds, False, False
            timers.append(self)
        def start(self):
            self.started = True
        def cancel(self):
            self.cancelled = True

    monkeypatch.setattr(check_page.threading, "Timer", Timer)
    assert check_page.timed_job(timings, 0.0, staged_job, "early", check_page.check_access_fixes, ("ok", ["x"]), limit=7) == ["x"]
    with pytest.raises(RuntimeError, match="boom"):
        check_page.timed_job(timings, 0.0, staged_job, "early", check_page.check_access_fixes, ("raise", "boom"))
    assert [(timer.seconds, timer.started, timer.cancelled) for timer in timers] == [(7, True, True), (check_page.JOB_LIMIT, True, True)]
    assert [line["exception"] for line in check_page.read_timings(timings)] == [None, "RuntimeError"]


def test_a_job_ending_as_its_watchdog_fires_leaves_one_timeout_line(tmp_path, monkeypatch):
    """The boundary race, made deterministic: the watchdog fires while the job is returning; it owns the line, and
    the job's own outcome is never written over it."""
    timings, exits = tmp_path / "timings.jsonl", []

    class FiredTimer:
        def __init__(self, seconds, function):
            self.function = function
        daemon = False
        def start(self):
            pass
        def cancel(self):
            raise AssertionError("the job must not take the line once the watchdog owns it")
        def join(self):
            pass  # the real watchdog's os._exit never returns

    monkeypatch.setattr(check_page.os, "_exit", exits.append)
    monkeypatch.setattr(check_page.faulthandler, "dump_traceback", lambda all_threads: None)
    timer = {}
    monkeypatch.setattr(check_page.threading, "Timer", lambda seconds, function: timer.setdefault("t", FiredTimer(seconds, function)))

    def job(stage, check, args):
        timer["t"].function()  # the limit passes as the job ends
        return ["x"]

    check_page.timed_job(timings, 0.0, job, "early", check_page.check_access_fixes, ())
    assert exits == [check_page.JOB_TIMEOUT_EXIT]
    assert [(line["errors"], line["exception"]) for line in check_page.read_timings(timings)] == [(None, "JobTimeout")]


def test_jobs_never_submitted_to_a_broken_pool_are_crashes(monkeypatch):
    from concurrent.futures.process import BrokenProcessPool

    class BrokenPool:
        def __init__(self):
            self.calls = 0
        def submit(self, run, *job):
            self.calls += 1
            if self.calls > 1:
                raise BrokenProcessPool("a worker died")
            return "future"

    futures = check_page.submit_jobs(BrokenPool(), staged_job, [("a", None, ()), ("b", None, ())], [0, 1])
    assert futures == {0: "future"}
    with pytest.raises(check_page.WorkerCrashed, match="early/check_access_fixes has no result"):
        check_page.pool_result(futures.get(1), ("early", check_page.check_access_fixes, ()))


def test_a_dead_worker_is_reported_as_a_crash_naming_the_job(tmp_path):
    work = [("early", check_page.check_access_fixes, ("die", None)), ("early", check_page.check_menu_touch, ("ok", ["e1"]))]  # the awaited one dies
    with pytest.raises(check_page.WorkerCrashed, match="early/check_access_fixes has no result.*possibly SIGKILL: out of memory"):
        check_page.run_stages(staged_job, work, {}, jobs=2)
    outcomes = check_page.run_shard(staged_job, work, [0, 1], jobs=2)
    assert outcomes[0]["exception"]["type"] == "WorkerCrashed" and outcomes[0]["exception"]["caught"]


def test_job_count_is_at_least_one():
    assert check_page.job_count("3") == 3
    with pytest.raises(check_page.argparse.ArgumentTypeError):
        check_page.job_count("0")


def test_run_stages_stops_workers_it_no_longer_needs(tmp_path):
    import os
    import time

    pid_file = tmp_path / "stuck.pid"
    work = [("early", None, ("after", (str(pid_file), ["e1"]))), ("late", None, ("stuck", str(pid_file)))]
    start = time.monotonic()
    assert check_page.run_stages(staged_job, work, {}, jobs=2) == ["e1"]
    # The stuck job sleeps 600 s: finishing in under half of that shows it was stopped, not waited for, with room
    # for slow process start-up on a loaded host.
    assert time.monotonic() - start < 300
    with pytest.raises(ProcessLookupError):  # terminated, then killed when it ignored that, and reaped
        os.kill(int(pid_file.read_text()), 0)


def test_job_ids_are_unique_and_shards_cover_every_job_once():
    ids = [check_page.job_id(*job) for job in check_page.browser_jobs(Path("site"), {}, None)]
    assert len(ids) == len(set(ids))
    for count in (1, 2, 4, 7):
        numbers = check_page.shard_numbers(ids, count)
        assert len(numbers) == len(ids) and set(numbers) <= set(range(1, count + 1))
    loads = Counter()
    for job, number in zip(ids, check_page.shard_numbers(ids, 4)):
        loads[number] += check_page.JOB_COST.get(job, check_page.DEFAULT_COST)
    assert max(loads.values()) - min(loads.values()) < 10  # seconds of CI work


def test_an_unmeasured_job_counts_the_default_cost():
    assert check_page.shard_numbers(["stage/new_a", "stage/new_b", "stage/new_c"], 2) == [1, 2, 1]


def shard_files(tmp_path, ids, outcomes_by_shard, changes=None):
    """Shard result files for a small site under tmp_path, one per shard, as write_shard writes them."""
    site = tmp_path / "site"
    site.mkdir(exist_ok=True)
    (site / "index.html").write_text("page")
    files = check_page.file_digests(site)
    results = tmp_path / "results"
    for number, outcomes in outcomes_by_shard.items():
        result = {"format": check_page.SHARD_FORMAT, "shard": number, "count": len(outcomes_by_shard), "jobs": ids,
                  "checker": check_page.checker_digest(), "site": check_page.tree_digest(files), "site_files": files,
                  "outcomes": {str(n): o for n, o in outcomes.items()},
                  "timings": [{"job": ids[n], "started": 0.5, "seconds": 2.0, "errors": 0, "exception": None} for n in outcomes]}
        result.update((changes or {}).get(number, {}))
        (results / f"gate-shard-{number}").mkdir(parents=True, exist_ok=True)
        (results / f"gate-shard-{number}" / f"shard-{number}.json").write_text(json.dumps(result))
    return site, results


OK = {"errors": []}


def test_load_shards_returns_every_outcome(tmp_path):
    site, results = shard_files(tmp_path, ["a", "b", "c"], {1: {0: OK, 2: {"errors": ["x"]}}, 2: {1: OK}})
    assert check_page.load_shards(results, site, ["a", "b", "c"]) == {0: OK, 1: OK, 2: {"errors": ["x"]}}


@pytest.mark.parametrize("shards, changes, problem", [
    ({1: {0: OK}, 2: {1: OK}}, {2: {"jobs": ["a", "z"]}}, "other browser jobs"),
    ({1: {0: OK}, 2: {1: OK}}, {2: {"checker": "0" * 64}}, "other check scripts"),
    ({1: {0: OK}, 2: {1: OK}}, {2: {"site": "0" * 64, "site_files": {"index.html": "0" * 64}}}, "differs in index.html"),
    ({1: {0: OK}, 2: {1: OK}}, {2: {"shard": 1}}, "appears twice"),
    ({1: {0: OK}, 2: {1: OK}}, {2: {"count": 3}}, "expected shards 1 to N"),
    ({1: {0: OK}, 2: {}}, {}, "no outcome for b"),
    ({1: {0: OK, 1: OK}, 2: {1: OK}}, {}, "outcomes in two shards"),
    ({1: {0: OK, 1: OK}, 2: {}}, {2: {"outcomes": {"5": OK}}}, "does not exist"),
    ({1: {0: OK}, 2: {1: {"errors": [3]}}}, {}, "malformed outcome"),
    ({1: {0: OK}, 2: {1: {"errors": [], "exception": None}}}, {}, "malformed outcome"),
    ({1: {0: OK}, 2: {1: OK}}, {2: {"format": 99}}, "not a format"),
    ({1: {0: OK}, 2: {1: OK}}, {2: {"format": True}}, "not a format"),
    ({1: {0: OK}, 2: {1: OK}}, {2: {"shard": []}}, "not K of N"),
    ({1: {0: OK}, 2: {1: OK}}, {2: {"shard": True}}, "not K of N"),
    ({1: {0: OK}, 2: {1: OK}}, {2: {"count": 2.0}}, "not K of N"),
    ({1: {0: OK}, 2: {1: OK}}, {2: {"shard": 3}}, "not K of N"),
    ({1: {0: OK}, 2: {1: OK}}, {2: {"site_files": ["index.html"]}}, "malformed jobs, digests or site files"),
    ({1: {0: OK}, 2: {1: OK}}, {2: {"jobs": "a,b"}}, "malformed jobs, digests or site files"),
    ({1: {0: OK}, 2: {1: OK}}, {2: {"format": 1}}, "not a format 2"),
    ({1: {0: OK}, 2: {1: OK}}, {2: {"timings": {}}}, "malformed timings"),
    ({1: {0: OK}, 2: {1: OK}}, {2: {"timings": [{"job": "z", "started": 0, "seconds": 1, "errors": 0, "exception": None}]}}, "malformed timings"),
    ({1: {0: OK}, 2: {1: OK}}, {2: {"timings": [{"job": "b", "started": 0, "seconds": 1, "errors": None, "exception": None}]}}, "malformed timings"),
    ({1: {0: OK}, 2: {1: OK}}, {2: {"timings": [{"job": "b", "started": -1, "seconds": 1, "errors": 0, "exception": None}]}}, "malformed timings"),
])
def test_load_shards_refuses_incomplete_or_foreign_results(tmp_path, shards, changes, problem):
    site, results = shard_files(tmp_path, ["a", "b"], shards, changes)
    with pytest.raises(ValueError, match=problem):
        check_page.load_shards(results, site, ["a", "b"])


def test_load_shards_refuses_missing_or_unreadable_files(tmp_path):
    site, results = shard_files(tmp_path, ["a"], {1: {0: OK}})
    with pytest.raises(ValueError, match="none found"):
        check_page.load_shards(tmp_path / "nothing", site, ["a"])
    (results / "gate-shard-1" / "shard-1.json").write_text("{")
    with pytest.raises(ValueError, match="not readable JSON"):
        check_page.load_shards(results, site, ["a"])


def test_load_shards_refuses_a_duplicated_outcome_within_one_file(tmp_path):
    site, results = shard_files(tmp_path, ["a"], {1: {0: {"errors": ["the failure"]}}})
    path = results / "gate-shard-1" / "shard-1.json"
    path.write_text(path.read_text().replace('"outcomes": {"0": {"errors": ["the failure"]}}',
                                             '"outcomes": {"0": {"errors": ["the failure"]}, "0": {"errors": []}}'))
    with pytest.raises(ValueError, match="duplicate keys"):
        check_page.load_shards(results, site, ["a"])


def local_run(stage, check, args):
    """A browser job for the local reduction: ("errors", list) returns it, ("raise", exception) raises it."""
    kind, value = args
    if kind == "raise":
        raise value
    return list(value)


def reduce(run, work, seeds):
    """The reduction as (errors, None), or how it ended: ("caught", message) for a type main() reports, else
    ("uncaught", message)."""
    try:
        return check_page.run_stages(run, work, seeds), None
    except check_page.CAUGHT as exc:
        return "caught", str(exc)
    except Exception as exc:
        return "uncaught", str(exc) if isinstance(exc, check_page.ShardJobError) else f"{type(exc).__name__}: {exc}"


@pytest.mark.parametrize("jobs, seeds", [
    ([("hydration", ("errors", []))] * 2 + [("interactions", ("errors", ["i1"])), ("interactions", ("errors", ["i2"]))], {}),
    ([("hydration", ("errors", ["h1"])), ("origins", ("errors", ["o1"])), ("interactions", ("raise", RuntimeError("late")))], {}),
    ([("hydration", ("errors", [])), ("interactions", ("errors", ["i1"]))], {"interactions": lambda: ["seed"]}),
    ([("hydration", ("errors", [])), ("interactions", ("errors", ["i1"])), ("interactions", ("raise", RuntimeError("stops")))], {}),
    ([("hydration", ("raise", ValueError("bad value"))), ("interactions", ("errors", ["i1"]))], {}),
    ([("hydration", ("raise", KeyError("uncaught"))), ("interactions", ("errors", ["i1"]))], {}),
])
def test_reduction_from_shard_results_matches_the_local_one(jobs, seeds):
    work = [(stage, None, args) for stage, args in jobs]
    outcomes = {n: check_page.outcome(value) if kind == "raise" else check_page.outcome(errors=value)
                for n, (_, _, (kind, value)) in enumerate(work)}
    indexed = [(stage, n, ()) for n, (stage, _, _) in enumerate(work)]
    assert reduce(check_page.partial(check_page.recorded, outcomes), indexed, seeds) == reduce(local_run, work, seeds)


def test_a_recorded_exception_prints_as_the_original(rendered, monkeypatch, capsys):
    _, data, dist = rendered
    (dist / "data/aisi.json").write_text(json.dumps(data))
    (dist / "index.html").write_text("Built page")
    monkeypatch.setattr(check_page, "find_chrome", lambda: "chrome")

    def fail(*args):
        raise check_page.RecordedError("headless Chrome interaction checks failed: boom", "the tail of stderr")

    monkeypatch.setattr(check_page, "render", fail)
    assert check_page.main(dist) == 1
    err = capsys.readouterr().err
    assert "error: page check failed: headless Chrome interaction checks failed: boom" in err and "the tail of stderr" in err


def fake_hydration(*args):
    """Stand-ins for browser checks in the shard round trip; run_round_trip decides what each returns."""


def fake_origins(*args):
    """See fake_hydration."""


def fake_interaction(*args):
    """See fake_hydration."""


def run_round_trip(tmp_path, monkeypatch, behaviour, count=2):
    """Write count shard files with write_shard for jobs whose behaviour is ("errors", list) or ("raise", exc),
    merge them with check_merged, and reduce the same jobs directly; return both endings (see reduce)."""
    checks = {"hydration": fake_hydration, "origins": fake_origins, "interactions": fake_interaction}
    work = [(stage, checks[stage], (n,)) for n, (stage, _) in enumerate(behaviour)]

    def run(chrome, dist, stage, check, args):
        kind, value = behaviour[args[0]][1]
        if kind == "raise":
            raise value
        return list(value)

    site = tmp_path / "site"
    site.mkdir()
    (site / "index.html").write_text("page")
    monkeypatch.setattr(check_page, "browser_jobs", lambda dist, data, screenshots: work)
    monkeypatch.setattr(check_page, "job_id", lambda stage, check, args: f"{stage}/{check.__name__}/{args[0]}")
    monkeypatch.setattr(check_page, "run_browser_job", run)
    monkeypatch.setattr(check_page, "browser_seeds", lambda dist: {"interactions": lambda: ["seed"]})
    for number in range(1, count + 1):
        check_page.write_shard("chrome", site, {}, (number, count), tmp_path / "gate" / f"gate-shard-{number}" / f"shard-{number}.json", 1)

    def merged():
        return check_page.check_merged(site, {}, tmp_path / "gate", tmp_path / "timings.jsonl")

    def local():
        return check_page.check_browser("chrome", site, {})

    return reduce_call(merged), reduce_call(local)


def reduce_call(call):
    try:
        return call(), None
    except check_page.CAUGHT as exc:
        return "caught", str(exc)
    except Exception as exc:
        return "uncaught", str(exc) if isinstance(exc, check_page.ShardJobError) else f"{type(exc).__name__}: {exc}"


@pytest.mark.parametrize("behaviour", [
    [("hydration", ("errors", [])), ("origins", ("errors", [])), ("interactions", ("errors", ["i1", "i2"])), ("interactions", ("errors", []))],
    [("hydration", ("errors", [])), ("origins", ("errors", ["o1"])), ("interactions", ("raise", RuntimeError("never reached")))],
    [("hydration", ("errors", [])), ("interactions", ("errors", ["i1"])), ("interactions", ("raise", RuntimeError("Chrome failed")))],
    [("hydration", ("raise", ValueError("bad value"))), ("interactions", ("errors", ["i1"]))],
    [("hydration", ("raise", KeyError("uncaught"))), ("interactions", ("errors", ["i1"]))],
    [("hydration", ("errors", ["\x1fh1"])), ("interactions", ("errors", ["i1", "\x1fi2"]))],  # report lines (check_page.REPORT_MARK)
])
def test_shard_files_merge_to_the_local_report(tmp_path, monkeypatch, behaviour):
    merged, local = run_round_trip(tmp_path, monkeypatch, behaviour)
    assert merged == local
    # Every job was timed in its shard, and the merge lists them all with their shard.
    lines = check_page.read_timings(tmp_path / "timings.jsonl")
    assert len(lines) == len(behaviour) and {line["shard"] for line in lines} <= {"1/2", "2/2"}
    assert sorted(line["exception"] is not None for line in lines) == sorted(kind == "raise" for _, (kind, _) in behaviour)
    assert not list((tmp_path / "gate").glob("**/*.timings"))  # the shard's working file does not outlive it


@pytest.mark.parametrize("stderr", [b"line one\nline two\n", b"x" * 2000 + b"line one", b"\x00\xff" * 1500 + b"line one", "line one " * 600])
def test_a_recorded_exception_prints_as_the_original_through_main(rendered, monkeypatch, capsys, stderr):
    _, data, dist = rendered
    (dist / "data/aisi.json").write_text(json.dumps(data))
    (dist / "index.html").write_text("Built page")
    monkeypatch.setattr(check_page, "find_chrome", lambda: "chrome")
    original = subprocess.CalledProcessError(1, ["chrome", "--dump-dom"], stderr=stderr)
    record = check_page.outcome(original)
    printed = []
    for exc in (original, None):
        def fail(*args, exc=exc):
            if exc is not None:
                raise exc
            check_page.recorded({0: record}, "hydration", 0, ())

        monkeypatch.setattr(check_page, "render", fail)
        assert check_page.main(dist) == 1
        printed.append(capsys.readouterr().err)
    assert printed[0] == printed[1] and "line one" in printed[0]


STATIC_CHECKS = ("check_method_pages", "check_rendered_content", "check_dom", "check_agent_files", "check_static_page", "check_hydrated", "check_archive_links",
                 "check_launch_assets", "check_page_data", "check_budget_ratio_downloads", "check_state_downloads", "check_state_download_buttons")


def test_a_static_failure_is_reported_without_reading_shard_results(rendered, monkeypatch, capsys, tmp_path):
    _, data, dist = rendered
    (dist / "data/aisi.json").write_text(json.dumps(data))
    (dist / "index.html").write_text("Built page")
    monkeypatch.setattr(check_page, "find_chrome", lambda: "chrome")
    monkeypatch.setattr(check_page, "render", lambda *args: ("<html></html>", "http://127.0.0.1:1/"))
    for name in STATIC_CHECKS:
        monkeypatch.setattr(check_page, name, lambda *args: [])
    monkeypatch.setattr(check_page, "check_dom", lambda *args: ["a static failure"])
    assert check_page.main(dist, merge=tmp_path / "no-shards") == 1
    err = capsys.readouterr().err
    assert "error: a static failure" in err and "shard results" not in err


def test_no_rate_amounts_stay_in_their_currency_and_say_why():
    data = {"dataset": {"exchange_rates": [{"currency": "USD"}]}, "institutes": [{"id": "ke", "display": {"comparison_funding": None},
            "funding": [{"id": "f-ke", "currency": "KES", "amount": 2_000_000_000}, {"id": "f-us", "currency": "USD"}]}]}
    tip = check_page.NO_RATE_TIP
    good = f'<span class="budget-value" data-funding-id="f-ke"><span class="budget-amount" data-tip="{tip}" tabindex="0">KES 2.0bn</span></span>'
    assert check_page.check_no_rate_amounts(good, data) == []
    assert any("why there is no euro figure" in e for e in check_page.check_no_rate_amounts(good.replace(f' data-tip="{tip}"', ""), data))
    assert any("in KES only" in e for e in check_page.check_no_rate_amounts(good.replace("KES 2.0bn", "~EUR 13m (KES 2.0bn)"), data))
    assert any("not shown" in e for e in check_page.check_no_rate_amounts("<p></p>", data))
    data["institutes"][0]["display"]["comparison_funding"] = "f-ke"
    assert any("never converted or compared" in e for e in check_page.check_no_rate_amounts(good, data))


def test_euro_amounts_follow_the_rate_and_cite_it():
    """Every amount with an ECB rate is also shown in euros, from an independent oracle, citing the rate (rule currency-rounding)."""
    data = {"dataset": {"exchange_rates": [{"currency": "JPY", "eur_per_unit": 0.00591563881476, "source_ids": ["ecb"]}]},
            "institutes": [{"id": "jp", "display": {"comparison_funding": None},
                            "funding": [{"id": "f-jp", "currency": "JPY", "amount": 8_800_000_000}, {"id": "f-ke", "currency": "KES", "amount": 2_000_000_000}]}]}
    assert check_page.expected_shown_funding_value(data["institutes"][0]["funding"][0], data) == "~EUR 52m (JPY 8.8bn)"
    assert check_page.expected_shown_funding_value(data["institutes"][0]["funding"][1], data) == "KES 2.0bn"  # no ECB rate: native, as in 1.0.2
    good = ('<div class="v"><span class="budget-value" data-funding-id="f-jp"><span class="budget-amount">~EUR 52m (JPY 8.8bn)</span></span><span class="cites"><a class="cn" data-source-id="amount">1</a>,<a class="cn" data-source-id="ecb">2</a></span></div>'
            '<span class="related-funding-item" data-funding-id="f-jp"><span class="related-funding-eur">~EUR 52m</span>: JPY 8.8bn<span class="cites"><a class="cn" data-source-id="ecb">2</a></span></span>')
    assert check_page.check_euro_amounts(good, data) == []
    assert any("expected '~EUR 52m'" in e for e in check_page.check_euro_amounts(good.replace('"related-funding-eur">~EUR 52m<', '"related-funding-eur">~EUR 52m (JPY 8.8bn)<'), data))  # the wording states the native amount
    assert any("expected '~EUR 52m (JPY 8.8bn)'" in e for e in check_page.check_euro_amounts(good.replace("~EUR 52m", "~EUR 51m", 1), data))
    assert any("does not cite the amount and exchange-rate sources" in e for e in check_page.check_euro_amounts(good.replace('<a class="cn" data-source-id="ecb">2</a>', "", 1), data))
    assert any("does not lead with its euro figure" in e for e in check_page.check_euro_amounts(good.replace('<span class="related-funding-eur">~EUR 52m</span>: ', ""), data))


def test_gate_jobs_default_follows_the_host_setting(monkeypatch):
    """A host whose memory holds one browser sets AISI_GATE_JOBS=1; elsewhere the CPUs, at most 8."""
    monkeypatch.setattr(check_page.os, "cpu_count", lambda: 16)
    assert check_page.default_jobs({}) == 8
    assert check_page.default_jobs({"AISI_GATE_JOBS": " "}) == 8
    assert check_page.default_jobs({"AISI_GATE_JOBS": "1"}) == 1
    assert check_page.default_jobs({"AISI_GATE_JOBS": "12"}) == 12  # explicit: no cap
    for bad in ("0", "two"):
        with pytest.raises(SystemExit, match="AISI_GATE_JOBS"):
            check_page.default_jobs({"AISI_GATE_JOBS": bad})


def test_a_held_request_must_have_been_held_before_release():
    """Held: a check acts while the request is pending; a hold that never caught its request fails, never passes."""
    class Route:
        def __init__(self):
            self.done = None

        def continue_(self):
            assert self.done is None, "Route is already handled!"  # Playwright: unroute settles a held route
            self.done = "continued"

        def abort(self):
            assert self.done is None, "Route is already handled!"
            self.done = "aborted"

    class Page:
        def __init__(self, arrives_after=None):
            self.handlers, self.waited, self.arrives_after, self.route_obj = {}, 0, arrives_after, Route()

        def route(self, pattern, handler):
            self.handlers[pattern] = handler

        def unroute(self, pattern, handler):
            assert self.handlers.pop(pattern) == handler  # a bound method: equal, not identical (Playwright keys it so)
            if self.route_obj.done is None:
                self.route_obj.done = "unrouted"  # as Playwright does with a route still held (measured, 1.63)

        def wait_for_timeout(self, ms):
            self.waited += ms
            if self.arrives_after is not None and self.waited >= self.arrives_after and self.handlers:
                next(iter(self.handlers.values()))(self.route_obj)

    page = Page(arrives_after=100)  # the request arrives while the check waits for it
    held = check_page.Held(page, check_page.PAGE_DATA)
    held.release()
    assert page.route_obj.done == "continued" and not page.handlers
    page = Page(arrives_after=0)
    held = check_page.Held(page, check_page.PAGE_DATA)
    held.release(abort=True)
    assert page.route_obj.done == "aborted"
    with pytest.raises(AssertionError, match="no request for"):
        check_page.Held(Page(), check_page.PAGE_DATA).wait(timeout_ms=120)


class _Opened(dict):
    """A fake page or context: the options it was opened with, and the init scripts added to it."""

    def __init__(self, options):
        super().__init__(options)
        self.scripts = []

    def add_init_script(self, script):
        self.scripts.append(script)


class _FakeBrowser:
    version = "1"

    def new_page(self, **options):
        return _Opened(options)

    def new_context(self, **options):
        return _Opened(options)


def test_pages_and_contexts_open_in_the_fixed_locale_unless_a_check_names_one():
    browser = check_page.LocalisedBrowser(_FakeBrowser())
    assert browser.new_page(color_scheme="dark") == {"locale": check_page.LOCALE, "color_scheme": "dark"}
    assert browser.new_context(locale="de-AT") == {"locale": "de-AT"}
    assert browser.version == "1"


def test_the_gate_has_one_hydration_wait_and_its_timeout_names_where(monkeypatch):
    import sys
    import types

    class PlaywrightTimeout(Exception):
        pass

    module = types.ModuleType("playwright.sync_api")
    module.TimeoutError = PlaywrightTimeout
    monkeypatch.setitem(sys.modules, "playwright", types.ModuleType("playwright"))
    monkeypatch.setitem(sys.modules, "playwright.sync_api", module)
    source = Path(check_page.__file__).read_text(encoding="utf-8")
    assert source.count("!document.documentElement.hasAttribute('data-prerendered')") == 1  # wait_hydrated only

    class Page:
        def wait_for_function(self, script, timeout):
            raise PlaywrightTimeout("Timeout 30000ms exceeded.")

    with pytest.raises(PlaywrightTimeout, match="did not take over within 30 s") as caught:
        check_page.wait_hydrated(Page())
    assert check_page.raised_at(caught.value).startswith(" at check_page.py:")
    assert check_page.raised_at(caught.value).endswith("(wait_hydrated)")
    assert check_page.raised_at(ValueError()) == ""


def test_a_run_of_named_jobs_never_reads_as_the_gate(rendered, monkeypatch, capsys):
    _, data, dist = rendered
    (dist / "data/aisi.json").write_text(json.dumps(data))
    (dist / "index.html").write_text("Built page")
    monkeypatch.setattr(check_page, "find_chrome", lambda: "chrome")
    monkeypatch.setattr(check_page, "render", lambda *args: ("<html></html>", "http://127.0.0.1:1/"))
    for name in STATIC_CHECKS:
        monkeypatch.setattr(check_page, name, lambda *args: [])
    asked = []
    monkeypatch.setattr(check_page, "check_browser", lambda chrome, dist, data, screenshots, jobs, timings, only, quarantine, limit: asked.append(only) or [])
    assert check_page.main(dist, only=["hydration/hydration_paint"]) == 0
    assert asked == [["hydration/hydration_paint"]]
    assert "not the full gate" in capsys.readouterr().out


def test_rendered_content_runs_once_in_the_static_checks_not_per_state():
    import inspect

    assert "check_rendered_content(html, data)" in inspect.getsource(check_page.main)
    passed = inspect.getsource(check_page.interaction_pass)
    for helper in ("check_budget_output", "check_staff_figures", "check_generic_sources", "check_nonidentified_folds", "BeautifulSoup("):
        assert helper not in passed, helper  # no per-state copy of a content check (filtered views keep their own)
    assert 'theme_invariant=theme == "light"' in passed  # the first screen's theme-invariant steps run once per width
    assert 'anchor = (width, theme) == (1440, "light")' in passed and passed.count("    if anchor:") == 3
    assert passed.count('if theme == "light":') == 2  # the expanded sweep's theme-invariant checks
    sweep = passed.split("# Check all controls revealed by disclosures.")[1].split("# Toolbar disclosures are exclusive")[0]
    unguarded = [line.strip() for line in sweep.splitlines() if line.startswith(("    for error in check_", "        check_"))]
    for colour in ("check_design_roles", "check_planned_swatches", "check_source_footers", "check_external_markers", "check_controls"):
        assert any(colour in line for line in unguarded), colour  # read colours: still in both themes
    for invariant in ("check_typography", "check_card_grid", "check_source_keyboard", "check_hit_targets"):
        assert not any(invariant in line for line in unguarded), invariant  # geometry and order only: light only


def test_rendered_content_reports_what_its_helpers_find(monkeypatch):
    monkeypatch.setattr(check_page, "check_staff_figures", lambda soup, data: ["staff off"])
    assert check_page.check_rendered_content("<html></html>", {}) == ["staff off"]


def test_laps_time_each_step_and_reach_the_job_line(tmp_path, monkeypatch):
    clock = iter([10.0, 10.0, 11.5, 14.0, 14.0, 20.0])  # timed_job start, step_clock start, two laps, ..., end
    monkeypatch.setattr(check_page.time, "monotonic", lambda: next(clock))

    def job(stage, check, args):
        lap = check_page.step_clock()
        lap("load")
        lap("layout")
        return []

    timings = tmp_path / "t.jsonl"
    assert check_page.timed_job(timings, 10.0, job, "interactions", check_page.check_access_fixes, ()) == []
    line = check_page.read_timings(timings)[0]
    assert line["steps"] == {"load": 1.5, "layout": 2.5} and check_page.timing_ok(line)


def test_a_job_without_laps_has_no_steps_key_and_bad_steps_are_refused(tmp_path):
    timings = tmp_path / "t.jsonl"
    check_page.timed_job(timings, 0.0, lambda stage, check, args: [], "origins", check_page.check_access_fixes, ())
    assert "steps" not in check_page.read_timings(timings)[0]
    base = {"job": "a", "started": 0, "seconds": 1, "errors": 0, "exception": None}
    assert check_page.timing_ok({**base, "steps": {"load": 1.0}})
    for bad in ({"load": -1}, {"load": "1"}, ["load"], {3: 1.0}):
        assert not check_page.timing_ok({**base, "steps": bad})


def test_interaction_pass_marks_its_steps():
    import inspect

    source = inspect.getsource(check_page.interaction_pass)
    assert source.count('lap("') == 14 and "lap = step_clock()" in source  # one per block, the last before the return


def test_each_job_line_has_only_its_own_steps_also_when_it_raises(tmp_path):
    def first(stage, check, args):
        check_page.step_clock()("load")
        return []

    def second(stage, check, args):
        lap = check_page.step_clock()
        lap("layout")
        raise RuntimeError("broke after a lap")

    timings = tmp_path / "t.jsonl"
    check_page.timed_job(timings, 0.0, first, "interactions", check_page.check_access_fixes, ())
    with pytest.raises(RuntimeError):
        check_page.timed_job(timings, 0.0, second, "interactions", check_page.check_menu_touch, ())
    one, two = check_page.read_timings(timings)
    assert set(one["steps"]) == {"load"} and set(two["steps"]) == {"layout"} and two["exception"] == "RuntimeError"


def test_pages_and_contexts_draw_every_section_unless_a_check_keeps_skipping():
    """Content checks read text that skipped sections report empty; checks of the page as shipped keep skipping."""
    browser = check_page.LocalisedBrowser(_FakeBrowser())
    for opened in (browser.new_page(), browser.new_context()):
        assert opened.scripts == [check_page.NO_SKIPPING]
    for opened in (browser.new_page(skipping=True), browser.new_context(skipping=True)):
        assert opened.scripts == [] and "skipping" not in opened


def test_a_crop_never_captures_skipped_content_blank(monkeypatch, tmp_path):
    """The contact sheet once photographed off-screen sections that content-visibility skipped, as blank areas
    reported as changes; a crop with skipped text inside it now stops the capture instead."""

    class Target:
        def evaluate(self, script, name):
            return {"x": 0, "y": 0, "width": 100, "height": 100}

    class Page:
        def __init__(self, skipped):
            self.skipped, self.shots = skipped, []

        def evaluate(self, script, clip):
            assert script == check_page.SKIPPED_IN_CLIP
            return self.skipped

        def screenshot(self, **options):
            self.shots.append(options["path"])

    monkeypatch.setattr(check_page, "section_targets", lambda page: [("cards", Target())])
    blind = Page(3)
    with pytest.raises(RuntimeError, match="skipped inside the crop"):
        check_page.capture_sections(blind, tmp_path, "1440-light")
    assert blind.shots == []
    drawn = Page(0)
    check_page.capture_sections(drawn, tmp_path, "1440-light")
    assert len(drawn.shots) == 1


@pytest.mark.parametrize("use, expected", [(True, {"hydration/hydration_paint"}), (False, set())])
def test_no_quarantine_reports_every_finding_but_still_validates_the_register(quarantine_file, rendered, monkeypatch, use, expected):
    _, data, dist = rendered
    (dist / "data/aisi.json").write_text(json.dumps(data))
    (dist / "index.html").write_text("Built page")
    quarantine_file.write_text(json.dumps([quarantine_entry()]))
    monkeypatch.setattr(check_page, "find_chrome", lambda: "chrome")
    monkeypatch.setattr(check_page, "render", lambda *args: ("<html></html>", "http://127.0.0.1:1/"))
    for name in STATIC_CHECKS:
        monkeypatch.setattr(check_page, name, lambda *args: [])
    seen = []
    monkeypatch.setattr(check_page, "check_browser", lambda *args: seen.append(set(args[7])) or [])  # the quarantine argument
    assert check_page.main(dist, use_quarantine=use) == 0 and seen == [expected]
    quarantine_file.write_text(json.dumps([quarantine_entry(owner="")]))  # a malformed register fails even with --no-quarantine
    assert check_page.main(dist, use_quarantine=use) == 1


@pytest.mark.parametrize("left, limit, code, marker", [
    (5000.0, None, 0, ""),  # room for the full watchdog: unchanged, no marker
    (100.0, 100.0, 0, "job deadline: watchdog lowered to 100 s"),
    (0.5, None, 3, "NOT RUN: job deadline passed"),
])
def test_job_deadline_counts_from_the_process_start_and_lowers_the_watchdog(rendered, monkeypatch, capsys, left, limit, code, marker):
    _, data, dist = rendered
    (dist / "data/aisi.json").write_text(json.dumps(data))
    (dist / "index.html").write_text("Built page")
    monkeypatch.setattr(check_page, "find_chrome", lambda: "chrome")
    monkeypatch.setattr(check_page, "render", lambda *args: ("<html></html>", "http://127.0.0.1:1/"))
    for name in STATIC_CHECKS:
        monkeypatch.setattr(check_page, name, lambda *args: [])
    limits = []
    monkeypatch.setattr(check_page, "check_browser", lambda *args: limits.append(args[8]) or [])
    now = check_page.time.monotonic()
    monkeypatch.setattr(check_page, "PROCESS_START", now - 1000.0)  # this process started 1000 s ago
    monkeypatch.setattr(check_page, "guard_setup", lambda deadline: None)  # the start-up timer would end this worker; tested in a child below
    assert check_page.main(dist, only=["hydration/hydration_paint"], deadline=1000.0 + left) == code
    err = capsys.readouterr().err
    if code == 0:
        assert len(limits) == 1 and (limits[0] == check_page.JOB_LIMIT if limit is None else abs(limits[0] - limit) < 1)
    else:
        assert limits == []
    assert marker in err


def test_job_deadline_also_bounds_start_up_and_static_checks(tmp_path):
    # A real child process: its static checks outlast the deadline, so it ends NOT_RUN with the marker, on time.
    script = tmp_path / "slow.py"
    script.write_text(
        "import sys, time\n"
        f"sys.path.insert(0, {str(Path(check_page.__file__).parent)!r})\n"
        "import check_page\n"
        "check_page.find_chrome = lambda: 'chrome'\n"
        "check_page.load_quarantine = lambda: {}\n"
        "check_page.guard_setup(1.5)\n"
        "time.sleep(30)\n"
        "print('not stopped')\n"
    )
    started = __import__("time").monotonic()
    result = subprocess.run([sys.executable, str(script)], capture_output=True, text=True, timeout=60)
    took = __import__("time").monotonic() - started
    assert result.returncode == check_page.NOT_RUN and check_page.DEADLINE_PASSED in result.stderr and "not stopped" not in result.stdout
    assert took < 25


@pytest.mark.parametrize("jobs, ok", [("hydration/hydration_paint", True), ("hydration/hydration_paint,hydration/hydration_edges", False)])
def test_job_deadline_takes_exactly_one_job(jobs, ok):
    result = subprocess.run([sys.executable, str(Path(check_page.__file__)), "--job", jobs, "--jobs", "1", "--job-deadline", "1", "--find-chrome"],
                            capture_output=True, text=True, timeout=120)
    assert ("exactly one job" in result.stderr) != ok



def test_job_deadline_needs_one_browser_job_at_a_time():
    result = subprocess.run([sys.executable, str(Path(check_page.__file__)), "--job", "hydration/hydration_paint", "--jobs", "2",
                             "--job-deadline", "1", "--find-chrome"], capture_output=True, text=True, timeout=120)
    assert "--jobs 1" in result.stderr


def test_the_deadline_timer_stays_armed_through_the_job(tmp_path):
    # A real child: the job's hand-over and work outlast the deadline after job_limit() ran; the timer still ends it
    # NOT_RUN with the marker, by the deadline, so no pass is reported after it.
    script = tmp_path / "slow_job.py"
    script.write_text(
        "import sys, time\n"
        f"sys.path.insert(0, {str(Path(check_page.__file__).parent)!r})\n"
        "import check_page\n"
        "check_page.guard_setup(2.0)\n"
        "check_page.job_limit(2.0)\n"
        "time.sleep(30)\n"
        "print('passed after the deadline')\n"
    )
    result = subprocess.run([sys.executable, str(script)], capture_output=True, text=True, timeout=60)
    assert result.returncode == check_page.NOT_RUN and check_page.DEADLINE_PASSED in result.stderr and "passed after" not in result.stdout


def test_the_definition_must_come_before_its_rule_link():
    data = {"dataset": {"definition": "Example.", "status_definitions": {}}}
    html = '<div id="headline"></div><p id="definition-link"><a href="#definition">Read</a></p><section id="method-sec"><div class="section-body"><p id="definition">Example. <a href="method.html#qualifying-body">What counts</a></p></div></section>'
    assert check_page.check_supporting_text(BeautifulSoup(html, "html.parser"), data) == []
    moved = html.replace('Example. <a href="method.html#qualifying-body">What counts</a>', '<a href="method.html#qualifying-body">What counts</a> Example.')
    assert any("dataset.definition" in e for e in check_page.blocking(check_page.check_supporting_text(BeautifulSoup(moved, "html.parser"), data)))


def test_the_new_tab_notice_keeps_its_meaning_in_any_wording():
    source = check_page.check_external_markers.__code__.co_consts
    script = next(c for c in source if isinstance(c, str) and "external link lost accessible new-tab text" in c)
    assert "/new (tab|window)/i" in script  # a notice that no longer says it opens a new tab or window fails


def test_skip_static_runs_only_the_job_but_still_scans_for_the_reserved_mark(rendered, monkeypatch, capsys):
    _, data, dist = rendered
    (dist / "data/aisi.json").write_text(json.dumps(data))
    (dist / "index.html").write_text("Built page")
    monkeypatch.setattr(check_page, "find_chrome", lambda: "chrome")
    monkeypatch.setattr(check_page, "render", lambda *args: (_ for _ in ()).throw(AssertionError("rendered")))
    for name in STATIC_CHECKS:
        monkeypatch.setattr(check_page, name, lambda *args: (_ for _ in ()).throw(AssertionError("static check ran")))
    ran = []
    monkeypatch.setattr(check_page, "check_browser", lambda *args: ran.append(args[6]) or [])
    assert check_page.main(dist, only=["hydration/hydration_paint"], use_quarantine=False, skip_static=True) == 0
    assert ran == [["hydration/hydration_paint"]] and "static checks skipped" in capsys.readouterr().out
    (dist / "index.html").write_text("Built page \x1f")
    assert check_page.main(dist, only=["hydration/hydration_paint"], use_quarantine=False, skip_static=True) == 1


@pytest.mark.parametrize("args, ok", [
    (["--job", "hydration/hydration_paint", "--no-quarantine", "--skip-static"], True),
    (["--job", "hydration/hydration_paint", "--skip-static"], False),
    (["--skip-static", "--no-quarantine"], False),
])
def test_skip_static_is_only_for_detector_runs(args, ok):
    result = subprocess.run([sys.executable, str(Path(check_page.__file__)), *args, "--list-jobs"], capture_output=True, text=True, timeout=120)
    assert result.returncode == (0 if ok else 2) and ("--skip-static is for the flake detector" in result.stderr) != ok


MOVED_TO_CONTENT = {  # content family module -> the check_page helpers it now runs
    "funding": ("check_budget_output", "check_funding_outside", "check_interval_output", "check_per_person", "check_no_rate_amounts", "check_euro_amounts"),
    "sources": ("check_source_alignment", "check_generic_sources"),
    "record_rows": ("check_leadership_rows", "check_access_groups", "check_setup_groups"),
    "summary": ("check_summary_data", "check_figures_row", "check_two_significant_figures"),
    "activity": ("check_operating_output",),
    "names": ("check_section_bar", "check_institute_names", "check_former_names", "check_nonidentified_folds"),
}
MOVED_HELPERS = tuple(h for helpers in MOVED_TO_CONTENT.values() for h in helpers)


def test_each_moved_content_family_has_one_home(monkeypatch):
    # D2: each helper of a moved family runs exactly once in the content step (tests/content) and not at all in the
    # browser gate's static pass. Spies record what each path actually calls.
    import importlib

    calls = []
    for name in MOVED_HELPERS:
        monkeypatch.setattr(check_page, name, lambda *args, name=name: calls.append(name) or [])
    for module, helpers in MOVED_TO_CONTENT.items():
        calls.clear()
        for helper in importlib.import_module(f"content.test_{module}").FAMILY.values():
            helper("<html></html>", {})
        assert sorted(calls) == sorted(helpers), module  # each once, none missing, none twice
    calls.clear()
    rendered = check_page.check_rendered_content  # the real browser-path function; its other callees stubbed
    for name in [n for n in dir(check_page) if n.startswith("check_") and n not in MOVED_HELPERS and n != "check_rendered_content"]:
        monkeypatch.setattr(check_page, name, lambda *args, **kwargs: [])
    rendered("<html></html>", {})
    assert calls == []  # no moved helper runs in the browser gate any more
