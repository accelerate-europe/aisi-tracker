import subprocess
import sys
import copy
import json
from collections import Counter
from pathlib import Path

from bs4 import BeautifulSoup
import pytest

from conftest import ROOT, SOURCE_URL
from scripts import check_page


def test_legend_definitions_allow_rewording_but_require_every_status_once():
    statuses = ("established", "planned", "proposed", "exploring", "none_identified")
    data = {"dataset": {"status_definitions": dict.fromkeys(statuses, "Full definition."), "definition": "Example.", "version": "1.0.0"}, "jurisdictions": [], "institutes": [], "changelog": [{"date": "2026-11-09"}]}
    entries = {status: f'<div class="legend-entry" data-status="{status}"><i class="sw st-{status}"></i><abbr class="g legend-name" data-exp="Reworded definition.">{status}</abbr></div>' for status in statuses}
    html = '<div id="legend">' + "".join(entries.values()) + '</div>'

    def errors(markup):
        return [e for e in check_page.check_summary_data(markup, data) if e.startswith("legend")]

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
    ("article.card", "status cards"),
    (".card.mini", "mini-cards"),
    (".smark", "status mark"),
    ("#world-rows tr", "world table"),
    (".cn", "citation marks missing"),
])
def test_missing_elements_fail(rendered, selector, message):
    rendered[0].select_one(selector).decompose()
    assert any(message in e for e in errors_for(rendered))


@pytest.mark.parametrize(("selector", "message"), [
    ("article.card", "status cards"),
    (".card.mini", "mini-cards"),
    ("#world-rows tr", "world table"),
])
def test_duplicate_elements_fail(rendered, selector, message):
    soup = rendered[0]
    node = soup.select_one(selector)
    node.insert_after(BeautifulSoup(str(node), "html.parser"))
    assert any(message in e for e in errors_for(rendered))


@pytest.mark.parametrize(("selector", "href", "message"), [
    (".cn", "#world", "not a source URL"),
])
def test_broken_links_fail(rendered, selector, href, message):
    rendered[0].select_one(selector)["href"] = href
    assert any(message in e for e in errors_for(rendered))


def test_missing_citation_href_fails(rendered):
    del rendered[0].select_one(".cn")["href"]
    assert any("not a source URL" in e for e in errors_for(rendered))


def test_swapped_jurisdiction_cannot_hide_behind_card_count(rendered):
    rendered[0].select_one("article.card")["data-j"] = "AU"
    assert any("status cards" in e for e in errors_for(rendered))


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
    """The build, gate, capture, sweep and source monitor pin the same Playwright, so one browser."""
    import tomllib

    pins = {}
    for lock in sorted((ROOT / "scripts").glob("*.py.lock")):
        packages = {p["name"]: p["version"] for p in tomllib.loads(lock.read_text(encoding="utf-8")).get("package", [])}
        if "playwright" in packages:
            pins[lock.name] = packages["playwright"]
    assert {"check_page.py.lock", "prerender.py.lock", "monitor_browser.py.lock", "sweep_page.py.lock"} <= set(pins)
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


def test_every_browser_starts_without_a_session_bus(monkeypatch, tmp_path):
    """Chromium with a session bus moves its browser process into a systemd scope of its own, out of a job's memory cap.

    Each entry point that starts Chromium (the gate's session, the contact-sheet capture and the pre-render)
    runs against a fake Playwright that records the environment the browser would receive."""
    import os
    import types
    from contextlib import contextmanager

    from scripts import prerender

    class Launched(Exception):
        pass

    environments = []

    def launch(*args, **kwargs):
        environments.append(kwargs.get("env"))  # without env, Playwright passes the caller's environment on
        raise Launched

    @contextmanager
    def sync_playwright():
        yield types.SimpleNamespace(chromium=types.SimpleNamespace(launch=launch, launch_persistent_context=launch))

    module = types.ModuleType("playwright.sync_api")
    module.sync_playwright = sync_playwright
    monkeypatch.setitem(sys.modules, "playwright", types.ModuleType("playwright"))
    monkeypatch.setitem(sys.modules, "playwright.sync_api", module)
    monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", "unix:path=/run/user/1000/bus")
    with pytest.raises(Launched), check_page.chrome_session("chrome", tmp_path):
        pass
    with pytest.raises(Launched):
        check_page.capture_states("chrome", tmp_path, tmp_path / "states")
    with pytest.raises(Launched):
        prerender.prerender(tmp_path, "chrome")
    assert len(environments) == 3
    for env in environments:
        assert env is not None and env["DBUS_SESSION_BUS_ADDRESS"] == "disabled:"
        assert env["PATH"] == os.environ["PATH"]  # the rest of the environment is kept



def test_budget_oracle_rejects_wrong_id_and_missing_value_structure():
    data = {"jurisdictions": [{"id": "JP", "status": "established"}], "institutes": [{"id": "jp-aisi", "jurisdiction_id": "JP", "funding": []}]}
    identity = "funding-7d0b36a48cc65315865a6d6ec83ddeb6"
    value = f'<span class="budget-value" data-funding-id="{identity}"><span class="budget-amount">~EUR 52m (JPY 8.8bn)</span><span class="budget-qualifiers"><span class="meta-item">one-off</span><span class="sep-gap"> </span><span class="meta-item">FY2025 supplementary</span><span class="sep-gap"> </span><span class="meta-item">adopted</span></span></span>'
    html = f'<article class="card" data-j="JP"><div class="row" data-track="9"><div class="k">Budget</div><div class="v">{value}</div></div></article><table><tbody id="world-rows"><tr data-j="JP"><td data-col="budget">{value}</td></tr></tbody></table><div id="fund-rows"><div class="fund-list" data-kind="budget"><div class="fund-other-line"><button class="fund-name-row" data-institute="jp-aisi" data-funding-id="{identity}">Japan: one-off amount, in the records</button></div></div></div>'
    assert check_page.check_budget_output(html, data) == []
    assert any("wrong funding ID" in error for error in check_page.check_budget_output(html.replace(identity, "older-record"), data))
    assert any("separate amount and qualifiers" in error for error in check_page.check_budget_output(html.replace("budget-qualifiers", "removed"), data))
    assert any("latest own JPY 8.8bn" in error for error in check_page.check_budget_output(html.replace("~EUR 52m (JPY 8.8bn)", "~EUR 2.2m (JPY 380m)"), data))
    assert any("latest own JPY 8.8bn" in error for error in check_page.check_budget_output(html.replace("~EUR 52m (JPY 8.8bn)", "JPY 8.8bn"), data))


def test_budget_oracle_covers_institutes_before_establishment():
    """Norway's proposed institute shows its selected ~NOK 45m record; Not published there is an error."""
    identity = check_page.EXPECTED_BUDGET_IDS["no-ki-norge"]
    data = {"jurisdictions": [{"id": "NO", "status": "proposed"}], "institutes": [{"id": "no-ki-norge", "jurisdiction_id": "NO", "funding": []}]}
    value = f'<span class="budget-value" data-funding-id="{identity}"><span class="budget-amount">~NOK 45m</span><span class="budget-qualifiers"><span class="meta-item">a year</span><span class="sep-gap"> </span><span class="meta-item">2027 budget proposal</span></span></span>'
    html = f'<article class="card" data-j="NO"><div class="row" data-track="9"><div class="k">Budget</div><div class="v">{value}</div></div></article><table><tbody id="world-rows"><tr data-j="NO"><td data-col="budget">{value}</td></tr></tbody></table>'
    assert check_page.check_budget_output(html, data) == []
    missing = html.replace(value, '<span class="missing">Not published</span>')
    assert any("wrong funding ID" in error for error in check_page.check_budget_output(missing, data))
    assert any("separate amount and qualifiers" in error for error in check_page.check_budget_output(html.replace("budget-amount", "removed"), data))


def test_funding_charts_rank_their_own_figure_with_state_and_institute_ties(monkeypatch):
    monkeypatch.setattr(check_page, "EXPECTED_FUNDING_IDS", {"AA": "annual-a", "ZZ": "annual-z", "PP": "annual-p"})
    monkeypatch.setattr(check_page, "EXPECTED_BUDGET_IDS", {"aa": "annual-a", "zz": "annual-z", "pp": "annual-p", "own": "one-off", "proposed": "proposal"})
    data = {
        "jurisdictions": [
            {"id": jid, "name": name, "status": status}
            for jid, name, status in (("AA", "Alpha", "established"), ("ZZ", "Zulu", "established"),
                                      ("OO", "Own", "established"), ("BB", "Beta", "established"),
                                      ("PP", "Pending", "planned"), ("NN", "New", "proposed"), ("MM", "Missing", "exploring"))
        ],
        "institutes": [
            {"id": "missing-b", "jurisdiction_id": "BB", "staff": [{"total": 900, "as_of": "2026"}]},
            {"id": "missing-a", "jurisdiction_id": "BB"},
            {"id": "own", "jurisdiction_id": "OO"},
            {"id": "aa", "jurisdiction_id": "AA", "funding": [{"id": "annual-a", "eur_per_year_est": 10}]},
            {"id": "zz", "jurisdiction_id": "ZZ", "funding": [{"id": "annual-z", "eur_per_year_est": 20}]},
            {"id": "unpublished", "jurisdiction_id": "MM"},
            {"id": "proposed", "jurisdiction_id": "NN"},
            {"id": "pp", "jurisdiction_id": "PP", "funding": [{"id": "annual-p", "eur_per_year_est": 30}]},
        ],
    }
    assert check_page.expected_funding_rows(data) == {
        "budget": ["zz", "aa"],
        "staff": ["missing-b"],
    }
    next(i for i in data["institutes"] if i["id"] == "aa")["funding"][0]["eur_per_year_est"] = 20
    assert check_page.expected_funding_rows(data)["budget"] == ["aa", "zz"]
    # A technical count or total does not promote an institute into the own-funding group.
    assert check_page.funding_groups(data) == ({"AA", "ZZ"}, {"OO"}, {"BB"})


