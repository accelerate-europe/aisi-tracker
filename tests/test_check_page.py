import subprocess
import json
from pathlib import Path
from urllib.request import urlopen

from bs4 import BeautifulSoup
import pytest

from conftest import SOURCE_URL
from scripts import check_page


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


def test_country_search_wording_fails(rendered):
    rendered[0].select_one("#world-filter")["placeholder"] = "Search country or institute"
    assert any("#world-filter: placeholder" in e for e in errors_for(rendered))


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


def test_chrome_discovery_linux_and_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(check_page, "MAC_CHROME", tmp_path / "absent")
    monkeypatch.setattr(check_page.shutil, "which", lambda name: "/usr/bin/chromium" if name == "chromium" else None)
    assert check_page.find_chrome() == "/usr/bin/chromium"
    monkeypatch.setattr(check_page.shutil, "which", lambda name: None)
    assert check_page.find_chrome() is None


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


def test_render_uses_loopback_free_port_isolated_profile_and_required_flags(tmp_path, monkeypatch):
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("Fixture page")
    profiles = []

    def fake_chrome(command, **kwargs):
        assert command[0] == "chrome"
        assert {"--headless=new", "--dump-dom", "--virtual-time-budget=5000"} <= set(command)
        profile = Path(next(x.split("=", 1)[1] for x in command if x.startswith("--user-data-dir=")))
        assert profile.is_dir()
        profiles.append(profile)
        assert command[-1].startswith("http://127.0.0.1:")
        with urlopen(command[-1], timeout=5) as response:
            assert response.read().decode() == "Fixture page"
        assert kwargs["timeout"] == 45
        return subprocess.CompletedProcess(command, 0, stdout="<html>Rendered</html>")

    monkeypatch.setattr(subprocess, "run", fake_chrome)
    html, _ = check_page.render("chrome", dist, tmp_path)
    assert html == "<html>Rendered</html>"
    assert all(not path.exists() for path in profiles)


def test_budget_oracle_rejects_wrong_id_and_missing_value_structure():
    data = {"jurisdictions": [{"id": "JP", "status": "established"}], "institutes": [{"id": "jp-aisi", "jurisdiction_id": "JP", "funding": []}]}
    identity = "funding-7d0b36a48cc65315865a6d6ec83ddeb6"
    value = f'<span class="budget-value" data-funding-id="{identity}"><span class="budget-amount">JPY 8.8bn</span><span class="budget-qualifiers">one-off · FY2025 supplementary · adopted</span></span>'
    html = f'<article class="card" data-j="JP"><div class="row"><div class="k">Budget</div><div class="v">{value}</div></div></article><table><tbody id="world-rows"><tr data-j="JP"><td data-col="budget">{value}</td></tr></tbody></table>'
    assert check_page.check_budget_output(html, data) == []
    assert any("wrong funding ID" in error for error in check_page.check_budget_output(html.replace(identity, "older-record"), data))
    assert any("separate amount and qualifiers" in error for error in check_page.check_budget_output(html.replace("budget-qualifiers", "removed"), data))
    assert any("latest own JPY 8.8bn" in error for error in check_page.check_budget_output(html.replace("JPY 8.8bn", "JPY 380m"), data))


def test_uncited_values_need_a_citation_link_except_absence_and_known_gaps(monkeypatch):
    from bs4 import BeautifulSoup

    monkeypatch.setattr(check_page, "KNOWN_UNCITED", {("net", "US"): "no quotation yet"})
    html = """<table><tbody id="world-rows">
      <tr data-j="GB"><td data-col="net">Member<span class="cites"><a class="cn" href="#">1</a></span></td><td data-col="budget"><span class="missing">Not published</span></td></tr>
      <tr data-j="US"><td data-col="net">Member</td><td data-col="op">None found yet</td></tr>
      <tr data-j="FR"><td data-col="op"><span class="activity-kind">Joint testing<span class="cites"><a class="cn" href="#">1</a></span></span><span class="activity-kind">Published work</span></td></tr>
    </tbody></table>
    <article class="card" data-j="KR"><div class="row"><div class="k">Related funding</div><div class="v"><span class="related-funding-item" data-funding-id="f1">KRW 20.1bn<span class="cites"></span></span></div></div></article>"""
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