@pytest.fixture
def funding_rows_case():
    data = {
        "dataset": {"as_of": "2026-11-19"},
        "jurisdictions": [{"id": "XX", "name": "Example", "status": "established"}, {"id": "YY", "name": "Another", "status": "established"}, {"id": "ZZ", "name": "Pending", "status": "proposed"}],
        "institutes": [
            {"id": "xx", "jurisdiction_id": "XX", "staff": [{"total": 140, "total_qualifier": "at_least", "as_of": "2026-10"}], "display": {"staff_records": {}}},
            {"id": "yy", "jurisdiction_id": "YY", "staff": [], "display": {"staff_records": {}}},
            {"id": "zz", "jurisdiction_id": "ZZ", "staff": [{"total": 500, "as_of": "2026"}]},
        ],
    }

    def opener(iid, cls="fund-name-row"):
        name = {"xx": "Example", "yy": "Another"}[iid]
        return f'<button class="{cls}" data-institute="{iid}" data-j="{iid.upper()}" aria-label="Funding and staff records: {iid}" aria-expanded="false" aria-controls="records-{iid}">' + (name if cls == "fund-name-row" else "") + "</button>"

    def missing_line(kind, ids):
        names = ", ".join({"xx": "Example", "yy": "Another"}[iid] for iid in ids)
        rows = "".join('<li class="fund-name-item">' + opener(iid) + '</li>' for iid in ids)
        identity = f"fund-{kind}-missing-list"
        return (f'<div class="fund-missing-line"><button class="fund-names-toggle" type="button" aria-expanded="false" aria-controls="{identity}">'
                f'<span class="fund-missing-names">Not published: {names}</span></button>'
                f'<ul class="fund-name-list" id="{identity}" role="list" hidden>{rows}</ul></div>')

    html = ('<p id="fund-chart-h">Established institutes only; planned and proposed ones remain in their records. Reference year 2026</p><div id="fund-rows">'
            '<div class="fund-list" data-kind="budget"><div class="fund-budget-head"><h3>Budget</h3></div><ul class="fund-chart-rows"></ul>'
            '<div class="fund-other"></div>' + missing_line("budget", ("xx", "yy")) + '</div>'
            '<div class="fund-list" data-kind="staff"><div class="fund-staff-head"><h3>Staff</h3></div><ul class="fund-chart-rows">'
            '<li class="fund-item" data-j="XX" data-institute="xx">' + opener("xx", "fund-row")
            + '<div class="fund-measure fund-staff"><div class="fund-bar"><span class="bar-value"></span></div><div class="fund-value"><span class="fund-figure"><strong><abbr class="g val" tabindex="0" aria-description="As at Oct 2026, at least.">140+</abbr></strong></span></div></div></li></ul>'
            '<div class="fund-other"></div>' + missing_line("staff", ("yy",)) + '</div>'
            '<div id="fund-records">' + ''.join(f'<details class="fund-detail" data-institute="{iid}" id="records-{iid}"><summary>Funding and staff records</summary></details>' for iid in ("xx", "yy"))
            + '</div></div><a id="funding-download" href="data/funding.csv" download data-tip="Staff records are in staff.csv">Funding CSV</a>')
    return BeautifulSoup(html, "html.parser"), data


def test_funding_rows_cover_established_institutes_and_keep_dated_staff(funding_rows_case):
    soup, data = funding_rows_case
    assert check_page.check_funding_outside(str(soup), data) == []


@pytest.mark.parametrize("commitment", ["adopted", "announced"])
@pytest.mark.parametrize("shown,valid", [
    ("A year, average over 2026-27 to 2029-30, {commitment}.", True),
    ("A year, {commitment}.", False),
    ("A year, average over 2026-27 to 2029-30.", False),
    ("A year.", False),
    ("{commitment}.", False),
    ("A year, average over 2026-27 to 2029-30, proposed.", False),
    ("A year, 2026-27 to 2029-30, {commitment}.", False),
])
def test_budget_explanation_keeps_period_basis_and_commitment(funding_rows_case, monkeypatch, commitment, shown, valid):
    soup, data = funding_rows_case
    monkeypatch.setitem(check_page.EXPECTED_FUNDING_IDS, "XX", "budget")
    data["institutes"][0]["funding"] = [{"id": "budget", "eur_per_year_est": 100, "commitment": commitment, "annual_basis_code": "period_mean", "period": "2026-27 to 2029-30"}]
    chart = soup.select_one('.fund-list[data-kind="budget"]')
    chart.select_one('.fund-name-row[data-institute="xx"]').parent.decompose()
    chart.select_one('.fund-missing-names').string = "Not published: Another"
    row = copy.deepcopy(soup.select_one(".fund-item"))
    row["data-funding-id"] = "budget"
    row.select_one(".fund-measure")["class"] = "fund-measure fund-budget"
    figure = row.select_one(".fund-figure abbr")
    figure["aria-description"] = shown.format(commitment=commitment)
    figure.string = "EUR 100"
    chart.select_one(".fund-chart-rows").append(row)
    errors = check_page.check_funding_outside(str(soup), data)
    assert (errors == []) is valid, errors
    if not valid:
        assert any("Budget explanation" in error for error in errors)


@pytest.mark.parametrize("mutation, finding", [
    ("duplicate", "each institute once"),
    ("missing", "each institute once"),
    ("staff", "every printed staff figure"),
    ("staff-date", "published dated total only"),
    ("staff-bound", "must be stated in words"),
    ("missing-list", "Not published list"),
    ("partial-clause", "recorded date"),
    ("duplicate-records", "canonical records"),
    ("no-tooltip", "focusable tooltip"),
    ("different-tooltip", "accessible description"),
    ("pending", "each institute once"),
    ("bound-symbol", "bound markers"),
    ("scope", "funding scope"),
])
def test_funding_row_mutants_cannot_drop_coverage_or_qualifications(funding_rows_case, mutation, finding):
    soup, data = funding_rows_case
    row = soup.select_one(".fund-item")
    if mutation == 'duplicate':
        row.insert_after(BeautifulSoup(str(row), "html.parser"))
    elif mutation == 'missing':
        row.decompose()
    elif mutation == 'staff':
        row.select_one(".fund-value").decompose()
    elif mutation == 'staff-date':
        data["institutes"][0]["staff"][0].pop("as_of")
    elif mutation == 'staff-bound':
        row.select_one(".fund-figure abbr")["aria-description"] = "As at Oct 2026."
    elif mutation == 'missing-list':
        soup.select_one('.fund-list[data-kind="budget"] .fund-missing-line .fund-name-row').extract()
    elif mutation == 'partial-clause':
        row.select_one(".fund-figure abbr")["aria-description"] = "As at, at least."
    elif mutation == 'duplicate-records':
        detail = soup.select_one(".fund-detail")
        detail.insert_after(BeautifulSoup(str(detail), "html.parser"))
    elif mutation == 'no-tooltip':
        del row.select_one(".fund-figure abbr")["aria-description"]
    elif mutation == 'different-tooltip':
        row.select_one(".fund-figure abbr")["data-tip"] = "Something else."
    elif mutation == 'pending':
        row.select_one(".fund-row")["data-institute"] = "zz"
    elif mutation == 'bound-symbol':
        row.select_one(".fund-figure abbr").string = "140"
    elif mutation == 'scope':
        soup.select_one("#fund-chart-h").string = "Reference year 2026"
    assert any(finding in error for error in check_page.check_funding_outside(str(soup), data))


@pytest.mark.parametrize("record, text", [
    ({"technical": 12, "as_of": "2026-10"}, "technical staff"),
    ({"target": 100, "as_of": "2026-10"}, "staff target"),
    ({"description": "Between 20 and 30 staff", "as_of": "2026-10"}, "planned range"),
])
def test_technical_counts_targets_and_ranges_keep_words_without_a_staff_bar(funding_rows_case, record, text):
    soup, data = funding_rows_case
    data["institutes"][1]["staff"] = [record]
    chart = soup.select_one('.fund-list[data-kind="staff"]')
    button = chart.select_one('.fund-name-row[data-institute="yy"]')
    button.string = "Another (" + text + ")"
    line = chart.select_one(".fund-missing-line").extract()
    line["class"] = "fund-other-line"
    names = line.select_one(".fund-missing-names")
    names["class"] = "fund-other-names"
    names.string = "Not compared: Another (" + text + ")"
    listing = line.select_one(".fund-name-list")
    listing["id"] = "fund-staff-other-list"
    line.select_one(".fund-names-toggle")["aria-controls"] = listing["id"]
    chart.select_one(".fund-other").append(line)
    assert check_page.check_funding_outside(str(soup), data) == []
    button.string = "Another (100 people)"
    names.string = "Not compared: Another (100 people)"
    assert any("unscaled amounts must use words" in error for error in check_page.check_funding_outside(str(soup), data))


@pytest.mark.parametrize("mutation, finding", [
    ("missing-summary-name", "all names and reasons as plain text"),
    ("wrong-state", "institute identity and state name"),
    ("wrong-name", "institute identity and state name"),
])
def test_funding_names_disclosure_mutants_reject_broken_structure_and_saved_text(funding_rows_case, mutation, finding):
    soup, data = funding_rows_case
    line = soup.select_one('.fund-list[data-kind="budget"] .fund-missing-line')
    toggle, listing = line.select_one(".fund-names-toggle"), line.select_one(".fund-name-list")
    names, row = toggle.select_one(".fund-missing-names"), listing.select_one(".fund-name-item")
    button = row.select_one(".fund-name-row")
    if mutation == 'missing-summary-name':
        names.string = "Not published: Example"
    elif mutation == 'wrong-state':
        button["data-j"] = "YY"
    elif mutation == 'wrong-name':
        button.string = "Wrong name"
    assert any(finding in error for error in check_page.check_funding_outside(str(soup), data))


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
    assert check_page.check_setup_groups(renamed, data) == ["set-up heading is missing or empty"]
    renamed.select_one("#set-up").decompose()
    assert check_page.check_setup_groups(renamed, data) == ["set-up heading is missing or empty"]
    assert any("lists" in error for error in check_page.check_setup_groups(page([("NO", "proposed"), ("GB", "established")]), data))  # status on the ladder first, then name
    assert any("lists" in error for error in check_page.check_setup_groups(page([("GB", "established")]), data))
    assert any("marks differ" in error for error in check_page.check_setup_groups(page([("GB", "established"), ("NO", "established")]), data))


def test_uncited_values_need_a_citation_link_except_absence():
    from bs4 import BeautifulSoup

    html = """<table><tbody id="world-rows">
      <tr data-j="GB"><td data-col="net">Member<span class="cites"><a class="cn" href="#">1</a></span></td><td data-col="budget"><span class="missing">Not published</span></td></tr>
      <tr data-j="US"><td data-col="net">Member</td><td data-col="op">None found yet</td></tr>
      <tr data-j="FR"><td data-col="op"><span class="activity-kind">Joint testing<span class="cites"><a class="cn" href="#">1</a></span></span><span class="activity-kind">Published work</span></td></tr>
    </tbody></table>
    <article class="card" data-j="KR"><div class="row" data-track="12"><div class="k">Related funding</div><div class="v"><span class="related-funding-item" data-funding-id="f1">KRW 20.1bn<span class="cites"></span></span></div></div></article>"""
    found = check_page.uncited_values(BeautifulSoup(html, "html.parser"))
    assert found == ["card KR: Related funding", "related funding f1", "table US: net", "table FR: op"]


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
        state = next(j['name'] for j in _names_data()['jurisdictions'] if j['id'] == jid)
        return f'<article class="card" id="c-{jid.lower()}" data-j="{jid}" aria-labelledby="{jid}-name"><div class="top"><h4 class="name" id="{jid}-name" title="{state}">{state}</h4></div><div class="row" data-track="2"><div class="k">Institute</div><div class="v">{name}</div></div></article>'
    def funding(jid, iid, short, name):
        state = next(j['name'] for j in _names_data()['jurisdictions'] if j['id'] == jid)
        base = name.removesuffix(f" ({state})")
        return f'<li class="fund-item" data-j="{jid}" data-institute="{iid}"><button class="fund-row" aria-label="{name} ({short})" aria-description="{name} ({short})"></button><div class="fund-identity"></div><details class="fund-detail" data-institute="{iid}"><summary><span class="fund-head-name" title="{state}, {base}">{state}, {base}</span></summary></details></li>'
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
        + '<div id="fund-rows">' + funding("ES", "es-isia", "Spain AISI", "AI Security Institute (Spain)") + "</div>"
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
    assert any("does not show the short form" in e for e in findings)
    data["institutes"][0]["short_name"] = "AISI"
    assert check_page.check_institute_names(_names_html(gb_short="AISI"), data) == []
    data["institutes"][0]["short_name"] = "UK AISI"
    # The table shows the full name instead of the recorded short form.
    assert any("does not show the short form" in e for e in check_page.check_institute_names(_names_html(fr_short="National Institute"), data))
    # A short form that does not give the full name on hover.
    html = _names_html().replace('data-exp="AI Security Institute (Spain)"', 'data-exp="Spain"')
    assert any("on hover" in e for e in check_page.check_institute_names(html, data))
    # A card or table row that disappears, (the funding row cases follow).
    assert any("card has no Institute row" in e for e in check_page.check_institute_names(_names_html().replace('id="c-es"', 'id="c-xx"'), data))
    assert any("ES table cell" in e for e in check_page.check_institute_names(_names_html().replace('<tr data-j="ES">', '<tr data-j="XX">'), data))
    # Compact Funding openers expose both names on focus; the detail always spells the full name out.
    bad = _names_html().replace('>Spain, AI Security Institute</span>', '>Spain, Spain AISI</span>')
    assert any("funding" in e and "name" in e for e in check_page.check_institute_names(bad, data))
    bad = BeautifulSoup(_names_html(), "html.parser")
    del bad.select_one(".fund-row")["aria-description"]
    assert any("funding" in e and "focus" in e for e in check_page.check_institute_names(str(bad), data))
    tab = _names_html().replace('>Spain, AI Security Institute</span>', '>Spain, AI Security Institute<span class="vh"> Opens elsewhere</span></span>')
    assert check_page.check_institute_names(tab, data) == []
    # No funding rows at all is a failure, not a pass.
    assert any("no funding rows" in e for e in check_page.check_institute_names(_names_html().replace('id="fund-rows"', 'id="x"'), data))


WORDS = {"single_point_of_contact": "single point of contact", "market_surveillance": "market-surveillance authority", "notifying": "notifying authority"}


@pytest.mark.parametrize("linked", [False, True])
def test_institute_local_name_is_the_complete_names_tooltip(linked):
    data = _names_data()
    data["institutes"][0]["name_local"] = "Institut local"
    shown = "AI Security Institute (United Kingdom)"
    soup = BeautifulSoup(_names_html(), "html.parser")
    value = soup.select_one('#c-gb .v')
    tag = 'a href="https://example.org/institute"' if linked else 'span tabindex="0"'
    end = 'a' if linked else 'span'
    value.clear()
    value.append(BeautifulSoup(f'<{tag} class="original-text" aria-description="Institut local">{shown}</{end}>', "html.parser"))
    html = str(soup)
    assert check_page.check_institute_names(html, data) == []
    for broken in (
        html.replace('aria-description="Institut local"', 'aria-description="Wrong original"'),
        html.replace('aria-description="Institut local"', 'aria-description="Institut local" data-tip="Wrong override"'),
        html.replace('href="https://example.org/institute"' if linked else 'tabindex="0"', ''),
        html.replace('class="original-text"', 'class="old-route"'),
    ):
        assert any('Institute: the shown name must carry name_local' in e for e in check_page.check_institute_names(broken, data))
    data['institutes'][0]['name_local'] = shown
    assert check_page.check_institute_names(_names_html(), data) == []


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
        "dataset": {"display": {"authority_roles": WORDS}},
    }


def _authority(name, roles="market-surveillance authority, proposed", original="", det=""):
    tip = f' class="original-text" aria-description="{original}" tabindex="0"' if original else ""
    return (f'<li><span class="body-name"><span{tip}>{name}</span><span class="body-roles">: {roles}<span class="cites"><a class="cn">1</a></span></span></span>'
            f'{det}</li>')


def _fold_html(roles="market-surveillance authority, proposed", det='<span class="det">Proposed market authority.</span>', original=True, label="AI Act authorities", extra=""):
    orig = 'Комисия за финансов надзор' if original else ""
    return (
        '<details class="card mini" id="c-bg" data-j="BG"><summary><span class="name">Bulgaria</span></summary>'
        f'<div class="body-group"><p class="gl">{label}</p><ul>{_authority("Financial Supervision Commission", roles, orig, det)}</ul>{extra}</div>'
        '<div class="body-group"><p class="gl">Other bodies</p><ul><li><span class="body-name">INSAIT (Sofia University)<span class="cites"><a class="cn">1</a></span></span></li></ul></div>'
        "</details>"
    )


def test_nonidentified_folds_pass_and_fail_on_each_rule():
    data = _fold_data()
    assert check_page.check_nonidentified_folds(_fold_html(), data) == []
    assert check_page.check_nonidentified_folds(_fold_html(label="Authorities under the AI Act"), data) == []  # the owner may reword a heading
    assert any("heading is empty" in e for e in check_page.check_nonidentified_folds(_fold_html(label=" "), data))
    assert any("original name in a focusable tooltip" in e for e in check_page.check_nonidentified_folds(_fold_html(original=False), data))
    for broken in (_fold_html().replace('tabindex="0"', ''), _fold_html().replace('aria-description="Комисия за финансов надзор"', 'aria-description="Wrong original"')):
        assert any("original name in a focusable tooltip" in e for e in check_page.check_nonidentified_folds(broken, data))
    assert any("does not name its roles" in e for e in check_page.check_nonidentified_folds(_fold_html(roles="market surveillance, proposed"), data))
    assert any("no inline citation" in e for e in check_page.check_nonidentified_folds(_fold_html().replace('<a class="cn">1</a>', "", 1), data))
    assert any("role text is nowhere" in e for e in check_page.check_nonidentified_folds(_fold_html(det=""), data))
    assert any("removed" in e for e in check_page.check_nonidentified_folds(_fold_html().replace("</summary>", '</summary><p class="related">Listed.</p>'), data))
    gone = {"jurisdictions": [{"id": "BG", "status": "none_identified", "europe_scope": True}], "dataset": {}}
    assert any("entry is missing" in e for e in check_page.check_nonidentified_folds("<ul id='rules'></ul>", gone))
    alone = _fold_html(original=False).replace("Financial Supervision Commission<", "Комисия за финансов надзор<")
    assert check_page.check_nonidentified_folds(alone, data)  # wrong rendered name still blocks
    data["jurisdictions"][0]["related_bodies"][0].pop("name_en")
    assert check_page.check_nonidentified_folds(alone, data) == []  # untranslated names stand as they are


@pytest.mark.parametrize("space", [" ", "\u00a0"])
def test_authority_line_read_across_its_glued_last_word(space):
    """The citation glue stays inside one roles span; the check reads the whole line with either kind of space."""
    data = _fold_data()
    body = data["jurisdictions"][0]["related_bodies"][0]
    body.update(designation_state="designated", designation_basis="law")
    glued = (f'<li><span class="body-name"><span class="original-text" aria-description="Комисия за финансов надзор" tabindex="0">Financial Supervision Commission</span><span class="body-roles">: market-surveillance authority, designated{space}by{space}'
             '<span class="cite-glue">law<span class="cites"><a class="cn">1</a></span></span></span></span>'
             '<span class="det">Proposed market authority.</span></li>')
    page = _fold_html().replace(_authority("Financial Supervision Commission", "market-surveillance authority, proposed",
                                            'Комисия за финансов надзор', '<span class="det">Proposed market authority.</span>'), glued)
    assert glued in page
    assert check_page.check_nonidentified_folds(page, data) == []
    assert any("designated by" in e for e in check_page.check_nonidentified_folds(page.replace(">law<", "><"), data))
    split = page.replace('<span class="cite-glue">law', '</span><span class="cite-glue"><span class="body-roles">law</span>').replace(
        '</a></span></span></span></span>', '</a></span></span></span>')
    assert check_page.check_nonidentified_folds(split, data) == [
        "BG body 'Financial Supervision Commission': expected exactly one .body-roles span, found 2"
    ]
    missing = BeautifulSoup(page, "html.parser")
    missing.select_one(".body-roles").decompose()
    assert any("expected exactly one .body-roles span, found 0" in e for e in check_page.check_nonidentified_folds(str(missing), data))