def test_each_constructions_in_page_text():
    html = "<main><p>Source excerpts</p><p>quotations from sources and the values each supports</p></main>"
    assert check_page.each_constructions(BeautifulSoup(html, "html.parser"))
    assert check_page.each_constructions(BeautifulSoup("<p>Each card links to its sources.</p>", "html.parser")) == []


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
    return (
        card("GB", gb) + card("ES", "AI Security Institute (Spain)") + card("FR", "National Institute for AI Evaluation and Security")
        + "<table class='world'><tbody>"
        + row("GB", gb_short, "AI Security Institute (United Kingdom)") + row("ES", "Spain AISI", "AI Security Institute (Spain)")
        + row("FR", fr_short, "National Institute for AI Evaluation and Security")
        + "</tbody></table>"
        + '<div id="fund-rows">' + funding("ES", "AI Security Institute (Spain)") + "</div>"
    )


def test_institute_names_pass_and_fail_on_each_rule():
    data = _names_data()
    assert check_page.check_institute_names(_names_html(), data) == []
    # Same-named bodies without their country.
    assert any("card names its institute" in e for e in check_page.check_institute_names(_names_html(gb="AI Security Institute"), data))
    # A bare generic acronym in a tight place.
    assert any("bare generic acronym" in e for e in check_page.check_institute_names(_names_html(gb_short="AISI"), data))
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
    tab = _names_html().replace("AI Security Institute (Spain)</span></div></article>", "AI Security Institute (Spain) (opens in a new tab)</span></div></article>")
    assert check_page.check_institute_names(tab, data) == []
    # No funding rows at all is a failure, not a pass.
    assert any("no funding rows" in e for e in check_page.check_institute_names(_names_html().replace('id="fund-rows"', 'id="x"'), data))


def _fold_data():
    return {
        "jurisdictions": [{
            "id": "BG", "notes": "Own note.",
            "related_bodies": [
                {"name": "Комисия за финансов надзор", "name_en": "Financial Supervision Commission", "kind": "ai_act_authority",
                 "authority_roles": ["market_surveillance"], "designation_state": "proposed", "source_ids": ["s1"], "role": "Proposed market authority."},
                {"name": "INSAIT (Sofia University)", "kind": "other", "source_ids": ["s1"]},
            ],
        }],
        "dataset": {},
    }


def _fold_html(status='<p class="basis">Proposed, not yet designated.</p>', det='<span class="det">Proposed market authority.</span>', original=True, label="AI Act market surveillance", boilerplate="", how=True, method_count=1):
    orig = '<span class="orig">Комисия за финансов надзор</span>' if original else ""
    how_block = f'<div class="how"><p class="gl">How we checked</p><p>Own note. {boilerplate}</p></div>' if how else ""
    return (
        '<details class="card mini" id="c-bg" data-j="BG"><summary><span class="name">Bulgaria</span></summary>'
        f'<div class="body-group"><p class="gl">{label}</p>{status}<ul><li><span class="body-name">Financial Supervision Commission<span class="cites"><a class="cn">1</a></span></span>{orig}{det}</li></ul></div>'
        '<div class="body-group"><p class="gl">Other bodies</p><ul><li><span class="body-name">INSAIT (Sofia University)<span class="cites"><a class="cn">1</a></span></span></li></ul></div>'
        f"{how_block}</details>"
        f'<ul id="rules"><li>{" ".join(["for a government-designated institute that evaluates advanced models."] * method_count)}</li></ul>'
    )


def test_nonidentified_folds_pass_and_fail_on_each_rule():
    data = _fold_data()
    assert check_page.check_nonidentified_folds(_fold_html(), data) == []
    # The original name must sit beneath an English name.
    assert any("original name must sit beneath" in e for e in check_page.check_nonidentified_folds(_fold_html(original=False), data))
    # A body under the wrong function heading.
    assert any("fold-out groups" in e for e in check_page.check_nonidentified_folds(_fold_html(label="AI Act notifying"), data))
    # Shared boilerplate repeated in an entry, or missing from Method.
    html = _fold_html(boilerplate="Searched in English for a government-designated institute that evaluates advanced models.")
    assert any("Method states once" in e for e in check_page.check_nonidentified_folds(html, data))
    assert any("Method states the search wording 0 times" in e for e in check_page.check_nonidentified_folds(_fold_html(method_count=0), data))
    # Notes outside a How we checked block; a body without its citation.
    assert any("not in a How we checked block" in e for e in check_page.check_nonidentified_folds(_fold_html(how=False), data))
    assert any("no inline citation" in e for e in check_page.check_nonidentified_folds(_fold_html().replace('<a class="cn">1</a>', "", 1), data))
    # A role text that is nowhere in the entry.
    assert any("role text is nowhere" in e for e in check_page.check_nonidentified_folds(_fold_html(det=""), data))
    # A designation status that is not shown; an entry that did not render; a non-Latin name standing alone.
    assert any("designation status" in e for e in check_page.check_nonidentified_folds(_fold_html(status=""), data))
    gone = {"jurisdictions": [{"id": "BG", "status": "none_identified", "europe_scope": True}], "dataset": {}}
    assert any("entry is missing" in e for e in check_page.check_nonidentified_folds("<ul id='rules'></ul>", gone))
    alone = _fold_html(original=False).replace("Financial Supervision Commission<", "Комисия за финансов надзор<")
    assert any("non-Latin script" in e for e in check_page.check_nonidentified_folds(alone, data))