def test_ai_act_authorities_in_pattern_4_order_and_folded_after_three():
    data = _fold_data()
    bodies = [{"name": n, "kind": "ai_act_authority", "authority_roles": r, "designation_state": "designated", "designation_basis": "law",
               "source_ids": ["s1"], "role": ""} for n, r in [("Bravo", ["market_surveillance"]), ("Alpha", ["market_surveillance"]),
                                                             ("Delta", ["single_point_of_contact"]), ("Echo", ["market_surveillance", "notifying"]),
                                                             ("Charlie", ["notifying"])]]
    data["jurisdictions"][0]["related_bodies"] = bodies
    order = ["Delta", "Echo", "Alpha", "Bravo", "Charlie"]  # the single point of contact, then the most roles, ties by name
    line = {"Delta": "single point of contact", "Echo": "market-surveillance authority and notifying authority", "Alpha": "market-surveillance authority",
            "Bravo": "market-surveillance authority", "Charlie": "notifying authority"}
    lis = [_authority(n, line[n] + ", designated by law") for n in order]
    page = ('<details class="card mini" id="c-bg" data-j="BG"><summary>Bulgaria</summary><div class="body-group"><p class="gl">AI Act authorities</p>'
            f'<ul>{"".join(lis[:3])}</ul><details class="more"><summary>2 more AI Act bodies</summary><ul>{"".join(lis[3:])}</ul></details></div></details>')
    assert check_page.check_nonidentified_folds(page, data) == []
    unfolded = page.replace('</ul><details class="more"><summary>2 more AI Act bodies</summary><ul>', "")
    assert any("folded" in e for e in check_page.check_nonidentified_folds(unfolded, data))
    swapped = page.replace(lis[0], "@").replace(lis[1], lis[0]).replace("@", lis[1])
    assert any("fold-out groups" in e for e in check_page.check_nonidentified_folds(swapped, data))
    four = page.replace(lis[4], "")
    data["jurisdictions"][0]["related_bodies"] = [b for b in bodies if b["name"] != "Charlie"]
    assert any("folded" in e for e in check_page.check_nonidentified_folds(four, data))  # one hidden body is not worth a fold
    assert check_page.check_nonidentified_folds(four.replace('</ul><details class="more"><summary>2 more AI Act bodies</summary><ul>', "").replace("</ul></details></div>", "</ul></div>"), data) == []


@pytest.mark.parametrize("stage, note, dets, ok", [
    ("exists", "It says it tests models.", ["It says it tests models."], True),
    ("exists", "It says it tests models.", ["It says it tests models.", "Exists."], False),  # no stage line for an existing body
    ("planned", "It says it tests models.", ["It says it tests models.", "Planned."], True),
    ("planned", "It says it tests models.", ["It says it tests models."], False),
    ("exists", "INSAIT (Sofia University).", [], True),  # a note that is only the name is not said again
    ("exists", "INSAIT (Sofia University).", ["INSAIT (Sofia University)."], False),
])
def test_closest_body_note_and_stage_line(stage, note, dets, ok):
    data = _fold_data()
    data["jurisdictions"][0]["closest_body"] = {"name": "INSAIT (Sofia University)", "stage": stage, "note": note, "source_ids": ["s1"]}
    lines = "".join(f'<span class="det">{d}</span>' for d in dets)
    page = _fold_html().replace(
        '<div class="body-group"><p class="gl">AI Act authorities</p>',
        f'<div class="body-group closest"><p class="gl">Closest body</p><ul><li><span class="body-name">INSAIT (Sofia University)</span>{lines}<span class="cites"><a class="cn">1</a></span></li></ul></div>'
        '<div class="body-group"><p class="gl">AI Act authorities</p>').replace(
        '<div class="body-group"><p class="gl">Other bodies</p><ul><li><span class="body-name">INSAIT (Sofia University)<span class="cites"><a class="cn">1</a></span></span></li></ul></div>', "")
    assert (check_page.check_nonidentified_folds(page, data) == []) is ok


def _closest_fold_fixture(*, named=True, stage="planned", claims=(), finding_sources=("s1",), body_sources=("s1",), migrated=True):
    name = "Fixture lab"
    summary = f"Closest body: {name}, planned and not yet set up." if named else "An AI Act authority is listed."
    body = {"name": name, "body_id": "lab", "stage": stage, "source_ids": list(body_sources),
            "display": {"source_links": list(body_sources)}}
    record = {"id": "XX", "status": "none_identified", "europe_scope": True, "summary": summary, "closest_body": body,
              "related_bodies": [{"id": "lab", "name": name, "kind": "research"}]}
    if migrated:
        record["claims"] = list(claims)

    def cites(ids):
        return '<span class="cites">' + "".join(f'<a class="cn" data-source-id="{sid}">{n}</a>' for n, sid in enumerate(ids, 1)) + '</span>'

    html = f'<details class="card mini" id="c-xx"><summary>Fixture state</summary><p>{summary}{cites(finding_sources)}</p></details>'
    lines = "".join(f'<div class="claim det" data-claim-id="{c["id"]}">{c["text"]}{cites(body_sources)}</div>' for c in claims if not c.get("lead"))
    in_summary = named and migrated  # the summary gives name and stage, so the group leaves both out
    if stage != "exists" and not in_summary:
        lines += f'<span class="det">{stage.replace("_", " ").capitalize()}.</span>'
    name_line = "" if in_summary else f'<span class="body-name">{name}{cites(body_sources)}</span>'
    group = (f'<div class="body-group closest"><p class="gl">Closest body</p><ul><li data-body-id="lab">'
             f'{name_line}{lines}</li></ul></div>')
    return {"jurisdictions": [record]}, html, group


@pytest.mark.parametrize("claims", [
    [],
    [{"id": "lead", "kind": "function", "body_id": "lab", "lead": True, "text": "It tests models."}],
])
def test_closest_group_absent_when_summary_names_and_cites_it_without_role_claims(claims):
    data, html, group = _closest_fold_fixture(claims=claims)
    assert check_page.check_nonidentified_folds(html, data) == []
    assert any("matches no expected group" in e for e in check_page.check_nonidentified_folds(html.replace("</details>", group + "</details>"), data))


@pytest.mark.parametrize("stage", ["exists", "planned", "proposed", "under_study"])
def test_closest_group_name_only_when_summary_does_not_name_it(stage):
    data, html, group = _closest_fold_fixture(named=False, stage=stage)
    page = html.replace("</details>", group + "</details>")
    assert check_page.check_nonidentified_folds(page, data) == []
    assert check_page.check_nonidentified_folds(html, data)
    extra = page.replace("</li>", '<span class="det">An extra note.</span></li>')
    assert any("role sentences" in e for e in check_page.check_nonidentified_folds(extra, data))
    if stage != "exists":
        missing_stage = page.replace(f'<span class="det">{stage.replace("_", " ").capitalize()}.</span>', "")
        assert any("role sentences" in e for e in check_page.check_nonidentified_folds(missing_stage, data))


def test_closest_group_normal_with_remaining_live_claims():
    claims = [{"id": "role", "kind": "function", "body_id": "lab", "text": "It publishes research."}]
    data, html, group = _closest_fold_fixture(claims=claims)
    page = html.replace("</details>", group + "</details>")
    assert check_page.check_nonidentified_folds(page, data) == []
    assert check_page.check_nonidentified_folds(html, data)
    assert any("role sentences" in e for e in check_page.check_nonidentified_folds(page.replace("It publishes research.", ""), data))


def test_closest_group_kept_for_an_expired_role_claim():
    # An expired role stays shown and marked (design section 4), so its group is never omitted.
    claims = [{"id": "old", "kind": "function", "body_id": "lab", "expired": True, "text": "It tested models."}]
    data, html, group = _closest_fold_fixture(claims=claims)
    page = html.replace("</details>", group + "</details>")
    assert check_page.check_nonidentified_folds(page, data) == []
    assert check_page.check_nonidentified_folds(html, data)
    assert any("role sentences" in e for e in check_page.check_nonidentified_folds(page.replace("It tested models.", ""), data))


def test_closest_body_sources_move_to_the_finding_that_names_it():
    data, html, group = _closest_fold_fixture(body_sources=("s1", "s2"))
    assert check_page.check_nonidentified_folds(html, data)
    # A source link in the footer is not a citation mark on the card's finding.
    footer = '<div class="foot"><a data-source-id="s2">Source two</a></div>'
    assert check_page.check_nonidentified_folds(html.replace("</details>", footer + "</details>"), data)
    complete = html.replace('</p></details>', '<span class="cites"><a class="cn" data-source-id="s2">2</a></span></p></details>')
    assert check_page.check_nonidentified_folds(complete, data) == []


def test_closest_group_legacy_still_shows_name_and_stage():
    data, html, group = _closest_fold_fixture(migrated=False)
    assert check_page.check_nonidentified_folds(html.replace("</details>", group + "</details>"), data) == []
    assert check_page.check_nonidentified_folds(html, data)