@pytest.mark.parametrize("label", ["Closest candidate", "Closest candidates", "Not counted"])
def test_nonidentified_folds_reject_short_run_on_candidate_text(label):
    data = _fold_data()
    page = _fold_html()
    assert check_page.check_nonidentified_folds(page, data) == []
    text = f"{label}: INSAIT."
    broken = page.replace("</details>", f'<p class="related">{text}</p></details>')
    assert check_page.check_nonidentified_folds(broken, data) == [
        f"BG fold-out still shows run-on text {text!r}"
    ]


def test_nonidentified_fold_accepts_structured_closest_candidate():
    data = _fold_data()
    (jurisdiction,) = data["jurisdictions"]
    jurisdiction["closest_body"] = {
        "name": "INSAIT (Sofia University)",
        "source_ids": ["s1"],
    }
    page = _fold_html().replace(">Other bodies<", ">Closest candidate<")
    assert check_page.check_nonidentified_folds(page, data) == []


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


def interval_html(status="7 days after announcement", activity="about 3 months after establishment", cites=("designation", "speech"),
                  caveat="It does not measure speed or quality.", table="",
                  activity_caveat="It is not a measure of speed or quality."):
    links = "".join(f'<a class="cn" data-source-id="{sid}">{n}</a>' for n, sid in enumerate(cites, 1))
    amount, _, words = (status or "").partition(" after ")
    gap = f'<abbr class="g interval" data-exp="{caveat}">{amount}</abbr> after <span class="cite-glue">{words}' if status else ""
    line = f'<span class="small qualifiers activity-interval">{activity}</span>' if activity else ""
    return (
        '<article class="card" data-j="GB">'
        f'<div class="row"><div class="k">Status since</div><div class="v"><span class="qualified"><span class="value-line">2 Nov 2023</span>'
        f'<span class="small qualifiers">{gap}<span class="cites">{links}</span>{"</span>" if status else ""}</span></span></div></div>'
        f'<div class="row"><div class="k">Known activity</div><div class="v"><span class="smark value activity" title="{activity_caveat}"><span class="activity-kinds">'
        '<span class="qualified activity-kind"><span class="value-line activity-clause" data-kind="published_work">Published work</span>'
        f'<span class="small qualifiers activity-since">since 9 Feb 2024</span>{line}</span></span></span></div></div></article>'
        f'<table><tbody id="world-rows"><tr data-j="GB"><td>{table}</td></tr></tbody></table>'
    )


def test_interval_oracle_accepts_exact_intervals_with_caveat_and_citations():
    hand = {"GB": ("7 days after announcement", "about 3 months after establishment")}
    assert check_page.check_interval_output(interval_html(), interval_case(), hand) == []


@pytest.mark.parametrize(
    ("html", "data", "message"),
    [
        (interval_html(status="7 days after proposal"), interval_case(), "Status since interval"),
        (interval_html(status="8 days after announcement"), interval_case(), "Status since interval"),
        (interval_html(activity="about 4 months after establishment"), interval_case(), "Known activity interval"),
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
    html = interval_html(activity="about 6 months after establishment").replace("since 9 Feb 2024", "since 1 May 2024")
    assert check_page.check_interval_output(html, data) == []


def test_interval_oracle_keeps_negative_intervals_and_pins_hand_worked_wording():
    since, line, counted = check_page.expected_intervals(*interval_case(credit="2023-09-07")["jurisdictions"], interval_case(credit="2023-09-07")["institutes"][0], "2026-10-20")
    assert (since, line, counted) == ("7 days after announcement", {"kind": "published_work", "text": "56 days before establishment"}, [0, 1])
    errors = check_page.check_interval_output(interval_html(), interval_case(), {"GB": ("7 days after announcement", None)})
    assert any("differs from the hand-worked" in error for error in errors)


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