def _translated_lead_fixture():
    claim = {"id": "lead", "kind": "function", "body_id": "lab", "lead": True,
             "text": 'It says it "tests models" with them.', "citations": {"text": ["s1"]}}
    data = {"jurisdictions": [{"id": "XX", "status": "none_identified", "europe_scope": True,
                              "closest_body": {"body_id": "lab"}, "claims": [claim]}],
            "sources": [{"id": "s1", "language": "de", "excerpts": [{"text": "prüft Modelle", "gloss": "tests models",
                         "supports": ["/jurisdictions/0/claims/0/text"]}]}]}
    quote = '<span class="quotation"><span class="quote-text" tabindex="0" aria-description="prüft Modelle">"tests models"</span></span>'
    marker = '<span class="small translation"> translated from German</span>'
    cites = '<span class="cites"><a class="cn" data-source-id="s1">1</a></span>'
    html = (f'<details id="c-xx"><div class="claim" data-claim-id="lead"><span class="claim-text">It says it {quote} with them.{cites}</span>'
            '<details class="quote" data-quote-claim="lead" data-translated="Translated from German"><summary aria-label="Quote, translated from German">Quote</summary></details></div></details>')
    return data, html, quote, marker, cites


def test_quote_translation_sentence_has_no_marker_and_keeps_original_with_citation_glue():
    data, html, _, _, cites = _translated_lead_fixture()
    assert check_page.check_claim_cards(html, data) == []
    glued = html.replace("them." + cites, '<span class="cite-glue">them.' + cites + '</span>')
    assert check_page.check_claim_cards(glued, data) == []


@pytest.mark.parametrize("placement", ["inside-quote", "sentence-end", "after-citations", "missing-original", "missing-focus"])
def test_quote_translation_rejects_sentence_markers_or_unreachable_original(placement):
    data, html, quote, marker, cites = _translated_lead_fixture()
    if placement == "inside-quote":
        html = html.replace(quote, quote[:-7] + marker + "</span>")
    elif placement == "sentence-end":
        html = html.replace(cites, marker + cites)
    elif placement == "after-citations":
        html = html.replace(cites, cites + marker)
    elif placement == "missing-original":
        html = html.replace(' aria-description="prüft Modelle"', "")
    else:
        html = html.replace(' tabindex="0"', "")
    assert any("translation" in e or "translated" in e for e in check_page.check_claim_cards(html, data))


def test_quote_label_names_each_plain_language_once():
    data = {"jurisdictions": [{"id": "XX", "claims": [{"id": "c", "kind": "fact", "text": "t"}]}],
            "sources": [{"id": "a", "language": "nn", "excerpts": [{"text": "x", "gloss": "y", "supports": ["/jurisdictions/0/claims/0/text"],
                                                                   "anchors": [{"text": "z", "gloss": "w"}, {"text": "u", "gloss": "v", "source_id": "b"}]}]},
                        {"id": "b", "language": "fr"}]}
    assert check_page.expected_quote_label(data, "c") == "Quote, translated from Norwegian and French"
    data["sources"][0]["excerpts"][0] = {"text": "x", "supports": ["/jurisdictions/0/claims/0/text"]}
    assert check_page.expected_quote_label(data, "c") == "Quote"


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
    failed = {"text": "q", "supports": [], "reread": {"state": "blocked_or_failed", "last_found": "2026-10-17", "since": "2026-11-10"}}
    return {
        "sources": [
            {"id": "kept", "url": "https://k.example/", "archive_url": "https://web.archive.org/web/1/https://k.example/"},
            {"id": "live", "url": "https://l.example/"},
            {"id": "gone", "url": "https://g.example/", "archive_url": "https://web.archive.org/web/1/https://g.example/", "excerpts": [failed]},
        ],
        "dataset": {"display": {"generic_sources": ["kept", "gone"]}, "other_overviews": [{"display": {"source_links": ["live"]}}]},
    }


ARCHIVE_TAIL = ('<span class="sep-gap"> </span><span class="reread-tail trailing-qualifier" data-tip="Source could not be re-read since 10 Nov 2026; last read 17 Oct 2026.">'
                '<span aria-hidden="true"><span class="qualifier-text">last read <span class="date-text">17 Oct 2026</span></span></span>'
                '<span class="vh"> Source could not be re-read since 10 Nov 2026; last read 17 Oct 2026.</span></span>')


def _archive_html(kept_archive=True, live_archive=False, claim_archive=False, gone_archive=True):
    copy = lambda url: f'<span class="sep-gap"> </span><span class="archive-copy trailing-qualifier"><span class="qualifier-text"><a class="archive" href="{url}">archived copy<span class="vh"> (opens in a new tab)</span></a></span></span>'  # noqa: E731
    kept = copy("https://web.archive.org/web/1/https://k.example/")
    gone = ARCHIVE_TAIL + (copy("https://web.archive.org/web/1/https://g.example/") if gone_archive else "")
    return (
        f'<article class="card" data-j="GB"><div class="srcs"><ol>'
        f'<li data-source-id="kept"><a class="standalone" href="https://k.example/">K</a>, Publisher{kept if kept_archive else ""}</li>'
        f'<li data-source-id="live"><a class="standalone" href="https://l.example/">L</a>, Publisher{copy("https://l.example/") if live_archive else ""}</li>'
        f'<li data-source-id="gone"><a class="standalone" href="https://g.example/">G</a>, Publisher{gone}</li></ol></div>'
        f'<ol class="fn-sources"><li data-source-id="kept"><a class="standalone" href="https://k.example/">K</a>, Publisher{kept if claim_archive else ""}</li></ol>'
        f'<p>Value<sup><a class="cn" data-source-id="kept" href="https://k.example/">1</a></sup></p></article>'
        f'<ul><li id="generic-sources"><span class="inline-links"><a href="https://k.example/">K</a>{kept if claim_archive else ""}, '
        f'<a href="https://g.example/">G</a>{gone}</span></li>'
        f'<li><span class="inline-links"><a href="https://l.example/">L</a>{copy("https://l.example/") if live_archive else ""}</span>: note</li></ul>'
    )


def _reread(state, last_found="2026-11-10", since=None):
    return {"text": "q", "supports": [], "reread": {"state": state, "last_found": last_found, "since": since}}


def test_reread_line_follows_the_worst_quotation():
    line = lambda *excerpts: check_page.expected_reread_line({"excerpts": list(excerpts)})  # noqa: E731
    assert line() is None
    assert line(_reread("confirmed", "2026-11-03"), _reread("not_attempted", "2026-11-10")) == "Source last re-read 10 Nov 2026."
    assert line(_reread("confirmed"), _reread("blocked_or_failed", "2026-10-17", "2026-11-10")) == \
        "Source could not be re-read since 10 Nov 2026; last read 17 Oct 2026."
    assert line(_reread("source_gone", "2026-10-17", "2026-11-10")) == "Source offline since 10 Nov 2026; last read 17 Oct 2026."
    assert line(_reread("quote_gone", "2026-10-17", "2026-11-10")) == \
        "Not found in the source on 10 Nov 2026; last found 17 Oct 2026; under review."
    assert line(_reread("confirmed"), _reread("quote_gone", "2026-10-17", "2026-11-10"), _reread("blocked_or_failed", "2026-10-01", "2026-10-03")) == \
        "Not found in the source on 10 Nov 2026: 1 of 3 quotes; last found 17 Oct 2026; under review."
    # The count only when some quotation is still found.
    assert line(_reread("quote_gone", "2026-10-17", "2026-11-10"), _reread("blocked_or_failed", "2026-10-01", "2026-10-03")) == \
        "Not found in the source on 10 Nov 2026; last found 17 Oct 2026; under review."
    # Exact ties: offline before not found, then the earliest last found, in either order.
    pair = [_reread("quote_gone", "2026-10-17", "2026-11-10"), _reread("source_gone", "2026-10-20", "2026-11-10")]
    for excerpts in (pair, pair[::-1]):
        assert line(*excerpts) == "Source offline since 10 Nov 2026; last read 20 Oct 2026."
    pair = [_reread("blocked_or_failed", "2026-10-17", "2026-11-10"), _reread("blocked_or_failed", "2026-10-03", "2026-11-10")]
    for excerpts in (pair, pair[::-1]):
        assert line(*excerpts) == "Source could not be re-read since 10 Nov 2026; last read 3 Oct 2026."


def test_reread_lines_match_the_data():
    data = {"sources": [{"id": "a", "excerpts": [_reread("confirmed")]}, {"id": "b", "excerpts": []}]}
    item = '<li data-source-id="a"><a href="https://a.example/">A</a>, P{}</li><li data-source-id="b"><a href="https://b.example/">B</a>, P</li>'
    good = '<span class="reread">Source last re-read <span class="date-text">10 Nov 2026</span>.</span>'
    assert check_page.check_reread_lines(item.format(good), data) == []
    assert "source a: re-read line missing, expected Source last re-read 10 Nov 2026." in check_page.check_reread_lines(item.format(""), data)
    assert any("not the item's last part" in e for e in check_page.check_reread_lines(item.format(good + "<span>x</span>"), data))
    assert any("not the item's last part" in e for e in check_page.check_reread_lines(item.format(good + " trailing text"), data))
    # A page whose sources have no states shows no line, and that is valid.
    assert check_page.check_reread_lines(item.format(""), {"sources": [{"id": "a", "excerpts": []}, {"id": "b"}]}) == []
    verified = {"sources": [{"id": "a", "excerpts": []}]}
    assert "a re-read line says verified" in check_page.check_reread_lines(item.format('<span class="reread">Verified.</span>'), verified)


def test_archive_links_only_where_needed():
    data = _archive_data()
    assert check_page.check_archive_links(_archive_html(), data) == []
    missing = check_page.check_archive_links(_archive_html(kept_archive=False), data)
    assert "GB source list: source kept lacks its archived copy link" in missing
    gone = check_page.check_archive_links(_archive_html(gone_archive=False), data)
    assert "GB source list: source gone lacks its archived copy link" in gone and "Method: source gone lacks its archived copy link" in gone
    extra = check_page.check_archive_links(_archive_html(live_archive=True), data)
    assert "GB source list: source live shows an archived copy link it should not" in extra
    assert "Method: source live shows an archived copy link" in extra
    # A claim's own sources and Method give an archived copy only beside a caveat.
    inline = check_page.check_archive_links(_archive_html(claim_archive=True), data)
    assert "GB source list: source kept shows an archived copy link it should not" in inline and "Method: source kept shows an archived copy link" in inline
    wrong = _archive_html().replace("web/1/", "web/2/", 1)
    assert "GB source list: source kept archived copy link differs from data" in check_page.check_archive_links(wrong, data)
    loose = _archive_html().replace('<a class="archive"', '(<a class="archive"', 1)
    assert any("not one" in e for e in check_page.check_archive_links(loose, data))
    early = BeautifulSoup(_archive_html(), "html.parser")
    line = early.select_one('li[data-source-id="gone"]')
    line.append(line.select_one('.reread-tail').extract())
    assert any("does not end its source line" in e for e in check_page.check_archive_links(str(early), data))
    extra_citation = _archive_html().replace(">1</a></sup>", '>1</a><span class="sep-gap"> </span><span class="archive-copy trailing-qualifier"><span class="qualifier-text"><a class="archive" href="x">archived copy</a></span></span></sup>')
    assert "4 archived copy links on the page, 3 expected from source lists and Method" in check_page.check_archive_links(extra_citation, data)
    as_citation = _archive_html().replace('class="archive" href="https://web.archive.org', 'class="archive cn" href="https://web.archive.org', 1)
    assert check_page.check_archive_links(as_citation, data) == ["a superscript citation carries an archived copy link"]


def test_reread_caveat_ends_the_source_line():
    data = _archive_data()
    assert check_page.check_reread_lines(_archive_html(), data) == []
    as_line = _archive_html().replace(ARCHIVE_TAIL, '<span class="reread">Source could not be re-read since 10 Nov 2026; last read 17 Oct 2026.</span>', 1)
    assert any("re-read caveat" in e for e in check_page.check_reread_lines(as_line, data))
    no_reader = _archive_html().replace('<span class="vh"> Source could not', '<span> Source could not', 1)
    assert any("re-read caveat" in e or "screen reader" in e for e in check_page.check_reread_lines(no_reader, data))
    no_tip = _archive_html().replace(' data-tip="Source could not be re-read since 10 Nov 2026; last read 17 Oct 2026."', "", 1)
    assert any("tip" in e for e in check_page.check_reread_lines(no_tip, data))
    found = _archive_html().replace('class="qualifier-text">last read', 'class="qualifier-text">last found', 1)
    assert any("re-read caveat" in e for e in check_page.check_reread_lines(found, data))


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
    # Funding identifies rows by institute, so two bodies in one state do not make its existing row ambiguous.
    assert not any("ES funding row: the institute name rule needs a decision" in e for e in errors)


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


@pytest.fixture
def alignment_case():
    data = {"sources": [{"id": "s", "url": SOURCE_URL}], "institutes": [], "jurisdictions": [{
        "id": "XX", "kind": "state", "status": "planned", "status_since": "2026-01-01",
        "source_ids": ["s"], "citations": {"since": ["s"]},
    }]}
    cite = f'<span class="cites"><a class="cn" data-source-id="s" href="{SOURCE_URL}">1</a></span>'
    html = (
        '<article class="card" data-j="XX"><div class="row" data-track="3"><div class="v">Planned ' + cite + '</div></div>'
        f'<details class="srcs"><ol><li data-source-id="s"><a href="{SOURCE_URL}">Source</a></li></ol></details></article>'
        '<table><tbody id="world-rows"><tr data-j="XX"><td><span class="smark"><i class="sw"></i>Planned</span></td>'
        '<td data-col="since">1 Jan 2026 ' + cite + '</td>'
        '<td data-col="budget"><span class="missing na"><span aria-hidden="true">–</span><span class="vh">Not applicable</span></span></td></tr></tbody></table>'
    )
    return BeautifulSoup(html, "html.parser"), data


@pytest.mark.parametrize("selector, message", [
    ('.card', '.card:not(.mini)[data-j="XX"]'),
    ('.row', 'XX .row'),
    ('.srcs li', 'XX .srcs li'),
    ('li[data-source-id]', 'li[data-source-id]'),
    ('.cn', '.cn'),
    ('.cites', '.cites'),
    ('#world-rows tr', '#world-rows tr[data-j="XX"]'),
    ('#world-rows [data-col="since"]', 'XX #world-rows [data-col="since"]'),
    ('#world-rows td', '#world-rows td'),
    ('.smark', '.smark:not([data-state])'),
    ('.missing.na', '.missing.na'),
])
def test_source_alignment_rejects_empty_selections(alignment_case, selector, message):
    soup, data = alignment_case
    assert check_page.check_source_alignment(str(soup), data) == []
    for node in soup.select(selector):
        node.unwrap() if selector == '.cites' else node.decompose()
    assert f"source_alignment: {message} matched 0" in check_page.check_source_alignment(str(soup), data)


def test_source_alignment_rejects_zero_comparable_rows(alignment_case):
    soup, data = alignment_case
    soup.select_one('.row')['data-track'] = '999'
    assert 'source_alignment: XX .row[data-track] for card/table citations matched 0' in check_page.check_source_alignment(str(soup), data)


def test_source_alignment_unknown_figures_need_missing_marks(alignment_case):
    soup, data = alignment_case
    data['institutes'] = [{'id': 'xx', 'jurisdiction_id': 'XX', 'status': 'planned', 'display': {'budget': None, 'staff': None}}]
    soup.append(BeautifulSoup('<div id="fund-rows"><div id="fund-records"><details class="fund-detail" data-j="XX" data-institute="xx"><summary>Funding and staff records</summary></details></div></div><span class="missing" data-tip="Unknown">Not published</span>', 'html.parser'))
    assert check_page.check_source_alignment(str(soup), data) == []
    soup.select_one('.missing:not(.na)').decompose()
    assert 'source_alignment: .missing:not(.na) matched 0' in check_page.check_source_alignment(str(soup), data)


def test_source_alignment_allows_source_free_records_and_eu_without_table(alignment_case):
    soup, data = alignment_case
    for node in soup.select('.srcs, .cites'):
        node.decompose()
    data['jurisdictions'][0].update(source_ids=[], citations={})
    assert check_page.check_source_alignment(str(soup), data) == []
    data['jurisdictions'][0]['kind'] = 'supranational'
    soup.select_one('#world-rows').decompose()
    assert check_page.check_source_alignment(str(soup), data) == []


@pytest.mark.parametrize("selector, message", [
    ('article.card', 'article.card:not(.mini)[data-j="GB"]'),
    ('.row[data-track="3"]', 'GB .row[data-track="3"]'),
    ('.row[data-track="11"]', 'GB .row[data-track="11"]'),
])
def test_interval_output_requires_card_and_rows_even_without_intervals(selector, message):
    data = interval_case(kind='none')
    data['jurisdictions'][0]['timeline'] = []
    soup = BeautifulSoup(interval_html(status=None, activity=None), 'html.parser')
    assert check_page.check_interval_output(str(soup), data) == []
    for node in soup.select(selector):
        node.decompose()
    assert f'interval_output: {message} matched 0' in check_page.check_interval_output(str(soup), data)


def test_interval_output_allows_absent_full_cards_for_none_identified():
    data = interval_case()
    data['jurisdictions'][0]['status'] = 'none_identified'
    assert check_page.check_interval_output('', data) == []


@pytest.fixture
def figures_case():
    # Reuse the existing small figure inputs and their matching budget cards.
    from test_figures import figures_row, institute

    rows = [institute('a', 'A', 1_000_000, 31), institute('b', 'B', 1_000_000, 31)]
    for row in rows:
        row['display']['funding_records']['x']['display'] = {'value': 'EUR 1.0m'}
    data = {'institutes': rows, 'jurisdictions': [{'id': key, 'name': key, 'kind': 'state'} for key in 'AB']}
    soup = figures_row('EUR 1.0m', 'EUR 1.0m', '31', '31', data)
    return soup, data


@pytest.mark.parametrize('selector, message', [
    ('#figures > .figure', '#figures > .figure'),
    ('#figures > .figure b', '#figures > .figure:nth-child(1) b'),
    ('#figures > .figure > span', '#figures > .figure:nth-child(1) span'),
    ('.card', '.card[data-j="A"] .row[data-track="9"] .v'),
    ('.row', '.card[data-j="A"] .row[data-track="9"] .v'),
    ('.v', '.card[data-j="A"] .row[data-track="9"] .v'),
])
def test_figures_row_rejects_empty_selections(figures_case, selector, message):
    soup, data = figures_case
    assert check_page.check_figures_row(soup, data) == []
    for node in soup.select(selector):
        node.decompose()
    assert f'figures_row: {message} matched 0' in check_page.check_figures_row(soup, data)


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
def test_active_quarantine_skips_jobs_through_local_shards_and_merge(quarantine_file, tmp_path, monkeypatch, expires):
    entry = quarantine_entry(job="hydration/fake_hydration/0", expires=expires)
    quarantine_file.write_text(json.dumps([entry]))
    behaviour = [("hydration", ("raise", AssertionError("quarantined job ran"))), ("interactions", ("errors", ["i1"]))]
    merged, local = run_round_trip(tmp_path, monkeypatch, behaviour)
    assert merged == local == (["seed", "i1"], None)
    lines = check_page.read_timings(tmp_path / "timings.jsonl")
    assert all(check_page.timing_ok({key: value for key, value in line.items() if key != "shard"}) for line in lines)
    assert {line["job"]: line["errors"] for line in lines} == {"interactions/fake_interaction/1": 1}
    outcomes = check_page.load_shards(tmp_path / "gate", tmp_path / "site", check_page.all_job_ids())
    assert outcomes[0] == {"not_run": entry}
    # A later merge has no evidence from the skipped job, even when its quarantine has expired.
    monkeypatch.setattr(check_page, "today", lambda: check_page.date.fromisoformat(expires) + check_page.date.resolution)
    assert check_page.check_merged(tmp_path / "site", {}, tmp_path / "gate") == ["seed", "i1"]


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


def test_quarantined_job_is_neither_run_nor_timed(quarantine_file, tmp_path, monkeypatch, capsys):
    quarantine_file.write_text(json.dumps([quarantine_entry(job="hydration/fake_hydration/0")]))
    merged, local = run_round_trip(tmp_path, monkeypatch, [("hydration", ("raise", RuntimeError("harness exploded")))])
    assert merged == local == ("NOT RUN", None)
    output = capsys.readouterr()
    assert "1 NOT RUN, 0 findings" in output.out
    assert output.err.count("NOT RUN: hydration/fake_hydration/0: quarantined until 2026-11-09 (owner Test owner, #123)") == 3
    assert check_page.read_timings(tmp_path / "timings.jsonl") == []


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
    assert check_page.main(dist) == check_page.NOT_RUN
    output = capsys.readouterr()
    assert output.err.splitlines() == ["NOT RUN: hydration/hydration_paint: quarantined until 2026-11-09 (owner Test owner, #123)"]
    assert "passed" not in output.out and "error:" not in output.err


def test_checker_digest_does_not_depend_on_the_quarantine_or_the_date(tmp_path, monkeypatch):
    # Skips are recorded in the shard outcome, so changing the register cannot turn them into passes.
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    monkeypatch.setattr(check_page, "__file__", str(scripts / "check_page.py"))
    register = scripts / "quarantine.json"
    register.write_text("[]")
    empty = check_page.checker_digest()
    register.write_text(json.dumps([quarantine_entry()]))
    assert check_page.checker_digest() == empty


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
    timings.write_text('{"job": "a", "started": 0, "seconds": 1, "errors": 0, "exception": null}\n{"job": "b", "sta\n{"job": 3}\nnot json\n')
    assert [line["job"] for line in check_page.read_timings(timings)] == ["a", 3]  # the cut line is dropped; merge validates the rest
    assert capsys.readouterr().err == f"warning: 2 unparsable browser job timing lines in {timings}\n"
    monkeypatch.setattr(Path, "read_text", lambda self, *a, **k: (_ for _ in ()).throw(PermissionError("denied")))
    assert check_page.read_timings(timings) == [] and "not read" in capsys.readouterr().err
    monkeypatch.undo()
    timings.write_bytes(b"\xff\xfe not utf-8\n")
    assert check_page.read_timings(timings) == [] and "not read" in capsys.readouterr().err


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
    chosen = ["interactions/check_theme_default", "hydration/hydration_paint"]
    check_page.check_browser("chrome", Path("site"), {}, only=chosen)
    assert seen["ids"] == ["hydration/hydration_paint", "interactions/check_theme_default"]  # report order, not the order asked


def test_a_subset_runs_through_the_real_stages(monkeypatch):
    ran = []
    monkeypatch.setattr(check_page, "run_browser_job", lambda chrome, dist, stage, check, args: ran.append(check_page.job_id(stage, check, args)) or [])
    monkeypatch.setattr(check_page, "browser_seeds", lambda dist: {"interactions": lambda: ran.append("seed") or ["seed error"]})
    # Only an interactions job: the empty earlier stages pass, the stage's seed runs, nothing else does.
    assert check_page.check_browser("chrome", Path("site"), {}, only=["interactions/check_clickable_widths"]) == ["seed error"]
    assert ran == ["seed", "interactions/check_clickable_widths"]
    ran.clear()
    # Mixed stages: a hydration finding stops the run before the interactions stage, as in the full gate.
    monkeypatch.setattr(check_page, "run_browser_job", lambda chrome, dist, stage, check, args: ran.append(check_page.job_id(stage, check, args)) or (["h"] if stage == "hydration" else []))
    assert check_page.check_browser("chrome", Path("site"), {}, only=["interactions/check_clickable_widths", "hydration/hydration_edges"]) == ["h"]
    assert ran == ["hydration/hydration_edges"]


def test_job_ids_must_exist():
    assert check_page.job_ids("hydration/hydration_paint, interactions/interaction_pass/390/light") == [
        "hydration/hydration_paint", "interactions/interaction_pass/390/light"]
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
    work = [("early", check_page.check_access_fixes, ("die", None)), ("early", check_page.check_clickable_widths, ("ok", ["e1"]))]  # the awaited one dies
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
    ({1: {0: OK}, 2: {1: OK}}, {2: {"format": 1}}, "not a format 3"),
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
    except check_page.JobsNotRun:
        return "NOT RUN", None
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


STATIC_CHECKS = ("check_method_pages", "check_dom", "check_agent_files", "check_static_page", "check_hydrated", "check_archive_links",
                 "check_reread_lines", "check_launch_assets", "check_page_data", "check_budget_ratio_downloads", "check_state_downloads", "check_state_download_buttons")


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


def test_annual_budget_summary_keeps_the_source_record_separate_and_cites_both_sources():
    record = {"id": "multi-year", "currency": "USD", "amount": 120_000_000, "years": 4,
              "annual_amount": 30_000_000, "annual_basis_code": "period_mean", "citations": {"amount": ["amount-source"]}}
    data = {"dataset": {"exchange_rates": [{"currency": "USD", "eur_per_unit": 0.9, "source_ids": ["rate-source"]}]},
            "institutes": [{"id": "institute", "display": {"comparison_funding": "multi-year"}, "funding": [record]}]}
    good = '''<details class="fund-detail" data-institute="institute"><div class="fund-budget-summary" data-source-funding-id="multi-year">
      <div class="fund-value"><strong>~EUR 27m (USD 30m)</strong><span class="qualifiers"><span class="meta-item">a year</span><span class="sep-gap"> </span><span class="meta-item">average over period</span>
      <a class="cn" data-source-id="amount-source">1</a><a class="cn" data-source-id="rate-source">2</a></span></div></div>
      <div class="funding-record" data-record-id="multi-year"><p class="funding-native"><span class="budget-amount">~EUR 110m (USD 120m)</span>
      <a class="cn" data-source-id="amount-source">1</a><a class="cn" data-source-id="rate-source">2</a></p></div></details>'''
    assert check_page.check_euro_amounts(good, data) == []
    for changed in (
        good.replace('data-source-funding-id="multi-year"', ''),
        good.replace('data-source-funding-id="multi-year"', 'data-source-funding-id="wrong"'),
        good.replace('data-source-funding-id=', 'data-record-id='),
        good.replace('data-source-funding-id=', 'data-funding-id='),
        good.replace('~EUR 27m (USD 30m)', '~EUR 110m (USD 120m)'),
        good.replace('<strong>~EUR 27m (USD 30m)</strong>', ''),
        good.replace('<a class="cn" data-source-id="amount-source">1</a>', '', 1),
        good.replace('<a class="cn" data-source-id="rate-source">2</a>', '', 1),
    ):
        assert any("annual budget summary" in e for e in check_page.check_euro_amounts(changed, data))


def test_gate_jobs_default_follows_the_host_setting(monkeypatch):
    """AISI_GATE_JOBS where set; else the CPUs, at most 8 and at most what the memory holds at 4 GiB a job."""
    monkeypatch.setattr(check_page.os, "cpu_count", lambda: 16)
    assert check_page.default_jobs({}, memory=64) == 8
    assert check_page.default_jobs({"AISI_GATE_JOBS": " "}, memory=64) == 8
    assert check_page.default_jobs({}, memory=15.6) == 3  # a CI runner
    assert check_page.default_jobs({}, memory=7.7) == 1  # a small shared host
    monkeypatch.setattr(check_page, "memory_gib", lambda: None)
    assert check_page.default_jobs({}) == 8  # memory unknown: the CPU rule alone
    assert check_page.default_jobs({"AISI_GATE_JOBS": "1"}) == 1
    assert check_page.default_jobs({"AISI_GATE_JOBS": "12"}) == 12  # explicit: no cap
    for bad in ("0", "two"):
        with pytest.raises(SystemExit, match="AISI_GATE_JOBS"):
            check_page.default_jobs({"AISI_GATE_JOBS": bad})


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


def test_a_job_without_laps_has_no_steps_key_and_bad_steps_are_refused(tmp_path):
    timings = tmp_path / "t.jsonl"
    check_page.timed_job(timings, 0.0, lambda stage, check, args: [], "origins", check_page.check_access_fixes, ())
    assert "steps" not in check_page.read_timings(timings)[0]
    base = {"job": "a", "started": 0, "seconds": 1, "errors": 0, "exception": None}
    assert check_page.timing_ok({**base, "steps": {"load": 1.0}})
    for bad in ({"load": -1}, {"load": "1"}, ["load"], {3: 1.0}):
        assert not check_page.timing_ok({**base, "steps": bad})


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


def test_skip_static_runs_only_the_job(rendered, monkeypatch, capsys):
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


@pytest.mark.parametrize("args, ok", [
    (["--job", "hydration/hydration_paint", "--no-quarantine", "--skip-static"], True),
    (["--job", "hydration/hydration_paint", "--skip-static"], False),
    (["--skip-static", "--no-quarantine"], False),
])
def test_skip_static_is_only_for_detector_runs(args, ok):
    result = subprocess.run([sys.executable, str(Path(check_page.__file__)), *args, "--list-jobs"], capture_output=True, text=True, timeout=120)
    assert result.returncode == (0 if ok else 2) and ("--skip-static is for the flake detector" in result.stderr) != ok


MOVED_TO_CONTENT = {  # content family module -> the check_page helpers it now runs
    "claims": ("check_claim_cards",),
    "functions": ("functions_structure", "functions_forms_structure"),
    "funding": ("check_budget_output", "check_funding_outside", "check_interval_output", "check_per_person", "check_no_rate_amounts", "check_euro_amounts"),
    "sources": ("check_source_alignment", "check_generic_sources"),
    "record_rows": ("check_leadership_rows", "check_access_groups", "check_setup_groups"),
    "summary": ("check_summary_data", "check_figures_row", "check_two_significant_figures"),
    "activity": ("check_operating_output",),
    "names": ("check_section_bar", "check_institute_names", "check_former_names", "check_nonidentified_folds", "check_terms_once"),
    "staff": ("check_staff_figures",),
}


MOVED_HELPERS = tuple(h for helpers in MOVED_TO_CONTENT.values() for h in helpers)


def test_each_moved_content_family_has_one_home(monkeypatch):
    # D2: each helper of a moved family runs exactly once in the content step (tests/content) and not at all in the
    # browser gate's static pass. Spies record what each path actually calls.
    import importlib

    calls = []
    for name in MOVED_HELPERS:
        monkeypatch.setattr(check_page, name, lambda *args, name=name, **kwargs: calls.append((name, kwargs)) or [])
    for module, helpers in MOVED_TO_CONTENT.items():
        calls.clear()
        for helper in importlib.import_module(f"content.test_{module}").FAMILY.values():
            helper("<html></html>", {})
        assert sorted(name for name, kwargs in calls) == sorted(helpers), module  # each once, none missing, none twice
        if module == "funding":
            assert dict(calls)["check_funding_outside"] == {"saved": True}
    # The browser gate's static pass (main) no longer calls any of them; the calls that remain elsewhere run them on
    # planted fixtures or filtered views inside browser jobs.
    import inspect

    static = inspect.getsource(check_page.main)
    assert not [name for name in MOVED_HELPERS if name + "(" in static]


def test_staff_totals_come_from_the_reviewed_fixture_not_the_producer():
    row = '<div id="fund-rows"><div class="fund-list" data-kind="staff"><li class="fund-item" data-j="AU" data-institute="au-aisi"><div class="fund-staff"><span class="fund-value"><strong>25</strong></span></div><details class="fund-detail" data-institute="au-aisi"><summary>Funding and staff records</summary><div class="fund-staff-summary"><span class="fund-value"><strong>25</strong></span></div></details></li></div>'
    soup = BeautifulSoup(row, "html.parser")
    entry = {"institute_id": "au-aisi", "as_of": "2026-09-18", "total": 25, "total_qualifier": "exact"}
    data = {"jurisdictions": [{"id": "AU", "status": "established"}], "institutes": [{"id": "au-aisi", "jurisdiction_id": "AU", "staff": [{"total": 25, "total_qualifier": "exact", "as_of": "2026-09-18"}],
                            "display": {"total_staff": "producer-says-something-else"}}]}
    assert check_page.check_staff_figures(soup, data, {"au-aisi": entry}) == []
    assert check_page.check_staff_figures(soup, data, {}) == ["au-aisi: staff total needs a reviewed entry in tests/fixtures/staff_figures.json"]
    newer = check_page.copy.deepcopy(data)
    newer["institutes"][0]["staff"].append({"total": 40, "total_qualifier": "exact", "as_of": "2026-11-01"})
    assert "differs from the reviewed entry" in check_page.check_staff_figures(soup, newer, {"au-aisi": entry})[0]
    assert check_page.check_staff_figures(soup, data, {"au-aisi": entry, "gone-aisi": entry})[0] == "gone-aisi: staff fixture entry for an institute the data does not have"
    for rival in ("2026-09-18", "2026-09", "2026"):  # the same date, or one that overlaps it: which is latest is not settled
        tied = check_page.copy.deepcopy(data)
        tied["institutes"][0]["staff"].append({"total": 30, "total_qualifier": "exact", "as_of": rival})
        assert "needs review" in check_page.check_staff_figures(soup, tied, {"au-aisi": entry})[0], rival
    for selector in (".fund-staff strong", ".fund-staff-summary strong"):
        shown = BeautifulSoup(row, "html.parser")
        shown.select_one(selector).string = "26"
        errors = check_page.check_staff_figures(shown, data, {"au-aisi": entry})
        assert len(errors) == 1 and "staff total shown as '26', expected '25'" in errors[0]


def test_the_staff_fixture_names_its_source_and_quote_for_every_entry():
    import json

    entries = json.loads(check_page.STAFF_FIGURES.read_text(encoding="utf-8"))["staff"]
    assert entries and all(e.get("source_id") and e.get("quote", "").strip() for e in entries)  # the reviewer checks each quote against its source
    assert len({e["institute_id"] for e in entries}) == len(entries)
    # Each quote is a recorded excerpt of its source in the authored data (the evidence, not the export).
    data = json.loads((ROOT / "data/aisi.json").read_text(encoding="utf-8"))
    excerpts = {s["id"]: [x["text"] for x in s.get("excerpts", [])] for s in data["sources"]}
    assert [e["institute_id"] for e in entries if e["quote"] not in excerpts.get(e["source_id"], [])] == []


@pytest.mark.parametrize("body, shown", [
    ({"name": "Agenzia per la cybersicurezza nazionale (ACN)", "name_en": "National Cybersecurity Agency (ACN)"}, "Agenzia per la cybersicurezza nazionale"),
    ({"name": "Datatilsynet (DT)", "name_en": "Danish Data Protection Agency"}, "Datatilsynet (DT)"),
    ({"name": "Komisja (KRiBSI)"}, "Komisja (KRiBSI)"),
])
def test_original_name_says_a_shared_abbreviation_once(body, shown):
    assert check_page.shown_original(body) == shown


@pytest.mark.parametrize("space", [" ", "\u00a0"])
def test_authority_line_states_the_recorded_designation(space):
    data = _fold_data()
    assert any("does not say it is proposed" in e for e in check_page.check_nonidentified_folds(_fold_html(roles="market-surveillance authority, designated by law"), data))
    body = data["jurisdictions"][0]["related_bodies"][0]
    body["designation_step"] = {"text": "bill tabled", "date": "2026-09-24"}
    assert any("latest step" in e for e in check_page.check_nonidentified_folds(_fold_html(), data))
    assert check_page.check_nonidentified_folds(_fold_html(roles=f"market-surveillance authority, proposed (bill tabled on 24{space}Sep{space}2026)"), data) == []
    body.pop("designation_step")
    body.update(designation_state="designated", designation_basis="law")
    assert check_page.check_nonidentified_folds(_fold_html(roles=f"market-surveillance authority, designated{space}by{space}law"), data) == []
    assert any("designated by law" in e for e in check_page.check_nonidentified_folds(_fold_html(roles="market-surveillance authority, designated by decree"), data))
    assert any("designated by law" in e for e in check_page.check_nonidentified_folds(_fold_html(roles="market-surveillance authority, not designated by law"), data))
    body.update(designation_state=None, designation_basis=None)
    assert check_page.check_nonidentified_folds(_fold_html(roles="market-surveillance authority, designation not yet identified"), data) == []
    assert any("does not record" in e for e in check_page.check_nonidentified_folds(_fold_html(roles="market-surveillance authority, designated"), data))


def test_closest_body_stage_line_says_its_stage():
    data = _fold_data()
    data["jurisdictions"][0]["closest_body"] = {"name": "INSAIT (Sofia University)", "stage": "planned", "note": "It tests.", "source_ids": ["s1"]}
    def page(lines):
        dets = "".join(f'<span class="det">{d}</span>' for d in lines)
        return _fold_html().replace(
            '<div class="body-group"><p class="gl">Other bodies</p><ul><li><span class="body-name">INSAIT (Sofia University)<span class="cites"><a class="cn">1</a></span></span></li></ul></div>',
            f'<div class="body-group closest"><p class="gl">Closest body</p><ul><li><span class="body-name">INSAIT (Sofia University)</span>{dets}<span class="cites"><a class="cn">1</a></span></li></ul></div>')
    def closest_first(html):  # pattern order: the closest body's group comes first
        a, rest = html.split('<div class="body-group closest">', 1)
        closest, after = rest.split("</div>", 1)
        return a.replace('<div class="body-group"><p class="gl">AI Act authorities</p>', '<div class="body-group closest">' + closest + '</div><div class="body-group"><p class="gl">AI Act authorities</p>', 1) + after
    assert check_page.check_nonidentified_folds(closest_first(page(["It tests.", "Planned."])), data) == []
    assert any("closest body shows" in e for e in check_page.check_nonidentified_folds(closest_first(page(["It tests.", "Proposed."])), data))
    assert any("closest body shows" in e for e in check_page.check_nonidentified_folds(closest_first(page(["It tests.", "Planned; its government role does not qualify."])), data))


def test_an_extra_or_repeated_group_fails():
    data = _fold_data()
    extra = _fold_html().replace("</details>", '<div class="body-group"><p class="gl">Again</p><ul><li><span class="body-name">INSAIT (Sofia University)</span></li></ul></div></details>')
    assert any("matches no expected group or repeats one" in e for e in check_page.check_nonidentified_folds(extra, data))


def test_fold_count_and_members_are_exact():
    data = _fold_data()
    bodies = [{"name": n, "kind": "ai_act_authority", "authority_roles": ["market_surveillance"], "designation_state": "designated",
               "designation_basis": "law", "source_ids": ["s1"], "role": ""} for n in ("A1", "A2", "A3", "A4", "A5")]
    data["jurisdictions"][0]["related_bodies"] = bodies
    lis = [_authority(n, "market-surveillance authority, designated by law") for n in ("A1", "A2", "A3", "A4", "A5")]
    def page(shown, hidden, summary):
        return ('<details class="card mini" id="c-bg" data-j="BG"><summary>Bulgaria</summary><div class="body-group"><p class="gl">AI Act authorities</p>'
                f'{shown}<details class="more"><summary>{summary}</summary><ul>{hidden}</ul></details></div></details>')
    assert check_page.check_nonidentified_folds(page("<ul>" + "".join(lis[:3]) + "</ul>", "".join(lis[3:]), "2 more AI Act bodies"), data) == []
    assert any("folded" in e for e in check_page.check_nonidentified_folds(page("<ul>" + "".join(lis[:3]) + "</ul>", "".join(lis[3:]), "12 more AI Act bodies"), data))
    wrong = ('<details class="card mini" id="c-bg" data-j="BG"><summary>Bulgaria</summary><div class="body-group"><p class="gl">AI Act authorities</p>'
             f'<details class="more"><summary>2 more AI Act bodies</summary><ul>{"".join(lis[:2])}</ul></details><ul>{"".join(lis[2:])}</ul></div></details>')
    assert any("folded" in e for e in check_page.check_nonidentified_folds(wrong, data))
