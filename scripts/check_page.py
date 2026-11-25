# /// script
# requires-python = ">=3.11"
# dependencies = ["beautifulsoup4>=4.12", "playwright==1.63.0"]
# ///
"""Check an already built site with Chrome; this script never builds or writes the checkout.

Run from any directory: uv run --locked scripts/build.py --out DIR && uv run --locked scripts/check_page.py --dist DIR
(--dist defaults to dist/). Exit 0 when every check passed, 1 on any failure, and NOT_RUN (3)
when Playwright's Chromium is missing, so callers can tell "not run" from "passed".
"""

import argparse
import calendar
import copy
import csv
import faulthandler
import hashlib
import io
import json
import math
import os
import re
import subprocess
import sys
import tempfile
from html import unescape
from html.parser import HTMLParser
import threading
import time
from collections import Counter, defaultdict
from contextlib import contextmanager
from datetime import date
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP, Decimal, localcontext
from pathlib import Path
from xml.etree import ElementTree
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup, Comment, NavigableString

sys.path.insert(0, str(Path(__file__).resolve().parent))
from agent_files import AI_CRAWLERS  # noqa: E402
from browser_lock import browser_slot, lock_path  # noqa: E402
from host_checks import INSTALL_CHROME, chrome_problem as missing_chrome, default_jobs as host_default_jobs, jobs_problem, memory_gib, playwright_version  # noqa: E402
from display import GENERIC_FIELDS, record_sources as entry_sources  # noqa: E402
from row_checks import RELATIONS as PARTNER_RELATIONS  # noqa: E402  the order the card sorts by
from method_copy import rule_label  # noqa: E402
from release_checks import today  # noqa: E402
from versioning import parse_version  # noqa: E402

def updated_date(data: dict) -> str:
    """The page's update line: recorded work on development builds, otherwise the last release."""
    if parse_version(data["dataset"]["version"]).development or not data.get("changelog"):
        return data["dataset"]["as_of"]
    return data["changelog"][-1]["date"]


PROCESS_START = time.monotonic()  # --job-deadline counts from here: after the imports above, before everything else
DEADLINE_LOWERED = "job deadline: watchdog lowered to"  # stderr marker the flake detector reads
DEADLINE_PASSED = "NOT RUN: job deadline passed"
ROOT = Path(__file__).resolve().parents[1]
QUARANTINE = ROOT / "scripts/quarantine.json"
NOT_RUN = 3
def tip_of(node) -> str:
    """A tooltip's text from the saved or drawn page: its one home, the description screen readers hear
    (aria-description), or data-tip where the element has none (site/index.html tipText); data-exp for older pages."""
    return node.get("data-tip") or node.get("data-exp") or node.get("aria-description") or ""


def original_tip_matches(node, shown: str, original: str) -> bool:
    """The complete name/title is one focusable target on the shared translation route."""
    if node is None:
        return False
    text = "".join(t for t in node.find_all(string=True) if not t.find_parent(class_=("vh", "cites")))
    return bool(
        text == shown and "original-text" in node.get("class", [])
        and node.get("aria-description") == original and tip_of(node) == original
        and ((node.name == "a" and node.get("href")) or node.get("tabindex") == "0")
    )


# One browser for build, gate and contact sheet on every host: the Chromium of the Playwright version this
# script's lock pins (every lock pins the same, the lint checks), in Playwright's default browser folder (host_checks.INSTALL_CHROME).


PLAIN_KEY = re.compile(r"[A-Za-z0-9_-]+")


class Keyed:
    """soup.select_one('<selector>[<attr>="<value>"] <rest>') for many values with one walk of the document.

    The elements matching the selector are grouped by the attribute once. A value held by one element is searched
    inside it (:scope keeps the rest of the selector below it, as in the plain query); a value held by several
    elements, or a value that is not a plain name (which CSS would read differently), takes the plain query, which
    keeps document order. Either way the result is the plain query's. Supported: a selector list split at its commas
    (so no commas inside an arm) and a rest without commas. Build it after the last change to the tree that the
    lookups depend on.
    """

    def __init__(self, soup, selector: str, attr: str = "data-j"):
        self.soup, self.selector, self.attr = soup, selector, attr
        self.held = defaultdict(list)
        for node in soup.select(selector):
            self.held[node.get(attr)].append(node)

    def plain(self, value: str, rest: str) -> str:
        return ", ".join(f'{arm.strip()}[{self.attr}="{value}"] {rest}'.rstrip() for arm in self.selector.split(","))

    def one(self, value: str, rest: str = ""):
        nodes = self.held.get(value, [])
        if not PLAIN_KEY.fullmatch(value) or (rest and len(nodes) > 1):
            return self.soup.select_one(self.plain(value, rest))
        if not nodes:
            return None
        return nodes[0].select_one(f":scope {rest}") if rest else nodes[0]

    def all(self, value: str, rest: str) -> list:
        nodes = self.held.get(value, [])
        if not PLAIN_KEY.fullmatch(value) or len(nodes) > 1:
            return self.soup.select(self.plain(value, rest))
        return nodes[0].select(f":scope {rest}") if nodes else []


def chrome_problem() -> str | None:
    """None when the Chromium build the locked Playwright expects is installed; otherwise which build is missing,
    which are installed and the fix (host_checks.chrome_problem)."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:  # outside the locked script environment (tests that stand in for find_chrome)
        return f"Playwright's Chromium is not installed ({INSTALL_CHROME})"
    with sync_playwright() as playwright:
        path = playwright.chromium.executable_path
    return missing_chrome(path, playwright_version())


def find_chrome() -> str | None:
    """The locked Playwright's Chromium, or None when that build is not installed (chrome_problem says why). It runs
    as an ordinary child process, so a memory cap on the job holds it; a snap browser moved into its own scope."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        path = playwright.chromium.executable_path
    return path if os.path.isfile(path) and os.access(path, os.X_OK) else None


def browser_env() -> dict:
    """The environment every browser starts in: without a session bus, since Chromium otherwise registers its
    browser process as a systemd scope of its own (app-org.chromium.Chromium-PID.scope), out of a job's memory cap.
    Measured with Chrome for Testing 153 under a systemd memory cap: with the bus, the browser process left the
    job's scope; without it, every process stayed."""
    return {**os.environ, "DBUS_SESSION_BUS_ADDRESS": "disabled:"}


LOCALE = "en-GB"  # every page's locale: its default-locale collation then matches the gate's English oracle on any host

# Principle Off-screen sections: the content checks read text and hit-test boxes, which Chromium reports empty while
# content-visibility skips them, so their pages draw every section; checks of the page's rendering as shipped (the
# off-screen sections, the hydration flash) open theirs with skipping=True.
NO_SKIPPING = ("document.addEventListener('DOMContentLoaded', () => { const s = document.createElement('style'); s.textContent ="
               " 'main section.offscreen, main section.offscreen :is(.card-row, .fund-item) { content-visibility: visible !important; }';"
               " document.head.appendChild(s); });")


class LocalisedBrowser:
    """A browser whose new pages and contexts open in LOCALE unless a check names another: Chromium otherwise takes
    its locale from the host (LANG on Linux, the system language on a Mac). They also draw every off-screen section
    (NO_SKIPPING) unless a check passes skipping=True."""

    def __init__(self, browser):
        self._browser = browser

    def __getattr__(self, name):
        return getattr(self._browser, name)

    def new_page(self, skipping: bool = False, **options):
        page = self._browser.new_page(**{"locale": LOCALE, **options})
        if not skipping:
            page.add_init_script(NO_SKIPPING)
        return page

    def new_context(self, skipping: bool = False, **options):
        context = self._browser.new_context(**{"locale": LOCALE, **options})
        if not skipping:
            context.add_init_script(NO_SKIPPING)
        return context


def install_chrome(with_deps: bool = False) -> int:
    """Download the locked Playwright's Chromium (no headless shell: every launch names this browser); with_deps also
    installs its system libraries on Linux, which Playwright does through sudo."""
    code = subprocess.run([sys.executable, "-m", "playwright", "install", *(["--with-deps"] if with_deps else []),
                           "--no-shell", "chromium"]).returncode
    if code:
        return code
    found = find_chrome()
    print(found or "error: the install finished but no Chromium was found", file=sys.stderr)
    return 0 if found else 1


# How we fetched or processed a source is for the data and the repository, not
# for page readers. The version history is exempt: its draft round entries are
# removed at release (release_checks.DRAFT_WORDING).
INTERNAL_VOCABULARY = re.compile(
    r"\br\.jina\b|\bjina\b|\breader proxy\b|\bHTTP [1-5]\d\d\b|\bround \d+(?=:| (?:review|attempt|check|reclassif))"
    r"|\bbreaks ties\b|\brecord IDs?\b|\bthe build\b"
    r"|\b(?:about|exact|at_least|at_most) (?:code|qualifier)\b",
    re.I,
)


def internal_vocabulary(soup) -> list[str]:
    """Phrases of internal process in the page text outside the version history."""
    soup = BeautifulSoup(str(soup), "html.parser")
    for node in soup.select("script, style, template, #changes"):
        node.decompose()
    text = " ".join(soup.get_text(" ").split())
    return [text[max(0, m.start() - 40) : m.end() + 20] for m in INTERNAL_VOCABULARY.finditer(text)]


def check_footer_licence(soup) -> list[str]:
    """Check visible footer copy independently of the dataset and generator."""
    footer = soup.select_one("footer #foot")
    text = " ".join(footer.get_text(" ", strip=True).split()) if footer else ""
    errors = []
    if "Code: MIT; data, page text and documentation: CC BY 4.0" not in text:
        errors.append("footer: missing code/content licence split")
    if (
        "Quotations from sources remain with their rights holders and are reproduced "
        "as quotations; the licence covers our own compilation, text and data."
    ) not in text:
        errors.append("footer: missing quotations licence notice")
    return errors


def labelable(node) -> bool:
    """HTML's labelable elements: what a <label> can name."""
    return node.name in {"button", "meter", "output", "progress", "select", "textarea"} or (node.name == "input" and node.get("type") != "hidden")


def accessible_name(soup, node) -> str:
    """A static approximation of an element's accessible name: aria-labelledby (each referenced element's aria-label,
    else its text with img alt included), else aria-label, else for a form control its <label for> or wrapping
    <label>, else title."""
    def text(n) -> str:
        if n.get("aria-label", "").strip():
            return n["aria-label"].strip()
        parts = [c.get("alt", "") if getattr(c, "name", None) == "img" else (c if isinstance(c, str) else "") for c in n.descendants]
        return " ".join(" ".join(parts).split())
    if node is None:
        return ""
    if refs := [r for key in node.get("aria-labelledby", "").split() if (r := soup.find(id=key)) is not None]:
        return " ".join(text(r) for r in refs).strip()
    if node.get("aria-label", "").strip():
        return node["aria-label"].strip()
    if labelable(node):  # only form controls take a name from <label for> or a wrapping <label>
        label = soup.find("label", attrs={"for": node["id"]}) if node.get("id") else None
        label = label or node.find_parent("label")
        if label is not None and (name := text(label)):
            return name
    return node.get("title", "").strip()


def check_search_wording(soup) -> list[str]:
    """The search inputs and the suggestion list have accessible names (their wording is reviewed, not pinned)."""
    errors = [f"{sel}: no accessible name" for sel in ("#map-search", "#world-filter", "#map-search-list") if not accessible_name(soup, soup.select_one(sel))]
    errors += [f"{sel}: no placeholder" for sel in ("#map-search", "#world-filter") if (n := soup.select_one(sel)) is not None and not n.get("placeholder", "").strip()]
    return errors


def check_dom(html: str, data: dict, dist: Path, base_url: str) -> list[str]:
    """Check the rendered DOM, excluding script/style text from prose checks."""
    soup = BeautifulSoup(html, "html.parser")
    for node in soup.select("script, style, template"):
        node.decompose()
    errors = check_footer_licence(soup) + check_search_wording(soup)
    bad = sorted(
        set(re.findall(r"\b(?:undefined|NaN)\b", soup.get_text(" ", strip=True)))
    )
    if bad:
        errors.append(f"invalid rendered text: {', '.join(bad)}")
    if internal := internal_vocabulary(soup):
        errors.append(f"internal process text shown to readers: {internal[:5]}")

    jurisdictions = data["jurisdictions"]
    positive = {j["id"]: j for j in jurisdictions if j["status"] != "none_identified"}
    mini_ids = {
        j["id"]
        for j in jurisdictions
        if j["europe_scope"] and j["status"] == "none_identified"
    }
    for selector, expected, label in (
        (".card:not(.mini)", positive, "status cards"),
        (".card.mini", mini_ids, "mini-cards"),
    ):
        actual = Counter(card.get("data-j") for card in soup.select(selector))
        if actual != Counter({jid: 1 for jid in expected}):
            errors.append(
                f"{label}: expected one per jurisdiction {sorted(expected)}, got {dict(actual)}"
            )
    for card in soup.select(".card:not(.mini)"):
        jid = card.get("data-j")
        group = card.find_parent(class_="statusgroup")  # under a status heading the card says its status in its accessible name only
        eu_level = card.find_parent(class_="eu-level") is not None  # the EU entry: its dated Status row says it, so the title row has no mark either (owner, 5 Nov 2026)
        if jid in positive and not (
            group.get("data-status") == positive[jid]["status"] and card.select_one(".top > .vh")
            if group else (lambda said: said is not None and said.get_text() == ", " + STATUS_LABELS[positive[jid]["status"]])(card.select_one(".top > .vh")) and not card.select_one(".top > .smark") if eu_level
            else card.select_one(f".smark .st-{positive[jid]['status']}")
        ):
            errors.append(f"card {jid}: missing or incorrect status mark")

    # The table opens filtered to jurisdictions with a status; the count line names the total.
    total = sum(j["kind"] != "supranational" for j in jurisdictions)
    expected_rows = sum(
        j["kind"] != "supranational" and j["status"] != "none_identified"
        for j in jurisdictions
    )
    rows = soup.select("#world-rows > tr")
    if not soup.select_one("table.world #world-rows") or len(rows) != expected_rows:
        errors.append(
            f"world table: expected {expected_rows} rows in the default view, got {len(rows)}"
        )
    count = soup.select_one("#world-count")
    if not count or f"of {total}" not in count.get_text(" ", strip=True):
        errors.append(f"world table: count line does not name the total of {total}")

    source_urls = {source_href(source) for source in data["sources"]}
    citations = soup.select(".cn, .cites a")
    if positive and not citations:
        errors.append("citation marks missing")
    for mark in citations:
        if mark.name != "a" or mark.get("href") not in source_urls:
            errors.append(f"citation mark is not a source URL: {mark.get('href')!r}")

    ids = {str(node["id"]) for node in soup.select("[id]")}
    nav = soup.select("nav a")
    if not nav:
        errors.append("navigation links missing")
    for link in nav:
        href = str(link.get("href") or "")
        if not href.startswith("#") or href[1:] not in ids:
            errors.append(f"navigation target does not exist: {href!r}")

    # The downloads are the data files; the per-record API under api/ is described, not listed.
    files = {
        p.relative_to(dist).as_posix()
        for p in (dist / "data").rglob("*")
        if p.is_file() and p.suffix in (".csv", ".json") and p.name != PAGE_ONLY_DATA
    }
    entries = [n for n in soup.select(".dlist > li") if not n.find_parent(id="using-data")]  # the data files; For programs and agents links the API, llms.txt and the feed
    links = [n for n in soup.select(".dlist a") if not n.find_parent(id="using-data")]
    downloads = Counter(urljoin(base_url, str(a.get("href") or "")) for a in links)
    expected_downloads = Counter({urljoin(base_url, name): 1 for name in files})
    if not files or len(entries) != len(files) or downloads != expected_downloads:
        errors.append(
            f"downloads: expected one entry per CSV/JSON file ({len(files)}), got {len(entries)} entries and {len(links)} links"
        )
    return errors


DOWNLOAD_GROUPS = [
    ("CSV", ["jurisdictions.csv", "institutes.csv", "events.csv", "news.csv"]),
    ("CSV", ["staff.csv", "funding.csv", "people.csv", "outputs.csv", "agreements.csv", "national_partners.csv", "known_activity.csv", "functions.csv"]),
    ("CSV", ["timeline.csv", "closest_bodies.csv", "related_bodies.csv", "network_milestones.csv"]),
    ("CSV", ["sources.csv", "source_excerpts.csv", "claims.csv", "exchange_rates.csv"]),
    ("JSON", ["aisi.json", "aisi.schema.json", "datapackage.json", "shared-claim-coverage.json"]),
]


def check_download_groups(soup, files: set) -> list[str]:
    """Grouped downloads retain their file contracts and non-empty headings and link labels."""
    errors = []
    groups = soup.select(".downloads > .dlgroup")
    got = []
    for group in groups:
        heading = group.select_one("h4")
        # The file-type badge alone does not name the group.
        title = "".join(t for t in heading.find_all(string=True) if not t.find_parent(class_="label")).strip() if heading else ""
        if not title:
            errors.append("download group heading is missing or empty")
        kind = group.select_one("h4 .label")
        rows = []
        for li in group.select(".dlist > li"):
            label, file = li.select_one(".dl-title"), li.select_one(".dl-file")
            if not label or not label.get_text(strip=True):
                errors.append("download link label is missing or empty")
            rows.append(file.get_text(strip=True) if file else "")
        got.append((kind.get_text(strip=True) if kind else "", rows))
    if got != DOWNLOAD_GROUPS:
        errors.append(f"download file groups differ from the contract: {got}")
    if soup.select(".dlist .tag, .dlist .dl-file:empty, .dlist .d:empty") or soup.select(".two .dlist"):
        errors.append("downloads repeat the file type per row, lack a file name or description, or sit in a second column")
    listed = [name for _, rows in got for name in rows]
    if files and Counter(listed) != Counter(name.removeprefix("data/") for name in files if "/" not in name.removeprefix("data/")):
        errors.append("download groups do not cover the data files exactly once")
    return errors


def check_info_lines(page, width: int) -> list[str]:
    """Supporting text: information marks remain legible and reachable at the surrounding text size."""
    marks = page.evaluate("""() => [...document.querySelectorAll('.info')].filter(n=>n.checkVisibility({contentVisibilityAuto:true})).map(n=>{
      const s=getComputedStyle(n), p=getComputedStyle(n.parentElement), ring=getComputedStyle(n,'::before');
      const letter=n.querySelector('.info-letter'), l=letter && getComputedStyle(letter), r=n.getBoundingClientRect();
      return {font:parseFloat(p.fontSize), height:parseFloat(ring.height), width:parseFloat(ring.width), stroke:parseFloat(ring.borderTopWidth),
        face:!!l && l.fontFamily===p.fontFamily && l.fontStyle==='normal' && l.fontWeight==='400',
        scale:!!l && Math.abs(parseFloat(l.fontSize)/parseFloat(p.fontSize)-.65)<.01,
        hit:Math.min(r.width,r.height), required:parseFloat(s.height)};
    })""")
    return [f"{width}px: {e}" for e in info_mark_errors(marks)]


def info_mark_errors(marks: list[dict]) -> list[str]:
    errors = []
    if not marks:
        return ['no information marks measured']
    for mark in marks:
        size = mark['font']
        if not .68 <= mark['height'] / size <= .76 or abs(mark['width'] - mark['height']) > .1:
            errors.append('information ring is not a .72em circle')
        if not .04 <= mark['stroke'] / size <= .08:
            errors.append('information ring stroke is outside .04 to .08em')
        if not mark['face'] or not mark['scale']:
            errors.append('information i does not use the surrounding face at .65em and regular weight')
        if mark['hit'] < mark['required'] - 1:
            errors.append('information mark lost its hit area')
    return errors


def check_legend_place(page, width: int) -> list[str]:
    """The map legend sits directly under the map: beside the region list on wide screens, and above it where the list stacks (1100px and narrower)."""
    got = page.evaluate("""() => { const r = s => document.querySelector(s).getBoundingClientRect(), map = r('#worldmap'), legend = r('#legend'), list = r('#regions');
      return {gap: legend.top - map.bottom, before: legend.bottom <= list.top + 1, stacked: list.top >= map.bottom - 1}; }""")
    if got["stacked"] != (width <= 1100):
        return [f"{width}px: the region list is {'stacked under' if got['stacked'] else 'beside'} the map, not {'stacked under' if width <= 1100 else 'beside'} it"]
    if got["gap"] < -1 or got["gap"] > 64:
        return [f"{width}px: the map legend is {got['gap']:.0f}px below the map, not directly under it"]
    if got["stacked"] and not got["before"]:
        return [f"{width}px: the map legend comes after the stacked region list instead of directly under the map"]
    return []


def check_version_history(page, data: dict) -> list[str]:
    """The row and list show the recorded versions and dates, and each item has notes."""
    latest = data["changelog"][-1]
    day = date.fromisoformat(latest["date"]).strftime("%-d %b %Y")
    errors = []
    page.evaluate("document.querySelector('#changes').open = false")
    row = page.locator("#changes-latest")
    text = " ".join(row.inner_text().split())
    if text != f"{latest['version']} {day}":
        errors.append(f"version history row reads {text[:60]!r}, expected only version and date {latest['version']} \u00b7 {day}")
    if page.evaluate("() => { const s = document.querySelector('#changes > summary').getBoundingClientRect(), b = document.querySelector('#changes').getBoundingClientRect(); return s.right > b.right + 1 || s.width > innerWidth; }"):
        errors.append("version history row is wider than its box")
    page.evaluate("document.querySelector('#changes').open = true")
    heads = [" ".join(t.split()) for t in page.locator("#changelog > li > .change-head").all_inner_texts()]
    shown = data["dataset"]["display"]["changelog"]
    want = [f"{c['version']} {date.fromisoformat(c['date']).strftime('%-d %b %Y')}" for c in shown]
    if heads != want:
        errors.append(f"version history list {heads} differs from the changelog {want}")
    if any(not t.strip() for t in page.locator("#changelog > li > p").all_inner_texts()):
        errors.append("a version history item has no notes")
    page.evaluate("document.querySelector('#changes').open = false")
    return errors


def check_download_layout(page) -> list[str]:
    """Downloads must contain visible rows."""
    found = page.evaluate("""() => {
      return [...document.querySelectorAll('.dlist a')].some(a => a.getClientRects().length);
    }""")
    return [] if found else ["no download rows"]


USING_THE_DATA = "For programs and agents"


PAGE_BYTES_BUDGET = 2_000_000  # search engines read only the first 2 MB of a page, so everything a reader should find must sit within it
# What a first visit downloads, in gzip bytes (level 6, as the host compresses text; woff2 counts as stored). Budgets,
# not timings, so the check is deterministic: a saved map drawing past its budget, or the data the page reads, or the
# whole first load, fails the gate and asks for a decision instead of slowing the first screen unnoticed. Set about a
# quarter (maps) or a tenth (the rest) above the sizes on 8 Nov 2026: map-wide 71 KB, map-narrow 44 KB, page.json
# 541 KB, first load 1.51 MB (all font subsets counted, the larger map once). page.json raised to 750 KB by the
# owner's decision of 14 Nov 2026 (593 KB on 1.0.10, about 144 KB of it source excerpts), and the first load by the
# same 150 KB so that page.json can reach its budget.
SAVED_MAP_GZIP_BUDGET = {"assets/map-wide.svg": 90_000, "assets/map-narrow.svg": 56_000}
PAGE_DATA_GZIP_BUDGET = 750_000
FIRST_LOAD_GZIP_BUDGET = 1_800_000
# Every file a first visit requests, as patterns relative to the site; check_first_load fails on a request outside them.
FIRST_LOAD = ("index.html", "data/page.json", "data/datapackage.json", "assets/map-wide.svg", "assets/map-narrow.svg", "assets/d3.*.js",
              "assets/topojson-client.*.js", "assets/world-*.json", "assets/fonts/*.woff2", "favicon.svg")


def check_meta_descriptions(soup, data: dict) -> list[str]:
    """The heading stays European; the title and the search and social descriptions say what the tracker covers (counted here)."""
    states = sum(j["kind"] == "state" for j in data["jurisdictions"])
    coverage = f"Covers all {states} UN member states, Taiwan and the EU, with sources and open data"
    errors = []
    if not soup.title or not soup.title.get_text(strip=True):
        errors.append("page title is missing or empty")
    for selector in ('meta[name="description"]', 'meta[property="og:description"]', 'meta[name="twitter:description"]'):
        nodes = soup.select(selector)
        if len(nodes) != 1 or coverage not in nodes[0].get("content", ""):
            errors.append(f"{selector} must appear once and say '{coverage}'")
    return errors


def check_share_tags(soup, name: str, title: str, address: str) -> list[str]:
    """The Open Graph and Twitter tags of one built page: each once, titled like the page, one site name, and the
    same preview image and address on both networks."""
    def one(selector):
        return [str(n.get("content") or "").strip() for n in soup.select(selector)]

    want = {'meta[property="og:title"]': title, 'meta[name="twitter:title"]': title,
            'meta[property="og:url"]': address, 'meta[property="og:image"]': address.rsplit("/", 1)[0] + "/social-preview.png",
            'meta[name="twitter:image"]': address.rsplit("/", 1)[0] + "/social-preview.png", 'meta[name="twitter:card"]': "summary_large_image", 'meta[property="og:type"]': "website"}
    errors = [f"{name}: {selector} is {one(selector)}, expected [{value!r}]" for selector, value in want.items() if one(selector) != [value]]
    for selector in ('meta[property="og:site_name"]', 'meta[property="og:title"]', 'meta[name="twitter:title"]', 'meta[property="og:description"]', 'meta[name="twitter:description"]', 'meta[property="og:image:alt"]'):
        if len(one(selector)) != 1 or not one(selector)[0]:
            errors.append(f"{name}: {selector} must appear once and not be empty")
    return errors


class _WorldSectionEnd(HTMLParser):
    """Walks the page keeping the stack of open <section> elements (as start offsets) and records the offset after the
    end tag that closes the innermost section open where the element with id="world" starts. Offsets are into the
    page decoded as latin-1, which is one character per byte."""

    def __init__(self, text: str):
        super().__init__(convert_charrefs=False)
        self.text = text
        self.lines = [0]
        for line in text.split("\n"):  # as the parser counts lines: only at \n, not at other separators such as \x85
            self.lines.append(self.lines[-1] + len(line) + 1)
        self.stack: list[int] = []
        self.world: int | None = None  # start offset of the section holding the world heading
        self.found = False  # the element with id="world" was seen
        self.end: int | None = None

    def position(self) -> int:
        line, column = self.getpos()
        return self.lines[line - 1] + column

    def handle_starttag(self, tag, attrs):
        if tag == "section":
            self.stack.append(self.position())
        if ("id", "world") in attrs and not self.found:
            self.found = True
            self.world = self.stack[-1] if self.stack else None

    def handle_endtag(self, tag):
        if tag == "section" and self.stack:
            start = self.stack.pop()
            if self.end is None and self.world is not None and start == self.world:
                self.end = self.text.index(">", self.position()) + 1


def world_section_end(page: bytes) -> int | None:
    """The byte offset where the section that holds the element with id="world" closes; None if that element is
    missing, is not inside a section, or its section never closes."""
    parser = _WorldSectionEnd(page.decode("latin-1"))
    parser.feed(parser.text)
    parser.close()
    return parser.end


def gzip_size(path: Path) -> int:
    """The bytes the host sends: gzip level 6 for text, the stored size for woff2, which is compressed already."""
    import gzip

    return path.stat().st_size if path.suffix == ".woff2" else len(gzip.compress(path.read_bytes(), 6, mtime=0))


def check_transfer_budgets(dist: Path) -> list[str]:
    """The first visit's download stays within its budgets (SAVED_MAP_GZIP_BUDGET, PAGE_DATA_GZIP_BUDGET,
    FIRST_LOAD_GZIP_BUDGET): each saved map drawing, the page data and the sum over FIRST_LOAD, counting one map."""
    from fnmatch import fnmatch

    errors, files = [], [p.relative_to(dist).as_posix() for p in dist.rglob("*") if p.is_file()]
    sizes = {}
    for pattern in FIRST_LOAD:
        found = [f for f in files if fnmatch(f, pattern)]
        if not found:
            errors.append(f"first load: no file in the build matches {pattern!r}")
        sizes.update({f: gzip_size(dist / f) for f in found})
    for name, budget in [*SAVED_MAP_GZIP_BUDGET.items(), ("data/page.json", PAGE_DATA_GZIP_BUDGET)]:
        if sizes.get(name, 0) > budget:
            errors.append(f"{name}: {sizes[name]} bytes gzip, over its budget of {budget}")
    total = sum(sizes.values()) - min(sizes.get(name, 0) for name in SAVED_MAP_GZIP_BUDGET)  # a visit loads one drawing
    if total > FIRST_LOAD_GZIP_BUDGET:
        errors.append(f"first load: {total} bytes gzip, over its budget of {FIRST_LOAD_GZIP_BUDGET}")
    return errors


def check_saved_map_early(html: str, soup) -> list[str]:
    """The head script that starts the visible saved map drawing's download (saved-map-early in site/index.html) comes
    before every stylesheet and external script, names exactly the files the map shells load, and picks the narrow one
    under the query where the page's CSS shows the narrow drawing."""
    script, errors = soup.select_one("head > script#saved-map-early"), []
    if script is None:
        return ["head: no script#saved-map-early to start the saved map drawing's download"]
    first = soup.head.find(lambda tag: tag is script or (tag.name == "script" and tag.get("src")) or (tag.name == "link" and "stylesheet" in (tag.get("rel") or [])) or tag.name == "style")
    if first is not script:
        errors.append("head: script#saved-map-early does not come before every stylesheet and external script")
    shells = {layout: [str(s.get("data-src")) for s in soup.select(f"#worldmap svg.map-{layout}[data-src]")] for layout in ("wide", "narrow")}
    named = {layout: [str(script.get(f"data-{layout}"))] for layout in ("wide", "narrow")}
    if shells != named:
        errors.append(f"head: script#saved-map-early names {named}, the map shells load {shells}")
    swap = html.find(".worldmap .map-narrow { display: block; }")
    media = html.rfind("@media", 0, swap)
    query = html[media + len("@media"):html.find("{", media)].strip() if swap > 0 and media >= 0 else None
    if query != script.get("data-narrow-media"):
        errors.append(f"head: script#saved-map-early picks the narrow drawing under {script.get('data-narrow-media')!r}, the CSS shows it under {query!r}")
    return errors


def check_page_size(dist: Path) -> list[str]:
    """Search engines read only the first PAGE_BYTES_BUDGET bytes, so the state-by-state content must end within them."""
    page = (dist / "index.html").read_bytes()
    end = world_section_end(page)
    if end is None:
        return ["index.html: cannot find where the world section (id=\"world\") ends"]
    if end > PAGE_BYTES_BUDGET:
        return [f"index.html: the world section ends at byte {end}, beyond the {PAGE_BYTES_BUDGET} that search engines read"]
    return []


def check_network_cells(soup, data) -> list[str]:
    """The network's member list is complete: on the saved page, which holds every table row, a non-member reads
    Not a member with its tooltip, never Not published, and the Member cells match the recorded memberships."""
    errors = []
    members = {j["id"] for j in data["jurisdictions"] if any(m.get("body") == "intl-network" for m in j.get("memberships", []))}
    rows = soup.select("#world-rows > tr, #world-rest > tr")
    cells = soup.select('#world-rows > tr > td[data-col="net"], #world-rest > tr > td[data-col="net"]')
    if not rows or len(cells) != len(rows):
        errors.append(f"table: {len(cells)} Network cells for {len(rows)} rows")
    for td in cells:
        jid = td.find_parent("tr").get("data-j")
        mark = td.select_one(".nonmember")
        if td.select_one(".missing"):
            errors.append(f"table: Network cell for {jid} says Not published; the member list is complete")
        if mark is not None and (mark.get_text(strip=True) != "Not a member" or not tip_of(mark).strip() or mark.get("tabindex") != "0"):
            errors.append(f"table: Network cell for {jid} lacks 'Not a member' with a non-empty focusable tooltip")
        if (mark is None) != (jid in members):
            errors.append(f"table: Network cell for {jid} disagrees with the recorded memberships")
    return errors


def check_form_basis(soup, data) -> list[str]:
    """legal_basis gives the qualifying mandate; the Form row keeps unknown form distinct from a law-based mandate."""
    errors = []
    institutes = {i["jurisdiction_id"]: i for i in data["institutes"]}
    for card in soup.select(".card:not(.mini)[data-j]"):
        inst = institutes.get(card.get("data-j"))
        if not inst or not inst.get("legal_basis"):
            continue
        text = " ".join(card.get_text(" ").split())
        # A founding act alone is no form: without a known legal form or host type the Form row says Not published first.
        form_known = inst.get("legal_form") in {"new_body", "unit_in_existing_body", "federation_of_existing_bodies", "university_centre"} or (inst.get("placement") or {}).get("host_type") in {"ministry", "government_agency", "public_research_body", "university", "supranational"}
        form = card.select_one('.row[data-track="4"] .v')
        if not form_known and (not form or not " ".join(form.get_text(" ").split()).startswith("Not published;")):
            errors.append(f"{card.get('data-j')}: Form gives only the founding act; it should open with 'Not published'")
        if inst["legal_basis"]["type"] == "law":
            want = "mandate to be set by law" if inst["legal_basis"]["state"] == "foreseen" else "mandate set by law"
            if want not in text:
                errors.append(f"{card.get('data-j')}: Form lacks {want!r}")
    return errors


HOST_ACRONYM = re.compile(r"[A-Z][\w-]*(?: (?:[A-Z][\w-]*|of|and|for|the))* \(([A-Z][A-Za-z]{1,7})\)", re.ASCII)


def host_short(inst: dict) -> str | None:
    """The host's short form for the Form row, or None (mirrors hostShort in site/index.html)."""
    if not inst.get("host") or inst.get("legal_form") not in {"unit_in_existing_body", "university_centre"}:
        return None
    host = re.split(r",? (?:with|under) |; ", inst["host"])[0].strip()
    named = re.sub(r"\s*\([^)]*\)", "", host).lower()
    rt = inst.get("reports_to") or {}
    parents = ";".join(v for v in (rt.get("name"), rt.get("name_en")) if v).lower().split(";")
    if any(p.strip() and named in p for p in parents):
        return None
    return HOST_ACRONYM.sub(r"\1", host, count=1)


def check_form_host(soup, data) -> list[str]:
    """The Form row names the host's short form in brackets after the form, unless Reports to already names the host."""
    errors = []
    institutes = {i["jurisdiction_id"]: i for i in data["institutes"]}
    for card in soup.select(".card:not(.mini)[data-j]"):
        inst = institutes.get(card.get("data-j"))
        form = card.select_one('.row[data-track="4"] .v')
        if not inst or not form:
            continue
        form = copy.copy(form)  # the soup is shared with later checks
        for marks in form.select(".cites"):
            marks.decompose()
        first = " ".join(form.get_text().split()).split(";")[0]  # no separator: an acronym is its own element
        short = host_short(inst)
        if short and not first.endswith(f"({short})"):
            errors.append(f"{card.get('data-j')}: Form should name the host as ({short}), got {first!r}")
        # A federation's Form row also holds its member list, so only a unit's or centre's form text is held to no brackets.
        if not short and inst.get("legal_form") in {"unit_in_existing_body", "university_centre"} and "(" in first:
            errors.append(f"{card.get('data-j')}: Form names a host in brackets where none is due: {first!r}")
    return errors


def check_static_page(dist: Path, data: dict) -> list[str]:
    """The pre-rendered page as saved, before any script runs: every jurisdiction, the stat row, the download links,
    the saved map's early download and the first visit's byte budgets."""
    html = (dist / "index.html").read_text(encoding="utf-8")
    soup = BeautifulSoup(html, "html.parser")
    errors = check_meta_descriptions(soup, data) + check_saved_map_early(html, soup) + check_transfer_budgets(dist)
    if not soup.html or not soup.html.has_attr("data-prerendered") or not soup.select_one("template#page-template"):
        errors.append("static page: not pre-rendered (no data-prerendered marker or page template)")
    for node in soup.select("script, style, template"):
        node.decompose()
    errors.extend(check_footer_licence(soup))
    text = " ".join(soup.get_text(" ").split())
    missing = [j["name"] for j in data["jurisdictions"] if j["name"] not in text]
    if missing:
        errors.append(f"static page: {len(missing)} jurisdictions missing, e.g. {missing[:5]}")
    errors.extend(check_network_cells(soup, data))
    errors.extend(check_form_basis(soup, data))
    errors.extend(check_form_host(soup, data))
    rows = soup.select("#world-rows > tr, #world-rest > tr")
    if len(rows) != sum(j["kind"] != "supranational" for j in data["jurisdictions"]):
        errors.append(f"static page: expected a table row for every state and territory, got {len(rows)}")
    if len(soup.select("#figures > .figure")) != 3:
        errors.append("static page: the figures row is missing")
    if uncited := uncited_values(soup):
        errors.append(f"static page: {len(uncited)} displayed values without a citation: {uncited[:10]}")
    files = sorted(p.relative_to(dist).as_posix() for p in (dist / "data").rglob("*") if p.suffix in (".csv", ".json") and p.name != PAGE_ONLY_DATA)
    errors.extend(check_download_groups(soup, set(files)))
    links = sorted(str(a.get("href")) for a in soup.select(".dlist a") if not a.find_parent(id="using-data"))
    if links != files:
        errors.append(f"static page: expected one download link per data file ({len(files)}), got {len(links)}")
    return errors


# A pixel counts as changed above 12 in any colour channel (as in the contact sheet). Re-rendering the same DOM moves
# some glyph edges by a subpixel; more than this many changed pixels in one viewport is a visible change.
FLASH_PIXELS = 500  # measured: at most 75 on an unchanged page


def flash_evidence(page, name: str, before: bytes, after: bytes) -> None:
    """Where AISI_GATE_EVIDENCE names a directory (CI uploads it with the shard results), keep a flash's two frames,
    the box of the changed pixels and the page's state, so an occurrence that a rerun does not repeat can be read."""
    if not (root := os.environ.get("AISI_GATE_EVIDENCE")):
        return
    out = Path(root) / re.sub(r"[^\w.-]+", "-", name)
    out.mkdir(parents=True, exist_ok=True)
    (out / "before.png").write_bytes(before)
    (out / "after.png").write_bytes(after)
    import base64

    box = page.evaluate(
        """async ([a, b]) => {
          const load = async src => { const i = new Image(); i.src = 'data:image/png;base64,' + src; await i.decode(); return i; };
          const [x, y] = await Promise.all([load(a), load(b)]);
          const data = img => { const c = new OffscreenCanvas(img.width, img.height), g = c.getContext('2d'); g.drawImage(img, 0, 0); return g.getImageData(0, 0, img.width, img.height).data; };
          if (x.width !== y.width || x.height !== y.height) return null;
          const p = data(x), q = data(y); let x0 = Infinity, y0 = Infinity, x1 = -1, y1 = -1;
          for (let i = 0; i < p.length; i += 4) if (Math.abs(p[i] - q[i]) > 12 || Math.abs(p[i + 1] - q[i + 1]) > 12 || Math.abs(p[i + 2] - q[i + 2]) > 12) {
            const n = i / 4, px = n % x.width, py = (n - px) / x.width; x0 = Math.min(x0, px); x1 = Math.max(x1, px); y0 = Math.min(y0, py); y1 = Math.max(y1, py); }
          return x1 < 0 ? null : { x0, y0, x1, y1 };
        }""",
        [base64.b64encode(before).decode(), base64.b64encode(after).decode()],
    )
    state = page.evaluate(
        """() => ({ scrollY, height: document.documentElement.scrollHeight, settled: document.documentElement.hasAttribute('data-settled'),
          map: document.getElementById('worldmap')?.dataset.map, fonts: document.fonts.status,
          sections: [...document.querySelectorAll('main section')].map(s => [s.id, Math.round(s.getBoundingClientRect().top), Math.round(s.getBoundingClientRect().height)]) })"""
    )
    (out / "state.json").write_text(json.dumps({"diff_box": box, "state": state}, indent=1))


def changed_pixels(page, before: bytes, after: bytes) -> int:
    """Count pixels that differ by more than 12 in any channel, decoding both screenshots in the page itself."""
    import base64

    return page.evaluate(
        """async ([a, b]) => {
          const load = async src => { const i = new Image(); i.src = 'data:image/png;base64,' + src; await i.decode(); return i; };
          const [x, y] = await Promise.all([load(a), load(b)]);
          const data = img => { const c = new OffscreenCanvas(img.width, img.height), g = c.getContext('2d'); g.drawImage(img, 0, 0); return g.getImageData(0, 0, img.width, img.height).data; };
          if (x.width !== y.width || x.height !== y.height) return x.width * x.height;
          const p = data(x), q = data(y); let n = 0;
          for (let i = 0; i < p.length; i += 4) if (Math.abs(p[i] - q[i]) > 12 || Math.abs(p[i + 1] - q[i + 1]) > 12 || Math.abs(p[i + 2] - q[i + 2]) > 12) n++;
          return n;
        }""",
        [base64.b64encode(before).decode(), base64.b64encode(after).decode()],
    )


MAP_WAIT_MS = 10_000  # the page's MAP_WAIT


def mapless_controls(page, failed: bool) -> list[str]:
    """On a page that took over without the map: the saved World drawing stands in and its dots open records;
    search, the region list, table sort and filter work; another region says the map is loading or failed."""
    errors = []
    try:
        page.wait_for_selector("#worldmap svg.map-wide .tgt", state="attached", timeout=10_000)
    except Exception:  # noqa: BLE001 - the Playwright timeout
        errors.append("the saved map drawing is gone")
    page.locator("#map-search").fill("ger")
    page.locator("#map-search").press("Enter")
    if page.locator("#record-name .head-state").text_content() != "Germany":
        errors.append("search does not open Germany's record")
    page.locator("#record .record-close").click()
    page.locator('#worldmap svg.map-wide .tgt[data-j="FR"]').dispatch_event("click")
    if not page.locator("#record").is_visible() or page.locator("#record-name .head-state").text_content() != "France":
        errors.append("a dot on the saved map does not open France's record")
    page.locator("#record .record-close").click()
    page.locator('#regions .region-head[data-region="Asia"]').first.click()
    want = "failed" if failed else "loading"
    if page.locator('#regions .region-head[data-region="Asia"]').first.get_attribute("aria-expanded") != "true" or page.locator("#headline").get_attribute("data-region") != "Asia":
        errors.append("the region list does not switch to Asia")
    if page.locator("#worldmap").get_attribute("data-map") != want or not page.locator("#worldmap").inner_text().strip() or page.locator("#worldmap svg").count():
        errors.append(f"Asia's map box does not show the {want} state with a message (it reads {page.locator('#worldmap').inner_text()!r})")
    page.locator('#regions .region-head[data-region="Europe"]').first.click()
    first = "() => document.querySelector('table.world tbody tr:not([hidden])')?.dataset.j"
    before = page.evaluate(first)
    page.evaluate("() => { const th = document.querySelector('table.world th[data-k=\"name\"]'); for (let n = 0; n < 3 && th.getAttribute('aria-sort') !== 'descending'; n++) th.querySelector('button').click(); }")
    if page.locator('table.world th[data-k="name"]').get_attribute("aria-sort") != "descending" or page.evaluate(first) == before:
        errors.append("the table does not sort by name")
    page.locator("#world-filter").fill("fra")
    shown = page.evaluate("() => [...document.querySelectorAll('table.world tbody tr')].filter(r => r.getClientRects().length).map(r => r.dataset.j)")
    if not shown or "FR" not in shown or len(shown) > 3:
        errors.append(f"the table filter 'fra' shows {shown}")
    page.locator("#world-filter").fill("")
    return errors


def hydration_paint(browser, base_url: str) -> list[str]:
    """No visible flash: with the data held back, the saved page paints exactly what the hydrated page paints, at the
    top of the page and scrolled to the worldwide cards, at 1440, 1000 and 390 px in both themes. The section
    bar and the group rows are masked: their scroll-dependent state starts with the script."""
    errors = []
    for width in (1440, 1000, 390):
        for theme in ("light", "dark"):
            for place in ("top", "#world-cards"):
                page = themed_page(browser, theme, viewport={"width": width, "height": 1000}, skipping=True)  # the page as shipped
                held = Held(page, PAGE_DATA)
                page.goto(base_url, wait_until="load")
                page.evaluate("document.fonts.ready")
                if place != "top":
                    page.evaluate(f"window.scrollTo(0, document.querySelector('{place}').getBoundingClientRect().top + window.scrollY)")
                page.wait_for_timeout(300)
                before = page.screenshot(mask=[page.locator(".secnav, .group-nav")])
                saved_cards = page.locator('#world-cards .card').evaluate_all(
                    "cards => cards.filter(c => { const r=c.getBoundingClientRect(); return r.top<innerHeight && r.bottom>0; }).map(" + CARD_HEAD_SPACE_JS + ")"
                ) if place == "#world-cards" else []
                held.release()
                wait_hydrated(page)
                page.wait_for_timeout(300)
                if page.evaluate("getComputedStyle(document.body).backgroundColor") != GROUNDS[theme]:
                    errors.append(f"hydration {width}px {theme}: the page does not show the {theme} theme")
                after = page.screenshot(mask=[page.locator(".secnav, .group-nav")])
                if place == "#world-cards":
                    live_cards = page.locator('#world-cards .card').evaluate_all(
                        "(cards, ids) => cards.filter(c => ids.includes(c.id)).map(" + CARD_HEAD_SPACE_JS + ")",
                        [m["id"] for m in saved_cards],
                    )
                    errors.extend(f"hydration {width}px {theme}: {e}" for e in card_hydration_errors(saved_cards, live_cards))
                changed = changed_pixels(page, before, after)
                if changed > FLASH_PIXELS:
                    errors.append(f"hydration {width}px {theme} at {place}: {changed} pixels change when the script takes over (visible flash)")
                    try:  # best effort: the flash is reported either way
                        flash_evidence(page, f"hydration-{width}-{theme}-{place}", before, after)
                    except Exception as exc:  # noqa: BLE001 - a full disk or a browser error must not hide the flash
                        errors.append(f"hydration {width}px {theme} at {place}: the flash's evidence was not saved ({type(exc).__name__}: {exc})")
                if place == "top":  # the drawn map replaces the saved drawing after the take-over: that swap shows nothing either
                    map_ready(page)
                    page.wait_for_timeout(300)
                    drawn = page.screenshot(mask=[page.locator(".secnav, .group-nav")])
                    changed = changed_pixels(page, before, drawn)
                    if changed > FLASH_PIXELS:
                        errors.append(f"hydration {width}px {theme}: {changed} pixels change when the drawn map replaces the saved drawing (visible flash)")
                        try:
                            flash_evidence(page, f"map-swap-{width}-{theme}", before, drawn)
                        except Exception as exc:  # noqa: BLE001 - as above
                            errors.append(f"hydration {width}px {theme}: the map swap's evidence was not saved ({type(exc).__name__}: {exc})")
                page.close()
    return errors


def hydration_edges(browser, base_url: str, dist: Path) -> list[str]:
    """What a reader did before the script took over survives it; without the data the saved page stays; without
    the map, or with a late one, the page still takes over."""
    from playwright.sync_api import TimeoutError as PlaywrightTimeout

    errors = []
    # What a reader does before the script takes over survives it: an opened disclosure, a typed filter.
    page = browser.new_page(viewport={"width": 1440, "height": 1000})
    held = Held(page, PAGE_DATA)
    page.goto(base_url, wait_until="load")
    page.locator("#world-cards details summary").first.click()
    page.fill("#world-filter", "fra")
    page.locator("#method-details > summary").focus()  # a control below the table, without an id
    held.release()
    wait_hydrated(page)
    state = page.evaluate("[document.querySelector('#world-cards details').open, document.getElementById('world-filter').value, document.activeElement === document.querySelector('#method-details > summary')]")
    if state != [True, "fra", True]:
        errors.append(f"hydration: the reader's open disclosure, filter text or focus was lost ({state})")
    page.close()
    # A link to a disclosure leaves it open and the page where the browser put it, also when the fonts
    # have loaded before the data (cached fonts): the order in which a restore could close the target.
    data = json.loads((dist / "data/aisi.json").read_text(encoding="utf-8"))
    mini = sorted(j["id"] for j in data["jurisdictions"] if j["europe_scope"] and j["status"] == "none_identified")[0]
    for target in ("method-details", "changes", f"c-{mini.lower()}"):
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        held = Held(page, PAGE_DATA)
        page.goto(f"{base_url}#{target}", wait_until="load")
        fonts = page.evaluate("document.fonts.ready.then(() => document.fonts.status)")
        held.wait()
        if len(held.routes) != 1:
            errors.append(f"hydration: the data request for #{target} was not held once ({len(held.routes)} held)")
        before = page.evaluate("window.scrollY")
        held.release()
        wait_hydrated(page)
        page.evaluate("new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)))")
        state = page.evaluate(f"[document.getElementById('{target}')?.open, window.scrollY]")
        if fonts != "loaded" or state[0] is not True or abs(state[1] - before) > 1:
            errors.append(f"hydration: a link to #{target} (fonts {fonts}) ends {'open' if state[0] else 'closed'} at y={state[1]}, scrolled from y={before}")
        page.close()
    page = browser.new_page(viewport={"width": 1440, "height": 1000})
    page.route(PAGE_DATA, lambda route: route.abort())
    page.goto(base_url, wait_until="load")
    page.wait_for_timeout(1000)
    if not page.evaluate("document.documentElement.hasAttribute('data-prerendered') && document.querySelectorAll('#figures > .figure').length > 0"):
        errors.append("hydration: without the data file the saved page does not stay as it is")
    page.close()
    # Without the map (topology and d3 blocked) the page still takes over: the saved drawing stays.
    page = browser.new_page(viewport={"width": 1440, "height": 1000})
    page.route("**/assets/world-*.json", lambda route: route.abort())
    page.route("**/assets/d3.*.js", lambda route: route.abort())
    page.goto(base_url, wait_until="load")
    try:
        wait_hydrated(page, 15_000)
        if (mark := page.evaluate("document.getElementById('worldmap').dataset.map")) != "saved":
            errors.append(f"hydration without the map: #worldmap data-map is {mark!r}, not 'saved' (the saved drawing stands in)")
        errors.extend(f"hydration without the map: {e}" for e in mapless_controls(page, failed=True))
    except PlaywrightTimeout:
        errors.append("hydration: without the map topology and d3 the page script does not take over")
    page.close()
    # A tap on the phone's saved drawing opens the nearest dot's record, as on the drawn map: at France's centre the
    # overlapping targets of its neighbours (painted later) must not win. A real pointer click, so hit testing applies.
    page = browser.new_page(viewport={"width": 390, "height": 900})
    page.route("**/assets/world-*.json", lambda route: route.abort())
    page.route("**/assets/d3.*.js", lambda route: route.abort())
    page.goto(base_url, wait_until="load")
    try:
        wait_hydrated(page, 15_000)
        france = page.locator('#worldmap svg.map-narrow .tgt[data-j="FR"]')
        france.wait_for(state="attached", timeout=10_000)
        france.scroll_into_view_if_needed()
        box = france.bounding_box()
        page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
        if (name := page.locator("#record-name .head-state").text_content()) != "France":
            errors.append(f"hydration without the map: a tap at France's centre on the phone's saved drawing opens {name!r}, not France")
    except PlaywrightTimeout:
        errors.append("hydration without the map: the phone's saved drawing has no target for France")
    page.close()
    # The saved drawing follows the window across the width where the layouts swap, also while its file is still loading.
    page = browser.new_page(viewport={"width": 1440, "height": 1000})
    page.route("**/assets/world-*.json", lambda route: route.abort())
    page.route("**/assets/d3.*.js", lambda route: route.abort())
    held = Held(page, "**/assets/map-narrow.svg*")
    page.goto(base_url, wait_until="load")
    try:
        wait_hydrated(page, 15_000)
        page.wait_for_selector("#worldmap svg.map-wide .tgt", state="attached", timeout=10_000)
        for width in (390, 1440, 390):  # the narrow file stays pending across these
            page.set_viewport_size({"width": width, "height": 1000})
            page.wait_for_timeout(300)
        held.release()
        page.wait_for_selector("#worldmap svg.map-narrow .tgt", state="attached", timeout=10_000)
        page.set_viewport_size({"width": 1440, "height": 1000})
        page.wait_for_timeout(500)
        counts = page.evaluate("() => ['wide', 'narrow'].map(l => [document.querySelectorAll('#worldmap svg.map-' + l + ' pattern').length, document.querySelectorAll('#worldmap svg.map-' + l + ' .tgt').length > 0])")
        if counts != [[1, True], [1, True]]:
            errors.append(f"hydration without the map: the saved drawings after crossing the layout width are {counts}, expected one filled drawing per layout")
    except PlaywrightTimeout:
        errors.append("hydration without the map: the saved drawing does not follow the window across the layout width")
    page.close()
    # A map that is slow: the page takes over without it, says so after the wait, and draws it when it comes.
    page = browser.new_page(viewport={"width": 1440, "height": 1000})
    late = Held(page, "**/assets/world-*.json")
    page.goto(base_url, wait_until="load")
    try:
        wait_hydrated(page, 15_000)
        errors.extend(f"hydration before the map: {e}" for e in mapless_controls(page, failed=False))
        page.locator('#regions .region-head[data-region="Asia"]').first.click()
        page.wait_for_timeout(MAP_WAIT_MS + 500)
        mark = page.evaluate("document.getElementById('worldmap').dataset.map")
        if not page.locator("#worldmap").inner_text().strip() or mark != "failed":
            errors.append(f"hydration: {MAP_WAIT_MS} ms without the map, Asia's map box reads {page.locator('#worldmap').inner_text()!r} (data-map {mark!r})")
        late.release()
        map_ready(page, 10_000)
        page.locator('#regions .region-head[data-region="Europe"]').first.click()
        page.locator('#worldmap svg.map-wide .tgt[data-j="FR"]').dispatch_event("click")
        if not page.locator("#worldmap svg.map-wide .hl-sel").count() or page.locator("#record-name .head-state").text_content() != "France":
            errors.append("hydration: a map arriving late is not drawn as the interactive map (no selection highlight for France)")
    except PlaywrightTimeout as exc:
        errors.append(f"hydration: a late map: {exc}")
    page.close()
    # A map drawn while the fonts still load places its names when they come: data-map says placing until then.
    page = browser.new_page(viewport={"width": 1440, "height": 1000})
    fonts = Held(page, "**/assets/fonts/**")
    page.goto(base_url, wait_until="commit")  # held fonts hold the load event
    try:
        wait_hydrated(page, 15_000)
        settle(page, "['placing', 'drawn'].includes(document.getElementById('worldmap').dataset.map)", timeout_ms=15_000)
        if (mark := page.evaluate("document.getElementById('worldmap').dataset.map")) != "placing":
            errors.append(f"hydration: a map drawn while the fonts load has data-map {mark!r}, not 'placing'")
        fonts.release()
        map_ready(page, 10_000)
    except PlaywrightTimeout as exc:
        errors.append(f"hydration: a map drawn before its fonts: {exc}")
    page.close()
    return errors


def check_mixed_release(browser, base_url: str) -> list[str]:
    """A mixed release (owner, 14 Nov 2026): for up to ten minutes after a deploy a browser can hold the old page and
    fetch the new data. A page whose data carries another build mark reloads itself exactly once, then renders what
    it got, also when the data's mark differs again on the reload; with matching marks, or without session storage
    (no guard against a loop), it never reloads. Loads are counted as main-frame document requests, which a reload
    makes even when it starts before the first document's load event."""
    from playwright.sync_api import TimeoutError as PlaywrightTimeout

    errors = []

    def documents_of(case: str, marks: list[str], *, storage: bool = True) -> int | None:
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        documents, served = [], []
        page.on("request", lambda r: documents.append(r.url) if r.is_navigation_request() and r.frame == page.main_frame else None)

        def other_build(route):  # each data request gets the next mark, the last one repeated
            response = route.fetch()
            data = response.json()
            data["build_mark"] = marks[min(len(served), len(marks) - 1)]
            served.append(data["build_mark"])
            route.fulfill(response=response, body=json.dumps(data, ensure_ascii=False, separators=(",", ":")))

        if not storage:
            page.add_init_script("Object.defineProperty(window, 'sessionStorage', { get() { throw new Error('blocked'); } });")
        if marks:
            page.route(PAGE_DATA, other_build)
        want = 2 if marks and storage else 1
        try:
            page.goto(base_url, wait_until="commit")
            end = time.monotonic() + 15
            while len(documents) < want and time.monotonic() < end:
                page.wait_for_timeout(100)
            page.wait_for_load_state("load")
            wait_hydrated(page, 15_000)
            page.wait_for_timeout(1500)  # a further reload would have started by now
            if not page.evaluate("document.querySelectorAll('#figures > .figure').length > 0"):
                errors.append(f"mixed release, {case}: the page did not render after {len(documents)} loads")
            if marks and storage and (flag := page.evaluate("sessionStorage.getItem('aisi-reloaded-for')")) != marks[0]:
                errors.append(f"mixed release, {case}: the session remembers {flag!r}, not the first mismatching mark {marks[0]!r}")
            if len(documents) != want:
                errors.append(f"mixed release, {case}: the page loaded {len(documents)} times, expected {want} (data marks served: {served})")
            return len(documents)
        except PlaywrightTimeout as exc:
            errors.append(f"mixed release, {case}: {exc} after {len(documents)} loads")
            return None
        finally:
            page.close()

    documents_of("same build", [])
    documents_of("another build", ["0.0.0-000000000a"])
    documents_of("another build, then a third", ["0.0.0-000000000a", "0.0.0-000000000b", "0.0.0-000000000a"])
    documents_of("another build without session storage", ["0.0.0-000000000a"], storage=False)
    return errors


def check_first_load(browser, base_url: str) -> list[str]:
    """A cold first visit at 390, 640, 641 and 1440 px: the visible saved map drawing is downloaded once, by the head
    script (whose download the page then uses), and nothing else a visit requests falls outside FIRST_LOAD, the
    inventory the byte budgets count."""
    from fnmatch import fnmatch

    errors = []
    for width in (390, 640, 641, 1440):
        context = browser.new_context(viewport={"width": width, "height": 1000})
        page, requested = context.new_page(), []
        page.on("request", lambda request: requested.append(request.url))
        # The drawn map replaces the saved drawing once it arrives: note each drawing as it is filled in.
        page.add_init_script("""window.__filled = []; new MutationObserver(records => records.forEach(r => { const s = r.target;
          if (s.getAttribute('data-saved') === 'done') window.__filled.push([s.getAttribute('data-src'), getComputedStyle(s).display !== 'none']); }))
          .observe(document, { subtree: true, attributes: true, attributeFilter: ['data-saved'] });""")
        # The world topology is held until the saved drawing is filled in: once it arrives the drawn map replaces the
        # shells, and a shell replaced before its file came in is never filled (a valid order, not the one tested here).
        topology = Held(page, "**/assets/world-*.json")
        page.goto(base_url, wait_until="load")
        try:
            page.wait_for_function("window.__filled.length > 0", timeout=15_000)
        except Exception:  # noqa: BLE001 - the Playwright timeout; the check below reports what is missing
            pass
        topology.release()
        page.wait_for_load_state("networkidle")
        state = page.evaluate("() => ({ filled: window.__filled, early: window.SAVED_MAP_EARLY ? [SAVED_MAP_EARLY.url, SAVED_MAP_EARLY.used] : null })")
        maps = [url[len(base_url):] for url in requested if "/assets/map-" in url]
        filled = [src for src, shown in state["filled"] if shown]
        if len(state["filled"]) != 1 or len(filled) != 1 or maps != filled or state["early"] != [filled[0], True]:
            errors.append(f"first load {width}px: the visible drawing should be requested once, by the head script, and filled in from that download; requested {maps}, head script {state['early']}, filled (src, visible) {state['filled']}")
        paths = {urlsplit(url).path.lstrip("/") or "index.html" for url in requested if url.startswith(base_url)}
        if outside := sorted(p for p in paths if not any(fnmatch(p, pattern) for pattern in FIRST_LOAD)):
            errors.append(f"first load {width}px: requests outside the budgeted FIRST_LOAD inventory: {outside}")
        context.close()
    return errors


VENDORED_FONTS = {"Inter": "assets/fonts/inter-", "Source Sans 3": "assets/fonts/source-sans-3-"}


def check_origins(browser, base_url: str, dist: Path) -> list[str]:
    """The tracker and its Method page request nothing from another origin (fonts, scripts and map are vendored
    under assets/), and both text faces load from the vendored font files."""
    errors = []
    for name in ("index.html", "method.html"):
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        foreign, fonts = [], []
        local = lambda url: url.startswith((base_url, "data:", "blob:"))  # noqa: E731
        page.on("request", lambda request: None if local(request.url) else foreign.append(request.url))
        page.on("response", lambda response: fonts.append(response.url[len(base_url):]) if response.ok and response.url.startswith(base_url + "assets/fonts/") else None)
        page.route(lambda url: not local(url), lambda route: route.abort())  # recorded above; never wait on the network
        page.goto(base_url + name, wait_until="load")
        if name == "index.html":
            wait_hydrated(page)
        faces = page.evaluate("document.fonts.ready.then(() => [...document.fonts].map(f => [f.family.replace(/[\"']/g, ''), f.style, f.weight, f.unicodeRange.slice(0, 24), f.status]))")
        loaded = [f[0] for f in faces if f[4] == "loaded"]
        for family, path in VENDORED_FONTS.items():
            if family not in loaded or not any(f.startswith(path) for f in fonts):
                errors.append(f"{name}: {family} does not load from the vendored files {path}*.woff2")
        errors.extend(f"{name}: the font face {f[:4]} failed to load" for f in faces if f[4] == "error")
        html_text = (dist / name).read_text(encoding="utf-8")
        errors.extend(f"{name}: @font-face names a missing file {url}" for url in sorted(set(re.findall(r"url\((assets/fonts/[^)]+)\)", html_text))) if not (dist / url).is_file())
        errors.extend(f"{name}: request to another origin: {url}" for url in sorted(set(foreign)))
        page.close()
    return errors


# Structural card field identities, independent of reader-facing labels.
CARD_TRACKS = {"Institute": "2", "Status": "3", "Form": "4", "Head": "5", "Reports to": "6", "Partners": "7", "Mandate": "8", "Budget": "9", "Staff": "10", "Known activity": "11", "Related funding": "12", "Facts": "13"}
CITED_COLUMNS = ("inst", "since", "op", "budget", "staff", "net")  # Region and status: reference data and derived status
NOTHING_FOUND = "None found yet"


def uncited_values(soup) -> list[str]:
    """Every displayed value that a citation could support carries a citation mark: card rows, related funding
    and the worldwide table's value columns. Absence ("Not published", "None found yet") and an institute name
    linking to its own website (the link is the evidence) need none."""
    found = []

    def bare(node) -> bool:
        text = " ".join(node.get_text(" ").split())
        return bool(text) and text not in ("\u2013", NOTHING_FOUND) and not node.select_one(".cites a.cn, .missing, .nonmember")

    for row in soup.select(".card .row"):
        label, value = row.select_one(".k"), row.select_one(".v")
        card = row.find_parent(class_="card")
        if not label or not value or (row.get("data-track") == CARD_TRACKS["Institute"] and value.select_one("a[href^='http']")):
            continue
        name = label.get_text(strip=True)
        # A Known activity value lists each kind found with its own citations.
        parts = value.select(".activity-kind") or [value]
        for part in parts:
            if bare(part):
                found.append(f"card {card.get('data-j')}: {name}")
    for item in soup.select(".related-funding-item"):
        if bare(item):
            found.append(f"related funding {item.get('data-funding-id')}")
    for row in soup.select(".fund-item, .fund-detail"):
        for value in row.select(":scope > .fund-measure .fund-value, .fund-staff-summary .fund-value, .fund-budget-summary .fund-value"):
            if bare(value):
                found.append(f"funding {row.get('data-institute')}: uncited Budget or Staff figure")
    for tr in soup.select("#world-rows > tr, #world-rest > tr"):
        jid = tr.get("data-j") or ""
        for td in tr.select("td[data-col]"):
            column = td["data-col"]
            if column not in CITED_COLUMNS or (column == "inst" and td.select_one("a[href^='http']")):
                continue
            for part in td.select(".activity-kind") or [td]:
                if bare(part):
                    found.append(f"table {jid}: {column}")
    return found


def check_hydrated(html: str) -> list[str]:
    """After the script has taken over: no saved page left behind and no element twice."""
    soup = BeautifulSoup(html, "html.parser")
    errors = []
    for selector in ("template#page-template", "#world-rest", "html[data-prerendered]"):
        if soup.select_one(selector):
            errors.append(f"hydrated page still has {selector}")
    ids = Counter(str(node["id"]) for node in soup.select("[id]"))
    duplicated = sorted(i for i, n in ids.items() if n > 1)
    if duplicated:
        errors.append(f"hydrated page repeats ids: {duplicated[:10]}")
    return errors


def check_agent_files(dist: Path) -> list[str]:
    """The built page's head links, its Using the data block and llms.txt, before any script runs."""
    errors = []
    soup = BeautifulSoup((dist / "index.html").read_text(encoding="utf-8"), "html.parser")
    alternates = [str(a.get("href") or "") for a in soup.select('head link[rel="alternate"][type="application/json"]')]
    indexes = [h for h in alternates if re.fullmatch(r"api/v\d+/index\.json", h)]
    if len(indexes) != 1 or "data/datapackage.json" not in alternates:
        errors.append(f"head: expected JSON alternate links to one api/v<major>/index.json and data/datapackage.json, got {alternates}")
    for href in alternates:
        if not (dist / href).is_file():
            errors.append(f"head: alternate link {href!r} has no file in the build")
    errors += check_json_ld(soup, dist) + check_crawl_files(dist) + check_feed(soup, dist) + check_page_size(dist)
    canonical = soup.select_one('head link[rel="canonical"]')
    if canonical:
        site = str(canonical.get("href"))
        errors += check_share_tags(soup, "index.html", soup.title.get_text(strip=True) if soup.title else "", site)
        if (dist / "method.html").is_file():
            method = BeautifulSoup((dist / "method.html").read_text(encoding="utf-8"), "html.parser")
            errors += check_share_tags(method, "method.html", method.title.get_text(strip=True) if method.title else "", site + "method.html")
    if len(indexes) != 1 or not (dist / indexes[0]).is_file():
        return errors
    orientation = json.loads((dist / indexes[0]).read_text(encoding="utf-8")).get("orientation") or []
    block = soup.select_one("#using-data")
    api = f"api/v{json.loads((dist / 'data/aisi.json').read_text(encoding='utf-8'))['dataset']['version'].split('.')[0]}/index.json"
    rows = block.select(".dlist > li > a") if block else []
    if (not block or sorted(a.get("href") for a in rows) != sorted([api, "llms.txt", "feed.xml"]) or not block.find_parent("section", attrs={"aria-labelledby": "data"})
            or any(not a.select_one(".dl-title") or not a.select_one(".d") or not a.select_one(".d").get_text(strip=True) for a in rows)):
        errors.append(f"Data section: '{USING_THE_DATA}' is not a group of rows in the Data section for {api}, llms.txt and the feed, each with a description")
    elif not orientation:
        errors.append(f"{indexes[0]} carries no orientation text")
    llms = dist / "llms.txt"
    if not llms.is_file():
        return errors + ["llms.txt missing from the build"]
    text = llms.read_text(encoding="utf-8")
    missing = [o["heading"] for o in orientation if " ".join(o["text"].split()) not in " ".join(text.split())]
    if missing:
        errors.append(f"llms.txt lacks orientation sections: {missing}")
    # Absolute links under one site address (the page link is the shortest), each to a built file.
    links = re.findall(r"\]\((https://[^)]*)\)", text)
    root = min(links, key=len) if links else ""
    if not root.endswith("/"):
        errors.append(f"llms.txt: expected absolute https links including the page address, got {links}")
    for link in links:
        if not link.startswith(root) or not (dist / (link[len(root):] or "index.html")).is_file():
            errors.append(f"llms.txt: link {link!r} is not a built file under {root!r}")
    return errors


FEED_ID = "tag:aisi.accelerate-europe.org,2026:feed"
FEED_ENTRY_ID = "tag:aisi.accelerate-europe.org,2026:release/{version}"


# The median tile's tooltip when the median is a ceiling (site/index.html MEDIAN_CEILING).
MEDIAN_CEILING = "Some of these budgets are ceilings, so the median is a ceiling too and is rounded up."


def check_feed(soup, dist: Path) -> list[str]:
    """One Atom alternate link in the head to feed.xml and one link from News, a valid Atom feed with one entry per
    changelog version and one per News item (the page's developments, owner decision 4 Nov 2026), each with a unique
    permanent id, newest first; releases point at the version history, News items at their state's card."""
    errors = []
    atoms = [str(a.get("href") or "") for a in soup.select('head link[rel="alternate"][type="application/atom+xml"]')]
    if atoms != ["feed.xml"]:
        return [f"head: expected one Atom alternate link to feed.xml, got {atoms}"]
    link = soup.select_one("#developments .news-links a#news-feed")  # the saved page; its template copy repeats it
    if not link or link.get("href") != "feed.xml":
        errors.append("News: no Atom feed link after its list")
    try:
        root = ElementTree.parse(dist / "feed.xml").getroot()
    except (OSError, ElementTree.ParseError) as exc:
        return [f"feed.xml missing or invalid: {exc}"]
    ns = "{http://www.w3.org/2005/Atom}"
    if root.tag != ns + "feed" or not all(root.findtext(ns + t) for t in ("id", "title", "updated")) or root.find(f"{ns}author/{ns}name") is None:
        errors.append("feed.xml: not an Atom feed with id, title, updated and author")
    published = json.loads((dist / "data/aisi.json").read_text(encoding="utf-8"))
    changelog = published["changelog"]
    if root.findtext(ns + "id") != FEED_ID:
        errors.append(f"feed.xml: feed id {root.findtext(ns + 'id')!r}, expected {FEED_ID!r}")
    canonical = soup.select_one('head link[rel="canonical"]')
    base = str(canonical.get("href")) if canonical else ""
    day = lambda d: {10: d, 7: d + "-01", 4: d + "-01-01"}[len(d)] + "T00:00:00Z"  # noqa: E731
    # Independent oracle: id, title, updated and link per entry, from the changelog and the page's News items.
    want = {FEED_ENTRY_ID.format(version=c["version"]): (f"Version {c['version']}", day(c["date"]), [base + "#changes"]) for c in changelog}
    sources = {x["id"]: x for x in published["sources"]}
    kinds, bodies = {i: "release" for i in want}, {}
    news = published["dataset"]["display"]["developments"]
    for g in news:
        text = " ".join(t for t in g["texts"] if t)
        key = g["date"] if len(g["date"]) == 10 else f"{g['date']}/{g['order']}"  # the page's grouping key
        nid = f"tag:aisi.accelerate-europe.org,2026:news/{g['jurisdiction']}/{key}"
        want[nid] = (f"{g['name']}: {text}", day(g["date"]), [base + "#c-" + g["jurisdiction"].lower()])
        kinds[nid] = "news"
        bodies[nid] = (text, [(sources[i]["url"], sources[i]["title"]) for i in g["citations"]["display"] if sources.get(i, {}).get("url")])
        if not soup.select_one(f"#c-{g['jurisdiction'].lower()}"):
            errors.append(f"feed.xml: News item for {g['jurisdiction']} links to a card the page lacks")
    entries = root.findall(ns + "entry")
    ids = [e.findtext(ns + "id") for e in entries]
    if len(set(ids)) != len(ids) or len({c["version"] for c in changelog}) != len(changelog):
        errors.append(f"feed.xml: entry ids or changelog versions repeat: {sorted(i for i in set(ids) if ids.count(i) > 1)}")
    if set(ids) != set(want):
        errors.append(f"feed.xml: entries differ from the releases and News items: extra {sorted(set(ids) - set(want))}, missing {sorted(set(want) - set(ids))}")
    for e in entries:
        got = (e.findtext(ns + "title"), e.findtext(ns + "updated"), [a.get("href", "") for a in e.findall(ns + "link")])
        if e.findtext(ns + "id") in want and got != want[e.findtext(ns + "id")]:
            errors.append(f"feed.xml: {e.findtext(ns + 'id')} has title, updated and links {got}, expected {want[e.findtext(ns + 'id')]}")
        if e.findtext(ns + "id") in bodies:  # a News item: its text as summary, its sources as links in the decoded HTML content
            text, links = bodies[e.findtext(ns + "id")]
            content = BeautifulSoup(e.findtext(ns + "content") or "", "html.parser")
            got_links = [(a.get("href"), a.get_text()) for a in content.select("a")]
            if e.findtext(ns + "summary") != text or content.select_one("p") is None or content.select_one("p").get_text() != text or got_links != links:
                errors.append(f"feed.xml: {e.findtext(ns + 'id')} summary, text or source links differ from its News item")
    stamps = [e.findtext(ns + "updated") or "" for e in entries]
    if stamps != sorted(stamps, reverse=True):
        errors.append("feed.xml: entries are not newest first")
    for a, b in zip(entries, entries[1:]):  # on one date a News item comes before a release
        if a.findtext(ns + "updated") == b.findtext(ns + "updated") and kinds.get(a.findtext(ns + "id")) == "release" and kinds.get(b.findtext(ns + "id")) == "news":
            errors.append("feed.xml: a release precedes a News item of the same date")
    if root.findtext(ns + "updated") != max(stamps, default=""):
        errors.append(f"feed.xml: feed updated {root.findtext(ns + 'updated')!r} is not its newest entry")
    return errors


def check_crawl_files(dist: Path) -> list[str]:
    """The sitemap lists the HTML pages except the noindex error page, each once; robots allows crawlers."""
    errors = []
    try:
        root = ElementTree.parse(dist / "sitemap.xml").getroot()
    except (OSError, ElementTree.ParseError) as exc:
        return [f"sitemap.xml missing or invalid: {exc}"]
    ns = "{http://www.sitemaps.org/schemas/sitemap/0.9}"
    locs = [loc.text or "" for loc in root.iter(f"{ns}loc")]
    site = min(locs, key=len) if locs else ""
    pages = {"" if p == dist / "index.html" else p.relative_to(dist).as_posix().removesuffix("index.html") for p in dist.rglob("*.html")} - {"404.html"}
    listed = [loc[len(site):] for loc in locs if loc.startswith(site)]
    if not site.endswith("/") or len(listed) != len(locs) or sorted(listed) != sorted(pages):
        errors.append(f"sitemap.xml: expected the HTML pages {sorted(pages)} except 404.html once under one address, got {sorted(locs)}")
    try:
        robots = (dist / "robots.txt").read_text(encoding="utf-8")
    except OSError:
        return errors + ["robots.txt missing"]
    groups = {}
    for block in robots.split("\n\n"):
        lines = [line.split(":", 1) for line in block.splitlines() if ":" in line]
        agents = [v.strip() for k, v in lines if k.strip().lower() == "user-agent"]
        rules = [(k.strip().lower(), v.strip()) for k, v in lines if k.strip().lower() in ("allow", "disallow")]
        for agent in agents:  # repeated groups for one agent merge (RFC 9309)
            groups.setdefault(agent, []).extend(rules)
    for agent in (*AI_CRAWLERS, "*"):
        if groups.get(agent) != [("allow", "/")]:
            errors.append(f"robots.txt: {agent} is not allowed everything ({groups.get(agent)})")
    if f"Sitemap: {site}sitemap.xml" not in robots.splitlines():
        errors.append(f"robots.txt: no Sitemap line for {site}sitemap.xml")
    return errors


def check_json_ld(soup, dist: Path) -> list[str]:
    """One schema.org Dataset in the head that names the published dataset and every data file."""
    blocks = soup.select('head script[type="application/ld+json"]')
    if len(blocks) != 1:
        return [f"head: expected one JSON-LD block, got {len(blocks)}"]
    try:
        ld = json.loads(blocks[0].string or "")
    except ValueError as exc:
        return [f"JSON-LD does not parse: {exc}"]
    dataset = json.loads((dist / "data/aisi.json").read_text(encoding="utf-8"))["dataset"]
    package = json.loads((dist / "data/datapackage.json").read_text(encoding="utf-8"))
    errors = []
    expected = {"@type": "Dataset", "name": dataset["title"], "version": dataset["version"],
                "license": "https://creativecommons.org/licenses/by/4.0/" if dataset["license"] == "CC BY 4.0" else None}
    for key, value in expected.items():
        if ld.get(key) != value:
            errors.append(f"JSON-LD {key}: expected {value!r}, got {ld.get(key)!r}")
    if dataset["publisher"] not in {c.get("name") for c in ld.get("creator", [])}:
        errors.append("JSON-LD creator does not name the publisher")
    doi = f"https://doi.org/{dataset['doi']}" if dataset.get("doi") else None
    if ld.get("identifier") != doi or package.get("id") != doi:
        errors.append(f"JSON-LD identifier and datapackage id: expected {doi!r}, got {ld.get('identifier')!r} and {package.get('id')!r}")
    urls = [d.get("contentUrl", "") for d in ld.get("distribution", []) if d.get("@type") == "DataDownload"]
    site = str(ld.get("url", ""))
    wanted = sorted(f"data/{r['path']}" for r in package["resources"])
    if sorted(u[len(site):] for u in urls if site and u.startswith(site)) != wanted or len(urls) != len(wanted):
        errors.append(f"JSON-LD distribution: expected one DataDownload under {site!r} per data file {wanted}, got {urls}")
    return errors


PAGE_ONLY_DATA = "page.json"  # the page's own data file (build.page_data), not a download
PAGE_DATA = "**/data/page.json*"  # the page's data file, fetched with a version query (build.page_data)


class Held:
    """Requests for `pattern` held on `page` until release(): a check acts while they are pending, never after a
    pause that is only hoped to be long enough. wait() and release() first wait (bounded) until one was actually
    held, so a check whose hold missed its request fails instead of passing on the wrong state."""

    def __init__(self, page, pattern: str):
        self.page, self.pattern, self.routes = page, pattern, []
        page.route(pattern, self._hold)

    def _hold(self, route) -> None:
        self.routes.append(route)

    def wait(self, timeout_ms: int = 30_000) -> None:
        deadline = time.monotonic() + timeout_ms / 1000
        while not self.routes:
            if time.monotonic() > deadline:
                raise AssertionError(f"no request for {self.pattern} was held within {timeout_ms} ms")
            self.page.wait_for_timeout(50)

    def release(self, abort: bool = False) -> None:
        """Let the held requests go on (or fail them), then stop holding new ones (in this order: unrouting first
        settles the held routes itself, and continuing them then fails with "Route is already handled")."""
        self.wait()
        for route in self.routes:
            route.abort() if abort else route.continue_()
        self.page.unroute(self.pattern, self._hold)
THEME_KEY = "aisi-theme"  # the page's stored theme choice (principle: Theme)
GROUNDS = {"light": "rgb(250, 247, 242)", "dark": "rgb(23, 25, 26)"}  # --ground per theme, the evidence that a theme applied


def themed_page(browser, theme: str, **options):
    """A page that shows `theme` whichever mechanism decides it: the stored choice (this page) and the system
    scheme (a base build that still follows the system, so contact sheets compare like for like).

    Every page opened per theme goes through here (tests/test_check_page.py enforces it); hydrated() then
    confirms the theme took effect, so a capture labelled dark can never silently be light.
    """
    page = browser.new_page(color_scheme=theme, **options)
    page.add_init_script(f"try {{ localStorage.setItem({json.dumps(THEME_KEY)}, {json.dumps(theme)}); }} catch (e) {{}}")
    page.aisi_theme = theme
    return page


def set_theme(page, theme: str) -> None:
    """Switch an open page to `theme` the way the toggle does (and the system scheme with it)."""
    page.emulate_media(color_scheme=theme)
    page.evaluate("t => t === 'dark' ? document.documentElement.setAttribute('data-theme', 'dark') : document.documentElement.removeAttribute('data-theme')", theme)


HYDRATION_MS = 30_000  # the page script's takeover; past it under load, the host is contended (do not raise it)
MAP_MS = 15_000


def playwright_timeout(exc: BaseException):
    """Playwright's TimeoutError class when `exc` is one, else None (also without Playwright: the unit tests)."""
    try:
        from playwright.sync_api import TimeoutError as PlaywrightTimeout
    except ImportError:
        return None
    return PlaywrightTimeout if isinstance(exc, PlaywrightTimeout) else None


def wait_hydrated(page, timeout_ms: int = HYDRATION_MS) -> None:
    """Wait until the page script has taken over the saved page: it removes data-prerendered once every listener is
    bound, so a check never acts on the saved page's controls. The one hydration wait of the gate."""
    try:
        page.wait_for_function("!document.documentElement.hasAttribute('data-prerendered')", timeout=timeout_ms)
    except Exception as exc:
        if (PlaywrightTimeout := playwright_timeout(exc)) is None:
            raise
        raise PlaywrightTimeout(f"the page script did not take over within {timeout_ms // 1000} s (data-prerendered still set)") from exc


def map_ready(page, timeout_ms: int = MAP_MS) -> None:
    """Wait until the map is drawn with its names placed (#worldmap[data-map=drawn], set in drawMap): a check that
    uses map dots must not meet the saved drawing, whose dots are no controls, or a map still loading. Checks of
    the late or missing map do not call it."""
    try:
        page.wait_for_function("document.getElementById('worldmap')?.dataset.map === 'drawn'", timeout=timeout_ms)
    except Exception as exc:
        if (PlaywrightTimeout := playwright_timeout(exc)) is None:
            raise
        state = page.evaluate("document.getElementById('worldmap')?.dataset.map ?? null")
        raise PlaywrightTimeout(f"the map was not drawn within {timeout_ms // 1000} s (data-map: {state})") from exc


def layout_settled(page, timeout_ms: int = 10_000) -> None:
    """Wait until the layout has settled: force a layout first, so fonts that newly shown text needs are requested,
    then the fonts, then two frames (bounded, so a page that never settles fails instead of holding the browser)."""
    settled = page.evaluate("""ms => Promise.race([
      new Promise(done => { document.body.getBoundingClientRect(); document.fonts.ready.then(() => requestAnimationFrame(() => requestAnimationFrame(() => done(true)))); }),
      new Promise(done => setTimeout(() => done(false), ms))])""", timeout_ms)
    if not settled:
        raise AssertionError(f"the layout did not settle within {timeout_ms // 1000} s (fonts or frames pending)")


def settle(page, state: str, arg=None, timeout_ms: int = 5_000) -> None:
    """Wait (bounded) for the state a step should reach. Only before an assertion of that same state: a timeout falls
    through, and the assertion after it reports what went wrong."""
    from playwright.sync_api import TimeoutError as PlaywrightTimeout

    try:
        page.wait_for_function(state, arg=arg, timeout=timeout_ms)
    except PlaywrightTimeout:
        pass


FN_VIEW_BY_WIDTH_JS = """w => { const box = document.getElementById('functions-table'), view = w <= 640 ? 'grouped' : 'grid';
  if (box && box.fnView && box.dataset.view !== view) box.fnView(view); }  // the section's own setter: no click, focus untouched"""


def fn_view_by_width(page) -> None:
    """Most Functions checks test the full grid at desktop widths and the grouped view at phone widths. Since 18 Nov 2026 the
    grouped view is the default at every width and the grid one click away, so this
    sets the section's view through its own setter on every resize to keep that pairing for them, without touching focus."""
    if getattr(page, "_fn_view_by_width", False) or not hasattr(page, "set_viewport_size"):  # a test's stand-in page has no viewport
        return
    resize = page.set_viewport_size

    def sized(size):
        resize(size)
        page.evaluate(FN_VIEW_BY_WIDTH_JS, size["width"])
    page.set_viewport_size = sized
    page._fn_view_by_width = True
    page.evaluate(FN_VIEW_BY_WIDTH_JS, page.viewport_size["width"])


def hydrated(page, url: str, map_drawn: bool = False, fn_view: str = "by-width") -> None:
    """Open the page and wait until its script has taken over the pre-rendered page, and with `map_drawn` also until
    the map is drawn and the layout has settled. A page from themed_page must show its theme's ground. `fn_view`
    "by-width" pairs the Functions view with the width (fn_view_by_width); "default" leaves the page's own default."""
    page.goto(url)
    wait_hydrated(page)
    if fn_view == "by-width":
        fn_view_by_width(page)
    if map_drawn:
        map_ready(page)
        layout_settled(page)
    theme = getattr(page, "aisi_theme", None)
    if theme:
        ground = page.evaluate("getComputedStyle(document.body).backgroundColor")
        if ground != GROUNDS[theme]:
            raise AssertionError(f"a page opened in the {theme} theme shows ground {ground}, not {GROUNDS[theme]}")


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, format, *args):
        pass


NOT_APPLICABLE = "Not applicable"  # dataset.method/leadership: no institute exists.
OPERATING_CAVEAT = "not evidence of inactivity"
ACTIVITY_SINCE_DEFINITION = "earliest known item of that kind"  # the Known activity tooltip defines "since"


def structural_mark_ok(mark) -> bool:
    """A structural absence is a muted dash hidden from screen readers plus 'Not applicable' for them, nothing else."""
    dash = mark.select_one('[aria-hidden="true"]')
    hidden = mark.select_one(".vh")
    return bool(
        dash
        and dash.get_text() == "\u2013"
        and hidden
        and hidden.get_text() == NOT_APPLICABLE
        and mark.get_text() == "\u2013" + NOT_APPLICABLE
    )


def is_structural_absence(cell) -> bool:
    """The table cell holds only a structural-absence mark (citations removed or absent)."""
    if cell is None:
        return False
    marks = cell.select(".missing.na")
    return (
        len(marks) == 1
        and structural_mark_ok(marks[0])
        and " ".join(cell.get_text().split()) == "\u2013" + NOT_APPLICABLE
    )


def in_numeral_order(ids, order):
    """Citation groups list their sources in ascending numeral (source-list) order.

    IDs missing from the order sort last; the numbering check reports them.
    """
    return sorted(ids, key=lambda sid: order.index(sid) if sid in order else len(order))


def card_source_orders(soup, data) -> dict:
    """Each record's source numbering as the page shows it: its card's source list.

    The page numbers a record's sources in the record's own citation order (site/index.html SOURCE_ORDERS), not the
    dataset's catalogue order; every citation group of that record (card rows, developments, Known activity) reads its
    numerals in that order. Records without a card follow the page's own fallback: the jurisdiction's source links,
    then its institute's, each in catalogue order, first occurrence kept.
    """
    catalogue = {source["id"]: index for index, source in enumerate(data.get("sources", []))}
    orders = {
        str(card["data-j"]): [li.get("data-source-id") for li in card.select(".srcs li")]
        for card in soup.select(".card[data-j]")
        if card.select(".srcs li")
    }
    institutes = {i.get("id"): i for i in data.get("institutes", [])}

    def links(record):
        # The page's recordSources(record): the record's own catalogue links, in catalogue order.
        if not record:
            return []
        shown = (record.get("display") or {}).get("source_links")
        if shown is not None:
            return list(shown)
        return sorted({sid for sid in entry_sources(record) if sid in catalogue}, key=catalogue.__getitem__)

    for jurisdiction in data.get("jurisdictions", []):
        if jurisdiction["id"] not in orders:
            # As the page: the jurisdiction's links, then its institute's, first occurrence kept.
            cited = links(jurisdiction) + links(institutes.get(jurisdiction.get("institute_id")))
            orders[jurisdiction["id"]] = [sid for sid in dict.fromkeys(cited) if sid in catalogue]
    return orders


def check_citation_ends(soup) -> list[str]:
    """Every citation group ends its value; partner names carry their own citations in the fold."""
    errors: list[str] = []
    for group in soup.select(".cites"):
        host = group.find_parent(
            lambda n: (
                n.name in ("td", "p")  # a funding record's parts are separate paragraphs
                or ("pname" in n.get("class", []) and n.find_parent(class_="partner-how"))
                or (n.name == "li" and (n.find_parent(class_="partner-how") or n.find_parent(class_="member-list")))
                or "member-names" in n.get("class", [])
                or n.has_attr("data-record")
                or bool(set(n.get("class", [])) & {"v", "fund-figure", "fund-value", "fund-context", "activity-clause", "activity-since", "claim-text"})  # compact figures and claim sentences have their own boundary, before qualifiers or Quote
            )
        )
        if host:
            # A collapsed fold-out after the value (the partners' "How they work together") is not part of the value's text.
            text = [node for node in host.find_all(string=True) if not any(parent is not host and parent in node.parents for parent in host.select('.members, .partner-how, .member-details'))]
            citation_text = {id(n) for n in group.find_all(string=True)}
            last = max(
                (index for index, node in enumerate(text) if id(node) in citation_text),
                default=-1,
            )
            if last >= 0 and any(node.strip() for node in text[last + 1 :]):
                errors.append("citation group is not at the end of its value")
    return errors


def zero_match_errors(check: str, selector: str, count: int, expected: bool = True) -> list[str]:
    """An expected selection must exercise its check, even when the DOM disappears."""
    return [f"{check}: {selector} matched 0"] if expected and count == 0 else []


def full_card_expected(jurisdiction: dict) -> bool:
    """The page gives supranational and positive-status records a full card."""
    return jurisdiction.get("kind") == "supranational" or jurisdiction["status"] != "none_identified"


def check_source_alignment(html: str, data: dict) -> list[str]:
    """Cross-check rendered references against source records and card numbering."""
    soup = BeautifulSoup(html, "html.parser")
    sources = {source["id"]: source for source in data["sources"]}
    errors = []
    if soup.select(".orig, .title-en"):
        errors.append("translations: original names and translated titles must not have a second visible line or bracketed span")
    orders = card_source_orders(soup, data)
    jurisdictions = {j["id"]: j for j in data["jurisdictions"]}
    institutes = {i["jurisdiction_id"]: i for i in data["institutes"]}

    def required(selector, expected=True, root=soup, key=""):
        nodes = root.select(selector)
        errors.extend(zero_match_errors("source_alignment", f"{key} {selector}".strip(), len(nodes), expected))
        return nodes

    cited_cards = False
    for j in data["jurisdictions"]:
        full = full_card_expected(j)
        if not full and not j.get("europe_scope"):
            continue  # none-identified records outside Europe have no card
        selector = f'.card{":not(.mini)" if full else ".mini"}[data-j="{j["id"]}"]'
        cards = required(selector)
        wanted = set(entry_sources(j) + entry_sources(institutes.get(j["id"]))) & sources.keys()
        cited_cards |= bool(wanted)
        for card in cards:
            required(".srcs li", bool(wanted), card, j["id"])

    world_rows = Keyed(soup, "#world-rows tr[data-j]")
    for card in soup.select(".card[data-j]"):
        jid = str(card["data-j"])
        ids = [li.get("data-source-id") for li in card.select(".srcs li")]
        if len(ids) != len(set(ids)) or None in ids:
            errors.append(f"{jid}: source list has missing or duplicate IDs")
        if "mini" in card.get("class", []):
            continue
        table = world_rows.one(jid)
        if table is None and jurisdictions.get(jid, {}).get("kind") != "supranational":
            errors.extend(zero_match_errors("source_alignment", f'#world-rows tr[data-j="{jid}"]', 0))
        rows = required(".row", root=card, key=jid)
        if table:
            columns = {
                "Institute": "inst",
                "Status": "since",
                "Budget": "budget",
                "Staff": "staff",
                "Known activity": "op",
            }
            compared = 0
            for row in rows:
                column = next((column for label, column in columns.items() if row.get("data-track") == CARD_TRACKS[label]), None)
                if column is None:
                    continue
                compared += 1
                cell = table.select_one(f'[data-col="{column}"]')
                errors.extend(zero_match_errors("source_alignment", f'{jid} #world-rows [data-col="{column}"]', int(cell is not None)))
                # The card-only activity sentence has its own evidence, checked by the activity-line content family.
                card_ids = [a.get("data-source-id") for a in row.select(".cn") if not a.find_parent(class_="activity-line")]
                if column == "since" and row.select_one(".qualifiers"):
                    # Only the dated announcement's extra sources may differ (dataset.method/intervals).
                    jurisdiction = next((j for j in data["jurisdictions"] if j["id"] == jid), None)
                    institute = next((i for i in data["institutes"] if i["jurisdiction_id"] == jid), None)
                    counted = expected_intervals(jurisdiction, institute, data.get("dataset", {}).get("as_of"))[2] if jurisdiction else []
                    extras = {sid for n in counted for sid in jurisdiction["timeline"][n].get("citations", {}).get("date", [])}
                    table_ids = {a.get("data-source-id") for a in cell.select(".cn")} if cell else set()
                    card_ids = [sid for sid in card_ids if sid not in extras or sid in table_ids]
                if cell and card_ids != [
                    a.get("data-source-id") for a in cell.select(".cn")
                ]:
                    errors.append(
                        f"{jid}: {column} citations differ between card and table"
                    )
            errors.extend(zero_match_errors("source_alignment", f"{jid} .row[data-track] for card/table citations", compared))

    # Records without a card: documented display order, source-catalogue order (card_source_orders).
    for li in required("li[data-source-id]", cited_cards):
        sid = str(li["data-source-id"])
        link = li.select_one("a[href]")
        if sid not in sources or not link or link["href"] != source_href(sources[sid]):
            errors.append(f"source list {sid}: URL does not match data")
            continue
        src = sources[sid]
        shown = "".join(t for t in link.find_all(string=True) if not t.find_parent(class_="vh"))
        if (title := src.get("title_en") or src.get("title")) and shown != title:
            errors.append(f"source list {sid}: title {shown!r}, expected {title!r}")
        if src.get("title_en") and not original_tip_matches(link, src["title_en"], src["title"]):
            errors.append(f"source list {sid}: the English title must carry the original in its tooltip")
        language = None if src.get("title_en") or src.get("language") == "en" else src.get("language") or None  # the page is lang="en"
        if link.get("lang") != language:
            errors.append(f"source list {sid}: title language {link.get('lang')!r}, expected {language!r}")
    # Source catalogues may contain background sources only. Dated status and mini-card findings,
    # however, render their field citations in the page (including the default positive-only table).
    cited_fields = any(
        citation_fields(j, ["since"]) if full_card_expected(j) and j.get("status_since")
        else citation_fields(j, ["finding"]) if j.get("europe_scope") and not full_card_expected(j) else []
        for j in data["jurisdictions"]
    )
    citations = required(".cn", cited_fields)
    required(".cites", bool(citations))  # check_citation_ends otherwise loops over nothing
    for cite in citations:
        host = cite.find_parent(attrs={"data-j": True})
        jid = str(host.get("data-j") or "") if host else ""
        sid = str(cite.get("data-source-id") or "")
        order = orders.get(jid, [])
        scope = cite.find_parent(attrs={"data-source-scope": True})
        if scope is not None:  # a section with its own source list (Testing and access): numbered from that list
            jid, order = str(scope["data-source-scope"]), [li.get("data-source-id") for li in scope.select(".srcs li")]
        fund = cite.find_parent(class_="fund-detail") or cite.find_parent(class_="fund-item")
        if fund is not None:  # Both charts use their institute's one records source list.
            detail = soup.select_one(f'.fund-detail[data-institute="{fund.get("data-institute")}"]')
            order = [li.get("data-source-id") for li in detail.select(".srcs li[data-source-id]")] if detail else []
        if sid not in order or cite.get_text(strip=True) != str(order.index(sid) + 1):
            errors.append(f"{jid}: citation {sid} differs from card source numbering")
        if sid not in sources or cite.get("href") != source_href(sources[sid]):
            errors.append(f"{jid}: citation {sid} URL does not match data")
    fund_items = Keyed(soup, ".fund-detail[data-institute]", "data-institute")
    for institute in established_funding_institutes(data):
        jid = institute["jurisdiction_id"]
        host = fund_items.one(institute["id"])
        if not host:
            errors.append(f"{jid}: missing funding entry")
            continue
        # Its own list: exactly the sources the funding and staff shown cite, each once (principle: Folds, part 1).
        ids = [li.get("data-source-id") for li in host.select(".srcs li[data-source-id]")]
        cited = {a.get("data-source-id") for a in host.select(".cn")}
        cited.update(a.get("data-source-id") for row in soup.select(f'.fund-item[data-institute="{institute["id"]}"]') for a in row.select(":scope > .fund-measure .cn"))
        if len(ids) != len(set(ids)) or set(ids) != cited:
            errors.append(f"{jid}: funding source list is not exactly the sources its funding and staff cite")
        fund_order = ids
        for kind in ("funding", "staff"):
            records = host.select(f'[data-record="{kind}"]')
            expected = institute.get(kind, [])
            if Counter(r.get("data-record-id") for r in records) != Counter(r["id"] for r in expected):
                errors.append(f"{jid}: missing {kind} records")
            by_id = {record["id"]: record for record in expected}
            for rendered in records:
                record = by_id.get(rendered.get("data-record-id"))
                if record is None:
                    errors.append(f"{jid}: {kind} record has unknown identity")
                    continue
                if kind == "funding" and (record.get("display") or {}).get("generated_description") and rendered.select_one(".source-statement"):
                    errors.append(f"{jid}: a generated undisclosed-funding line is shown as a source statement")
                if kind == "funding" and record.get("coverage") == "component":
                    label = rendered.select_one(".fund-component")
                    # Inline glossary/citation spans add no spaces to the text the reader sees.
                    text = " ".join(label.get_text().split()) if label else ""
                    description = " ".join((expected_funding_statement(record) or "").split())
                    if "Component" not in text or (description and description not in text):
                        errors.append(f"{jid}: component funding needs its visible identity beside its amount")
                if kind == "funding" and record.get("amount") is not None:
                    for selector, fields in (
                        (".funding-native", ["amount", "currency", "amount_qualifier", "period", "commitment", "kind", "coverage"]),
                        (".source-statement", ["description"]),
                        (".funding-metadata", ["as_of", "confidence", "notes"]),
                    ):
                        part = rendered.select_one(selector)
                        if part is None and selector == ".source-statement":
                            continue
                        actual = [a.get("data-source-id") for a in part.select(".cn")] if part else None
                        wanted = citation_fields(record, fields) + (rate_sources(record, data) if selector == ".funding-native" else [])
                        if actual != in_numeral_order(list(dict.fromkeys(wanted)), fund_order):
                            errors.append(f"{jid}: funding {selector} citations differ from data")
                    statement = rendered.select_one(".source-statement")
                    expected_statement = expected_funding_statement(record)
                    needs_statement = bool(expected_statement) and expected_statement != expected_native_funding_value(record)
                    if bool(statement) != needs_statement:
                        errors.append(f"{jid}: funding source statement missing or redundant")
                    elif statement:
                        text = copy.deepcopy(statement.select_one("p"))
                        if text:
                            for cite in text.select(".cites, .cn"):
                                cite.decompose()
                        if text is None or " ".join(text.get_text().split()) != " ".join(expected_statement.split()):
                            errors.append(f"{jid}: funding source statement loses source wording")
                    amount = rendered.select_one(".funding-native .budget-amount")
                    if amount is None or amount.get_text(strip=True) != expected_shown_funding_value(record, data):
                        errors.append(f"{jid}: native funding amount differs from primary value")
                    continue
                actual = [a.get("data-source-id") for a in rendered.select(".cn")]
                wanted = in_numeral_order(detail_citation_sources(record, kind), fund_order)
                if actual != wanted:
                    errors.append(f"{jid}: {kind} record citations differ from data")
    # dataset.method/missing-values requires Not published for unknown figures.
    positive_states = [j for j in data["jurisdictions"] if j.get("kind") != "supranational" and j["status"] != "none_identified"]
    unknown_figures = any("display" in i and (i["display"].get("budget") is None or i["display"].get("staff") is None) for i in data["institutes"])
    structural = any(j["status"] != "established" for j in positive_states)
    required(".missing:not(.na)", unknown_figures)
    required(".missing.na", structural)
    for missing in soup.select(".missing"):
        label = missing.get_text(strip=True)
        if "na" in missing.get("class", []):
            # Absence by category: a muted dash in the table; cards omit the row instead, except the three leadership rows (a dash when not applicable).
            row = missing.find_parent(class_="row")
            leadership = row is not None and row.get("data-track") in {CARD_TRACKS[label] for label in LEADERSHIP_ROWS}
            if missing.find_parent(class_="card") and not leadership:
                errors.append("card shows a structural absence instead of omitting the row")
            if not structural_mark_ok(missing):
                errors.append(
                    "structural absence is not a hidden dash with Not applicable for screen readers"
                )
            continue
        if label != "Not published":
            errors.append(
                "missing-value label does not distinguish unknown from inapplicable"
            )
        if label == "Not published" and not tip_of(missing).strip():
            errors.append("missing-value tooltip loses the null definition")
    for cell in required("#world-rows td", bool(positive_states)):
        if not cell.get_text(strip=True):
            errors.append("world table has an unexplained empty cell")
    errors += check_citation_ends(soup)
    for mark in required(".smark:not([data-state])", bool(positive_states) or any(j.get("europe_scope") for j in data["jurisdictions"])):
        if not mark.select_one(".sw"):
            errors.append("status mark lacks the shared sw symbol")
    cards = Keyed(soup, ".card[data-j]")
    for institute in data["institutes"]:
        if "operating" not in institute or institute["status"] != "established":
            continue
        jid = institute["jurisdiction_id"]
        state = "confirmed" if expected_activity(institute) else "not_confirmed"
        op_card = cards.one(jid)
        if not op_card or not op_card.select_one(f'.smark[data-state="{state}"]'):
            errors.append(f"{jid}: operating state does not use the text mark")
    return sorted(set(errors))


# Hand-worked evidence expectations from the reviewed credit audit and source excerpts, kept as a reviewed fixture
# (tests/fixtures/earliest_credit.json) so a data correction needs no code change. Deliberately independent of
# export.citations and the producer's pointer resolver.
EARLIEST_CREDIT = ROOT / "tests" / "fixtures" / "earliest_credit.json"


def load_credit_sources(path: Path = EARLIEST_CREDIT) -> tuple[dict, dict]:
    fixture = json.loads(path.read_text(encoding="utf-8"))
    table = lambda kind: {(e["institute_id"], e["lineage"]): list(e["source_ids"]) for e in fixture[kind]}  # noqa: E731
    activity, first = table("activity"), table("first_output")
    for kind, n in (("activity", len(activity)), ("first_output", len(first))):
        if n != len(fixture[kind]):
            raise ValueError(f"{path.name}: {kind} repeats an institute and lineage pair")
    return activity, first


ACTIVITY_CREDIT_SOURCES, FIRST_OUTPUT_CREDIT_SOURCES = load_credit_sources()


DEVELOPMENT_CREDIT_SOURCES = {
    ("NO", "no-1"): ["no-budget", "no-prop1s-2026-27-dfd"],
    ("NL", "nl-2"): ["nl-kamerbrief-2026-09"],
    ("ES", "es-1"): ["es-plan", "es-moncloa"],
    ("DE", "de-2"): ["de-bmds"],
    ("DE", "de-3"): ["de-bmds"],
}


def expected_event_sources(data, jurisdiction, event):
    if event["event"] == "first_output":
        institute = next((i for i in data["institutes"] if i["id"] == jurisdiction.get("institute_id")), None)
        output = next((o for o in (institute or {}).get("outputs", []) if o["lineage"] == event.get("output_lineage")), None)
        if output is None:
            raise ValueError("first-output oracle needs its credited catalogue item")
        return expected_credit_sources(institute, output)
    key = (jurisdiction["id"], event["id"])
    if key in DEVELOPMENT_CREDIT_SOURCES:
        return DEVELOPMENT_CREDIT_SOURCES[key]
    # Other events retain a render-fidelity check against their named field bindings.
    return citation_fields(event, ["description", "short", "date", "event"])


def expected_credit_sources(institute, output, credit_sources=None):
    key = (institute.get("id"), output["lineage"])
    reviewed = {**ACTIVITY_CREDIT_SOURCES, **FIRST_OUTPUT_CREDIT_SOURCES}
    if credit_sources is not None:
        reviewed.update(credit_sources)  # Explicit expectations for synthetic fixtures only.
    if key not in reviewed:
        raise ValueError(f"credit oracle needs reviewed sources for {key!r}")
    return reviewed[key]


def citation_fields(record, fields):
    return list(dict.fromkeys(sid for field in fields for sid in record.get("citations", {}).get(field, [])))


def expected_funding_statement(record):
    description = record.get("description", "")
    symbols = {"GBP": r"£\s*", "JPY": r"¥\s*", "SGD": r"S\$\s*", "EUR": r"€\s*", "KRW": r"₩\s*", "USD": r"(?:US)?\$\s*", "CAD": r"(?:CA?|CDN)\$\s*", "AUD": r"A(?:U)?\$\s*"}
    currency = record.get("currency")
    return re.sub(symbols[currency], currency + " ", description) if currency in symbols else description


def expected_native_funding_value(record):
    """Independent decimal formatting of the primary amount at two significant figures, without conversion."""
    qualifier = record.get("amount_qualifier")
    amount = half_up_two(record["amount"], qualifier)
    marker = "~" if qualifier != "about" and amount != Decimal(str(record["amount"])) else ""
    number = short_amount(amount) if amount >= 1000 else format(amount.normalize(), "f")
    prefix = {"about": "about ", "at_most": "up to ", "at_least": "at least ", "nearly": "nearly ", "more_than": "more than ", "less_than": "less than "}.get(qualifier, "")
    return prefix + marker + record["currency"] + " " + number


def ecb_rate(record, data):
    """The data's ECB rate row for the amount's currency, or None for EUR or a currency the ECB does not publish."""
    if record.get("currency") in (None, "EUR"):
        return None
    return next((r for r in (data.get("dataset") or {}).get("exchange_rates", []) if r["currency"] == record["currency"]), None)


def rate_sources(record, data):
    """The sources a euro figure rests on, cited beside an amount the page also shows in euros (rule currency-rounding):
    the amount's own (amount, currency, qualifier) and the rate row's."""
    rate = ecb_rate(record, data)
    if not rate or record.get("amount") is None:
        return []
    own = [i for field in ("amount", "currency", "amount_qualifier") for i in (record.get("citations") or {}).get(field, [])]
    return own + list(rate.get("source_ids", []))


def expected_shown_funding_value(record, data):
    """Independent text of a non-compared amount: every amount in a currency with an ECB rate is also shown in euros,
    "~EUR 52m (JPY 8.8bn)", converted at the data's rate and rounded to two figures (rule currency-rounding).
    EUR, and a currency with no ECB rate (KES), keep the native text."""
    native = expected_native_funding_value(record)
    rate = ecb_rate(record, data)
    if rate is None:
        return native
    qualifier = record.get("amount_qualifier")
    with localcontext() as context:
        context.prec = 50
        eur = half_up_two(Decimal(str(record["amount"])) * Decimal(str(rate["eur_per_unit"])), qualifier)
    prefix = {"about": "about ", "at_most": "up to ", "at_least": "at least ", "nearly": "nearly ", "more_than": "more than ", "less_than": "less than "}.get(qualifier, "")
    figure = short_amount(eur) if eur >= 1000 else format(eur.normalize(), "f")
    return prefix + ("" if qualifier == "about" else "~") + "EUR " + figure + " (" + native[len(prefix):] + ")"


def detail_citation_sources(record, kind):
    """Field order follows the visible description, then metadata on that line."""
    if kind == "funding":
        fields = ["description"] if record.get("description") else ["amount", "currency"]
        fields += ["period", "commitment"]
    elif record.get("description"):
        fields = ["description", "period"]
    else:
        if record.get("total") is not None:
            fields = ["total", "total_qualifier", "technical", "technical_qualifier", "as_of"]
        elif record.get("technical") is not None:
            fields = ["technical", "technical_qualifier", "as_of"]
        else:
            fields = ["description", "as_of"]
        fields += ["target", "period"]
    fields += ["as_of", "confidence", "notes"]
    return citation_fields(record, fields)


def expected_activity(institute: dict | None, credit_sources=None) -> list[dict]:
    """Independent oracle from authored outputs, never exported operating values.

    Intentionally does not import the build selector or execute page JavaScript.
    """
    selected = []
    for kind in ("published_work", "joint_testing"):
        candidates = [o for o in (institute or {}).get("outputs", []) if o["qualifies_as"] == kind]
        if candidates:
            winner = min(candidates, key=lambda o: (o["credit"]["date"], o["lineage"]))
            selected.append({"kind": kind, "date": winner["credit"]["date"], "lineage": winner["lineage"], "source_ids": expected_credit_sources(institute, winner, credit_sources)})
    return sorted(selected, key=lambda row: (row["date"], row["kind"]))


def expected_date(day: str) -> str:
    """Page date convention, preserving recorded precision: 7 Mar 2025, Mar 2025, 2025."""
    months = "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split()
    parts = day.split("-")
    text = parts[0]
    if len(parts) > 1:
        text = months[int(parts[1]) - 1] + " " + text
    if len(parts) > 2:
        text = str(int(parts[2])) + " " + text
    return text


def kind_label(kind: str, first: bool) -> str:
    label = "published work" if kind == "published_work" else "joint testing"
    return label[0].upper() + label[1:] if first else label


def expected_operating(jurisdiction: dict, institute: dict | None, credit_sources=None) -> str:
    """Independent per-kind prose: the view CSV value and the start of the mark's tooltip."""
    if jurisdiction["status"] != "established":
        return "Not applicable"
    if institute is None:
        return "Not published"
    clauses = [
        kind_label(item["kind"], not n) + " since " + expected_date(item["date"])
        for n, item in enumerate(expected_activity(institute, credit_sources))
    ]
    return "; ".join(clauses) or "None found yet"


def expected_operating_lines(jurisdiction: dict, institute: dict | None, credit_sources=None, interval=None) -> list[str]:
    """Independent displayed lines (layout A): each kind found in date order, then "since" its earliest date;
    on cards, the first qualifying item's interval line (expected_intervals) follows its since line."""
    evidence = expected_activity(institute, credit_sources) if jurisdiction["status"] == "established" and institute else []
    if not evidence:
        return [expected_operating(jurisdiction, institute, credit_sources)]
    return [
        line
        for item in evidence
        for line in (kind_label(item["kind"], True), "since " + expected_date(item["date"]))
        + ((f'{kind_label(item["kind"], True)} began {interval["text"]}',) if interval and interval["kind"] == item["kind"] else ())
    ]


def prose(node) -> str:
    """Keep item boundaries distinct from spaces within an item."""
    node = copy.copy(node)
    for gap in node.select(".sep-gap") if node else []:
        gap.replace_with(" · ")
    return " ".join(node.get_text().split()) if node else ""


def operating_lines(node) -> list[str]:
    """Rendered lines of a Known activity value: per kind, its value line, its since line and any interval line; else the whole value."""
    kinds = node.select(".activity-kind")
    if not kinds:
        return [prose(node)]
    return [
        prose(part)
        for kind in kinds
        for part in (kind.select_one(".activity-clause"), kind.select_one(".activity-since"), kind.select_one(".activity-interval"))
        if part is not None
    ]


# Intervals between milestones: an oracle written separately from scripts/published.py and
# scripts/display.py, reading the timeline and output catalogue, never the exported intervals.
INTERVAL_CAVEAT = "not a measure of speed or quality"  # Known activity tooltip
STATUS_INTERVAL_CAVEAT = "It does not measure speed or quality"  # Status since interval tooltip (owner copy, 23 Oct)
INTERVAL_MONTH = 30.4375
# Hand-worked card wording for the real records (Status qualifier "Announced on <date>, <interval> earlier", owner 1.0.3) at as_of 2026-10-20 (day counts worked in
# tests/test_intervals.py). New dates change these: update them in a separately reviewed CODE change.
HAND_WORKED_INTERVALS = {
    "GB": ("Announced on 26 Oct 2023, 7 days earlier", "about 3 months after it was established"),
    "FR": (None, "about 5 months after it was established"),
    "DE": ("Announced on 8 Jun 2026, about 3 months earlier", None),
    "EU": ("Announced on 24 Jan 2024, 28 days earlier", "about 17 months after it was established"),
    "US": ("Announced on 1 Nov 2023, about 3 months earlier", "about 9 months after it was established"),
    "CA": ("Proposed on 16 Apr 2024, about 7 months earlier", "about 7 months after it was established"),
    "JP": ("Announced on 21 Dec 2023, 55 days earlier", "about 14 months after it was established"),
    "KR": ("Announced on 22 May 2024, about 6 months earlier", "about 7 months after it was established"),
    "SG": (None, "about 6 months after it was established"),
    "AU": ("Announced on 25 Nov 2025", None),
    "MY": (None, None),
    "IN": ("Announced on 30 Jan 2025", None),
}


def oracle_day_range(day: str, as_of: str | None) -> tuple[date, date]:
    """Every day a recorded date can mean; month-only and year-only dates end by as_of."""
    import calendar
    parts = [int(p) for p in day.split("-")]
    if len(parts) == 3:
        return date(*parts), date(*parts)
    start = date(parts[0], parts[1] if len(parts) == 2 else 1, 1)
    end = date(parts[0], parts[1], calendar.monthrange(parts[0], parts[1])[1]) if len(parts) == 2 else date(parts[0], 12, 31)
    cap = date.fromisoformat(as_of) if as_of else end
    return start, max(start, min(end, cap))


def oracle_first(days: list[str], as_of: str | None) -> tuple[date, date] | None:
    """The earliest of several dates: earliest possible day to the earliest certain day."""
    ranges = [oracle_day_range(d, as_of) for d in days]
    return (min(r[0] for r in ranges), min(r[1] for r in ranges)) if ranges else None


def oracle_amount(start, end) -> tuple[str, int] | None:
    """An exact interval's amount and signed days: days under 60, else about N months (half up)."""
    if start is None or end is None or start[0] != start[1] or end[0] != end[1]:
        return None
    days = (end[0] - start[0]).days
    size = abs(days)
    return ((f"{size} days" if size != 1 else "1 day") if size < 60 else f"about {int(size / INTERVAL_MONTH + 0.5)} months"), days


def oracle_phrase(start, end, word: str) -> str | None:
    """Known activity wording, exact intervals only; signed."""
    found = oracle_amount(start, end)
    if found is None:
        return None
    amount, days = found
    if days == 0:
        return "on the same day as " + word
    return f"{amount} {'before' if days < 0 else 'after'} {word}"


def oracle_announced(start, end, word: str) -> str | None:
    """Status qualifier: "Announced on 24 Jan 2024, 28 days earlier"; the date alone for a range; nothing for an inexact date."""
    if start is None or start[0] != start[1]:
        return None
    text = f"{word.capitalize()} on {start[0].day} {start[0].strftime('%b')} {start[0].year}"
    found = oracle_amount(start, end)
    if found is None:
        return text
    amount, days = found
    return text + ", " + ("the same day" if days == 0 else f"{amount} {'earlier' if days > 0 else 'later'}")


def expected_intervals(jurisdiction: dict, institute: dict | None, as_of: str | None) -> tuple[str | None, dict | None, list[int]]:
    """Status since wording, the Known activity interval line and the announcement events it counts from."""
    if institute is None or jurisdiction["status"] != "established":
        return None, None, []
    events = jurisdiction.get("timeline", [])
    killed = {e.get("withdraws") for e in events if e["event"] == "withdrawn"}
    announcing = [n for n, e in enumerate(events) if e["event"] in ("commitment", "proposed")]
    designations = [e["date"] for e in events if e["event"] == "designated" and e["id"] not in killed]
    announced = oracle_first([events[n]["date"] for n in announcing], as_of)
    established = oracle_first(designations, as_of)
    qualifying = [o["credit"]["date"] for o in institute.get("outputs", []) if o["qualifies_as"] != "none"]
    first = oracle_first(qualifying, as_of)
    words = {"proposed" if events[n]["event"] == "proposed" else "announced" for n in announcing
             if announced and oracle_day_range(events[n]["date"], as_of)[0] <= announced[1]}
    since = None
    if established and not any(e["event"] == "closed" for e in events):
        since = oracle_announced(announced, established, words.pop() if len(words) == 1 else "announced")
    line = None
    text = oracle_phrase(established, first, "it was established")
    if text:
        day = first[0].isoformat()
        item = next((e for e in expected_activity(institute, {(institute["id"], o["lineage"]): [] for o in institute.get("outputs", [])}) if e["date"] == day), None)
        if item is not None:
            line = {"kind": item["kind"], "text": text}
    # Every record that could be either milestone is evidence for the qualifier.
    counted = [n for n in announcing if since and oracle_day_range(events[n]["date"], as_of)[0] <= announced[1]]
    counted += [n for n, e in enumerate(events) if since and e["event"] == "designated" and e["id"] not in killed
                and oracle_day_range(e["date"], as_of)[0] <= established[1]]
    return since, line, counted


def check_interval_output(html: str, data: dict, hand_worked: dict | None = None) -> list[str]:
    """Cards show only exact intervals, as muted qualifiers with the caveat; the table never shows them."""
    soup = BeautifulSoup(html, "html.parser")
    institutes = {i["id"]: i for i in data["institutes"]}
    as_of = data["dataset"]["as_of"]
    errors = []
    if soup.select("#world-rows .interval, #world-rows .activity-interval"):
        errors.append("table shows an interval; intervals are card qualifiers only")
    full_cards = Keyed(soup, "article.card:not(.mini)[data-j]")
    for j in data["jurisdictions"]:
        institute = institutes.get(j.get("institute_id"))
        since, line, counted = expected_intervals(j, institute, as_of)
        if hand_worked is not None and j["id"] in hand_worked and (since, line and line["text"]) != hand_worked[j["id"]]:
            errors.append(f"{j['id']}: interval oracle {(since, line and line['text'])!r} differs from the hand-worked {hand_worked[j['id']]!r}")
        card = full_cards.one(j["id"])
        if card is None:
            errors.extend(zero_match_errors("interval_output", f'article.card:not(.mini)[data-j="{j["id"]}"]', 0, full_card_expected(j)))
            continue
        rows = {r.get("data-track"): r for r in card.select(".row")}
        status = rows.get(CARD_TRACKS["Status"])
        errors.extend(zero_match_errors("interval_output", f'{j["id"]} .row[data-track="{CARD_TRACKS["Status"]}"]', int(status is not None)))
        shown = status.select(".interval") if status else []
        # The qualifier names the announcement and its date; the amount is the caveated term, the other words plain text on the same muted line.
        text = []
        for holder in status.select(".qualifiers") if status else []:
            holder = copy.copy(holder)
            for cites in holder.select(".cites"):
                cites.decompose()
            for gap in holder.select(".sep-gap"):
                gap.replace_with(";")
            # Any announcement or proposal wording counts, so a month-only or year-only date ("Announced on Mar 2025") cannot slip through.
            text += re.findall(r"(?:Announced|Proposed) on [^;]+", " ".join(holder.get_text().split()))
        text = [t.strip() for t in text]
        for node in shown:
            if not node.find_parent(class_="qualifiers") or STATUS_INTERVAL_CAVEAT not in tip_of(node):
                errors.append(f"{j['id']}: Status interval is not a muted qualifier with its caveat")
        if len(shown) != (1 if since and ", " in since else 0):
            errors.append(f"{j['id']}: Status shows {len(shown)} caveated intervals for {since!r}")
        if text != ([since] if since else []):
            errors.append(f"{j['id']}: Status interval {text!r} differs from {since!r}")
        if since and status is not None:
            # The qualifier counts from the announcement, so its date's sources join the row's citations (numeral order: check_citations).
            wanted = set(j.get("citations", {}).get("since", [])) | {
                sid for n in counted for sid in j["timeline"][n].get("citations", {}).get("date", [])
            }
            if {a.get("data-source-id") for a in status.select(".cn") if not a.find_parent(class_="activity-line")} != wanted:
                errors.append(f"{j['id']}: Status citations do not cover the announcement and establishment")
        activity = rows.get(CARD_TRACKS["Known activity"])
        errors.extend(zero_match_errors("interval_output", f'{j["id"]} .row[data-track="{CARD_TRACKS["Known activity"]}"]', int(activity is not None), institute is not None and j["status"] == "established"))
        lines = activity.select(".activity-interval") if activity else []
        found = [(n.find_parent(class_="activity-kind").select_one(".activity-clause").get("data-kind"), " ".join(n.get_text().split())) for n in lines]
        if found != ([(line["kind"], f"{kind_label(line['kind'], True)} began {line['text']}")] if line else []):
            errors.append(f"{j['id']}: Known activity interval {found!r} differs from {line!r}")
        for n in lines:
            if n.select(".cn") or "qualifiers" not in n.get("class", []):
                errors.append(f"{j['id']}: Known activity interval is not an uncited muted line")
        mark = activity.select_one(".smark") if activity else None
        if lines and INTERVAL_CAVEAT not in ((tip_of(mark) or mark.get("title") or "") if mark else ""):
            errors.append(f"{j['id']}: Known activity tooltip lacks the interval caveat")
    return errors


def check_operating_output(
    html: str, data: dict, csv_rows: list[dict] | None = None, *, credit_sources=None
) -> list[str]:
    """Check page findings and applicability without changing canonical data codes."""
    soup = BeautifulSoup(html, "html.parser")
    institutes = {i["id"]: i for i in data["institutes"]}
    errors = []
    # Structural absence is judged before hidden text is removed: the dash is aria-hidden, "Not applicable" is visually hidden.
    structural = {id(cell) for cell in soup.select("#world-rows td") if is_structural_absence(cell)}
    orders = card_source_orders(soup, data)
    clause_sources = {
        id(clause): [a.get("data-source-id") for a in clause.select(".cn")]
        for clause in soup.select(".activity-clause, .activity-since")
    }
    for node in soup.select(".cites, .vh"):
        node.decompose()
    full_cards, world_rows = Keyed(soup, "article.card:not(.mini)[data-j]"), Keyed(soup, "#world-rows tr[data-j]")
    for j in data["jurisdictions"]:
        institute = institutes.get(j.get("institute_id"))
        card_expected = expected_operating_lines(j, institute, credit_sources, expected_intervals(j, institute, data.get("dataset", {}).get("as_of"))[1])
        card = full_cards.one(j["id"])
        table = world_rows.one(j["id"])
        card_values = (
            {
                r.get("data-track"): r.select_one(".v")
                for r in card.select(".row")
            }
            if card
            else {}
        )
        established = j["status"] == "established"
        if card is not None and not established and CARD_TRACKS["Known activity"] in card_values:
            errors.append(f"{j['id']}: card shows Known activity before establishment; the row must be omitted")
        if card is not None and institute is not None:
            for label in ("Budget", "Staff"):  # Cards retain both fields for every institute (principle: Scope).
                if CARD_TRACKS[label] not in card_values:
                    errors.append(f"{j['id']}: card has no {label} row; cards cover every institute")
        for view, node in (
            ("card", card_values.get(CARD_TRACKS["Known activity"])),
            ("table", table.select_one('[data-col="op"]') if table else None),
        ):
            if (view == "card" and (card is None or not established)) or (
                view == "table" and table is None
            ):
                continue
            if not established:
                if id(node) not in structural:
                    errors.append(
                        f"{j['id']}: table Known activity differs from structural absence"
                    )
                continue
            expected = card_expected if view == "card" else expected_operating_lines(j, institute, credit_sources)
            # Every line, and nothing else anywhere in the value (whitespace aside).
            if node is None or operating_lines(node) != expected or "".join(node.get_text().split()) != "".join("".join(expected).split()):
                errors.append(
                    f"{j['id']}: {view} Known activity differs from {expected!r}"
                )
            evidence = expected_activity(institute, credit_sources)
            if node and institute is not None:
                # Each kind line cites its own earliest item; the since lines carry no citations.
                if any(clause_sources[id(n)] for n in node.select(".activity-since")):
                    errors.append(f"{j['id']}: {view} Known activity since line carries a citation")
                clauses = node.select(".activity-clause")
                cited = [in_numeral_order(list(dict.fromkeys(e["source_ids"])), orders.get(j["id"], [])) for e in evidence]
                if [c.get("data-kind") for c in clauses] != [e["kind"] for e in evidence] or [
                    clause_sources[id(c)] for c in clauses
                ] != cited:
                    errors.append(f"{j['id']}: {view} Known activity clause citations differ")
                state = "confirmed" if evidence else "not_confirmed"
                if not node.select_one(f'.smark[data-state="{state}"]'):
                    errors.append(f"{j['id']}: {view} operational text mark missing")
                mark = node.select_one(".smark")
                # The tooltip carries the proxy caveat: no known work is not inactivity.
                tip = (tip_of(mark) or mark.get("title") or "") if mark else ""
                if OPERATING_CAVEAT not in tip:
                    errors.append(
                        f"{j['id']}: {view} operational tooltip lacks finding wording"
                    )
                if evidence and ACTIVITY_SINCE_DEFINITION not in tip:  # None found yet has no since line to define
                    errors.append(f"{j['id']}: {view} operational tooltip lacks the definition of since")
        if table is not None:
            for label in ("Budget", "Staff"):  # a dash only where the state has no institute (principle: Scope)
                if (id(table.select_one(f'[data-col="{label.lower()}"]')) in structural) != (institute is None):
                    errors.append(
                        f"{j['id']}: table {label} must be a structural absence exactly when the state has no institute"
                    )
        for row in csv_rows or []:
            if row["id"] != j["id"]:
                continue
            if row["operational"] != expected_operating(j, institute, credit_sources):
                errors.append(f"{j['id']}: view CSV operational label differs")
            if any((row[key] == "Not applicable") != (institute is None) for key in ("budget", "staff")):
                errors.append(f"{j['id']}: view CSV applicability differs")
    # The header has a tooltip; the method rule defines the evidence and its caveat.
    header = soup.select_one('table.world thead th[data-k="op"] button')
    if not header or not (tip_of(header) or header.get("title") or "").strip():
        errors.append("Known activity header has no tooltip")
    for selector, wordings in (
        ("#rules", ("published evaluation", "joint testing", OPERATING_CAVEAT)),
    ):
        node = soup.select_one(selector)
        if not node or any(wording.lower() not in node.get_text().lower() for wording in wordings):
            errors.append(f"{selector}: operational definitions lack finding labels")
    return errors


def check_activity_tooltip(page) -> list[str]:
    """Keyboard reaches the definition of "since"; the tip opens below the whole value, clear of the kind lines; Escape dismisses."""
    errors = []
    page.mouse.move(0, 0)  # a pointer left over the page would hover whatever scrolls under it
    for where in (f'.card .row[data-track="{CARD_TRACKS["Known activity"]}"]', '#world-rows td[data-col="op"]'):
        mark = page.locator(f"{where} .smark.value:has(.activity-kind)").first
        if not mark.count() or not mark.is_visible():
            continue
        mark.focus()
        page.wait_for_timeout(50)
        result = mark.evaluate(
            """m => { const t = document.getElementById('tip'), k = m.querySelector('.activity-kinds');
              if (!t || t.hidden || m !== document.activeElement) return 'tip not shown on keyboard focus';
              if (!t.textContent.includes('""" + ACTIVITY_SINCE_DEFINITION + """') || !m.getAttribute('aria-description')) return 'tip or description does not define since';
              const a = t.getBoundingClientRect(), b = k.getBoundingClientRect();
              if (a.top < b.bottom - 1 && a.bottom > b.top + 1) return 'tip covers the kinds line';
              return ''; }"""
        )
        if result:
            errors.append(f"Known activity {where}: {result}")
        page.keyboard.press("Escape")
        if not page.evaluate("() => { const t = document.getElementById('tip'); return !t || t.hidden; }"):
            errors.append(f"Known activity {where}: Escape does not dismiss the tip")
        page.evaluate("document.activeElement.blur()")
    return errors


def check_state_counts(soup, data: dict) -> list[str]:
    """Counts count states only: the EU is in no number and has its own entry at the top of Europe, state by state."""
    jurisdictions = data["jurisdictions"]
    errors = []
    errors.extend(check_headline(soup, data))
    errors.extend(region_partition_errors(data))
    for group in soup.select(".statusgroup[data-status]"):
        shown = group.select(".card")
        match = re.search(r"\((\d+)\)", group.select_one("h3").get_text())
        if not match or int(match.group(1)) != len(shown):
            errors.append(f"group {group['data-status']}: heading count differs from its {len(shown)} cards")
        for card in shown:
            if card.get("data-j") == "EU" or any(j["id"] == card.get("data-j") and j["kind"] == "supranational" for j in jurisdictions):
                errors.append(f"group {group['data-status']}: holds the EU, which is not a state")
    first = soup.select_one("#entries > :first-child")
    eu_cards = soup.select('#entries > .eu-level .card[data-j="EU"]')
    if not first or "eu-level" not in first.get("class", []) or len(eu_cards) != 1:
        errors.append("the EU level entry is not first in Europe, state by state")
    if soup.select_one('#world-cards .card[data-j="EU"]'):
        errors.append("the EU appears under Worldwide")
    # Principle Supporting text: the note is the (i) after the heading, never a line under it.
    note = first.select_one(".eu-head > button.info#eu-scope") if first else None
    if not note or not tip_of(note).strip():
        errors.append("EU level heading lacks its (i) with a non-empty scope tooltip")
    return errors


# Headline (decision D5, wording 4 Nov 2026): "N of M [European] states have an AI Security Institute. K more are planning, proposing or exploring one."
HEADLINE_WORDS = {"Europe": "European states", "Americas": "states in the Americas", "Asia": "Asian states", "Africa": "African states", "Oceania": "states in Oceania", "World": "states"}
LIST_REGIONS = ("Europe", "Americas", "Asia", "Oceania", "Africa")
MOVING_STATUSES = ("planned", "proposed", "exploring")


def region_map_pool(data: dict, region: str) -> list[dict]:
    """States on a region's map: the counted pool, plus for Europe the European states (UN M49) outside the 32, listed apart and never counted (owner, 5 Nov 2026)."""
    extra = [j for j in data["jurisdictions"] if region == "Europe" and j["kind"] != "supranational" and not j["europe_scope"] and (j.get("region") or {}).get("name") == "Europe"]
    return region_pool(data, region) + extra


def region_partition_errors(data: dict) -> list[str]:
    """Every state and territory is on exactly one listed region's map, so the regions together cover the World view's states."""
    seen: dict[str, list[str]] = {}
    for region in LIST_REGIONS:
        for j in region_map_pool(data, region):
            seen.setdefault(j["id"], []).append(region)
    states = [j["id"] for j in data["jurisdictions"] if j["kind"] != "supranational"]
    return [f"regions: {jid} is in {seen.get(jid, [])}, expected exactly one region" for jid in states if len(seen.get(jid, [])) != 1]


def region_pool(data: dict, region: str) -> list[dict]:
    """States a region counts: Europe is the 32-state scope; the others are UN regions without those states."""
    states = [j for j in data["jurisdictions"] if j["kind"] != "supranational"]
    if region == "World":
        return states
    if region == "Europe":
        return [j for j in states if j["europe_scope"]]
    return [j for j in states if not j["europe_scope"] and (j.get("region") or {}).get("name") == region]


def expected_headline(data: dict, region: str) -> str:
    pool = [j for j in region_pool(data, region) if j["kind"] == "state"]  # counts are of states; a territory is mapped and listed, not counted
    n = sum(j["status"] == "established" for j in pool)
    k = sum(j["status"] in MOVING_STATUSES for j in pool)
    one = f"{n} of {len(pool)} {HEADLINE_WORDS[region]} {'has' if n == 1 else 'have'} an AI Security Institute."
    if not k:
        two = f"No {'other ' if n else ''}state is planning, proposing or exploring one."
    elif n:
        two = f"{k} more {'is' if k == 1 else 'are'} planning, proposing or exploring one."
    else:
        two = f"{k} {'state is' if k == 1 else 'states are'} planning, proposing or exploring one."
    return f"{one} {two}"


def expected_region_rows(data: dict, region: str) -> list[str]:
    """The open region's institutes, by status then name; the EU AI Office under Europe."""
    rows = sorted((j for j in region_pool(data, region) if j["status"] != "none_identified"), key=lambda j: (STATUS_ORDER.index(j["status"]), j["name"], j["id"]))
    return [j["id"] for j in rows] + ([j["id"] for j in data["jurisdictions"] if j["kind"] == "supranational"] if region == "Europe" else [])


def check_headline(soup, data: dict) -> list[str]:
    """The saved headline is the World sentence pair, worded from the data; Europe's 'European' carries the scope."""
    node = soup.select_one("#headline .hl-version.current")
    if not node:
        return ["headline missing"]
    if [v.get("data-region") for v in soup.select("#headline .hl-version")] != ["Europe", "Americas", "Asia", "Africa", "Oceania", "World"] or len(soup.select("#headline .hl-version[aria-hidden=true]")) != 5:
        errors_cell = ["headline: the regional headlines do not share one cell with only the current one exposed"]
    else:
        errors_cell = []
    errors = []
    text = node.get_text().replace("\u00a0", " ")
    if text != expected_headline(data, "World"):
        errors.append(f"headline {text!r} differs from the data {expected_headline(data, 'World')!r}")
    europe = soup.select_one('#headline .hl-version[data-region="Europe"]')
    scope = europe.select_one("abbr.g") if europe else None
    if not scope or not scope.get_text(strip=True) or not tip_of(scope).strip():
        errors.append("headline: 'European' lacks its scope definition")
    for version in soup.select("#headline .hl-version"):
        region = version.get("data-region")
        if version.get_text().replace("\u00a0", " ") != expected_headline(data, region):
            errors.append(f"headline {region}: {version.get_text()!r} differs from the data {expected_headline(data, region)!r}")
    # Owner's order: title and meta line in the header, then headline, definition, region chips with the search, then map and region list.
    header = soup.select_one("#top .measure")
    if not header or [c.name if c.name == "h1" else c.get("id") for c in header.find_all(recursive=False)] != ["h1", "updated"] or not header.find_next("section", class_="first"):
        errors.append("first screen: the header is not the title then the meta line, before the first screen")
    first = soup.select_one("section.first")
    order = [c.get("id") or " ".join(c.get("class") or []) for c in first.find_all(recursive=False) if c.name != "h2"] if first else []
    if order[:4] != ["headline", "definition-link", "maptools", "explore"] or not soup.select_one("section.first .maptools #map-search") or not soup.select_one("section.first .maptools #region-chips"):
        errors.append(f"first screen: expected headline, definition, region chips with the search, then map and list; got {order}")
    return errors + errors_cell


def two_figures(text: str) -> bool:
    """Independent test of the display rule: every amount or count in the text has at most two significant figures."""
    for token in re.findall(r"(?<![\w.])(\d[\d,]*(?:\.\d+)?)(?:bn|m|k)?(?![\w])", text):
        digits = token.replace(",", "")
        mantissa = digits.replace(".", "").lstrip("0")
        if "." not in digits:
            mantissa = mantissa.rstrip("0")
        if len(mantissa) > 2:
            return False
    return True


def half_up_two(value, qualifier=None) -> Decimal:
    """Independent two-significant-figure rounding: half up, floor for a lower bound, ceiling for an upper bound."""
    rounding = ROUND_FLOOR if qualifier in ("at_least", "more_than") else ROUND_CEILING if qualifier in ("at_most", "less_than", "nearly") else ROUND_HALF_UP
    value = Decimal(str(value))
    return value.quantize(Decimal(1).scaleb(value.adjusted() - 1), rounding=rounding) if value else value


def short_amount(value: Decimal) -> str:
    """A two-significant-figure amount in k, m or bn; below ten the second figure is a decimal ("8.0m")."""
    for size, suffix in ((10**9, "bn"), (10**6, "m"), (10**3, "k")):
        if value >= size:
            scaled = value / size
            return (f"{scaled:.1f}" if scaled < 10 else format(scaled.normalize(), "f")) + suffix
    return format(value.normalize(), "f")


def expected_annual_native(record):
    """The annual amount in its own currency: a period mean is amount / years here, not its float annual_amount."""
    with localcontext() as context:
        context.prec = 50
        if record.get("annual_basis_code") == "period_mean" and "sum_of" not in record and record.get("amount") is not None and record.get("years"):
            return Decimal(str(record["amount"])) / Decimal(str(record["years"]))
        return Decimal(str(record["annual_amount"]))


def expected_annual_amount(record, data):
    """Independent raw arithmetic for labels; never round the six-figure export again."""
    amount = expected_annual_native(record)
    if record["currency"] == "EUR":
        return amount
    rates = {
        r["currency"]: r["eur_per_unit"] for r in data["dataset"]["exchange_rates"]
    }
    with localcontext() as context:
        context.prec = 50
        return amount * Decimal(str(rates[record["currency"]]))


TWO_FIGURE_SELECTORS = ('.fund-list[data-kind="budget"] .fund-budget .fund-figure strong', '.fund-list[data-kind="staff"] .fund-staff .fund-figure strong', "#world-rows td[data-col=budget] .budget-amount", "#world-rows td[data-col=staff]", ".fund-staff-summary .fund-value strong", ".fund-budget-summary .fund-value strong", ".funding-native .budget-amount", ".fund-staff-summary .fund-context")


def expected_staff_total(record) -> str:
    """Independent two-figure staff text: ~ when rounding changed the count, bounds rounded the way that keeps them true."""
    qualifier, count = record.get("total_qualifier"), Decimal(record["total"])
    kept = half_up_two(count, qualifier)
    number, mark = format(int(kept), ","), "~" if kept != count else ""
    return {"about": "~" + number, "at_least": mark + number + "+", "at_most": "up to " + mark + number}.get(qualifier, mark + number)


STAFF_FIGURES = ROOT / "tests" / "fixtures" / "staff_figures.json"  # hand-worked from the quotations, never the export


def latest_staff_total(institute: dict) -> tuple[dict | None, bool]:
    """The institute's latest authored staff record with a total, by as_of (ISO dates, months or years compare as
    text), and whether that choice is ambiguous: another record with a total has the same as_of, or one is a prefix
    of the other (2026 against 2026-10-12), so text order does not settle which is later."""
    records = [r for r in institute.get("staff") or [] if r.get("total") is not None]
    if not records:
        return None, False
    latest = max(records, key=lambda r: str(r.get("as_of") or ""))
    when = str(latest.get("as_of") or "")
    rivals = [r for r in records if r is not latest and (str(r.get("as_of") or "").startswith(when) or when.startswith(str(r.get("as_of") or "")))]
    return latest, bool(rivals)


def established_funding_institutes(data: dict) -> list[dict]:
    """The comparison's scope, independently read from the jurisdiction status."""
    statuses = {j["id"]: j.get("status") for j in data["jurisdictions"]}
    return [i for i in data.get("institutes", []) if statuses.get(i["jurisdiction_id"]) == "established"]


def check_staff_figures(soup, data: dict, expected: dict | None = None) -> list[str]:
    """Each chart row's staff total is the hand-worked figure (tests/fixtures/staff_figures.json) at two figures, marked
    ~ exactly when rounding changed it. The expectation is read from the quotation that supports the latest staff
    record, never from display.total_staff; an institute with a staff total and no entry, or whose latest record no
    longer matches its entry, fails with its key until the fixture is reviewed."""
    if expected is None:
        expected = {e["institute_id"]: e for e in json.loads(STAFF_FIGURES.read_text(encoding="utf-8"))["staff"]}
    errors = []
    chart_rows = Keyed(soup, '#fund-rows .fund-list[data-kind="staff"] .fund-item[data-institute]', "data-institute")
    details = Keyed(soup, '#fund-rows .fund-detail[data-institute]', "data-institute")
    known = {inst["id"] for inst in data["institutes"]}
    compared = {inst["id"] for inst in established_funding_institutes(data)}
    errors += [f"{iid}: staff fixture entry for an institute the data does not have" for iid in sorted(set(expected) - known)]
    for inst in data["institutes"]:
        latest, ambiguous = latest_staff_total(inst)
        entry = expected.get(inst["id"])
        if ambiguous:
            errors.append(f"{inst['id']}: two staff records with a total share or overlap the latest as_of ({latest.get('as_of')}); which is latest needs review")
            continue
        if latest is None:
            if entry is not None:
                errors.append(f"{inst['id']}: staff fixture has a total, the data has none")
            continue
        if entry is None:
            errors.append(f"{inst['id']}: staff total needs a reviewed entry in tests/fixtures/staff_figures.json")
            continue
        if (str(latest.get("as_of")), latest["total"], latest.get("total_qualifier")) != (entry["as_of"], entry["total"], entry["total_qualifier"]):
            errors.append(f"{inst['id']}: latest staff record {latest.get('as_of')} {latest['total']} {latest.get('total_qualifier')} differs from the reviewed entry {entry['as_of']} {entry['total']} {entry['total_qualifier']}")
            continue
        for place, selector in (("row", ".fund-staff .fund-value strong"), ("detail", ".fund-staff-summary .fund-value strong")):
            if inst["id"] not in compared:
                continue  # All institutes still need reviewed totals; cards and records retain their figures.
            node = (details if place == "detail" else chart_rows).one(inst["id"], selector)
            if place == "row" and not latest.get("as_of"):
                continue
            if node:
                node = copy.copy(node)
                for cites in node.select(".cites"):
                    cites.decompose()
            shown = node.get_text(strip=True) if node else None
            if shown != expected_staff_total(entry):
                errors.append(f"{inst['jurisdiction_id']}: {place} staff total shown as {shown!r}, expected {expected_staff_total(entry)!r}")
    return errors


def check_two_significant_figures(soup) -> list[str]:
    """No displayed budget or staff figure carries more than two significant figures (not the source statements and related-funding lines, which quote sources).

    Every selector must match: a selector that finds nothing would pass without checking anything."""
    errors = []
    for selector in TWO_FIGURE_SELECTORS:
        nodes = soup.select(selector)
        if not any(n.get_text(strip=True) for n in nodes):
            errors.append(f"{selector}: matches no node with text, so the two-figure check would pass vacuously")
        for node in nodes:
            text = re.sub(r"\bas of .*|\(.*? technical\)", "", node.get_text(" ", strip=True))
            if not two_figures(text):
                errors.append(f"{selector}: {text!r} shows more than two significant figures")
    return errors


def check_glyphs(soup) -> list[str]:
    """Squares are for status only: every swatch is a status swatch, and Known activity is text."""
    errors = []
    status = re.compile(r"^st-(?:established|planned|proposed|exploring|none_identified)$")
    for sw in soup.select(".sw"):
        classes = [c for c in sw.get("class", []) if c != "sw"]
        if len(classes) != 1 or not status.match(classes[0]):
            errors.append(f"swatch that is not a status swatch: {classes}")
    for place in soup.select(".smark[data-state], #operating-legend, td[data-col=op]"):
        if place.select(".sw"):
            errors.append("Known activity carries a swatch; squares are for status only")
    visible = BeautifulSoup(str(soup), "html.parser")
    for node in visible.select("script, style, template"):
        node.decompose()
    glyphs = sorted(set(re.findall("[\u25A0\u25A1\u25AA\u25AB\u25CF\u25CB]", visible.get_text())))
    if glyphs:
        errors.append(f"square or circle glyph characters in text: {glyphs}")
    return errors


DASH_COLUMNS = ("inst", "since", "op", "eur", "staff")


def check_absence_wording(soup) -> list[str]:
    """Absence terms explain their meaning in focusable tooltips on cards and in the table."""
    errors = []
    places = {"Europe cards": "#entries", "The world": "#world-cards, #world-rows"}
    for place, selector in places.items():
        roots = soup.select(selector)
        found = {"Not published": 0, "None found yet": 0}
        for root in roots:
            for node in root.select(".missing:not(.na)"):
                found["Not published"] += 1
                if not tip_of(node).strip() or node.get("tabindex") != "0":
                    errors.append(f"{place}: a Not published value lacks a non-empty focusable tooltip")
            for node in root.select('.smark[data-state="not_confirmed"]'):
                found["None found yet"] += 1
                if not tip_of(node).strip() or node.get("tabindex") != "0":
                    errors.append(f"{place}: an activity absence value lacks a non-empty focusable tooltip")
        for term, count in found.items():
            if not count:
                errors.append(f"{place}: no {term} value to check its tooltip")
    marks = soup.select('#entries .statusgroup[data-status="none_identified"] > h3 .smark')
    if not marks:
        errors.append("Europe cards: no None identified group heading to check its tooltip")
    for mark in marks:
        if not tip_of(mark).strip() or mark.get("tabindex") != "0":
            errors.append("Europe cards: the None identified heading lacks a non-empty focusable tooltip")
    for key in DASH_COLUMNS:
        button = soup.select_one(f'table.world thead th[data-k="{key}"] button')
        if not button or not tip_of(button).strip():
            errors.append(f"table column {key}: header lacks a non-empty tooltip")
    for node in soup.select("#entries .card .missing.na, #world-cards .card .missing.na"):
        if not tip_of(node).strip():
            errors.append("a card dash lacks a non-empty tooltip")
    visible = BeautifulSoup(str(soup), "html.parser")
    for node in visible.select("#method-details, #rules, #limits, #changes, template, script, style, .srcs, .card.mini, #tip"):
        node.decompose()
    # dataset.method/missing-values requires Not published; other absence variants are owner copy.
    for cell in visible.select(".missing:not(.na)"):
        if cell.get_text(strip=True) != "Not published":
            errors.append(f"a missing value says {cell.get_text(strip=True)!r}, not Not published")
    return errors


FIGURE_LABELS = ("indirect or secondary", "secondary sources", "press report", "our estimate")
LOW_FIGURE_SOURCES = {"secondary_sources": "secondary sources", "press_report": "press report", "our_estimate": "our estimate"}


def figure_grade(record: dict) -> str | None:
    """The label a figure must carry: none when primary (high), indirect or secondary when medium, its own figure_source when low."""
    if record.get("confidence") == "medium":
        return "indirect or secondary"
    if record.get("confidence") == "low":
        return LOW_FIGURE_SOURCES.get(record.get("figure_source"), "unknown figure source")
    return None


def check_figure_grades(soup, data: dict) -> list[str]:
    """Two plain scales: every displayed figure is graded; a primary figure is unmarked, any other names its source type.

    Checked for Budget and Staff in the cards, the table and the funding chart.
    """
    errors = []
    jurisdictions = {j["id"]: j for j in data["jurisdictions"]}
    # No legend line: each source-type and confidence marker carries its own definition, wherever it shows.
    for root in soup.select("#entries, #world-cards, #world-rows, #fund-rows"):
        for line in root.select(".qualifiers, .fund-context, .funding-metadata"):  # whole lines: a label may be split across a wrap span
            text = " ".join(line.get_text().split())
            for label in (*FIGURE_LABELS, "high confidence", "medium confidence", "low confidence"):
                shown = len(re.findall(rf"(?<![A-Za-z]){label}(?![A-Za-z])", text))
                marked = sum("".join(t for t in a.find_all(string=True) if not t.find_parent(class_="cites")).split() == label.split() for a in line.select("abbr.conf"))
                if shown != marked:
                    errors.append(f"a source-type or confidence marker without its definition: {label!r} in {text[:60]!r}")
    for mark in soup.select("abbr.conf"):
        if not tip_of(mark):
            errors.append(f"marker {mark.get_text()!r} has no definition")
    # Current source-type grades still qualify every non-primary figure.
    words = list(FIGURE_LABELS)
    checked = expected_places = 0
    full_cards = Keyed(soup, ".card:not(.mini)[data-j]")
    table_rows = Keyed(soup, "#world-rows tr[data-j], #world-rest tr[data-j]")
    chart_rows = Keyed(soup, "#fund-rows .fund-detail[data-j]")
    for inst in data["institutes"]:
        jid, display = inst["jurisdiction_id"], inst["display"]
        if jid not in jurisdictions:
            continue
        shown = {}
        for kind, collection, key in (("Budget", "funding", "budget"), ("Staff", "staff", "staff"), ("total", "staff", "total_staff"), ("technical", "staff", "technical_staff")):
            identity = display.get(key)
            if identity:
                shown[kind] = display[f"{collection}_records"][identity]
        for kind, record in shown.items():
            if record.get("confidence") not in ("high", "medium", "low") or figure_grade(record) == "unknown figure source":
                errors.append(f"{jid}: displayed {kind} figure has no grade ({record.get('confidence')!r})")
        places = {"card Budget": None, "card Staff": None, "table Budget": None, "table Staff": None}
        card = full_cards.one(jid)
        if card:
            for label in ("Budget", "Staff"):
                places[f"card {label}"] = card.select_one(f'.row[data-track="{CARD_TRACKS[label]}"] .v')
        table = table_rows.one(jid)
        if table:
            places["table Budget"] = table.select_one('[data-col="budget"]')
            places["table Staff"] = table.select_one('[data-col="staff"]')
        for place, node in places.items():
            kind = place.split()[1]
            record = shown.get(kind)
            if record is None:
                continue
            if place.startswith("table") and jurisdictions[jid]["kind"] == "supranational":
                continue
            expected_places += 1
            if node is None:
                errors.append(f"{jid}: {place} cell not found")
                continue
            checked += 1
            text = " ".join(node.get_text(" ").split())
            found = [w.replace("\\b", "").replace("(?! )", "") for w in words if re.search(w, text)]
            want = [figure_grade(record)] if figure_grade(record) else []
            if found != want:
                errors.append(f"{jid}: {place} shows grade {found}, expected {want}")
        chart = chart_rows.one(jid, ".fund-staff-summary")
        total = shown.get("total")
        if total and not chart and any(r.select_one(f'[data-j="{jid}"]') for r in soup.select("#fund-rows")):
            errors.append(f"{jid}: funding Staff cell not found")
        if chart and total:
            context = " ".join(chart.select_one(".fund-context").get_text(" ").split()) if chart.select_one(".fund-context") else ""
            found = [w.replace("\\b", "").replace("(?! )", "") for w in words if re.search(w, context)]
            want = [figure_grade(total)] if figure_grade(total) else []
            if found != want:
                errors.append(f"{jid}: funding Staff shows grade {found}, expected {want}")
            # Source grades belong to the opened records; the compact figure describes its date and bound.
            tech = shown.get("technical")
            tech_lines = [" ".join(n.get_text(" ").split()) for n in chart.select(".fund-context") if "technical" in n.get_text()]
            if tech:
                expected_tech = figure_grade(tech)
                got = [w for w in FIGURE_LABELS if tech_lines and w in tech_lines[0]]
                if got != ([expected_tech] if expected_tech else []):
                    errors.append(f"{jid}: funding technical staff shows grade {got}, expected {expected_tech}")
    if checked < 10:
        errors.append(f"figure grade check covered only {checked} cells")
    return errors


def check_supporting_text(soup, data: dict) -> list[str]:
    """The headline links to the institute definition; Method shows the dataset definitions."""
    errors = []
    definition = soup.select_one("#headline + #definition-link")
    links = definition.select("a") if definition else []
    if len(links) != 1 or links[0].get("href") != "#definition" or soup.find(id="definition") is None or not links[0].get_text(strip=True):
        errors.append("under the headline there is not one link to an existing #definition")
    full = soup.select_one("#method-sec .section-body > #definition:first-child")
    links = full.select("a") if full else []
    texts = [t for t in full.find_all(string=True) if t.strip()] if full else []
    definition_text = "".join(t for t in texts if not t.find_parent("a")).strip()
    # The definition comes first and whole, its rule link after it: no link text before or inside the definition.
    in_link = [t.find_parent("a") is not None for t in texts]
    in_order = in_link == sorted(in_link)  # every text outside the link comes before every text inside it
    if not full or definition_text != data["dataset"]["definition"] or not in_order:
        errors.append("Method does not open with dataset.definition as the first paragraph of its body")
    elif len(links) != 1 or links[0].get("href") != "method.html#qualifying-body" or not links[0].get_text(strip=True):
        errors.append("the definition's full-rule link does not go to method.html#qualifying-body")
    definitions = data["dataset"]["status_definitions"]
    method = [li.get_text() for li in soup.select("#status-defs > li")]
    wanted = [f"{STATUS_LABELS[k]}. {v}" for k, v in definitions.items()]
    if method != wanted:
        errors.append("Method does not hold each full status definition once")
    return errors


# Section bar, in reader order (decisions: section bar order): each item stands for a group of sections and links to
# its first; an item is current while any of its sections is. A group of several sections has a quiet row above its
# first section that names the group as the bar does and links each section.
SECTION_BAR = (
    ("Map", ("map",)),
    ("Compare", ("functions", "testing", "set-up", "funding")),
    ("News", ("news",)),
    ("States", ("europe", "world")),
    ("Data", ("data", "method")),
)
GROUP_ROWS = {"Compare": ("Functions", "Testing", "Set-up", "Funding"), "States": ("Europe", "The world"), "Data": ("Downloads", "Method")}
SECTION_HEADINGS = ("map", "functions", "testing", "set-up", "funding", "news", "europe", "world", "data", "method")


def check_section_bar(soup) -> list[str]:
    """The section bar names the groups in the decided order after the back-to-top title; group rows link their sections."""
    nav = soup.select_one(".secnav")
    links = nav.select(".measure > a:not(.navtitle)") if nav else []
    got = [(a.get_text(" ", strip=True), tuple(a.get("data-sections", "").split())) for a in links]
    # The groups and the sections they hold, in order, each with a visible label; the labels' wording is reviewed.
    errors = [] if [m for _, m in got] == [m for _, m in SECTION_BAR] and all(label for label, _ in got) else [f"section bar holds {got}, expected the groups {[m for _, m in SECTION_BAR]}, each labelled"]
    errors += [f"section bar item {a.get_text(strip=True)} links {a.get('href')}, not its first section" for a in links if a.get("href") != "#" + a.get("data-sections", " ").split()[0]]
    errors += [f"heading #{key} is missing or empty" for key in SECTION_HEADINGS if not (node := soup.find(id=key)) or not node.get_text(" ", strip=True)]
    # The sections follow the bar: each group's sections exist and come after the ones before them in the page.
    order = {id(node): n for n, node in enumerate(soup.find_all(True))}
    keys = [key for _, members in SECTION_BAR for key in members]
    found = [(key, order[id(node)]) for key in keys if (node := soup.find(id=key)) is not None]
    errors += [f"section bar: #{b[0]} comes before #{a[0]} in the page" for a, b in zip(found, found[1:]) if b[1] < a[1]]
    # Group rows: directly before the first section of each group of several, the bar's label and one link per section.
    rows = soup.select("main nav.group-nav")
    if len(rows) != len(GROUP_ROWS):
        errors.append(f"{len(rows)} group rows, expected {len(GROUP_ROWS)}")
    for label, members in SECTION_BAR:
        if label not in GROUP_ROWS:
            continue
        first = soup.find(id=members[0])
        section = first.find_parent("section") if first else None
        row = section.find_previous_sibling() if section else None
        if not row or row.name != "nav" or "group-nav" not in row.get("class", []):
            errors.append(f"no group row before #{members[0]}")
            continue
        row_links = [(a.get_text(" ", strip=True), a.get("href")) for a in row.select("a")]
        if [href for _, href in row_links] != ["#" + key for key in members] or not all(text for text, _ in row_links):
            errors.append(f"group row {label} links {row_links}, expected one labelled link per section {['#' + key for key in members]}")
        if not row.get("aria-label", "").strip():
            errors.append(f"group row {label} lacks an accessible name")
    return errors


# Testing and access: four groups from the published model access value; both access values form the first group.
ACCESS_GROUPS = (
    ("privileged", ("recent_access", "earlier_access")),
    ("law", ("by_law",)),
    ("agreed", ("access_agreed",)),
    ("none", ("none_found",)),
)
ACT_SHORT = {"Regulation (EU) 2024/1689": "AI Act"}  # the page's short form for a cited act, held here independently
EVENT_KIND = {"pre_release": "access before public release", "deeper_than_public": "deeper access (fewer safeguards, or weights or internals)"}


def access_test_rank(institute: dict) -> int:
    """Group order, first key: in house, in house and other bodies, other bodies only, planned, not published."""
    testing = institute["testing_capacity"]
    codes = [mode["code"] for mode in testing.get("modes", [])]
    if codes:
        return 2 if "in_house" not in codes else 1 if len(codes) > 1 else 0
    return 3 if testing["state"] == "planned" else 4


def expected_access_tips(institute: dict) -> list[tuple[str, str | None]]:
    """The tooltip terms in an institute's note, in order: developer names, the provision, MOU."""
    access, value = institute["model_access"], institute["model_access"]["value"]
    if value in ("recent_access", "earlier_access"):
        latest = {}
        for event in access["access_events"]:
            if event["developer"] not in latest or event["date"] > latest[event["developer"]]["date"]:
                latest[event["developer"]] = event
        return [(d, f"{EVENT_KIND[latest[d]['kind']][0].upper()}{EVENT_KIND[latest[d]['kind']][1:]} to {latest[d]['model']} is the latest on record, on or before {expected_date(latest[d]['date'])}.") for d in sorted(sorted(latest), key=lambda d: latest[d]["date"], reverse=True)]
    if value == "by_law":
        power = access["legal_power"]
        return [(f"{ACT_SHORT.get(power['act'], power['act'])} {re.sub(r'^Article ', 'Art. ', power['provision'])}", None)]
    if value == "access_agreed":
        agreements = {a["id"]: a for a in institute["agreements"]}
        return [("MOU", None) for item in access["developer_agreements"] if re.search("memorandum of understanding", agreements[item["agreement_id"]]["scope"], re.I)]
    return []


def expected_access_note(institute: dict, data: dict) -> tuple[str, list[set]]:
    """The muted text beside an institute and the sources of each citation group in it, worked out from the record."""
    access, value = institute["model_access"], institute["model_access"]["value"]
    if value in ("recent_access", "earlier_access"):
        latest = {}
        for event in access["access_events"]:
            if event["developer"] not in latest or event["date"] > latest[event["developer"]]["date"]:
                latest[event["developer"]] = event
        developers = sorted(sorted(latest), key=lambda d: latest[d]["date"], reverse=True)  # latest first, ties by name
        cites = [set().union(*(latest[d]["citations"][f] for f in ("developer", "kind", "model", "date"))) for d in developers]
        if value == "earlier_access":  # the year of the latest event, cited from that event's date
            last = next(e for e in access["access_events"] if e["date"] == access["latest_event"])
            cites.append(set(last["citations"]["date"]))
        return ", ".join(developers) + (f", evidence from {access['latest_event'][:4]}" if value == "earlier_access" else ""), [c for c in cites if c]
    if value == "by_law":
        power = access["legal_power"]
        return f"{ACT_SHORT.get(power['act'], power['act'])} {re.sub(r'^Article ', 'Art. ', power['provision'])}", [set(power["citations"]["provision"]) | set(power["citations"]["conditions"])]
    if value == "access_agreed":
        agreements = {a["id"]: a for a in institute["agreements"]}
        parts, cites = [], []
        for item in access["developer_agreements"]:
            agreement = agreements[item["agreement_id"]]
            mou = re.search("memorandum of understanding", agreement["scope"], re.I)
            parts.append(f"{agreement['counterparty']}{' MOU' if mou else ''}, {expected_date(agreement['date'][:7])}")
            cites.append(set(item["citations"]["agreement_id"]) | set(agreement["citations"]["counterparty"]) | set(agreement["citations"]["date"]) | (set(agreement["citations"]["scope"]) if mou else set()))
        return ", ".join(parts), cites
    testing, labels = institute["testing_capacity"], data["dataset"]["vocabularies"]["testing_capacity"]
    if testing.get("modes"):
        names = [labels[mode["code"]]["label"] for mode in testing["modes"]]
        return ", ".join([names[0]] + [n[0].lower() + n[1:] for n in names[1:]]), [set(mode["citations"]["code"]) for mode in testing["modes"]]
    label = labels[testing["state"]]["label"]
    return (label if label.startswith("Test") else "Testing " + label.lower()), [set(testing["citations"]["state"])] if testing["citations"]["state"] else []


def check_access_groups(soup, data: dict) -> list[str]:
    """Testing and access: group membership, order, marks, links and counterpart or testing text, each from aisi.json here."""
    errors, status_order = [], ("established", "planned", "proposed", "exploring", "none_identified")
    jurisdictions = {j["id"]: j for j in data["jurisdictions"]}
    ids = {str(node["id"]) for node in soup.select("[id]")}
    blocks = soup.select("#access .group-block")
    wanted = [key for key, values in ACCESS_GROUPS if any(i["model_access"]["value"] in values for i in data["institutes"])]
    if [b.get("data-group") for b in blocks] != wanted:
        errors.append(f"access groups {[b.get('data-group') for b in blocks]}, expected {wanted}")
    for key, values in ACCESS_GROUPS:
        block = soup.select_one(f'#access .group-block[data-group="{key}"]')
        if block is None:
            continue
        heading = block.select_one("h3")
        if not heading or not heading.get_text(" ", strip=True):
            errors.append(f"access group {key} heading is missing or empty")
        if key == "privileged":
            term = heading.select_one("abbr.g") if heading else None
            if not term or not term.get_text(strip=True) or not tip_of(term).strip() or term.get("tabindex") != "0":
                errors.append("privileged access heading lacks a non-empty term and focusable tooltip")
        members = sorted(
            (i for i in data["institutes"] if i["model_access"]["value"] in values),
            key=lambda i: (access_test_rank(i), status_order.index(jurisdictions[i["jurisdiction_id"]]["status"]), jurisdictions[i["jurisdiction_id"]]["name"], i["jurisdiction_id"]),
        )
        rows = block.select(".group-list > li")
        shown = [li.get("data-state") for li in rows]
        if shown != [i["jurisdiction_id"] for i in members]:
            errors.append(f"access group {key} lists {shown}, expected {[i['jurisdiction_id'] for i in members]}")
            continue
        mark_row = block.select_one(".marks")
        label = f"{len(members)} institute{'' if len(members) == 1 else 's'}: " + "; ".join(f"{jurisdictions[i['jurisdiction_id']]['name']}, {STATUS_LABELS[jurisdictions[i['jurisdiction_id']]['status']].lower()}" for i in members)
        if not mark_row or mark_row.get("role") != "img" or mark_row.get("aria-label") != label:
            errors.append(f"access group {key}: marks are not one image named with every member's status")
        marks = [m.get("class", []) for m in block.select(".marks > i")]
        if marks != [["sw", "st-" + jurisdictions[i["jurisdiction_id"]]["status"]] for i in members]:
            errors.append(f"access group {key}: marks {marks} differ from its members' statuses")
        tips = [(tip_of(m), m.get("data-tip-sub"), m.get("title")) for m in block.select(".marks > i")]
        if tips != [(jurisdictions[i["jurisdiction_id"]]["name"], STATUS_LABELS[jurisdictions[i["jurisdiction_id"]]["status"]], None) for i in members]:
            errors.append(f"access group {key}: a status square's tooltip is not the map dots' form (name, then the status line): {tips}")
        for li, institute in zip(rows, members):
            jid = institute["jurisdiction_id"]
            link = li.select_one("a")
            if not link or link.get_text(strip=True) != jurisdictions[jid]["name"] or link.get("href") != f"#c-{jid.lower()}" or link["href"][1:] not in ids:
                errors.append(f"access group {key}: {jid} is not a link to its card")
            note = copy.copy(li.select_one(".group-note"))
            if note is None:
                errors.append(f"access group {key}: {jid} has no note")
                continue
            groups = [{a.get("data-source-id") for a in c.select("a.cn")} for c in note.select(".cites")]
            tips = [(t.get_text(), tip_of(t)) for t in note.select("abbr.g")]
            expected_tips = expected_access_tips(institute)
            if len(tips) != len(expected_tips) or any(term != expected_term or not (tip or "").strip() or (expected_tip is not None and tip != expected_tip) for (term, tip), (expected_term, expected_tip) in zip(tips, expected_tips)) or any(t.get("tabindex") != "0" for t in note.select("abbr.g")):
                errors.append(f"access group {key}: {jid} tooltips {tips}, expected {expected_access_tips(institute)}")
            for cites in note.select(".cites"):
                cites.decompose()
            text, expected_cites = expected_access_note(institute, data)
            if note.get_text() != text:
                errors.append(f"access group {key}: {jid} note {note.get_text()!r}, expected {text!r}")
            if groups != expected_cites:
                errors.append(f"access group {key}: {jid} citations {groups}, expected {expected_cites}")
    return errors


FIGURE_NAMES = {"GB": "UK"}  # short state names in the figures row, held here independently of the page


FIGURES_NOTE = "Counts cover the {n} established state institutes."  # owner-approved copy (figures row) when every institute is in one of the numbers; n computed from the data
FIGURES_NO_BUDGET = "established institutes publish no budget."  # owner copy, 1.0.3
# The (i) when established institutes show a budget that is not compared, so they count in neither the budget median nor the no-budget figure; reasons as the funding section's.
FIGURES_LEFT_ONE = FIGURES_NOTE + " {names}'s budget is {reason}, so it counts neither in the budget median nor among those with no budget."
FIGURES_LEFT_MANY = FIGURES_NOTE + " The budgets of {names} are {reasons}, so they count neither in the budget median nor among those with no budget."
FIGURES_REASONS = {"proposal": ("a proposal", "proposals"), "one_off": ("a one-off amount", "one-off amounts"), "other": ("not for the reference year", "not for the reference year")}


def check_figures_row(soup, data: dict) -> list[str]:
    """The figures below the map: median budget and staff with n and the highest, and how many established institutes have no budget, from aisi.json and the cards.
    The note under the row says the counts are of established state institutes, so every median input must be one."""
    jurisdictions = {j["id"]: j for j in data["jurisdictions"]}
    bars, totals, counted, upper, lower = [], [], [], False, False
    unestablished, left = [], []
    for inst in data["institutes"]:
        if jurisdictions[inst["jurisdiction_id"]]["kind"] == "supranational":
            continue  # counts count states only
        display = inst["display"]
        funding = display["funding_records"].get(display["comparison_funding"]) if display.get("comparison_funding") else None
        if funding and funding.get("eur_per_year_est") is not None:
            bars.append((expected_annual_amount(funding, data), inst["id"], inst["jurisdiction_id"], funding["display"]["value"].split(" (")[0]))
            upper |= funding.get("amount_qualifier") in ("at_most", "less_than", "nearly")
            lower |= funding.get("amount_qualifier") in ("at_least", "more_than")
        staff = display["staff_records"].get(display["total_staff"]) if display.get("total_staff") else None
        if staff and staff.get("total") is not None:
            totals.append((Decimal(staff["total"]), inst["id"], inst["jurisdiction_id"], staff["display"]["total"]))
        if inst.get("status") == "established":
            counted.append(inst["jurisdiction_id"])
            shown = display["funding_records"].get(display.get("budget")) if display.get("budget") else None
            if shown and not display.get("comparison_funding"):
                reason = "proposal" if shown.get("commitment") == "proposed" else "one_off" if shown.get("kind") == "one_off" else "other"
                left.append((FIGURE_NAMES.get(inst["jurisdiction_id"], jurisdictions[inst["jurisdiction_id"]]["name"]), reason))
        elif (funding and funding.get("eur_per_year_est") is not None) or (staff and staff.get("total") is not None):
            unestablished.append(inst["id"])
    def words(node):  # the figure's text without its (i), which joins the last word in one unbreakable box
        return " ".join(s.strip() for s in node.find_all(string=True) if s.strip() and not s.find_parent(class_="info"))

    figures = soup.select("#figures > .figure")
    if not figures:
        return zero_match_errors("figures_row", "#figures > .figure", 0)
    errors = []
    for n, tile in enumerate(figures, 1):
        for selector in ("b", "span"):
            errors.extend(zero_match_errors("figures_row", f"#figures > .figure:nth-child({n}) {selector}", len(tile.select(selector))))
    if errors:
        return errors
    tiles = [(t.select_one("b").get_text(strip=True), words(t.select_one("span"))) for t in figures]
    if len(tiles) != 3:
        return [f"figures row has {len(tiles)} figures, expected budget, staff and no budget"]
    errors = []

    def middle(rows):
        values = sorted(r[0] for r in rows)
        k = len(values) // 2
        return values[k] if len(values) % 2 else (values[k - 1] + values[k]) / 2

    for (figure, text), rows, label, noun in zip(tiles, (bars, totals), ("budget", "staff"), ("median annual budget", "median staff")):
        if "~" in figure:
            errors.append(f"{label} median figure {figure!r} carries ~; medians in the figures row are never marked (dataset.method/currency-rounding)")
        if len(rows) < 2:
            errors.append(f"{label} figure shown with fewer than two institutes")
            continue
        top = max(rows, key=lambda r: (r[0], r[1]))
        # A qualifier is part of the figure, so lower case wherever it stands (display.py AMOUNT_PREFIX); held here independently.
        highest = re.sub(r"^(Up to|At least|About|Nearly|More than|Less than)\b", lambda m: m[0].lower(), top[3])
        want = f"{noun} of {len(rows)} institutes. {FIGURE_NAMES.get(top[2], jurisdictions[top[2]]['name'])} highest at {highest}."  # owner copy, 1.0.3: the named state is the highest
        if text != want:
            errors.append(f"{label} figure text {text!r}, expected {want!r}")
        # A median over upper-bound budgets is an upper bound and rounds up, over lower bounds a lower bound rounding down (dataset.method budget-ratios).
        bound = None if label != "budget" else "at_most" if upper and not lower else "at_least" if lower and not upper else None
        median = half_up_two(middle(rows), bound)
        if label == "budget":
            expected = {"at_most": "up to ", "at_least": "at least "}.get(bound, "") + "EUR " + short_amount(median)
        else:
            expected = format(int(median) if median == median.to_integral_value() else median.normalize(), ",")
        if figure != expected:
            errors.append(f"{label} median figure {figure!r} differs from the data median {expected!r}")
            continue
        tile = soup.select("#figures > .figure")[0 if label == "budget" else 1]
        tip = tile.select_one("b > abbr.g")
        if bound == "at_most" and (not tip or tip_of(tip) != MEDIAN_CEILING or tip.get_text(strip=True) != figure):
            errors.append(f"{label} median {figure!r} is a ceiling but lacks the tooltip {MEDIAN_CEILING!r}")
        elif bound != "at_most" and tip:
            errors.append(f"{label} median {figure!r} is not a ceiling but carries a tooltip")
    # No budget: the established state institutes whose card shows the Budget row as Not published.
    missing = 0
    cards = Keyed(soup, ".card[data-j]")
    for jid in counted:
        row = cards.one(jid, f'.row[data-track="{CARD_TRACKS["Budget"]}"]')
        value_node = row.select_one(".v") if row else None
        errors.extend(zero_match_errors("figures_row", f'.card[data-j="{jid}"] .row[data-track="{CARD_TRACKS["Budget"]}"] .v', int(value_node is not None)))
        value = value_node.get_text(" ", strip=True) if value_node else ""
        missing += value == MISSING_TEXT
    want = (f"{missing} of {len(counted)}", FIGURES_NO_BUDGET)
    if tiles[2] != want:
        errors.append(f"no-budget figure {tiles[2]}, expected {want}")
    if unestablished:
        errors.append(f"figures row medians include institutes that are not established ({', '.join(unestablished)}); the note says counts are of established state institutes")
    note = soup.select("#figures > .figure:last-child > span > .info-glue:last-child > button.info#figures-scope:last-child")
    names = [name for name, _ in left]
    names = " and ".join([", ".join(names[:-1]), names[-1]]) if len(names) > 1 else "".join(names)
    reasons = {reason for _, reason in left}
    want_note = (
        FIGURES_NOTE.format(n=len(counted)) if not left
        else FIGURES_LEFT_ONE.format(n=len(counted), names=names, reason=FIGURES_REASONS[left[0][1]][0]) if len(left) == 1
        else FIGURES_LEFT_MANY.format(n=len(counted), names=names, reasons=FIGURES_REASONS[left[0][1]][1] if len(reasons) == 1 else "not compared")
    )
    if len(note) != 1 or tip_of(note[0]) != want_note or len(soup.select("#figures-scope")) != 1:
        errors.append(f"figures row: the last figure does not end with one (i) reading {want_note!r}")
    return errors


# Institutional set-up (decisions: copy from Bahadir's feedback): every institute by form (principle: Scope), host as the note.
FORM_LABELS = {"unit_in_existing_body": "Unit in an existing body", "federation_of_existing_bodies": "Network of existing bodies", "university_centre": "University centre", "new_body": "New body"}
FORM_UNKNOWN = "Legal form not published"
PER_PERSON_TIP = "Annual budget divided by population. For scale only, since budgets cover different work and costs differ between states."


def check_setup_groups(soup, data: dict) -> list[str]:
    """Institutional set-up: heading, groups by form (larger first, Legal form not published last), members, marks, links, host notes and citations, from aisi.json."""
    errors, jurisdictions = [], {j["id"]: j for j in data["jurisdictions"]}
    heading = soup.select_one("#set-up")
    if not heading or not heading.get_text(strip=True):
        errors.append("set-up heading is missing or empty")
    groups = {}
    for inst in data["institutes"]:  # every institute, whatever its status (principle: Scope)
        groups.setdefault(inst["legal_form"] if inst["legal_form"] in FORM_LABELS else "unknown", []).append(inst)
    order = sorted(groups, key=lambda f: (f == "unknown", -len(groups[f]), FORM_LABELS.get(f, FORM_UNKNOWN)))
    # Laid out as Testing and access (principle: Groups): the largest group alone in the long column, the others stacked before it.
    blocks = soup.select("#setup .groups-short > .group-block") + soup.select("#setup .groups-long > .group-block")
    if [b.get("data-form") for b in blocks] != order[1:] + order[:1] or len(soup.select("#setup .group-block")) != len(order):
        return errors + [f"set-up groups {[b.get('data-form') for b in blocks]} (short column, then long), expected {order[1:] + order[:1]}"]
    blocks = [blocks[-1]] + blocks[:-1]  # back to size order
    by_id = Keyed(soup, "[id]", "id")
    for block, form in zip(blocks, order):
        # Squares and list alike by status on the ladder, then name (principle: Groups).
        members = sorted(groups[form], key=lambda i: (STATUS_ORDER.index(jurisdictions[i["jurisdiction_id"]]["status"]), jurisdictions[i["jurisdiction_id"]]["name"], i["jurisdiction_id"]))
        rows = block.select(".group-list > li")
        if [li.get("data-state") for li in rows] != [i["jurisdiction_id"] for i in members]:
            errors.append(f"set-up group {form} lists {[li.get('data-state') for li in rows]}")
            continue
        if [m.get("class", []) for m in block.select(".marks > i")] != [["sw", f"st-{jurisdictions[i['jurisdiction_id']]['status']}"] for i in members]:
            errors.append(f"set-up group {form}: marks differ from its members")
        for li, inst in zip(rows, members):
            jid = inst["jurisdiction_id"]
            link = li.select_one("a")
            if not link or link.get("href") != f"#c-{jid.lower()}" or not by_id.one(f"c-{jid.lower()}"):
                errors.append(f"set-up group {form}: {jid} is not a link to its card")
            note = copy.copy(li.select_one(".group-note"))
            cites = [{a.get("data-source-id") for a in c.select("a.cn")} for c in note.select(".cites")] if note else []
            for c in note.select(".cites") if note else []:
                c.decompose()
            want_cites = [set(inst["citations"]["legal_form"]) | set(inst["citations"]["host"])]
            if not note or note.get_text().strip() != (inst["host"] or MISSING_TEXT) or cites != [c for c in want_cites if c]:
                errors.append(f"set-up group {form}: {jid} note or citations differ from the host record")
    return errors


def check_funding_key(page) -> list[str]:
    """The funding key labels its swatches adopted and announced; bars carry the minimum-width caveat."""
    got = page.evaluate("""() => {
      const key = [...document.querySelectorAll('.fund-key .key-item')];
      const clear = key.every(k => { const sw = k.querySelector('.key-swatch').getBoundingClientRect(), r = document.createRange(); r.selectNodeContents(k); r.setStartAfter(k.querySelector('.key-swatch')); const t = r.getBoundingClientRect(); return sw.width > 0 && sw.right <= t.left + .5; });
      return { key: key.map(k => k.textContent.trim()), clear,
        budgetOnly: !!document.querySelector('.fund-budget-head .fund-key') && !document.querySelector('.fund-staff-head .fund-key'),
        tips: [...document.querySelectorAll('#fund-rows .fund-bar:has(.bar-value)')].map(b => b.dataset.tip || b.getAttribute('aria-description')) };
    }""")
    errors = []
    if got["key"] != ["adopted", "announced"]:
        errors.append(f"funding key is not two swatches labelled adopted and announced: {got['key']}")
    if not got["clear"]:
        errors.append("funding key: a swatch is hidden or overlapped by its label")
    if not got["budgetOnly"]:
        errors.append("funding adopted/announced key must belong to Budget only")
    # Separate charts replace the former paired labelled cells; each head labels its one measure.
    ragged = page.evaluate("""() => [...document.querySelectorAll('#fund-rows .fund-item')].filter(row => {
      const chart=row.closest('.fund-list'),kind=chart?.dataset.kind;
      return !['budget','staff'].includes(kind) || row.querySelectorAll(':scope > .fund-measure').length!==1 || !row.querySelector(':scope > .fund-'+kind) || chart.querySelector('h3')?.textContent.trim()!==kind[0].toUpperCase()+kind.slice(1);
    }).map(row => row.dataset.j)""")
    if ragged:
        errors.append(f"funding chart rows lack their own labelled measure: {ragged}")
    if not got["tips"] or any(not (t or "").strip() for t in got["tips"]):
        errors.append("funding bars lack a non-empty tooltip")
    return errors


def check_per_person(soup, data: dict) -> list[str]:
    """Compared established state budgets retain per-resident figures and years in their opened records."""
    errors, jurisdictions = [], {j["id"]: j for j in data["jurisdictions"]}
    for node in soup.select('#fund-rows, #fund-rows *, .fund-detail, .fund-detail *'):
        if any("per person" in str(value).lower() for value in [*node.attrs.values(), *node.find_all(string=True, recursive=False)]):
            errors.append("funding view retains per person in text or an accessible name/tooltip")
    for node in soup.select(".per-person"):
        if node.name != "abbr" or "g" not in node.get("class", []) or tip_of(node) != PER_PERSON_TIP or node.get("tabindex") != "0":
            errors.append(f"per-person figure {node.get_text()!r} lacks the focusable caveat tooltip {PER_PERSON_TIP!r}")
    shown = 0
    fund_items = Keyed(soup, ".fund-detail[data-j]")
    for inst in established_funding_institutes(data):
        jurisdiction = jurisdictions[inst["jurisdiction_id"]]
        record = inst["display"]["funding_records"].get(inst["display"].get("comparison_funding") or "")
        row = fund_items.one(jurisdiction["id"], ".fund-budget-summary")
        node = row.select_one(".qualifiers .per-person") if row else None
        want = expected_budget_ratios(record, jurisdiction, data)[2] if record and jurisdiction["kind"] == "state" and record.get("eur_per_year_est") is not None else None
        if want:
            want = want.removesuffix("per person") + f"per resident ({jurisdiction['population']['year']})"
        if (node.get_text() if node else None) != want:
            errors.append(f"{jurisdiction['id']}: per resident {node.get_text() if node else None!r}, expected {want!r}")
        if node:
            qualifiers = copy.deepcopy(row.select_one(".qualifiers"))
            for citation in qualifiers.select(".cites"):
                citation.decompose()
            if not qualifiers.get_text().strip().endswith(node.get_text().strip()):
                errors.append(f"{jurisdiction['id']}: per resident must remain the last Budget qualifier in the records")
        shown += want is not None
    if not shown:
        errors.append("no per-person figure shown")
    return errors


def check_state_download_buttons(html: str, dist: Path, data: dict) -> list[str]:
    """Every card (a full card in its title row, a mini card in its fold) has an icon-only download of its state's API file, which exists and holds that state."""
    soup, errors = BeautifulSoup(html, "html.parser"), []
    major = data["dataset"]["version"].split(".")[0]
    for card in soup.select(".card[data-j]"):
        jid = card["data-j"]
        link = card.select_one(":scope > .foot > a.state-dl" if "mini" in card.get("class", []) else ":scope > .top > a.state-dl")
        path = f"api/v{major}/jurisdictions/{jid}.json"
        if not link or link.get("href") != path or link.get("download") != f"{jid}.json" or [n for n in link.find_all(string=True) if n.strip() and not n.find_parent(class_="vh")] or not tip_of(link).strip() or not link.get_text(strip=True) or "i-dl" not in link.get("class", []):
            errors.append(f"card {jid}: download button missing or not the decided icon, path and label")
            continue
        file = dist / path
        if not file.is_file() or json.loads(file.read_text(encoding="utf-8")).get("record", {}).get("id") != jid:
            errors.append(f"card {jid}: download {path} missing or holds another state")
    return errors


ACCESS_LAYOUT_JS = """(sel) => {
  const box = document.querySelector(sel), short = box.querySelector('.groups-short'), long = box.querySelector('.groups-long');
  const sr = short.getBoundingClientRect(), lr = long.getBoundingClientRect();
  const groups = [...box.querySelectorAll('.group-block')].map(g => { const r = g.getBoundingClientRect(); return { top: r.top, bottom: r.bottom }; });
  return { groups, shortBottom: sr.bottom, longTop: lr.top };
}"""


def check_access_tips(page) -> list[str]:
    """Every tooltip term in Testing and access opens its definition on focus, hover and tap, and Escape closes it."""
    errors = []
    terms = page.locator("#access .group-block abbr.g:visible")
    for n in range(terms.count()):
        term = terms.nth(n)
        want, label = term.evaluate("n => n.dataset.tip || n.dataset.exp || n.getAttribute('aria-description') || ''"), term.inner_text()
        for how in ("focus", "hover", "tap"):
            page.keyboard.press("Escape")
            page.mouse.move(0, 0)
            term.scroll_into_view_if_needed()
            if how == "focus":
                term.focus()
            elif how == "hover":
                term.hover()
            else:
                term.dispatch_event("click")
            shown = page.evaluate("() => { const t = document.getElementById('tip'); return t && !t.hidden ? t.textContent : null; }")
            if shown != want:
                errors.append(f"access tooltip {label!r} on {how} shows {shown!r}")
        page.keyboard.press("Escape")
        if page.evaluate("() => { const t = document.getElementById('tip'); return !!t && !t.hidden; }"):
            errors.append(f"access tooltip {label!r} stays open after Escape")
        term.evaluate("n => n.blur()")
    page.mouse.move(0, 0)
    return errors


FIGURES_NOTE_JS = """() => {
  const note = document.getElementById('figures-scope');
  if (!note) return null;
  const r = note.getBoundingClientRect(), ns = getComputedStyle(note);
  return {visible: r.width > 0 && r.height > 0 && ns.visibility === 'visible' && parseFloat(ns.opacity) > 0};
}"""


def figures_note_layout_errors(m: dict | None) -> list[str]:
    """The figures row's counts (i) is shown."""
    if m is None:
        return ["figures row: counts (i) missing"]
    return [] if m["visible"] else ["figures row: counts (i) is not visible"]


def one_column_errors(name: str, m: dict, width: int) -> list[str]:
    """The short and long groups must not overlap."""
    errors = []
    if m["longTop"] < m["shortBottom"] - 1:  # overlapping groups cannot be read
        errors.append(f"{width}px: the long {name} group overlaps the short ones (short bottom {m['shortBottom']}, long top {m['longTop']})")
    groups = m["groups"]
    if any(g["top"] < p["bottom"] - 1 for p, g in zip(groups, groups[1:])):
        errors.append(f"{width}px: a {name} group starts above the end of the one before (overlap): {[(round(g['top']), round(g['bottom'])) for g in groups]}")
    return errors


def check_access_layout(page, width: int) -> list[str]:
    """Testing and access and Set-up stack full-width groups and row lists at every width."""
    errors = []
    for name, sel in (("access", "#access"), ("set-up", "#setup")):
        m = page.evaluate(ACCESS_LAYOUT_JS, sel)
        errors += one_column_errors(name, m, width)
    errors += check_funding_key(page)
    errors += figures_note_layout_errors(page.evaluate(FIGURES_NOTE_JS))
    if width == 390:  # the phone layout's upper edge
        size = page.viewport_size
        page.set_viewport_size({"width": 640, "height": size["height"]})
        page.wait_for_timeout(100)
        for name, sel in (("access", "#access"), ("set-up", "#setup")):
            errors += one_column_errors(name, page.evaluate(ACCESS_LAYOUT_JS, sel), 640)
        page.set_viewport_size(size)
        page.wait_for_timeout(100)
    return errors


GENERIC_PARTNER_HOW = {"Is listed among the institute's partners.", "Is named as a technical partner that the institute works with."}  # owner decision 05.11.2026: the page shows these partners as names only
PLURAL_PARTNER_HOW = {
    "Is to be tied to the institute by agreement and can contribute staff, computing capacity and specialist knowledge.": "Are to be tied to the institute by agreement and can contribute staff, computing capacity and specialist knowledge.",
    "Is named among the institute’s partners whose agreements enable evaluations of the latest AI capabilities.": "Are named among the institute’s partners whose agreements enable evaluations of the latest AI capabilities.",
    "Is one of the national evaluation and security bodies the institute federates without a new legal structure.": "Are among the national evaluation and security bodies the institute federates without a new legal structure.",
    "Takes part in the institute as a participating organisation of the AISI Partnership Agreement.": "Take part in the institute as participating organisations of the AISI Partnership Agreement.",
    "Has initial representation on the TRAINS Taskforce chaired by the then US AI Safety Institute.": "Have initial representation on the TRAINS Taskforce chaired by the then US AI Safety Institute.",
    "Collaborated with the institute and other agencies on the Rapid Review of an AI-driven cyber incident.": "Collaborated with the institute and other agencies on the Rapid Review of an AI-driven cyber incident.",
}  # independent copy oracle, including agreement inside the sentence
FIRST_OUTPUT_LINE = "First published work credited to the institute."  # owner-approved development line (decisions 21 Oct), one template for every first output


def first_output_data_errors(data: dict) -> list[str]:
    """Every generated first-output event carries the approved development line."""
    return [
        f"{event['id']}: first-output development line {event.get('short')!r}, expected {FIRST_OUTPUT_LINE!r}"
        for j in data["jurisdictions"]
        for event in j.get("timeline", [])
        if event["event"] == "first_output" and event.get("short") != FIRST_OUTPUT_LINE
    ]


def first_output_part_errors(parts: list[str], events: list[dict]) -> list[str]:
    """A development row's items: each first-output part reads the approved line, wherever it falls in a merged row."""
    if len(parts) == len(events) and all(part == FIRST_OUTPUT_LINE for part, event in zip(parts, events) if event["event"] == "first_output"):
        return []
    if not any(event["event"] == "first_output" for event in events):
        return []
    return [f"development line for a first output does not read {FIRST_OUTPUT_LINE!r}: {parts}"]


def check_cite(soup, dataset: dict) -> list[str]:
    """Cite as: dataset.citation verbatim, its closing DOI link (dataset.doi) the one link."""
    cite = soup.select_one("#cite")
    if cite:  # the external-link hint ("opens in a new tab") is not citation text
        cite = BeautifulSoup(str(cite), "html.parser").select_one("#cite")
        for hint in cite.select(".vh"):
            hint.decompose()
    if not cite or cite.get_text() != dataset["citation"]:
        return ["Cite as is not dataset.citation verbatim"]
    links = [(a.get("href"), a.get_text()) for a in cite.select("a")]
    doi = f"https://doi.org/{dataset['doi']}" if dataset.get("doi") else None
    if links != ([(doi, doi)] if doi else []) or (doi and not dataset["citation"].endswith(" " + doi)):
        return [f"Cite as: expected the DOI link {doi!r} at its end and no other link, got {links}"]
    return []


def expected_news_groups(data: dict) -> list[dict]:
    """Independent News oracle: canonical records, never the collector or events.news_kind.

    Budget amounts use the card's native label, independently of the News sentence.
    Registry target order supplies the order of the two state names.
    """
    jurisdictions = {j["id"]: j for j in data["jurisdictions"]}
    institutes = {i["id"]: i for i in data.get("institutes", [])}
    clauses = []
    for jid, jurisdiction in jurisdictions.items():
        for event in sorted(jurisdiction.get("timeline", []), key=lambda e: e["id"]):
            if event["event"] not in {
                "commitment", "proposed", "designated", "launched", "first_output",
                "exploration_started", "withdrawn", "closed", "named", "mandate_changed",
            }:
                continue
            text = event["news"] if "news" in event else (event.get("short") or "").strip()
            if not text:
                description = event.get("description", "").strip()
                text = description
                for end in re.finditer(r"[.!?][\"')\]]*(?=\s|$)", description):
                    candidate = description[:end.end()]
                    token = candidate.split()[-1].rstrip("\"')]").lower()
                    if token in {"mr.", "mrs.", "ms.", "dr.", "prof.", "st.", "e.g.", "i.e."} or re.fullmatch(r"(?:[a-z]\.)+", token):
                        continue
                    text = candidate
                    break
            sources = citation_fields(event, ["news"]) if "news" in event else expected_event_sources(data, jurisdiction, event)
            clauses.append((jid, event["date"], event["id"], f"timeline:{jid}:{event['id']}", text, event, sources, None))
    registered = {}
    for claim in data["dataset"].get("shared_claims", []):
        if claim["kind"] != "agreement":
            continue
        # A pair counts only when both parties and their states are in the data (a pruned fixture may hold one).
        if not all(target[1]["id"] in institutes and institutes[target[1]["id"]]["jurisdiction_id"] in jurisdictions for target in claim["targets"]):
            continue
        states = [institutes[target[1]["id"]]["jurisdiction_id"] for target in claim["targets"]]
        pair = {"id": claim["id"], "states": states, "names": [jurisdictions[jid]["name"] for jid in states]}
        for target in claim["targets"]:
            registered[(target[1]["id"], target[3]["id"])] = pair
    for iid, institute in sorted(institutes.items()):
        jid = institute["jurisdiction_id"]
        if jid not in jurisdictions:  # an institute whose state is not in the data (a pruned fixture) has no News row
            continue
        for budget in sorted(institute.get("funding", []), key=lambda b: b["id"]):
            if not budget.get("decision_date") or budget.get("commitment") not in ("announced", "adopted") or budget.get("coverage") not in ("whole_institute", "component"):
                continue
            text = budget["news"] if "news" in budget else f"{budget['display']['native_value']} for the institute {budget['commitment']}."
            sources = citation_fields(budget, ["news"] if "news" in budget else ["amount", "currency", "amount_qualifier", "commitment", "coverage", "decision_date"])
            clauses.append((jid, budget["decision_date"], budget["id"], f"funding:{iid}:{budget['id']}", text, None, sources, None))
        for agreement in sorted(institute.get("agreements", []), key=lambda a: a["id"]):
            if not agreement.get("date"):
                continue
            pair = registered.get((iid, agreement["id"]))
            if "news" in agreement:
                text = agreement["news"]
            elif agreement["state"] == "pledged":
                text = f"Pledge announced by {agreement['counterparty']}."
            elif agreement.get("participation") == "named_implementer":
                verb = "sign" if agreement["state"] == "signed" else "announce"
                text = (f"The two governments {verb} a text on cooperation naming their institutes." if pair
                        else f"Governments {verb} a text on cooperation naming the institute.")
            else:
                text = f"Agreement {agreement['state']} with {agreement['counterparty']}."
            sources = citation_fields(agreement, ["news"] if "news" in agreement else ["state", "counterparty", "date", "participation"])
            clauses.append((jid, agreement["date"], agreement["id"], f"agreements:{iid}:{agreement['id']}", text, None, sources, pair))
    # Independently enforce the method's weight order, then owner and record ID.
    def weight(clause):
        event = clause[5]
        return (1 if event["event"] in {"mandate_changed", "named"} else 0) if event else (2 if clause[3].startswith("funding:") else 3)

    clauses.sort(key=lambda c: (weight(c), c[3].split(":")[1], c[2]))
    groups = {}
    for jid, day, identity, item, text, event, sources, pair in clauses:
        key = (jid, day, item if len(day) < 10 else "")
        group = groups.setdefault(key, {"jurisdiction": jid, "name": jurisdictions[jid]["name"], "date": day, "order": identity if event is not None else item, "events": [], "items": [], "texts": [], "pairs": [], "line_source_ids": [], "source_ids": [], "timeline": []})
        group["items"].append(item)
        group["texts"].append(text)
        group["pairs"].append(pair)
        group["line_source_ids"].append(list(dict.fromkeys(sources)))
        group["source_ids"] = list(dict.fromkeys(group["source_ids"] + sources))
        if event is not None:
            group["events"].append(identity)
            group["timeline"].append(event)
    ordered = sorted(groups.values(), key=lambda g: (g["jurisdiction"], g["items"][0].split(":", 2)[2], g["items"]))
    return sorted(ordered, key=lambda g: g["date"], reverse=True)


def expected_news_page(groups: list[dict]) -> list[dict]:
    """Project canonical groups using each pair's first position, not page metadata."""
    first = {}
    for pos, group in enumerate(groups):
        for n, pair in enumerate(group["pairs"]):
            if pair:
                first.setdefault(pair["id"], (pos, n))
    entries = []
    for pos, group in enumerate(groups):
        keep = [n for n, pair in enumerate(group["pairs"]) if pair is None or first[pair["id"]] == (pos, n)]
        if not keep:
            continue
        pairs = [group["pairs"][n] for n in keep if group["pairs"][n]]
        names = {}
        for pair in pairs:
            for state, name in zip(pair["states"], pair["names"], strict=True):
                names.setdefault(state, name)
        names.setdefault(group["jurisdiction"], group["name"])
        labels = list(names.values())
        label = ", ".join(labels[:-1]) + " and " + labels[-1] if len(labels) > 1 else labels[0]
        sources = list(dict.fromkeys(sid for n in keep for sid in group["line_source_ids"][n]))
        entries.append({**group, "position": pos, "name": label, "texts": [group["texts"][n] for n in keep], "source_ids": sources, "pair_ids": [p["id"] for p in pairs]})
    return entries[:10]


def check_news(soup, data: dict) -> list[str]:
    """Check published identities and page rows against the independent News selection."""
    errors = first_output_data_errors(data)
    all_groups = expected_news_groups(data)
    page = expected_news_page(all_groups)
    cutoff = page[-1]["position"] + 1 if page else 0
    shown_pairs = {identity for g in page for identity in g["pair_ids"]}
    ordered = all_groups[:cutoff] + [g for g in all_groups[cutoff:] if any(p and p["id"] in shown_pairs for p in g["pairs"])]
    published = data["dataset"].get("display", {}).get("developments", [])
    fields = ("jurisdiction", "name", "date", "order", "events", "items", "texts", "pairs", "line_source_ids")
    if [{key: g.get(key) for key in fields} for g in published] != [{key: g[key] for key in fields} for g in ordered]:
        errors.append("News published identities, items or lines differ from canonical records")
    rows = soup.select("#development-list > .development-row")
    pair_counts = Counter(identity for row in rows for identity in row.get("data-pairs", "").split())
    if pair_counts != Counter({identity: 1 for identity in shown_pairs}):
        errors.append("News registered pairs must appear exactly once on the page")
    orders = card_source_orders(soup, data)
    if len(soup.select("#development-list > li:not(.earlier)")) != min(3, len(page)) or len(soup.select("#development-list > li.earlier")) != max(0, len(page) - 3):
        errors.append("developments do not use the three plus seven split")
    if len(rows) != len(page):
        errors.append("News must show ten entries after pair merging, or all if fewer")
    for row, group in zip(rows, page):
        clock = row.select_one(".development-date")
        name = row.select_one(".development-name")
        text = row.select_one(".development-text")
        if row.get("data-pairs", "").split() != group["pair_ids"]:
            errors.append("News pair entry differs from the registered agreement")
        # The DOM has no separate News ID; these attributes identify its published group.
        if row.get("data-j") != group["jurisdiction"] or row.get("data-events", "").split() != group["events"] or not clock or clock.get("data-date") != group["date"] or not name or name.get_text() != group["name"]:
            errors.append("development ordering, grouping, date or jurisdiction differs from data")
        if group["pair_ids"] and (not name or name.get_text() != group["name"]):
            errors.append("News pair entry must name both states in registration order")
        if [a.get("data-source-id") for a in row.select(".cn")] != in_numeral_order(group["source_ids"], orders.get(group["jurisdiction"], [])):
            errors.append("development citations differ from retained News line sources")
        if text:
            for cites in text.select(".cites"):
                cites.decompose()
            parts = [item.get_text() for item in text.select(':scope > .meta-item')]
            fallback_events = [(part, event) for part, event in zip(parts, group["timeline"]) if "news" not in event]
            errors += first_output_part_errors([part for part, _ in fallback_events], [event for _, event in fallback_events])
            if parts != group["texts"]:
                errors.append("development summary differs from canonical News lines")
        else:
            errors.append("development text missing")
    link = soup.select_one('#news-download[download="news.csv"]')
    if not link or link.get("href") != "data/news.csv":
        errors.append("News lacks its download of news.csv")
    return errors


def check_summary_data(html: str, data: dict) -> list[str]:
    """Compare the hero, legend and developments with their source records."""
    soup = BeautifulSoup(html, "html.parser")
    errors = []
    dataset = data["dataset"]
    entries = soup.select("#legend > .legend-entry")
    if Counter(entry.get("data-status") for entry in entries) != Counter({status: 1 for status in dataset["status_definitions"]}):
        errors.append("legend must have one entry per dataset status")
    for entry in entries:
        name = entry.select_one("abbr.g.legend-name")
        status = entry.get("data-status")
        # site/index.html SHORT supplies the definitions; the gate checks their presence only.
        if not name or not tip_of(name).strip():
            errors.append(f"legend name lacks a non-empty definition tooltip: {status}")
        if not entry.select_one(f".sw.st-{status}") or not entry.select_one(
            ".legend-name"
        ):
            errors.append(f"legend entry lacks its name or swatch: {status}")
    legend = soup.select_one("#legend")
    if (
        soup.select_one("#legend-def")
        or not legend
        or legend.get("aria-hidden") == "true"
    ):
        errors.append("map definitions remain separate or hidden from accessibility")
    errors.extend(check_state_counts(soup, data))
    errors.extend(check_supporting_text(soup, data))
    errors.extend(check_figure_grades(soup, data))
    errors.extend(check_glyphs(soup))
    errors.extend(check_absence_wording(soup))
    errors.extend(check_download_groups(soup, set()))
    updated = soup.select_one("#updated")
    version = soup.select_one('#updated a[href="#changes"]')
    expected_date = date.fromisoformat(updated_date(data)).strftime(
        "%-d %b %Y"
    )
    if (
        not updated
        or updated.get_text()
        != f"Updated {expected_date} v{dataset['version']}"
    ):
        errors.append("hero metadata is not just update date and version")
    if (
        not version
        or version.get_text() != "v" + dataset["version"]
        or tip_of(version)
        or version.get("title")
    ):
        errors.append("hero version is not 'v<version>' linking the changelog without a tooltip")
    changes = soup.select_one("#changes")
    section = changes.find_parent("section") if changes else None
    if not section or section.get("aria-labelledby") != "data":  # the Data section's accessible name
        errors.append("the changelog is not in a section named by its Data heading (aria-labelledby)")
    errors += check_cite(soup, dataset)

    errors += check_news(soup, data)
    return errors


def render(chrome: str, dist: Path) -> tuple[str, str]:
    """The page as the static checks read it: served from this dist on a loopback port and rendered in the locked
    Chromium until its script has taken over and the network is quiet (readiness, not a time budget; Chrome for
    Testing's --dump-dom does not return on Linux)."""
    from playwright.sync_api import Error as PlaywrightError

    try:
        with chrome_session(chrome, dist) as (browser, base_url):
            page = browser.new_page()
            hydrated(page, base_url)
            page.wait_for_load_state("networkidle")
            return page.content(), base_url
    except PlaywrightError as exc:
        raise RuntimeError(f"rendering the page failed: {exc}") from exc


BROWSER_GEOMETRY = """
  const visible = node => node.checkVisibility({checkOpacity:true, checkVisibilityCSS:true})
    && [...node.getClientRects()].some(r => r.width || r.height);
  const pseudoBox = node => {
    const r = node.getBoundingClientRect(), s = getComputedStyle(node, '::before');
    if (s.content === 'none' || s.content === 'normal' || s.position !== 'absolute') return null;
    const number = value => Number.parseFloat(value) || 0;
    const transform = s.transform === 'none' ? new DOMMatrix() : new DOMMatrix(s.transform);
    const left = r.left + number(s.left) + transform.e;
    const top = r.top + number(s.top) + transform.f;
    const width = Math.max(number(s.width), number(s.minWidth));
    const height = Math.max(number(s.height), number(s.minHeight));
    return {left, top, right:left + width, bottom:top + height, width, height};
  };
  const onScreen = (node, x, y) => {
    // elementFromPoint returns null for fractional points on the viewport's last pixel row or column.
    if (x < 0 || x > innerWidth - 1 || y < 0 || y > innerHeight - 1) return false;
    for (let p = node.parentElement; p; p = p.parentElement) {
      const s = getComputedStyle(p), r = p.getBoundingClientRect();
      if (/(auto|scroll|hidden|clip)/.test(s.overflowX) && (x < r.left || x >= r.right)) return false;
      if (/(auto|scroll|hidden|clip)/.test(s.overflowY) && (y < r.top || y >= r.bottom)) return false;
    }
    return true;
  };
"""


def check_hit_targets(page, scope: str = "body") -> list[str]:
    """Measure visible controls, including labelled inputs and expanded content.

    Only inline anchors within running paragraph/list text are exempt. Focusable
    glossary terms, citation marks and standalone links are controls too.
    """
    return page.evaluate(
        "scope => {"
        + BROWSER_GEOMETRY
        + """
      const errors = [], fundingTargets = [];
      const selector = 'a[href], button, input:not([type="hidden"]), select, textarea, summary, [tabindex]:not([tabindex="-1"]), [role="button"], [contenteditable="true"]';
      document.querySelector(scope).querySelectorAll(selector).forEach(control => {
        if (control.disabled || !visible(control)) return;
        // Citations are inline targets; their own check requires one surrounding line of height.
        if (control.matches('.cn')) return;
        const style = getComputedStyle(control);
        const prose = control.closest('p, li') || control.closest('.inline-links')?.parentElement;
        if (control.matches('a') && style.display === 'inline' && prose) {
          const walker = document.createTreeWalker(prose, NodeFilter.SHOW_TEXT);
          let runningText = false;
          while (walker.nextNode()) {
            if (!control.contains(walker.currentNode) && walker.currentNode.textContent.trim()) runningText = true;
          }
          if (runningText) return;
        }
        const targets = [control, ...Array.from(control.labels || [])];
        const height = Math.max(...targets.filter(visible).map(n => {
          const r = n.getBoundingClientRect(), p = pseudoBox(n);
          return p ? Math.max(r.bottom, p.bottom) - Math.min(r.top, p.top) : r.height;
        }));
        const funding = control.matches('.fund-row, .fund-name-row, .fund-names-toggle');
        if (height < (funding ? 44 : 27.5)) errors.push(`${control.tagName.toLowerCase()}#${control.id}.${control.getAttribute("class")}: ${(control.textContent.trim() || control.getAttribute("aria-label") || "").slice(0,60)} (${height.toFixed(1)}px hit height)`);
        if (funding) fundingTargets.push({name: control.textContent.trim(), text: control.getBoundingClientRect(), box: pseudoBox(control) || control.getBoundingClientRect()});
      });
      for (let a=0; a<fundingTargets.length; a++) for (let b=a+1; b<fundingTargets.length; b++) {
        const x=fundingTargets[a], y=fundingTargets[b];
        // Summary disclosures and institute rows have disjoint targets.
        if (Math.min(x.box.right,y.box.right)-Math.max(x.box.left,y.box.left)>.5 && Math.min(x.box.bottom,y.box.bottom)-Math.max(x.box.top,y.box.top)>.5)
          errors.push('Funding hit areas overlap: '+x.name+' / '+y.name);
      }
      return errors;
    }""", scope
    )


def check_baselines_and_rhythm(page) -> list[str]:
    """Visual rows retain the visible items required for their shape."""
    return page.evaluate(
        "() => {"
        + BROWSER_GEOMETRY
        + """
      const errors = [];
      // Each descriptor is one visual row, not a stack of independent rows.
      const rowTypes = [
        ['.card .row', '.k, .v'], ['.card .top', ':scope > span:not(.vh), :scope > .name'],
        ['.card.mini > summary', ':scope > span, :scope > .name'], ['.meta', ':scope > *'],
        ['.dlist a', innerWidth <= 640 ? ':scope > .dl-title, :scope > .dl-file' : ':scope > .dl-title, :scope > .dl-file, :scope > .d'],
        ['table.world tr', ':scope > th, :scope > td'],
        ['.secnav .measure', ':scope > a'], ['.region-chips', ':scope > button'],
        ['.legend-entry', ':scope > .legend-copy'],
        ['.development-label', '.development-date, .development-name'],
      ];
      rowTypes.push(['.fund-item', ':scope > .fund-identity, :scope > .fund-measure .fund-value']);
      for (const [selector, items] of rowTypes) {
        document.querySelectorAll(selector).forEach(row => {
          if (!visible(row)) return;
          const nodes = [...row.querySelectorAll(items)].filter(visible);
          if (nodes.length < (['.legend-entry', '.meta'].includes(selector) || (selector === '.card .top' && row.closest('.statusgroup, .eu-level')) || (selector === '.card.mini > summary' && row.parentElement.open) ? 1 : 2)) errors.push(`missing baseline items: ${selector}`);
        });
      }
      return errors;
    }"""
    )


def check_citations(page) -> list[str]:
    """Citations stay with their text and retain vertical-only effective hit regions."""
    return page.evaluate(
        "() => {"
        + BROWSER_GEOMETRY
        + """
      const errors = [];
      const stickyOverlay = hit => {
        const overlay = hit && hit.closest('.secnav, .section-label');
        return overlay && ['sticky', 'fixed'].includes(getComputedStyle(overlay).position);
      };
      const otherLinks = [...document.querySelectorAll('a[href]:not(.cn), abbr.g, summary, button')].filter(visible).map(link => {
        const walker = document.createTreeWalker(link, NodeFilter.SHOW_TEXT), rects = [];
        while (walker.nextNode()) {
          if (!walker.currentNode.textContent.trim() || !visible(walker.currentNode.parentElement)) continue;
          const range = document.createRange(); range.selectNodeContents(walker.currentNode);
          rects.push(...[...range.getClientRects()].filter(r => r.width && r.height));
        }
        return {link, rects};
      });
      document.querySelectorAll('.cites').forEach(cites => {
        if (!visible(cites)) return;
        const glue = cites.closest('.cite-glue');
        if (!glue) { errors.push('citation group has no preceding word'); return; }
        const walker = document.createTreeWalker(glue, NodeFilter.SHOW_TEXT);
        let word = null;
        while (walker.nextNode()) {
          if (cites.contains(walker.currentNode)) break;
          if (walker.currentNode.textContent.trim() && visible(walker.currentNode.parentElement)) word = walker.currentNode;
        }
        if (!word) { errors.push('empty citation glue'); return; }
        const range = document.createRange(); range.selectNodeContents(word);
        const wr = [...range.getClientRects()].at(-1), first = cites.querySelector('a');
        if (!wr || !wr.width || !wr.height) { errors.push('citation glue word has no box'); return; }
        if (!first) { errors.push('citation group has no link'); return; }
        range.selectNodeContents(first);
        const cr = range.getBoundingClientRect();
        if (wr.bottom < cr.top || wr.top > cr.bottom) errors.push('citation is on a separate line');
        const numbers = [...cites.querySelectorAll('a')].map(a => Number(a.textContent));
        if (numbers.some((n, k) => k && n <= numbers[k - 1])) errors.push(`citation numerals not ascending: ${cites.textContent}`);
        cites.querySelectorAll('a').forEach(a => {
          const r = a.getBoundingClientRect(), p = pseudoBox(a);
          const preceding = document.createTreeWalker(glue, NodeFilter.SHOW_TEXT), boxes = [];
          while (preceding.nextNode()) {
            const text = preceding.currentNode;
            if (a.contains(text)) break;
            const range = document.createRange(); range.selectNodeContents(text);
            boxes.push(...[...range.getClientRects()].filter(box => box.width && box.height));
          }
          if (boxes.some(box => box.left < r.right && box.right > r.left + .1 && box.top < (p || r).bottom && box.bottom > (p || r).top)) errors.push(`citation ${a.textContent} overlaps preceding text`);
          // Inline targets are exempt from WCAG 2.5.8's size minimum; our rule: three quarters of the surrounding line (28px is enough).
          const ps = getComputedStyle(glue), line = parseFloat(ps.lineHeight) || parseFloat(ps.fontSize) * 1.2;
          if (!p || p.height < Math.min(27.5, line * .75)) { errors.push('citation effective hit height below one text line'); return; }
          if (Math.abs(p.left-r.left) > .5 || Math.abs(p.right-r.right) > .5) errors.push('citation hit region expands horizontally');
          const x = (r.left+r.right)/2;
          for (const [name, y] of [['centre', (p.top+p.bottom)/2], ['upper extension', p.top+1], ['lower extension', p.bottom-1]]) {
            if (!onScreen(a, x, y)) continue;
            const hit = document.elementFromPoint(x, y);
            // Sticky navigation/section headings legitimately cover content at
            // a scrolled snapshot. Keep reporting every other obstruction.
            if (stickyOverlay(hit) || (hit && hit.closest('#tip'))) continue; // a tooltip left open by an earlier step is a floating surface, not layout
            if (hit !== a && !a.contains(hit)) errors.push(`citation ${a.textContent} ${name} hits ${hit ? hit.tagName.toLowerCase() + '.' + hit.className : 'nothing'}`);
          }
          // The extension must not steal the visible text of another link.
          for (const {link, rects} of otherLinks) for (const rect of rects) {
            if (link.contains(a)) continue;
            const left = Math.max(rect.left, p.left), right = Math.min(rect.right, p.right);
            const top = Math.max(rect.top, p.top), bottom = Math.min(rect.bottom, p.bottom);
            if (right <= left || bottom <= top) continue;
            const x = (left+right)/2, inset = Math.min(.5, (bottom-top)/4);
            for (const y of [(top+bottom)/2, top+inset, bottom-inset]) {
              if (!onScreen(link, x, y)) continue;
              const hit = document.elementFromPoint(x, y);
              if (stickyOverlay(hit)) continue;
              if (hit === a || a.contains(hit)) errors.push(`citation ${a.textContent} steals adjacent link text: ${link.textContent.trim().slice(0,60)}`);
            }
          }
        });
      });
      return [...new Set(errors)];
    }"""
    )


# These are logical boundaries, shared by CSS, matchMedia and the early-map attribute.
# Range syntax gives complementary <=/> or </>= sides without a one-pixel gap.
VIEWPORT_BOUNDARIES = {
    "360px": "narrowest phones: Functions head size, gap and marks",
    "390px": "phone function-group labels",
    "400px": "small-phone group navigation",
    "430px": "theme toggle and group label",
    "460px": "scrolled navigation title",
    "560px": "navigation gaps and keyboard hints",
    "640px": "phone forms and single-column cards",
    "700px": "short navigation title",
    "1100px": "stacked map and two-column cards",
    "1280px": "section heading above content below this width; sticky column from here",
}
CONTAINER_BOUNDARIES = {"funding": "40rem"}  # inline identity, bounded bars and equal flexible figures
WIDTH_GEOMETRY = {
    (".vh", "width", "1px"): "clipped accessible text must retain a nonzero box",
    (".close-glyph", "width", ".8em"): "square close glyph scales with the header label, paired with its height",
    (".close-glyph::before, .close-glyph::after", "width", "141.4214%"): "square diagonals are sqrt(2) times the side, rotated in opposite directions",
    (".info::before", "width", ".72em"): "cap-height information ring, paired with its circular height",
    (".ext-tail::after", "width", ".65em"): "external-link arrow scales with its text, paired with its height",
    (".fn-mark", "width", ".8rem"): "circular function glyph, paired with its height",
    ('.fn-table td[data-value="not_coded"]::before', "width", ".8rem"): "dash matches the function glyph width",
    (".fn-pip", "width", "8px"): "phone pip geometry, paired with its height",
    (".fn-phone .fn-pip", "width", "7px"): "narrowest phones' pip geometry, paired with its height",
    ("#tip", "max-width", "min(var(--tooltip-width), calc(100vw - var(--sp-7)))"): "tooltip stays within the viewport with its token gutter",
    (".palette", "width", "min(var(--palette-width), calc(100vw - 2 * var(--sp-4)))"): "palette stays within the viewport with its token gutters",
    (".fn-column-group::after", "width", "100%"): "group rule spans its tracks; there is no gutter between groups",
    (".bar-value", "min-width", "min(var(--bar-min), 100%)"): "minimum visible bar never exceeds its track",
    (":is(.section-foot > details, .section-foot > .foot > details, .card.mini > .foot > details) > summary", "max-width", "calc(100% - var(--foot-action-space))"): "only the fold summary reserves room for its action; opened content keeps the column width",
    **{(selector, prop, "100%"): "fill or cap at the containing track, without an independent measure" for selector, prop in (
        ("#changes > summary", "width"), (".suggest", "width"), (".worldmap svg", "width"),
        (".region-head", "width"), (".source-footer-line", "max-width"), (".meta-item", "max-width"),
        ("table", "width"), ("th button", "width"), (".field", "max-width"),
        (".search .field", "width"), (".selectwrap .field", "width"), (".fn-scroll", "max-width"),
        ('.fn-legend.fn-phone-only li[data-value="not_found"]', "max-width"),
        (".fn-column-group:nth-last-child(2)::after", "width"), (".fn-cell", "width"),
        (".fn-phone-row", "width"), (".fn-phone-detail", "width"), (".fn-phone-detail", "max-width"),
        (".fn-function-label", "max-width"), (".fund-bar", "width"), (".fund-row", "width"), (".fund-detail", "width"),
        (".fund-names-toggle", "width"), (".fund-name-row", "width"),
        (".bar-value", "max-width"), (".region-chips", "max-width"),
        (":is(.card.mini, .fund-detail)[open] > summary", "width"),  # backing fills the fold behind its separate close
        (".development-row", "width"), (".fn-table", "width"), (".fn-table .fn-pad", "width"),  # full content width; the spacer takes the slack
    )},
}
# Match every dimension and percentage, including exponent notation. Restricting
# this to a unit list would let a new font, viewport or container unit bypass it.
CSS_LENGTH = re.compile(r"(?<![\w.-])[+-]?(?:\d*\.)?\d+(?:e[+-]?\d+)?(?:[a-z]+\b|%)", re.I)
CSS_COLOUR_KEYWORDS = {
    "inherit", "initial", "unset", "revert", "revert-layer", "currentcolor", "transparent",
    "canvas", "canvastext", "linktext", "graytext",
}
TEXT_COLOUR_PROPERTIES = {
    "color", "text-decoration-color", "text-emphasis-color",
    "-webkit-text-fill-color", "-webkit-text-stroke-color", "-webkit-text-emphasis-color",
}
TEXT_COLOUR_SHORTHANDS = {
    "text-decoration": {"none", "underline", "overline", "line-through", "blink", "solid", "double", "dotted", "dashed", "wavy", "auto", "from-font"},
    "text-emphasis": {"none", "filled", "open", "dot", "circle", "double-circle", "triangle", "sesame"},
    "-webkit-text-stroke": {"thin", "medium", "thick"},
}
TEXT_COLOUR_SHORTHANDS["-webkit-text-emphasis"] = TEXT_COLOUR_SHORTHANDS["text-emphasis"]


def has_raw_text_colour(prop: str, value: str) -> bool:
    # Keep var() fallbacks: a literal fallback is still a raw colour.
    remaining = re.sub(r"var\(\s*--[\w-]+\s*\)", "", value)
    remaining = re.sub(r"!\s*important\b", "", remaining, flags=re.I).strip().lower()
    if prop in TEXT_COLOUR_SHORTHANDS:
        # These shorthands also carry line styles, thicknesses or an emphasis
        # glyph. Strip only those parts; colour names/functions remain findings.
        if prop in {"text-emphasis", "-webkit-text-emphasis"}:
            remaining = re.sub(r"\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*'", "", remaining)
        else:
            remaining = CSS_LENGTH.sub("", remaining)
            remaining = re.sub(r"(?<![\w.#-])0(?![\w.])", "", remaining)
        return bool(set(remaining.split()) - CSS_COLOUR_KEYWORDS - TEXT_COLOUR_SHORTHANDS[prop])
    return remaining not in CSS_COLOUR_KEYWORDS | {""}


def check_css_tokens(html: str) -> list[str]:
    """Blocking source guard for new width, breakpoint and text-colour literals.

    Inspect declarations in every style block, including nested media/container rules;
    custom-property definitions are the home for literals. Exceptions bind the exact
    selector, property and value, so a glyph's width cannot excuse a content width.
    """
    errors = []
    styles = re.findall(r"<style\b[^>]*>(.*?)</style\s*>", html, re.S | re.I)
    if not styles:
        return ["CSS tokens: no stylesheet found"]
    styles.extend("inline { " + unescape(m[2]) + " }" for m in re.finditer(r'''\bstyle\s*=\s*(["'])(.*?)\1''', html, re.S | re.I))
    queries = []
    for style in styles:
        css = re.sub(r"/\*.*?\*/", "", style, flags=re.S)
        queries.extend((kind.lower(), query.strip()) for kind, query in re.findall(r"@(media|container)\b\s*([^{}]+)\{", css, re.I))
        # Leaf rules also cover nested groups; braces in quoted URLs are not CSS blocks.
        css = re.sub(r'''url\(\s*(?:"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|[^)]*)\s*\)''', "url()", css, flags=re.I)
        for rule in re.finditer(r"([^{}]+)\{([^{}]*)\}", css):
            selector = " ".join(rule[1].split())
            if ";" in selector:
                # Declarations before a nested rule (CSS nesting) would hide from this scan; the page does not nest.
                errors.append(f"CSS tokens: nested rule after declarations is not supported: {selector[:80]}")
                continue
            for declaration in rule[2].split(";"):
                if ":" not in declaration:
                    continue
                prop, value = declaration.split(":", 1)
                prop, value = prop.strip().lower(), " ".join(value.split())
                if prop.startswith("--"):
                    continue
                if prop in {"width", "min-width", "max-width"} and CSS_LENGTH.search(value):
                    # The page authors no style attributes; an inline width in plain percent is a script-set data proportion (a funding bar's fill).
                    if selector == "inline" and prop == "width" and re.fullmatch(r"\d+(?:\.\d+)?%", value):
                        continue
                    if (selector, prop, value) not in WIDTH_GEOMETRY:
                        errors.append(f"CSS tokens: raw width in {selector}: {prop}: {value}")
                if prop in TEXT_COLOUR_PROPERTIES or prop in TEXT_COLOUR_SHORTHANDS:
                    if has_raw_text_colour(prop, value):
                        errors.append(f"CSS tokens: raw text colour in {selector}: {prop}: {value}")
    # Literal JS queries and the saved map's declarative query follow the same boundaries.
    queries.extend(("media", m[2]) for m in re.finditer(r'''\bmatchMedia\s*\(\s*(["'])(.*?)\1\s*\)''', html))
    queries.extend(("media", unescape(m[2])) for m in re.finditer(r'''\bdata-[\w-]*media\s*=\s*(["'])(.*?)\1''', html))
    # Allow-list, not a parser: remove every listed size clause; any size feature left over (any operand order, case,
    # nesting such as calc()/min()/var(), or legacy min-/max- form) is an unlisted breakpoint.
    for kind, query in queries:
        rest = " ".join(query.lower().split())
        if kind == "media":
            rest = re.sub(r"\(\s*width\s*[<>]=?\s*(\d+(?:\.\d+)?px)\s*\)", lambda m: "" if m[1] in VIEWPORT_BOUNDARIES else m[0], rest)
        else:
            name = rest.split("(", 1)[0].strip()
            rest = re.sub(r"\(\s*width\s*<\s*(\d+(?:\.\d+)?rem)\s*\)", lambda m: "" if m[1] == CONTAINER_BOUNDARIES.get(name) else m[0], rest)
        if re.search(r"(?:width|height|inline-size|block-size|aspect-ratio|resolution)", rest):
            errors.append(f"CSS tokens: unlisted breakpoint: {kind} {query}")
    return list(dict.fromkeys(errors))


def check_design_roles(page) -> list[str]:
    """Links retain their decoration and hit regions; bars and active navigation remain visible."""
    return page.evaluate(
        "() => {"
        + BROWSER_GEOMETRY
        + """
      const errors = [];
      const probe = document.createElement('span'); document.body.appendChild(probe);
      const barMin = 2; // owner's visibility floor in px, independent of the CSS under test
      probe.style.color = 'var(--ink)'; const ink = getComputedStyle(probe).color;
      probe.remove();
      document.querySelectorAll('a').forEach(n => {
        if (!visible(n) || n.matches(':hover, :focus-visible')) return;
        const s = getComputedStyle(n);
        const standalone = n.matches('.cn, .standalone, .wordmark, .tile, .correction, .secnav a, .group-nav a, .popover a, .dlist a');
        if (s.textDecorationLine !== (standalone ? 'none' : 'underline')) errors.push(`wrong resting link decoration: ${n.className} ${n.textContent.slice(0,40)}`);
      });
      document.querySelectorAll('.secnav a').forEach(n => {
        if (!n.classList.contains('active')) return;
        const line = getComputedStyle(n, '::after');
        const behind = getComputedStyle(n.closest('nav') || document.body).backgroundColor, paint = line.backgroundColor;
        if (line.content === 'none' || parseFloat(line.height) < 1 || parseFloat(line.width) < 1 || /^(transparent|rgba\\(.*,\\s*0\\))$/.test(paint) || paint === behind) errors.push('active nav shows no indicator');
      });
      const sourceLinks = [...document.querySelectorAll('.srcs a:not(.cn), .inline-links a')].filter(visible);
      for (const link of sourceLinks) {
        const extension = pseudoBox(link);
        if (!extension) continue;
        for (const other of sourceLinks) {
          if (other === link) continue;
          const walker = document.createTreeWalker(other, NodeFilter.SHOW_TEXT);
          while (walker.nextNode()) {
            const range = document.createRange(); range.selectNodeContents(walker.currentNode);
            for (const r of range.getClientRects()) {
              if (Math.min(r.right, extension.right) - Math.max(r.left, extension.left) > .1 && Math.min(r.bottom, extension.bottom) - Math.max(r.top, extension.top) > .1) errors.push('source link hit extension overlaps another link text');
            }
          }
        }
      }
      document.querySelectorAll('.bar-value').forEach(n => {
        const s = getComputedStyle(n), r = n.getBoundingClientRect();
        const track = n.parentElement.getBoundingClientRect();
        const zero=Number(n.parentElement.dataset.value)===0;
        if ((zero ? r.width > .5 : r.width < Math.min(barMin, track.width) - .5) || r.width > track.width + .5) errors.push('funding bar violates its minimum/maximum width');
        if (n.classList.contains('pending')) {
          if (!s.backgroundImage.includes('repeating-linear-gradient')) errors.push('non-adopted funding bar is not hatched');
          const rgb = c => { const m = c.match(/^color\\(srgb ([\\d.e-]+) ([\\d.e-]+) ([\\d.e-]+)/); return m ? m.slice(1, 4).map(v => parseFloat(v) * 255) : (c.match(/[\\d.]+/g) || []).slice(0, 3).map(Number); };
          const dist = (a, b) => Math.hypot(...a.map((v, k) => v - b[k]));
          const stops = [...s.backgroundImage.matchAll(/(rgba?\\([^)]*\\)|color\\([^)]*\\))\\s+([\\d.]+)px/g)].map(m => [rgb(m[1]), parseFloat(m[2])]);
          const [p0, p1, p2, p3] = stops.length === 4 ? stops.map(t => t[1]) : [0, 0, 0, 0], stripe = p1 - p0, gap = p3 - p2;
          // An invisible hatch leaves announced funding looking like spent funding.
          const unseen = stops.length !== 4 || stripe <= 0 || gap <= 0 || dist(stops[0][0], stops[2][0]) < 2;
          if (unseen) errors.push(`announced funding bar hatch is invisible (stops: ${JSON.stringify(stops)})`);
        } else if (s.backgroundImage !== 'none' || s.backgroundColor !== ink) errors.push('adopted funding bar is not solid ink');
      });
      return errors;
    }"""
    )


def check_external_markers(page) -> list[str]:
    """Check mask styling, line placement and the accessible new-tab suffix."""
    return page.evaluate(
        "() => {"
        + BROWSER_GEOMETRY
        + """
      const errors = [], links = [...document.querySelectorAll('a.ext')].filter(visible);
      if (!links.length) errors.push('external link markers missing');
      // Every link that opens a new tab says so to screen readers, in any wording: a citation in its label, any other
      // link in its description (the shared #opens-new-tab) or a hidden note of its own.
      const said = a => [a.getAttribute('aria-label') || '', ...(a.getAttribute('aria-describedby') || '').split(' ').filter(Boolean).map(id => document.getElementById(id)?.textContent || ''),
        ...[...a.querySelectorAll('.vh')].map(n => n.textContent)].some(t => /new (tab|window)/i.test(t));
      const silent = [...document.querySelectorAll('a[target="_blank"]')].filter(a => !said(a));
      if (silent.length) errors.push(`${silent.length} links open a new tab without saying so, first: ${(silent[0].textContent || silent[0].href).trim().slice(0, 60)}`);
      const probe = document.createElement('span');
      probe.style.forcedColorAdjust = 'none';
      probe.style.color = matchMedia('(forced-colors: active)').matches ? 'LinkText' : 'var(--muted)';
      document.body.appendChild(probe); const colour = getComputedStyle(probe).color; probe.remove();
      links.forEach(link => {
        const fail = message => errors.push(`${message}: ${link.textContent.trim().slice(0, 60)}`);
        const tail = link.querySelector('.ext-tail'), marker = getComputedStyle(tail || link, '::after'), size = parseFloat(getComputedStyle(link).fontSize);
        if (marker.content !== '""' || !(marker.maskImage || marker.webkitMaskImage || '').includes('data:image/svg+xml')) fail('external marker is not an SVG mask');
        if (marker.backgroundColor !== colour || (marker.maskSize || marker.webkitMaskSize) !== 'contain' || (marker.maskRepeat || marker.webkitMaskRepeat) !== 'no-repeat') fail('external marker paint differs from theme');
        if (Math.abs(parseFloat(marker.width) / size - .65) > .03 || Math.abs(parseFloat(marker.height) / size - .65) > .03 || Math.abs(parseFloat(marker.marginInlineStart) / size - .2) > .03) fail('external marker size or gap differs from text scale');
        if (link.target !== '_blank' || !link.relList.contains('noopener')) fail('external link does not open in a new tab');
        if (!tail || getComputedStyle(tail).whiteSpace !== 'nowrap' || !tail.textContent.trim()) fail('external marker lost its no-wrap last word');
        if (getComputedStyle(link).display !== 'inline') return;
        const walker = document.createTreeWalker(link, NodeFilter.SHOW_TEXT);
        let last = null;
        while (walker.nextNode()) {
          if (walker.currentNode.textContent.trim() && !walker.currentNode.parentElement.closest('.vh')) last = walker.currentNode;
        }
        const fragments = [...link.getClientRects()].filter(r => r.width > 1 && r.height > 1);
        if (last && fragments.length) {
          const range = document.createRange(), end = last.textContent.trimEnd().length;
          range.setStart(last, end - 1); range.setEnd(last, end);
          if (Math.abs(fragments[fragments.length - 1].top - range.getBoundingClientRect().top) > 1) fail('external marker wraps away from its last word');
        }
      });
      return [...new Set(errors)];
    }"""
    )


def check_card_grid(page) -> list[str]:
    """Cards retain their reading order, fields and non-overlapping rows."""
    return page.evaluate(
        """() => {
      // Three card shapes; structurally absent fields are omitted. Each field owns a fixed track so shapes stay aligned.
      const SHAPES = {
        established: ['2', '3', '4', '8', '9', '10', '11'],  // Head, Reports to and Partners are optional here: check_leadership_rows holds them to the data
        record: ['2', '3', '4', '8', '9', '10'],
        none: ['3', '8'],
      };
      const OPTIONAL = ['5', '6', '7', '12', '13'];
      const errors = [];
      document.querySelectorAll('.cards:not(.minis)').forEach(grid => {
        const cards = [...grid.querySelectorAll('.card')], rows = [...grid.children];
        const names = cards.map(c => c.querySelector('.name').textContent);
        if (names.some((n, i) => i && n.localeCompare(names[i-1], 'en') < 0)) errors.push('cards are not alphabetical in row reading order');
        rows.forEach((row, i) => {
          const siblings = [...row.children];
          siblings.forEach((card, j) => {
            const r = card.getBoundingClientRect();
            if (j && (r.left <= siblings[j-1].getBoundingClientRect().left || Math.abs(r.top - siblings[0].getBoundingClientRect().top) > 1)) errors.push('cards do not flow left to right');
            if (i && r.top < rows[i-1].getBoundingClientRect().bottom) errors.push('comparison rows overlap');
            const fieldRows = [...card.querySelectorAll(':scope > .row')];
            const tracks = fieldRows.map(n => n.dataset.track);
            if (fieldRows.some(n => !n.querySelector('.k')?.textContent.trim())) errors.push(`card field label missing or empty: ${card.id}`);
            if (fieldRows.some(n => n.querySelector('.k')?.textContent.trim() && !n.querySelector('.v')?.textContent.trim())) errors.push(`card label without a value: ${card.id}`);
            const shape = tracks[0] !== '2' ? 'none' : (card.querySelector('.top .sw.st-established') || card.closest('.statusgroup[data-status="established"]') || (card.closest('.eu-level') && card.querySelector(':scope > .top > .vh')?.textContent === ', Established')) ? 'established' : 'record';  // the EU entry says its status in its accessible name only
            if (JSON.stringify(tracks.filter(track => !OPTIONAL.includes(track))) !== JSON.stringify(SHAPES[shape])) errors.push(`missing or misplaced card fields: ${card.id} (${tracks.join(', ')})`);
            if (shape === 'none') {
              const finding = fieldRows[1]?.querySelector('.v');
              if (!finding || !finding.textContent.trim()) errors.push('record without an institute lost its finding');
            }
          });
        });
      });
      return errors;
    }"""
    )


def check_card_grid_edges(browser, base_url: str, data: dict) -> list[str]:
    """Missing fields and a wrapped institute name must keep their shared tracks."""
    fixture = copy.deepcopy(data)
    records = sorted(
        (
            j
            for j in fixture["jurisdictions"]
            if j["europe_scope"] and j["status"] == "established"
        ),
        key=lambda j: j["name"],
    )
    target = records[1]
    target.pop("status_since", None)
    inst = next(i for i in fixture["institutes"] if i["id"] == target["institute_id"])
    inst["name"] = (
        "Synthetic institute with a deliberately long name to exercise wrapped text"
    )
    for field in (
        "legal_form",
        "placement",
        "legal_basis",
        "head",
        "reports_to",
        "national_partners",
        "mandate_short",
        "mandate",
        "operating",
        "funding",
        "staff",
    ):
        inst.pop(field, None)
    without_institute = records[2]
    absent_id = without_institute.pop("institute_id")
    fixture["institutes"] = [i for i in fixture["institutes"] if i["id"] != absent_id]
    empty = next(
        j
        for j in fixture["jurisdictions"]
        if j["europe_scope"] and j["status"] == "none_identified"
    )
    empty.pop("closest_body", None)

    def uncite(value):
        # Card sources derive from every citation, so an empty footer needs none anywhere.
        if isinstance(value, list):
            for item in value:
                uncite(item)
        elif isinstance(value, dict):
            value.pop("citations", None)
            if "source_ids" in value:
                value["source_ids"] = []
            for item in value.values():
                uncite(item)

    uncite(empty)
    errors: list[str] = []
    for width in (1440, 1000, 390):
        page = browser.new_page(viewport={"width": width, "height": 1000})
        refresh_fixture_selections(fixture)
        page.route(PAGE_DATA, lambda route: route.fulfill(json=fixture))
        hydrated(page, base_url)
        page.wait_for_selector("#world-rows tr")
        page.evaluate("document.fonts.ready")
        errors.extend(
            f"missing-field fixture {width}px: {e}" for e in check_card_grid(page)
        )
        errors.extend(
            f"empty-source fixture {width}px: {e}" for e in check_source_footers(page)
        )
        if (
            page.locator(
                f"#c-{empty['id'].lower()} .foot > .source-footer-line"
            ).count()
            != 1
        ):
            errors.append(
                f"{width}px: empty-source fixture did not render its plain footer"
            )
        card = page.locator(f"#c-{target['id'].lower()}")
        # Without a date the Status row still names the status, so five fields show a placeholder.
        if card.locator(".row .v .missing").count() != 5:
            errors.append(
                f"{width}px: missing-field fixture did not render five placeholders"
            )
        status_row = card.locator('.row[data-track="3"]')
        if status_row.count() != 1 or " ".join(status_row.locator(".v").inner_text().split()) != STATUS_LABELS[target["status"]]:
            errors.append(f"{width}px: missing-field fixture Status row does not read {STATUS_LABELS[target['status']]!r} without a date")
        if (
            not card.locator(".row")
            .first.locator(".v")
            .evaluate(
                "e => e.getBoundingClientRect().height > parseFloat(getComputedStyle(e).lineHeight) * 1.5"
            )
        ):
            errors.append(f"{width}px: institute-name fixture did not wrap")
        finding = page.locator(f"#c-{without_institute['id'].lower()}")
        tracks = finding.locator(".row").evaluate_all("rows => rows.map(r => r.dataset.track)")
        if (
            tracks != [CARD_TRACKS["Status"], CARD_TRACKS["Mandate"]]  # Finding shares the Mandate track
            or [bool(text.strip()) for text in finding.locator(".row > .k").all_inner_texts()] != [True, True]
            # Dates in the value are joined by no-break spaces (nonbreakingDates), so the text is compared word by word.
            or finding.locator(".row")
            .nth(1)
            .locator(".v")
            .evaluate(
                "e => { const c = e.cloneNode(true); c.querySelectorAll('.cites').forEach(n => n.remove()); return c.textContent.trim(); }"
            ).split()
            != without_institute["summary"].split()
        ):
            errors.append(
                f"{width}px: record without an institute lost its finding or fields"
            )
        page.close()
    return errors


def check_source_footers(page) -> list[str]:
    """Only a map return may precede Sources; the check date and correction open with the list."""
    return page.evaluate(
        "() => {"
        + BROWSER_GEOMETRY
        + """
      const errors = [], probe = document.createElement('span');
      probe.style.cssText = 'font:var(--type-meta);color:var(--muted)';
      document.body.appendChild(probe);
      const props = ['fontSize', 'fontWeight', 'fontFamily', 'lineHeight', 'color'];
      const expected = Object.fromEntries(props.map(p => [p, getComputedStyle(probe)[p]]));
      const isMeta = n => props.every(p => getComputedStyle(n)[p] === expected[p]);
      probe.remove();
      document.querySelectorAll('.card .foot > .source-footer-line').forEach(line => {
        if (line.tagName !== 'DIV' || line.querySelector('.source-count')?.textContent !== '0 sources' || !isMeta(line) || line.parentElement.querySelector('details')) errors.push('invalid empty-source footer');
        const after = line.nextElementSibling;
        if (!after || !after.matches('.source-after') || !after.querySelector('.correction') || (visible(line) && !visible(after))) errors.push('empty-source footer lost its check date or correction');
      });
      const folds = [...document.querySelectorAll('.card details.srcs')];
      if (!folds.length) errors.push('source footers missing');
      if (!document.querySelector('.card details.srcs > .source-after .correction')) errors.push('no correction link after a source list');
      const isMapReturn = (n, control) => n.matches('a.back-map[href="#map"]') && n.textContent.trim() === '↑ Back to map'
        && n.closest('.card').matches('.arrived[data-from-panel="1"]') && n.parentElement === control.parentElement
        && n === n.parentElement.firstElementChild && n.nextElementSibling === control;
      document.querySelectorAll('.card .back-map').forEach(a => {
        const card = a.closest('.card'), control = card.querySelector('.foot > details.srcs, .foot > .source-footer-line');
        if (!control || !isMapReturn(a, control) || card.querySelectorAll('.back-map').length !== 1) errors.push('Back to map is not the single return link before Sources');
      });
      folds.forEach(fold => {
        const summary = fold.querySelector(':scope > summary'), foot = fold.closest('.foot');
        const count = summary?.querySelector('.source-count'), list = fold.querySelector(':scope > ol');
        if (!foot || !summary || !list || !count || count.textContent !== `${list.children.length} ${list.children.length === 1 ? 'source' : 'sources'}`) {
          errors.push('source footer lost its count or list'); return;
        }
        if (!foot.closest('.card').matches('.mini') && [...foot.children].some(n => n !== fold && !isMapReturn(n, fold))) errors.push('collapsed source footer holds more than Sources and its map return');
        if (!visible(summary)) return; // A mini card may itself be closed.
        const r = summary.getBoundingClientRect();
        if (summary.scrollWidth > summary.clientWidth + 1 || r.right > foot.getBoundingClientRect().right + 1) errors.push('source footer overflows');
        if (r.height < 27.5) errors.push('source summary lacks its inline hit height');
        if (summary.querySelector('a, .sep-gap') || summary.textContent !== count.textContent) errors.push('source summary holds more than the count');
        if (visible(list) !== fold.open) errors.push('source list visibility differs from disclosure state');
        const after = fold.querySelector(':scope > .source-after');
        if (foot.querySelectorAll('.correction').length !== (after ? after.querySelectorAll('.correction').length : 0)) errors.push('correction is outside the expanded sources');
        if (after && (after.previousElementSibling !== list || visible(after) !== fold.open)) errors.push('source metadata does not follow the list inside the fold');
      });
      return errors;
    }"""
    )


def checked_page_data(page) -> dict:
    """Read the same versioned, possibly fixture-routed data as the page, without changing callers."""
    return page.evaluate("""async () => {
      const meta = document.querySelector('meta[name="aisi-page-data"]');
      const response = await fetch(meta ? meta.content : 'data/page.json');
      if (!response.ok) throw new Error(`check data: HTTP ${response.status}`);
      return response.json();
    }""")


def source_keyboard_targets(data: dict) -> dict[str, bool]:
    """Each sourced card type exercises one disclosure; source-free records have no disclosure."""
    sources = {s["id"] for s in data["sources"]}
    institutes = {i["jurisdiction_id"]: i for i in data["institutes"]}
    targets = {"article.card": False, ".card.mini": False}
    for j in data["jurisdictions"]:
        full = full_card_expected(j)
        if full or j.get("europe_scope"):
            targets["article.card" if full else ".card.mini"] |= bool(sources.intersection(entry_sources(j) + entry_sources(institutes.get(j["id"]))))
    return targets


def check_source_keyboard(page) -> list[str]:
    """Exercise both card types and the correction link's accessible role."""
    errors = []
    data = checked_page_data(page)
    scroll = page.evaluate("[scrollX, scrollY]")
    for selector, expected in source_keyboard_targets(data).items():
        target = selector + ":has(details.srcs):visible"
        cards = page.locator(target)
        count = cards.count()
        errors.extend(zero_match_errors("source_keyboard", target, count, expected))
        if not count:
            continue
        card = cards.first
        fold = card.locator("details.srcs")
        missing = [error for child in ("summary", "ol") for error in zero_match_errors("source_keyboard", f"{target} details.srcs {child}", fold.locator(child).count())]
        errors.extend(missing)
        if missing:
            continue
        was_open = card.get_attribute("open") is not None
        if selector == ".card.mini":
            card.evaluate("e => e.open = true")
        fold = card.locator("details.srcs")
        fold_was_open = fold.get_attribute("open") is not None
        summary = fold.locator("summary")
        fold.evaluate("e => e.open = false")
        summary.focus()
        if not summary.evaluate("e => e === document.activeElement"):
            errors.append(f"{selector}: source summary cannot receive keyboard focus")
        summary.press("Enter")
        if fold.get_attribute("open") is None or not fold.locator("ol").is_visible():
            errors.append(f"{selector}: Enter does not expand Sources")
        summary.press("Space")
        if fold.get_attribute("open") is not None or fold.locator("ol").is_visible():
            errors.append(f"{selector}: Space does not collapse Sources")
        correction = fold.locator(".source-after .correction")
        errors.extend(zero_match_errors("source_keyboard", f"{target} .source-after .correction", correction.count(), bool(data["dataset"].get("repository"))))
        if correction.count():
            fold.evaluate("e => e.open = true")
            if fold.get_by_role("link").and_(correction).count() != 1:
                errors.append(f"{selector}: correction link has no accessible role")
            if not re.search(r'^- link "[^"\n]+"', correction.aria_snapshot()):
                errors.append(f"{selector}: correction link lacks a named link in its aria snapshot")
            correction.focus()
            if not correction.evaluate("e => e === document.activeElement"):
                errors.append(f"{selector}: correction cannot receive focus")
            url = correction.evaluate("a => a.href")

            def matches_url(requested):
                return requested == url

            page.context.route(
                matches_url,
                lambda route: route.fulfill(body="<title>Correction</title>"),
            )
            try:
                for activate in (lambda: correction.press("Enter"), correction.click):
                    with page.context.expect_page() as popup:
                        activate()
                    popup.value.close()
                    if fold.get_attribute("open") is None:
                        errors.append(f"{selector}: correction link toggles Sources")
            finally:
                page.context.unroute(matches_url)
        fold.evaluate("(e, open) => e.open = open", fold_was_open)
        if selector == ".card.mini":
            card.evaluate("(e, open) => e.open = open", was_open)
    page.evaluate("document.activeElement.blur()")
    page.evaluate("([x, y]) => scrollTo(x, y)", scroll)
    return errors


def check_rules_render(page) -> list[str]:
    """Funding must contain bar tracks."""
    return page.evaluate(
        """() => {
      const errors = [], bars = [...document.querySelectorAll('.fund-bar:has(.bar-value)')];
      if (!bars.length) errors.push('funding has no bar tracks to inspect');
      return errors;
    }"""
    )


WIDE_WIDTHS = (1100, 1101, 1120, 1200, 1279, 1280, 1440)


# Principle Re-read line: the line is a block of its own in the source item, so it wraps at the item's measure,
# and no visible text of the item runs past that measure.
REREAD_MEASURE_JS = """() => {
  const visible = (el, skip) => { const out = [], w = document.createTreeWalker(el, NodeFilter.SHOW_TEXT); let n;
    while ((n = w.nextNode())) { if (!n.data.trim()) continue; const p = n.parentElement; if (!p || !p.getClientRects().length || (skip && skip.contains(p))) continue;
      if (getComputedStyle(p).visibility === 'hidden' || p.closest('.vh, [hidden]')) continue;
      const r = document.createRange(); r.selectNodeContents(n); for (const b of r.getClientRects()) if (b.width > 0) out.push(b); }
    return out; };
  const errors = [], seen = new Set();
  for (const s of document.querySelectorAll('li[data-source-id] > .reread')) {
    if (!s.getClientRects().length) continue;
    const li = s.parentElement, id = li.dataset.sourceId, ls = getComputedStyle(li), lb = li.getBoundingClientRect(), sb = s.getBoundingClientRect();
    const left = lb.left + parseFloat(ls.paddingLeft) + parseFloat(ls.borderLeftWidth), right = lb.right - parseFloat(ls.paddingRight) - parseFloat(ls.borderRightWidth);
    const own = visible(s), rest = visible(li, s);
    const say = m => { if (!seen.has(id)) { seen.add(id); errors.push(`source ${id}: ${m}`); } };
    if (Math.abs(sb.left - left) > 1 || Math.abs(sb.right - right) > 1) say(`the re-read line spans ${Math.round(sb.left)}-${Math.round(sb.right)}px, its item ${Math.round(left)}-${Math.round(right)}px`);
    const over = Math.max(...own.concat(rest).map(r => r.right)) - right;
    if (over > 1) say(`text runs ${Math.round(over)}px past the item's measure`);
  }
  return errors;
}"""


def check_content_grid(browser, base_url: str) -> list[str]:
    """Heading placement across 1280px and the 1000px table fit/scroll state."""
    errors = []
    for width in (1000, 1100, 1101, 1120, 1200, 1279, 1280, 1920):
        page = browser.new_page(viewport={"width": width, "height": 1000})
        try:
            hydrated(page, base_url)
            page.add_style_tag(content=".offscreen, .card-row { content-visibility: visible !important; }")
            page.evaluate("""() => {
              const more = document.getElementById('developments-more');
              if (more && !more.hidden && more.getAttribute('aria-expanded') === 'false') more.click();
              document.querySelectorAll('details').forEach(n => n.open = true);
            }""")
            page.evaluate("async () => { await document.fonts.ready; await new Promise(requestAnimationFrame); }")
            headings = page.evaluate("""() => {
              const errors = [];
              for (const section of document.querySelectorAll('main section')) {
                const label = section.querySelector(':scope > .section-label'), body = section.querySelector(':scope > .section-body');
                if (!label || !body || !label.getClientRects().length) continue;
                const name = section.getAttribute('aria-labelledby'), l = label.getBoundingClientRect(), b = body.getBoundingClientRect();
                if (innerWidth < 1280) {
                  if (getComputedStyle(label).position !== 'static' || l.bottom > b.top + 1 || Math.abs(l.left - b.left) > 1)
                    errors.push(name + ': section heading is not static above its body');
                } else if (getComputedStyle(label).position !== 'sticky' || l.right > b.left + 1 || l.top < b.top - 1 || l.top > b.bottom) { // a pinned heading sits below its body's top once the page scrolls
                  errors.push(name + ': section heading is not a sticky column beside its body');
                }
              }
              return errors;
            }""")
            errors.extend(f"{width}px: {e}" for e in headings)
            if width == 1000:
                found = page.evaluate("""() => {
                  const scroll = document.querySelector('.fn-scroll'), table = scroll?.querySelector('table');
                  if (!table) return ['missing functions table'];
                  const r = scroll.getBoundingClientRect();
                  const errors = [];
                  // Without group gutters (owner, 18 Nov 2026) the table may fit at 1000px; it either fits, marked so, or scrolls inside its body.
                  const fits = table.getBoundingClientRect().width <= r.width + 1;
                  if (fits ? !scroll.classList.contains('fits') : (scroll.classList.contains('fits') || !['auto', 'scroll'].includes(getComputedStyle(scroll).overflowX)))
                    errors.push('functions table neither fits nor scrolls inside its body at 1000px');
                  return errors;
                }""")
                errors.extend(f"{width}px: {e}" for e in found)
        finally:
            page.close()
    return errors


def check_text_measure(browser, base_url: str) -> list[str]:
    """Re-read line containment and the headline's presence, including 390px."""
    errors = []
    for width in (1600, 1280, 390):
        page = browser.new_page(viewport={"width": width, "height": 900})
        hydrated(page, base_url)
        page.locator("details").evaluate_all("nodes => nodes.forEach(n => n.open = true)")
        page.evaluate("async () => { document.body.offsetHeight; await document.fonts.ready; await new Promise(requestAnimationFrame); }")  # opened lists reveal italic text in its own font
        if not page.evaluate("() => !!document.querySelector('#headline') && !!document.querySelector('section.first')"):
            errors.append(f"{width}px: the headline or the first screen's column is missing")
        off = page.evaluate(REREAD_MEASURE_JS)
        errors.extend(f"{width}px: {e}" for e in off[:8])
        page.close()
    return errors


# Hydration must preserve the first occupied field track and head offset, even at a scrolled position.
CARD_HEAD_SPACE_JS = """c => {
  const h=c.querySelector(':scope > .top').getBoundingClientRect();
  const fields=[...c.querySelectorAll(':scope > .row')];
  return {id:c.id,height:h.height,head_top:h.top-c.getBoundingClientRect().top,
    reserved:fields.length?Math.min(...fields.map(n=>n.getBoundingClientRect().top))-h.top:null};
}"""


def card_hydration_errors(before: list[dict], after: list[dict]) -> list[str]:
    """Catch both a changed title track and a head that only starts sticking after hydration."""
    if not before:
        return ["no visible saved cards measured"]
    live = {m["id"]: m for m in after}
    errors = []
    for saved in before:
        current = live.get(saved["id"])
        if current is None:
            errors.append(f"{saved['id']}: card missing after hydration")
            continue
        for key in ("height", "head_top", "reserved"):
            a, b = saved[key], current[key]
            if a is None or b is None or not math.isfinite(a) or not math.isfinite(b):
                errors.append(f"{saved['id']}: {key} is not finite across hydration")
            elif abs(a - b) > 1 / 64 + 1e-6:  # one layout unit (1/64 CSS px) can come from the page-wide bar heights being measured after hydration; hydration_paint judges visible shifts
                errors.append(f"{saved['id']}: {key} changes across hydration ({a} to {b})")
    return errors


def card_head_space_errors(m: dict) -> list[str]:
    if any(m[key] is None or not math.isfinite(m[key]) for key in ("reserved", "height")):
        return ["reserved head track or head height is not finite"]
    if m["reserved"] < m["height"] - 1:
        return ["reserved head track overlaps a field"]
    return []


def check_clipped_card_name(page, card, *, interactive: bool) -> list[str]:
    """Exercise ellipsis without losing the heading's spoken name or full tooltip."""
    errors = []
    full_name = "Synthetic country heading with deliberately long text to exercise ellipsis at every card width"
    name = card.locator(':scope > .top .name')
    saved = name.evaluate("""(n,text)=>{
      const saved={text:n.textContent,attrs:{}};
      n.textContent=text;
      for (const attr of ['title', 'aria-description', 'data-tip']) {
        if(n.hasAttribute(attr)) {saved.attrs[attr]=n.getAttribute(attr);n.setAttribute(attr,text);}
      }
      return saved;
    }""", full_name)
    try:
        clipped = name.evaluate("n => { const s=getComputedStyle(n); return s.whiteSpace==='nowrap' && s.overflowX==='hidden' && s.textOverflow==='ellipsis' && n.scrollWidth>n.clientWidth && n.getBoundingClientRect().height<=parseFloat(s.lineHeight)+1; }")
        if not clipped:
            errors.append("long heading is not clipped to one line with ellipsis")
        if card.get_by_role("heading", name=full_name, exact=True).count() != 1:
            errors.append("clipped heading lost its full accessible name")
        card.evaluate("c=>scrollTo(0,scrollY+c.getBoundingClientRect().top-innerHeight/2)")
        errors.extend(card_head_space_errors(card.evaluate(CARD_HEAD_SPACE_JS)))
        if interactive:
            # Real hover exercises TIPSEL/tipText/showTip, not just an attribute.
            page.mouse.move(0, 0)
            card.evaluate("c => c.scrollIntoView({block: 'center'})")
            name.hover()
            tip = page.locator('#tip')
            if not tip.is_visible() or tip.inner_text() != full_name:
                errors.append("clipped heading does not display its full name in the page tooltip")
            page.keyboard.press("Escape")
        elif not name.evaluate("(n, text) => (n.dataset.tip || n.getAttribute('aria-description') || n.title) === text", full_name):
            errors.append("saved clipped heading lost its full tooltip text")
    finally:
        name.evaluate("""(n,saved)=>{
          n.textContent=saved.text;
          Object.entries(saved.attrs).forEach(([key,value])=>n.setAttribute(key,value));
        }""", saved)
    return errors


def check_saved_card_heads(browser, base_url: str) -> list[str]:
    """Saved single-line heads reserve their track without JS or while hydration waits."""
    errors = []

    def settled(page):
        """With page scripts off, timers and frames never fire, so wait for the fonts and a short pause instead."""
        if scripting:
            layout_settled(page)
        else:
            page.evaluate("document.fonts.ready")
            page.wait_for_timeout(150)
    for scripting in (False, True):
        page = browser.new_page(viewport={"width": 1280, "height": 900}, java_script_enabled=scripting)
        mode = "before hydration" if scripting else "no JS"
        try:
            held = Held(page, PAGE_DATA) if scripting else None
            page.goto(base_url, wait_until="load")
            settled(page)
            if held:
                held.wait()
            card = page.locator('#c-fr')
            # Check real names before exercising ellipsis with a synthetic long title.
            for width in (1280, 1440):
                page.set_viewport_size({"width": width, "height": 900})
                settled(page)
                for name in page.locator('.cards .card > .top .name').all():
                    if name.evaluate("n => n.scrollWidth > n.clientWidth + 1"):
                        errors.append(f"saved cards {mode}, {width}px: country name cut: {name.text_content()}")
                    if not name.evaluate("n => (n.title || n.getAttribute('aria-description')) === n.textContent"):
                        errors.append(f"saved cards {mode}: full country name missing from tooltip")
            for width in (641, 1000, 1280, 1440, 1600, 390):
                page.set_viewport_size({"width": width, "height": 900})
                settled(page)
                for scale in (100, 150):
                    prefix = f"saved cards {mode}, {width}px, text {scale}%: "
                    page.evaluate("scale => document.documentElement.style.fontSize=scale+'%'", scale)
                    settled(page)
                    # Keep the head in natural flow for measurement, below the sticky bars even on phone.
                    card.evaluate("c => scrollTo(0,scrollY+c.getBoundingClientRect().top-innerHeight/2)")
                    settled(page)
                    if not page.evaluate("document.documentElement.hasAttribute('data-prerendered')"):
                        errors.append(prefix + "saved-page fixture hydrated before measurement")
                    errors.extend(prefix + e for e in check_clipped_card_name(page, card, interactive=False))
                    # Every peer's fields must clear the fixed head track, including enlarged text.
                    for peer in card.locator('..').locator(':scope > .card').all():
                        peer.evaluate("c => scrollTo(0,scrollY+c.getBoundingClientRect().top-innerHeight/2)")
                        settled(page)
                        m = peer.evaluate(CARD_HEAD_SPACE_JS)
                        errors.extend(prefix + m["id"] + ': ' + e for e in card_head_space_errors(m))
            failed_faces = page.evaluate("""() => [...document.fonts].filter(f => f.status === 'error')
              .map(f => [f.family, f.style, f.weight, f.unicodeRange])""")
            errors.extend(f"saved cards {mode}: the font face {face} failed to load" for face in failed_faces)
        finally:
            page.close()
    return errors


HEAD_TEXT_TOP_JS = """h => {
  const tops=[], walker=document.createTreeWalker(h,NodeFilter.SHOW_TEXT);
  while(walker.nextNode()) {
    const n=walker.currentNode, p=n.parentElement;
    if(!n.textContent.trim() || p.closest('.vh, .close-control') || getComputedStyle(p).visibility!=='visible') continue;
    const range=document.createRange(); range.selectNodeContents(n);
    for(const r of range.getClientRects()) if(r.width && r.height) tops.push(r.top);
  }
  return tops.length ? Math.min(...tops) : null;
}"""


def unstuck_head_gap(page, owner: str, head: str) -> float | None:
    page.locator(owner).evaluate("n => scrollTo(0,scrollY+n.getBoundingClientRect().top-innerHeight/2)")
    layout_settled(page)
    return page.locator(owner).locator(head).evaluate(
        "h => { const top=(" + HEAD_TEXT_TOP_JS + ")(h); return top===null ? null : top-h.getBoundingClientRect().top; }")


READING_EDGE_JS = """box => {
  const nav=document.querySelector('.secnav').getBoundingClientRect(), group=box.closest('.group')?.querySelector('.group-nav')?.getBoundingClientRect();
  const bars=group && group.top<=nav.bottom+1 && group.bottom>nav.bottom ? group.bottom : nav.bottom;
  const scrollport=box.matches('.record') && getComputedStyle(box).overflowY!=='visible';
  return {bars,edge:scrollport ? Math.max(bars,box.getBoundingClientRect().top+box.clientTop) : bars,
    mode:scrollport ? 'scrollport' : 'viewport',nav:nav.height,subnav:bars-nav.bottom};
}"""


READING_HEAD_JS = """({owner, head, close}) => {
  const box = document.querySelector(owner), h = box.querySelector(head), r = h.getBoundingClientRect();
  const edge = (/* READING_EDGE */)(box);
  const button = close && box.querySelector(close), b = button?.getBoundingClientRect();
  const bounds=box.getBoundingClientRect(), left=Math.max(0,r.left,bounds.left), right=Math.min(innerWidth,r.right,bounds.right);
  const x = b ? (b.left+b.right)/2 : (left+right)/2;
  const y = b ? (b.top+b.bottom)/2 : (r.top+r.bottom)/2, hit = document.elementFromPoint(x,y);
  const exposed = button ? !!hit && button.contains(hit) : [left+8,x,right-8]
    .every(x=>h.contains(document.elementFromPoint(x,y))); // also probe the frozen country column
  return {top:r.top, bottom:r.bottom, height:r.height, ...edge,
    visible:getComputedStyle(h).visibility==='visible' && r.height>0 && r.top>=0 && r.bottom<=innerHeight,
    topmost:exposed,
    textTop:(/* HEAD_TEXT_TOP */)(h),
    name:h.textContent.trim(), closeWidth:b?.width, closeHeight:b?.height, x,y};
}""".replace('/* HEAD_TEXT_TOP */', HEAD_TEXT_TOP_JS).replace('/* READING_EDGE */', READING_EDGE_JS)


def reading_head_errors(m: dict, *, close: bool = False) -> list[str]:
    """Geometry oracle shared by the existing table and card jobs, with mutation tests."""
    errors = []
    if not m["visible"] or abs(m["top"] - m.get("edge", m["bars"])) > 1:
        errors.append("reading head is not fully visible at its navigation stack or scrollport edge")
    if not m["topmost"]:
        errors.append("reading head or close control is covered at its centre")
    if not m["name"]:
        errors.append("reading head has no identity")
    errors.extend(head_padding_errors(m))
    if close and (m.get("closeWidth", 0) < 44 or m.get("closeHeight", 0) < 44):
        errors.append("close hit area is smaller than 44px")
    return errors


SURFACE_PAINT_JS = r"""
  // Uniform tint gradients are solid paint, not images. Use the browser's colour parser (rgb)
  // for colour-mix/light-dark's computed color(srgb ...) too. Unknown images fail closed.
  const surface = s => {
    const rgba = c => { const [r,g,b,a]=rgb(c); return [r,g,b,a/255]; };
    let paint=rgba(s.backgroundColor);
    if(s.backgroundImage!=='none') {
      const layers=s.backgroundImage.match(/linear-gradient\((?:[^()]|\([^()]*\))*\)/g) || [];
      if(!layers.length || s.backgroundImage.replace(/linear-gradient\((?:[^()]|\([^()]*\))*\)/g,'').replace(/[,\s]/g,'')) return null;
      for(const layer of layers.reverse()) {
        const colours=(layer.match(/rgba?\([^)]*\)|color\([^)]*\)/g) || []).map(rgba);
        if(colours.length!==2 || colours[0].some((v,i)=>v!==colours[1][i])) return null;
        paint=sourceOver(colours[0],paint);
      }
    }
    return paint;
  };
"""


def head_padding_errors(m: dict) -> list[str]:
    gap, top = m.get('unstuckGap'), m.get('textTop')
    if gap is None or top is None or gap <= 0:
        return ['reading head has no measured top padding before its first text line']
    if abs(top - m.get('edge', m['bars']) - gap) > 1:
        return ['stuck head text gap differs from its unstuck top padding by more than 1px']
    return []


FOCUS_CLEARANCE_JS = r"""n => {
  let r=n.getBoundingClientRect(), rects=[...n.getClientRects()].filter(r=>r.width>0 && r.height>0);
  // A zero-height inline citation has painted text even though its line box is empty.
  if(r.height===0) {
    const range=document.createRange(); range.selectNodeContents(n);
    r=range.getBoundingClientRect(); rects=[...range.getClientRects()].filter(r=>r.width>0 && r.height>0);
  }
  const nav=document.querySelector('.secnav').getBoundingClientRect();
  const group=n.closest('.group')?.querySelector('.group-nav')?.getBoundingClientRect();
  let edge=group && group.top<=nav.bottom+1 ? group.bottom : nav.bottom;
  const panel=n.closest('.fn-detail, .fund-detail'), owner=panel || n.closest('.fn-phone, .fn-scroll, .scroll, .fund-list');
  const head=owner?.querySelector(panel ? ':scope > .fn-detail-top, :scope > summary' : '.fn-phone-head, thead, .fund-axis');
  if(head && !head.contains(n) && getComputedStyle(head).visibility==='visible') {
    const h=head.getBoundingClientRect(); edge=Math.max(edge,Math.min(h.bottom,edge+h.height));
  }
  // Actual painted rectangles, not the page's readingEdge helper or its declared scroll margins.
  const covered=rects.map(r=>{
    const x=(r.left+r.right)/2,y=(r.top+r.bottom)/2,hit=document.elementFromPoint(x,y);
    return n.contains(hit) ? null : {x,y,hit:hit ? hit.tagName.toLowerCase()+(hit.id?'#'+hit.id:'')+(hit.className?'.'+String(hit.className).trim().replace(/\s+/g,'.'):'') : 'outside viewport'};
  }).filter(Boolean);
  const exposed=rects.length>0 && !covered.length;
  return {focused:n===document.activeElement, name:n.getAttribute('aria-label') || n.textContent.trim(),
    top:r.top, bottom:r.bottom, edge, viewport:innerHeight, exposed, covered};
}"""


def focus_clearance_errors(m: dict) -> list[str]:
    errors = []
    if not m['focused']:
        errors.append('keyboard focus did not reach the intended control')
    if m['top'] < m['edge'] - 1 or m['bottom'] > m['viewport'] + 1:
        errors.append(f"keyboard focus is covered by the reading head or navigation, or outside the viewport (top {m['top']:.1f}, bottom {m['bottom']:.1f}, head edge {m['edge']:.1f})")
    if not m['exposed']:
        detail = f": {m['covered']}" if m.get('covered') else ''
        errors.append('keyboard focus is covered at its painted rectangles' + detail)
    return errors


def exercise_table_head(page, owner: str, head: str) -> list[str]:
    """Two screens of vertical travel, plus both ends of horizontal travel, on the actual head.

    The current grouped/grid data can be shorter than two screens. Repeat inert rows only for this
    geometry probe, then remove them, so the gate also catches a head bounded by one short row block.
    """
    page.mouse.move(1, 1)
    gap = unstuck_head_gap(page, owner, head)
    page.locator(owner).evaluate("""box => {
      const list=box.querySelector('tbody:last-child, .fn-phone-block:last-child > ul, .fund-chart-rows');
      const sample=list.querySelector('tr[data-institute], tr[data-j], li[data-institute]');
      for(let n=0; sample && box.getBoundingClientRect().height<innerHeight*3 && n<100; n++) {
        const row=sample.cloneNode(true); row.dataset.stickyProbe='';
        row.querySelectorAll('[id]').forEach(n=>n.removeAttribute('id')); list.appendChild(row);
      }
      scrollTo(0,scrollY+box.getBoundingClientRect().top+innerHeight*2);
    }""")
    errors = []
    try:
        for end in (False, True):
            page.locator(owner).evaluate("(box,end) => box.scrollLeft=end?box.scrollWidth:0", end)
            layout_settled(page)
            m = page.evaluate(READING_HEAD_JS, {"owner": owner, "head": head, "close": None})
            m['unstuckGap'] = gap
            errors.extend(reading_head_errors(m))
        # Native keyboard focus must reveal a row below the same head, including in a sideways scroll box.
        control = page.locator(owner).locator('tbody tr:not([data-sticky-probe]) a, tbody tr:not([data-sticky-probe]) button, li:not([data-sticky-probe]) > .fn-phone-row, li:not([data-sticky-probe]) > .fund-row').first
        page.keyboard.press('Shift')
        # Closing the earlier detail may have left this same opener focused. A no-op focus() after our
        # deliberate scroll does not exercise keyboard arrival. Start a fresh transition, then Tab both ways.
        control.evaluate('n => n.blur()')
        control.focus()
        for key in (None, 'Tab', 'Shift+Tab'):
            if key:
                page.keyboard.press(key)
            layout_settled(page)
            focused = page.locator(':focus').evaluate(FOCUS_CLEARANCE_JS)
            errors.extend(f"{owner} {key or 'focus'} {focused['name']}: " + e for e in focus_clearance_errors(focused))
        # The same head must leave when its own table ends.
        page.locator(owner).evaluate("box => scrollBy(0,box.getBoundingClientRect().bottom-document.querySelector('.secnav').getBoundingClientRect().bottom+2)")
        layout_settled(page)
        m = page.evaluate(READING_HEAD_JS, {"owner": owner, "head": head, "close": None})
        if m["bottom"] > m["bars"] + 1:
            errors.append("reading head outlives its table")
    finally:
        page.locator(owner).evaluate("box => { box.querySelectorAll('[data-sticky-probe]').forEach(n=>n.remove()); box.scrollLeft=0; }")
    return errors


def check_wide_layout(browser, base_url: str) -> list[str]:
    """At 1100px and wider, with default settings, nothing needs sideways scroll and the worldwide
    table header sticks below the section navigation and States row at every width, including sideways scrolling."""
    errors = []
    for width in WIDE_WIDTHS + (641, 1000, 320, 390, 430):
        for theme in ("light", "dark"):
            page = themed_page(browser, theme, viewport={"width": width, "height": 900})
            try:
                hydrated(page, base_url)
                page.add_style_tag(content=".offscreen, .card-row { content-visibility: visible !important; }")
                page.wait_for_selector("#world-rows tr")
                page.evaluate("document.fonts.ready")
                errors.extend(f"{width}px {theme}: " + e for e in exercise_table_head(page, '#world-scroll', 'thead'))
                result = page.evaluate(
                    """() => new Promise(done => requestAnimationFrame(() => requestAnimationFrame(() => {
                  const scrolling = [...document.querySelectorAll('body *')].filter(n => n.getClientRects().length && /auto|scroll/.test(getComputedStyle(n).overflowX) && n.scrollWidth > n.clientWidth + 1)
                    .map(n => n.id || n.getAttribute('class') || n.tagName.toLowerCase());
                  const page = document.documentElement.scrollWidth > innerWidth + 1;
                  const table = document.querySelector('#world-rows').getBoundingClientRect();
                  scrollTo(0, scrollY + table.top + 300);
                  requestAnimationFrame(() => requestAnimationFrame(() => {
                    const th = document.querySelector('table.world thead'), nav = document.querySelector('.secnav').getBoundingClientRect(), row = th.closest('.group').querySelector('.group-nav').getBoundingClientRect();
                    done({ scrolling, page, sticky: getComputedStyle(th).position === 'sticky', top: th.getBoundingClientRect().top, nav: row.top <= nav.bottom + 1 ? row.bottom : nav.bottom });
                  }));
                }))) """
                )
            finally:
                page.close()
            label = f"{width}px {theme}"
            if width < 1100:
                if width <= 640 and "world-scroll" not in result["scrolling"]:
                    errors.append(f"{label}: phone table no longer scrolls sideways (gate expectation outdated?)")
                if result['page']:
                    errors.append(f'{label}: table overflows the page')
                continue
            if result["page"] or result["scrolling"]:
                errors.append(f"{label}: needs sideways scroll with default settings: {', '.join(result['scrolling']) or 'page'}")
            if not result["sticky"] or abs(result["top"] - result["nav"]) > 1:
                errors.append(f"{label}: table header does not stick below the section navigation and group row (top {result['top']:.0f}, rows {result['nav']:.0f})")
    return errors


THEME_PARITY_PAIRS = {"--line": "hairline and control border", "--field-fill": "control fill", "--selected-fill": "selected fill"}
THEME_PARITY_TOLERANCE = 0.1  # contrast-ratio units between the light and dark step


ANCHOR_ALIASES = {"map-h": "map", "test-h": "testing", "setup-h": "set-up", "fund-h": "funding", "developments-h": "news", "states-h": "europe",
                  "world-h": "world", "data-h": "data", "method-h": "method", "definition-full": "definition"}


def check_anchor_aliases(browser, base_url: str) -> list[str]:
    """Principle Anchors: every reader anchor exists once, and each name used before 1.0.2 lands on its new anchor, its heading in the upper half of the window below the sticky rows."""
    errors: list[str] = []
    page = browser.new_page(viewport={"width": 1280, "height": 800})
    hydrated(page, base_url)
    for new in ANCHOR_ALIASES.values():
        if page.evaluate("id => document.querySelectorAll('#' + CSS.escape(id)).length", new) != 1:
            errors.append(f"anchor #{new} is missing or not unique")
    page.close()
    for old, new in ANCHOR_ALIASES.items():
        page = browser.new_page(viewport={"width": 1280, "height": 800})
        hydrated(page, f"{base_url}#{old}")
        page.evaluate("document.fonts.ready")
        page.wait_for_timeout(300)
        # The heading (the section top for the visually hidden Map heading) lands in the upper half of the window, below the sticky rows.
        got = page.evaluate("""id => { const t = document.getElementById(id); if (!t) return [location.hash, null, null, innerHeight, false];
          const s = t.tagName === 'H2' ? t.closest('section') : t, top = (t.classList.contains('vh') ? s : t).getBoundingClientRect().top;
          let bars = document.querySelector('.secnav').getBoundingClientRect().bottom;
          for (const row of document.querySelectorAll('.group-nav')) { const r = row.getBoundingClientRect(); if (r.top < bars + 1 && r.bottom > bars) bars = r.bottom; }
          return [location.hash, top, bars, innerHeight, innerHeight + scrollY >= document.documentElement.scrollHeight - 1]; }""", new)
        hash, top, bars, height, at_end = got  # a heading near the page end can rise only as far as the page scrolls
        if hash != f"#{new}" or top is None or top < bars - 1 or (top > height / 2 and not at_end):
            errors.append(f"old anchor #{old} does not land on #{new} with its heading in the upper half below the sticky rows: hash {hash}, heading top {top}, rows end {bars}, window {height}")
        page.close()
    return errors


def check_theme_parity(browser, base_url: str) -> list[str]:
    """Each surface token steps from the ground by a similar contrast ratio in light and dark.

    Floating surfaces (paper, with their shadow) are exempt.
    """
    steps = {}
    for theme in ("light", "dark"):
        page = themed_page(browser, theme, viewport={"width": 1000, "height": 800})
        try:
            hydrated(page, base_url)
            page.wait_for_selector("#world-rows tr")
            steps[theme] = page.evaluate(
                """names => {
              const canvas = document.createElement('canvas').getContext('2d', { willReadFrequently: true });
              const rgb = c => { canvas.clearRect(0, 0, 1, 1); canvas.fillStyle = c; canvas.fillRect(0, 0, 1, 1); return [...canvas.getImageData(0, 0, 1, 1).data].slice(0, 3); };
              const lum = c => { const f = v => { v /= 255; return v <= .03928 ? v / 12.92 : ((v + .055) / 1.055) ** 2.4; }; const [r, g, b] = rgb(c).map(f); return .2126 * r + .7152 * g + .0722 * b; };
              const token = name => { const probe = document.createElement('span'); probe.style.color = `var(${name})`; document.body.appendChild(probe); const c = getComputedStyle(probe).color; probe.remove(); return c; };
              const ground = lum(token('--ground'));
              return Object.fromEntries(names.map(name => { const l = lum(token(name)); return [name, (Math.max(l, ground) + .05) / (Math.min(l, ground) + .05)]; }));
            }""",
                list(THEME_PARITY_PAIRS),
            )
        finally:
            page.close()
    return [
        f"theme parity: {label} ({name}) steps {steps['light'][name]:.2f}:1 from the ground in light but {steps['dark'][name]:.2f}:1 in dark"
        for name, label in THEME_PARITY_PAIRS.items()
        if abs(steps["light"][name] - steps["dark"][name]) > THEME_PARITY_TOLERANCE
    ]


HIGHLIGHT_STEP = 1.17  # the active item's contrast step from the paper, both themes (principle Highlight); muted text caps the light step
HIGHLIGHT_JS = """() => {
  const canvas = document.createElement('canvas').getContext('2d', { willReadFrequently: true });
  const rgb = c => { canvas.clearRect(0, 0, 1, 1); canvas.fillStyle = c; canvas.fillRect(0, 0, 1, 1); return [...canvas.getImageData(0, 0, 1, 1).data].slice(0, 3); };
  const lum = c => { const f = v => { v /= 255; return v <= .03928 ? v / 12.92 : ((v + .055) / 1.055) ** 2.4; }; const [r, g, b] = rgb(c).map(f); return .2126 * r + .7152 * g + .0722 * b; };
  const ratio = (a, b) => { const x = lum(a), y = lum(b); return (Math.max(x, y) + .05) / (Math.min(x, y) + .05); };
  const token = name => { const p = document.createElement('span'); p.style.color = `var(${name})`; document.body.appendChild(p); const c = getComputedStyle(p).color; p.remove(); return c; };
  return { step: ratio(token('--highlight-fill'), token('--paper')),
    texts: [...document.querySelectorAll('#map-search-list li[aria-selected="true"], #map-search-list li[aria-selected="true"] *')].filter(n => [...n.childNodes].some(c => c.nodeType === 3 && c.textContent.trim())).map(n => ratio(getComputedStyle(n).color, token('--highlight-fill'))),
    selected: getComputedStyle(document.querySelector('#map-search-list li[aria-selected="true"]') || document.body).backgroundColor, list: getComputedStyle(document.getElementById('map-search-list')).backgroundColor };
}"""


def check_highlight_fill(browser, base_url: str) -> list[str]:
    """Principle Highlight: the current item in a list or menu stands out from the paper by HIGHLIGHT_STEP in both themes, its text at least 4.5:1,
    and the active suggestion has a visible fill."""
    errors = []
    for theme in ("light", "dark"):
        page = themed_page(browser, theme, viewport={"width": 1280, "height": 900})
        try:
            hydrated(page, base_url)
            page.locator("#map-search").fill("den")
            page.locator("#map-search").press("ArrowDown")
            page.wait_for_timeout(50)
            got = page.evaluate(HIGHLIGHT_JS)
            if got["step"] < HIGHLIGHT_STEP:
                errors.append(f"{theme}: --highlight-fill steps {got['step']:.2f}:1 from the paper, below {HIGHLIGHT_STEP}:1")
            if not got["texts"]:
                errors.append(f"{theme}: no active suggestion with text to measure")
            elif min(got["texts"]) < 4.5:
                errors.append(f"{theme}: text on the active suggestion is {min(got['texts']):.2f}:1, below 4.5:1")
            if got["selected"] == got["list"]:  # the active suggestion cannot be seen
                errors.append(f"{theme}: the active suggestion has the list's own fill ({got['list']})")
        finally:
            page.close()
    return [f"highlight: {e}" for e in errors]



def check_group_rows(browser, base_url: str) -> list[str]:
    """Phones and sticky group rows (owner report from an Android phone): at 360, 390 and 412px, also with the root
    text 10% larger, the bar shows all five items without sideways scroll. Group links mark the current section,
    and a jump from a group row or hash link lands the heading below both rows."""
    errors: list[str] = []
    bar = """() => { const n = document.querySelector('.secnav'), m = n.querySelector('.measure'), items = [...m.children].filter(a => a.getBoundingClientRect().width);
      return [m.scrollWidth - m.clientWidth, items.map(a => a.innerText.trim())]; }"""
    for width in (360, 390, 412):
        for scale in ("100%", "110%"):
            page = browser.new_page(viewport={"width": width, "height": 780})
            hydrated(page, base_url)
            page.evaluate(f"document.documentElement.style.fontSize = '{scale}'")
            page.evaluate("document.fonts.ready")
            page.evaluate("scrollTo(0, document.querySelector('#testing').closest('section').offsetTop)")
            page.wait_for_timeout(150)
            got = page.evaluate(bar)
            if got[0] != 0 or len(got[1]) != len(SECTION_BAR) or not all(got[1]):
                errors.append(f"{width}px at {scale} text: the bar cuts off or lacks one of the five items (overflow, items: {got[0]}, {got[1]})")
            page.close()
    rows = """() => { const nav = document.querySelector('.secnav').getBoundingClientRect().bottom;
      return [...document.querySelectorAll('.group-nav')].filter(r => { const b = r.getBoundingClientRect(); return b.top <= nav + 1 && b.bottom > nav + 1; })
        .map(r => [r.getAttribute('aria-label'), Math.round(r.getBoundingClientRect().top - nav), [...r.querySelectorAll('a.active')].map(a => a.getAttribute('href')), [...r.querySelectorAll('a')].map(a => a.getAttribute('href'))]); }"""
    for width, height in ((360, 780), (1280, 800)):
        page = browser.new_page(viewport={"width": width, "height": height})
        hydrated(page, base_url)
        page.evaluate("document.fonts.ready")
        for key, label in (("set-up", "Compare"), ("funding", "Compare"), ("world", "States"), ("method", "Data")):
            page.evaluate("scrollTo(0, 0)")
            page.locator(f'.group-nav a[href="#{key}"]').click()
            page.wait_for_timeout(200)
            got = page.evaluate(rows)
            heading = page.evaluate("""k => { const h = document.getElementById(k).getBoundingClientRect(), nav = document.querySelector('.secnav').getBoundingClientRect().bottom;
              const row = [...document.querySelectorAll('.group-nav')].map(r => r.getBoundingClientRect()).find(b => b.top <= nav + 1 && b.bottom > nav);
              return [row ? row.bottom : null, h.top, h.bottom <= innerHeight]; }""", key)
            if not got or not (got[0][0] or "").strip() or got[0][1] != 0 or got[0][2] != [f"#{key}"] or f"#{key}" not in got[0][3]:
                errors.append(f"{width}px: after the {label} row's link to #{key}, its row is not stuck under the bar marking that link ({got})")
            if heading[0] is None or heading[1] < heading[0] or not heading[2]:
                errors.append(f"{width}px: #{key} lands with its heading under the sticky rows or out of view (row bottom, heading top, in view: {heading})")
        # Scrolling marks the section under the row, forwards and back (no link chosen: the spy alone decides).
        for key in ("testing", "set-up", "funding", "set-up", "testing"):
            page.evaluate("""k => { const nav = document.querySelector('.secnav').getBoundingClientRect().bottom, row = document.querySelector('.group-nav:has(a[href="#testing"])');
              scrollTo(0, document.getElementById(k).closest('section').getBoundingClientRect().top + scrollY - nav - row.offsetHeight - 8); }""", key)
            page.wait_for_timeout(150)
            got = page.evaluate("""() => [...document.querySelectorAll('.group-nav:has(a[href="#testing"]) a')].filter(a => a.classList.contains('active') || a.hasAttribute('aria-current')).map(a => [a.getAttribute('href'), a.getAttribute('aria-current')])""")
            if got != [[f"#{key}", "location"]]:
                errors.append(f"{width}px: scrolled to #{key}, the Compare row marks {got}")
        # Bar rows, names disclosures and opened list rows all escape the sticky rows when reached by Tab.
        for selector in ('.fund-row', '.fund-names-toggle', '.fund-name-row'):
            page.locator('#fund-rows ' + selector).first.evaluate(REVEAL_FUNDING_LIST_JS)
            got = page.evaluate("""selector => { const s = document.querySelector('#fund-rows '+selector), all = [...document.querySelectorAll('a[href], button, summary, input, select, [tabindex="0"]')].filter(n => n.getClientRects().length && n.tabIndex >= 0 && !n.matches(':disabled'));
              const prev = all[all.indexOf(s) - 1];
              if (!s || !prev) return false;
              const nav = document.querySelector('.secnav').getBoundingClientRect().bottom;
              scrollTo(0, s.getBoundingClientRect().top + scrollY - nav - 4); prev.focus({ preventScroll: true }); return true; }""", selector)
            if not got:
                errors.append(f"{width}px: cannot check funding {selector} focus by Tab: no control or preceding Tab stop")
            else:
                page.wait_for_timeout(100)
                page.keyboard.press("Tab")
                page.wait_for_timeout(200)
                got = page.evaluate("""selector => { const a = document.activeElement, s = document.querySelector('#fund-rows '+selector), row = document.querySelector('.group-nav:has(a[href="#testing"])').getBoundingClientRect();
                  return [a === s, Math.round(a.getBoundingClientRect().top), Math.round(row.bottom)]; }""", selector)
                if not got[0] or got[1] < got[2]:
                    errors.append(f"{width}px: funding {selector} focused by Tab stays under the group row (focused, top, row bottom: {got})")
        page.close()
        # A hash link from elsewhere lands the same way, on a fresh page.
        page = browser.new_page(viewport={"width": width, "height": height})
        hydrated(page, base_url + "#funding")
        page.evaluate("document.fonts.ready")
        page.wait_for_timeout(300)
        heading = page.evaluate("""() => { const nav = document.querySelector('.secnav').getBoundingClientRect().bottom, row = document.querySelector('.group-nav:has(a[href="#testing"])').getBoundingClientRect();
          return [Math.round(row.top - nav), row.bottom, document.getElementById('funding').getBoundingClientRect().top]; }""")
        if heading[0] != 0 or heading[2] < heading[1]:
            errors.append(f"{width}px: the hash link #funding lands with its heading under the sticky rows (row offset, row bottom, heading top: {heading})")
        page.close()
    return errors


def check_access_fixes(browser, base_url: str) -> list[str]:
    """Accessibility rules from the launch review: the table reflows at mid widths; list, search and map picks focus
    the record heading below the sticky bar (dots say they control the record); a search pick happens on click, not
    press; card names are h4 under their group's h3; controls focused under the sticky bar are brought out from under
    it; tooltips stay while the pointer is on them and fit the window; unchecked boxes' edges reach 3:1; without
    scripting no dead controls show; no names inside one-image mark rows; the wordmark sits in the one banner; at 390px the section bar is one row
    with no sideways scroll (its title hidden on phones; check_group_rows covers 360px and enlarged text)."""
    errors: list[str] = []
    for width in (650, 690, 730):
        page = browser.new_page(viewport={"width": width, "height": 800})
        hydrated(page, base_url)
        page.evaluate("document.fonts.ready")
        over = page.evaluate("document.documentElement.scrollWidth - innerWidth")
        if over > 0:
            errors.append(f"{width}px: the page scrolls sideways by {over}px")
        page.close()
    for width in (390, 320):
        page = browser.new_page(viewport={"width": width, "height": 700})
        hydrated(page, base_url)
        page.evaluate("document.fonts.ready")
        if width == 390:  # the bar is one row of the five items, the title hidden on phones, with no sideways scroll (320px may scroll)
            page.evaluate("scrollTo(0, document.querySelector('#testing').closest('section').offsetTop)")
            page.wait_for_timeout(150)
            got = page.evaluate("""() => { const n = document.querySelector('.secnav'), m = n.querySelector('.measure'), items = [...m.children].filter(a => a.getBoundingClientRect().width);
              return [n.classList.contains('scrolled'), m.scrollWidth - m.clientWidth, items.map(a => a.innerText.trim())]; }""")
            if got[0] is not True or got[1] != 0 or len(got[2]) != len(SECTION_BAR) or not all(got[2]):
                errors.append(f"390px: the section bar is not scrolled, or cuts off or lacks one of the five items (scrolled, overflow, items: {got[0]}, {got[1]}, {got[2]})")
            page.evaluate("scrollTo(0, 0)")
        box = page.locator("#map-search")
        box.fill("ger")
        page.locator("#map-search-0").dispatch_event("mousedown")
        if not page.evaluate("document.getElementById('record').hidden"):
            errors.append(f"{width}px: pressing a search suggestion opens it before the click")
            box.fill("ger")
        page.locator("#map-search-0").click()
        got = page.evaluate("() => { const a = document.activeElement, r = a.getBoundingClientRect(), nav = document.querySelector('.secnav').getBoundingClientRect(); return [a.id, a.querySelector('.head-state')?.textContent, r.top >= nav.bottom - 1 && r.bottom <= innerHeight]; }")
        if got != ["record-name", "Germany", True]:
            errors.append(f"{width}px: a search pick does not focus the record heading in view below the bar ({got})")
        if width == 390:  # the list below the map: a control partly under the sticky bar comes out from under it when focused
            page.locator("#record .record-close").click()
            for selector in ('#regions .region-head[data-region="Asia"]', "#region-rows-europe .pick"):
                got = page.evaluate("""s => { const n = document.querySelector(s), nav = document.querySelector('.secnav');
                  if (document.activeElement) document.activeElement.blur();
                  for (let k = 0; k < 4; k++) scrollBy({ top: n.getBoundingClientRect().top - (nav.getBoundingClientRect().bottom - 12), behavior: 'instant' });
                  const before = n.getBoundingClientRect().top; n.focus(); return [before, n.getBoundingClientRect().top, nav.getBoundingClientRect().bottom]; }""", selector)
                if got[0] >= got[2] or got[1] < got[2] - 0.5:
                    errors.append(f"{width}px: {selector} focused under the sticky bar stays under it (from {got[0]:.0f} to {got[1]:.0f}, bar {got[2]:.0f})")
        page.close()
    page = browser.new_page(viewport={"width": 1440, "height": 700})
    hydrated(page, base_url, map_drawn=True)
    page.evaluate("() => [...document.querySelectorAll('#worldmap svg')].find(n => n.getClientRects().length).querySelector('.tgt[data-j=\"FR\"]').focus()")
    page.keyboard.press("Enter")
    got = page.evaluate("() => [document.activeElement.id, [...document.querySelectorAll('#worldmap svg')].find(n => n.getClientRects().length).querySelector('.tgt[data-j=\"FR\"]').getAttribute('aria-expanded')]")
    if got != ["record-name", "true"]:
        errors.append(f"map: Enter on a dot does not focus the record heading and mark the dot expanded ({got})")
    page.keyboard.press("Escape")
    bad = page.evaluate("""() => [...document.querySelectorAll('.card')].filter(c => { const n = c.querySelector('.name'); const g = c.closest('.statusgroup, .eu-level'); return !n || n.tagName !== 'H4' || !g || !g.querySelector(':scope > h3, :scope > .eu-head > h3'); }).map(c => c.id)""")
    if bad:
        errors.append(f"card names are not h4 under a group h3: {bad[:6]}")
    # Tooltips: the pointer can move onto the tip and it stays; it fits a short window and scrolls.
    page.evaluate("scrollTo(0, 0)")
    page.locator('#region-chips button[data-region="Europe"]').click()  # the term is in Europe's headline
    term = page.locator("#headline .hl-version.current abbr.g").first
    term.scroll_into_view_if_needed()
    t = term.bounding_box()
    page.mouse.move(t["x"] + t["width"] / 2, t["y"] + t["height"] / 2)
    tip = page.locator("#tip")
    b = tip.bounding_box() if tip.is_visible() else None
    if not b:
        errors.append("tooltip: hovering a term shows no tooltip")
    else:
        page.mouse.move(b["x"] + b["width"] / 2, b["y"] + b["height"] / 2, steps=8)
        page.wait_for_timeout(500)
        if not tip.is_visible():
            errors.append("tooltip: it hides while the pointer moves onto it")
        page.mouse.move(0, 0)
        page.wait_for_timeout(600)
        if tip.is_visible():
            errors.append("tooltip: it stays after the pointer leaves")
    style = page.evaluate("() => { const s = getComputedStyle(document.getElementById('tip')); return [s.maxHeight, s.overflowY, s.pointerEvents]; }")
    # A tooltip taller than a short window scrolls, and scrolling it keeps it open.
    page.set_viewport_size({"width": 800, "height": 220})
    phrase = page.locator('#world-rows .smark[data-state="confirmed"]').first
    # Long probe text exercises tooltip scrolling.
    phrase.evaluate("n => { const long = Array(12).fill(n.dataset.tip || n.getAttribute('aria-description') || n.title).join(' '); n.dataset.tip = long; if (n.title) n.title = long; n.scrollIntoView({ block: 'center', behavior: 'instant' }); }")
    page.wait_for_timeout(100)
    p = phrase.bounding_box()
    page.mouse.move(p["x"] + 4, p["y"] + p["height"] / 2)
    b = tip.bounding_box() if tip.is_visible() else None
    if not b:
        errors.append("tooltip: the Known activity explanation shows no tooltip at 800 x 220")
    else:
        page.mouse.move(b["x"] + b["width"] / 2, b["y"] + b["height"] / 2, steps=8)
        page.mouse.wheel(0, 60)
        page.wait_for_timeout(300)
        got = page.evaluate("() => { const t = document.getElementById('tip'); return [!t.hidden, t.scrollHeight > t.clientHeight, t.scrollTop > 0, t.getBoundingClientRect().bottom <= innerHeight + 0.5]; }")
        if got != [True, True, True, True]:
            errors.append(f"tooltip: at 800 x 220 the long tooltip does not fit and scroll while staying open (open, overflows, scrolled, fits: {got})")
    page.mouse.move(0, 0)
    page.set_viewport_size({"width": 1440, "height": 700})
    if style[0] == "none" or style[1] != "auto" or style[2] == "none":
        errors.append(f"tooltip: no window-bounded height, scrolling or pointer events ({style})")
    for theme in ("light", "dark"):
        set_theme(page, theme)
        ratios = page.evaluate("() => {" + CONTRAST_JS + """
          const box = document.querySelector('input[type="checkbox"]'), edge = getComputedStyle(box).borderTopColor;
          return [ratio(edge, token('--paper')), ratio(edge, token('--ground'))]; }""")
        if min(ratios) < 3:
            errors.append(f"{theme}: unchecked checkbox edge is {min(ratios):.2f}:1 on its surface, below 3:1")
    set_theme(page, "light")
    named = page.evaluate("() => document.querySelectorAll('[role=img] [aria-label], [role=img] [aria-description]').length")
    if named:
        errors.append(f"{named} elements inside a one-image mark row carry their own name or description")
    banner = page.evaluate("""() => { const tops = [...document.querySelectorAll('header')].filter(h => !h.parentElement.closest('main, section, article, aside, nav, footer'));
      const w = document.querySelector('.wordmark'), h1 = document.querySelector('h1'); return [tops.length, !!(tops[0] && tops[0].contains(w) && tops[0].contains(h1))]; }""")
    if banner != [1, True]:
        errors.append(f"landmarks: expected one banner holding the wordmark and the title ({banner})")
    page.close()
    page = browser.new_page(viewport={"width": 1440, "height": 900}, java_script_enabled=False)
    page.goto(base_url)
    shown = page.evaluate("() => ['.map-search', '#regions button', '.tbar', '#world-count button', '#developments-more'].filter(s => [...document.querySelectorAll(s)].some(n => n.getClientRects().length))")
    if shown:
        errors.append(f"without scripting, controls that need it still show: {shown}")
    dots = page.evaluate("() => [...document.querySelectorAll('#worldmap .tgt')].filter(t => t.hasAttribute('tabindex') || t.hasAttribute('role')).length")
    if dots:
        errors.append(f"without scripting, {dots} saved map dots are still buttons")
    # Supporting text: without scripting each (i) shows its note, since no tooltip can open.
    notes = page.evaluate(NOSCRIPT_NOTES_JS)
    if sorted(n["id"] for n in notes) != sorted(NOSCRIPT_INFO_IDS):
        errors.append(f"without scripting, (i) notes {sorted(n['id'] for n in notes)}, expected {sorted(NOSCRIPT_INFO_IDS)}")
    silent = [n["id"] for n in notes if not n["ok"]]
    if silent:
        errors.append(f"without scripting, (i) notes are not shown as visible text: {silent}")
    # Regression case: a hidden note (the button still visible) must fail this check.
    page.evaluate("() => { const s = document.createElement('style'); s.textContent = 'button.info::after { display: none !important; }'; document.head.appendChild(s); }")  # not add_style_tag, which waits for a load event
    if any(n["ok"] for n in page.evaluate(NOSCRIPT_NOTES_JS)):
        errors.append("without scripting: the (i) note check passes a note hidden with ::after { display: none }")
    page.close()
    return errors


# Each (i)'s note as rendered without scripting: its ::after carries the tip text, is displayed, visible and opaque, and gives
# the button (whose own "i" is at font size 0) at least one note line of height; the button is visible and on top.
NOSCRIPT_NOTES_JS = """() => [...document.querySelectorAll('button.info')].map(n => {
  n.scrollIntoView({ block: 'center' });
  const r = n.getBoundingClientRect(), c = getComputedStyle(n), a = getComputedStyle(n, '::after'), x = r.left + Math.min(8, r.width / 2), y = r.top + r.height / 2, hit = document.elementFromPoint(x, y);
  const line = parseFloat(a.lineHeight) || parseFloat(a.fontSize) * 1.2;
  const note = a.content === JSON.stringify(n.dataset.tip) && a.display !== 'none' && a.visibility === 'visible' && parseFloat(a.opacity) > 0 && parseFloat(a.fontSize) > 0 && r.height >= line - 1 && r.width > parseFloat(a.fontSize);
  const visible = n.checkVisibility({ visibilityProperty: true, opacityProperty: true }) && c.clipPath === 'none';
  return { id: n.id, ok: note && visible && !!hit && (hit === n || n.contains(hit)) };
})"""
NOSCRIPT_INFO_IDS = ("map-info", "figures-scope", "test-scope", "eu-scope", "recheck-line")  # every (i) in the saved page (principle: Supporting text)


CONTRAST_JS = """
  const canvas = document.createElement('canvas').getContext('2d', { willReadFrequently: true });
  const rgb = c => { canvas.clearRect(0, 0, 1, 1); canvas.fillStyle = c; canvas.fillRect(0, 0, 1, 1); return [...canvas.getImageData(0, 0, 1, 1).data]; };
  const lum = c => { const f = v => { v /= 255; return v <= .03928 ? v / 12.92 : ((v + .055) / 1.055) ** 2.4; }; const [r, g, b] = rgb(c).map(f); return .2126 * r + .7152 * g + .0722 * b; };
  const ratio = (a, b) => { const x = lum(a), y = lum(b); return (Math.max(x, y) + .05) / (Math.min(x, y) + .05); };
  const opaque = c => rgb(c)[3] === 255;
  const token = name => { const probe = document.createElement('span'); probe.style.color = `var(${name})`; document.body.appendChild(probe); const c = getComputedStyle(probe).color; probe.remove(); return c; };
"""
STATUS_ORDER = ("established", "planned", "proposed", "exploring", "none_identified")


def status_marks(page) -> list[dict]:
    """Measure every status mark the page draws (legend swatch, first map dot of each status in the drawing on
    screen): edge against the ground (a dot's stroke at its drawn width) and text against what is behind it."""
    return page.evaluate(
        "statuses => {" + CONTRAST_JS + """
      const ground = token('--ground'), out = [];
      const behind = node => { for (let n = node; n; n = n.parentElement) { const s = getComputedStyle(n); if (opaque(s.backgroundColor)) return { colour: s.backgroundColor, pattern: false }; if (s.backgroundImage !== 'none') return { colour: null, pattern: true }; } return { colour: ground, pattern: false }; };
      const svg = [...document.querySelectorAll('#worldmap svg')].find(n => n.getClientRects().length);
      const scale = svg ? svg.getBoundingClientRect().width / svg.viewBox.baseVal.width : 1;
      for (const status of statuses) for (const [kind, node] of [['swatch', document.querySelector('#legend .sw.st-' + status)], ['dot', svg && svg.querySelector(':scope > g > .dot.st-' + status)]]) {
        if (!node) { out.push({ status, kind, missing: true }); continue; }
        if (kind === 'dot') {
          const s = getComputedStyle(node), hatch = s.fill.startsWith('url') && svg.querySelector('.hatch-line'), centre = node.nextElementSibling && node.nextElementSibling.matches('.dot-centre') ? node.nextElementSibling : null;
          out.push({ status, kind, edge: ratio(s.stroke, ground), edgeWidth: parseFloat(s.strokeWidth) * scale, image: hatch ? 'hatch' : 'none', text: [],
            centre: centre ? ratio(getComputedStyle(centre).fill, ground) : 0, centreRound: !!centre && centre.tagName.toLowerCase() === 'circle' });
          continue;
        }
        const s = getComputedStyle(node), shadow = s.boxShadow.match(/^(rgba?\\([^)]*\\)) 0px 0px 0px ([\\d.]+)px inset$/);
        const m = { status, kind, edge: ratio(s.borderTopColor, ground), edgeWidth: parseFloat(s.borderTopWidth), image: s.backgroundImage, ring: !!shadow && rgb(shadow[1]).join() === rgb(ground).join() && parseFloat(shadow[2]) >= 1,
          text: [] };
        if (status === 'planned') {
          // A square swatch: a square centre (its own fill, the established colour) inside an inset page-colour ring that leaves it visible (at least 2px across).
          const ring = s.boxShadow.match(/^(rgba?\\([^)]*\\)) 0px 0px 0px ([\\d.]+)px inset$/), centre = ring && s.backgroundImage === 'none' && s.backgroundColor;
          m.centre = centre && opaque(centre) ? ratio(centre, ground) : 0;
          m.centreFill = !!centre && rgb(centre).join() === rgb(token('--st-est')).join();
          const square = n => { const c = getComputedStyle(n), w = n.getBoundingClientRect().width; return ['borderTopLeftRadius', 'borderTopRightRadius', 'borderBottomRightRadius', 'borderBottomLeftRadius'].every(k => !c[k].includes('%') && parseFloat(c[k]) < .25 * w); };
          m.squareCentre = square(node) && !!ring && rgb(ring[1]).join() === rgb(ground).join() && parseFloat(ring[2]) >= 1 && node.clientWidth - 2 * parseFloat(ring[2]) >= 2;
        }
        out.push(m);
      }
      return out;
    }""",
        list(STATUS_ORDER),
    )


def check_tooltip_cues(page) -> list[str]:
    """Visible tooltip terms and values must have focusable tooltips."""
    got = page.evaluate("""() => [...document.querySelectorAll('abbr.g')].filter(n => n.getClientRects().length).map(n =>
      [n.textContent.trim().slice(0, 40), n.tabIndex === 0 && !!(n.dataset.tip || n.getAttribute('aria-description'))])""")
    return [f"tooltip cues: {text!r} has no focusable tooltip" for text, tip in got if not tip]


def check_group_row_cover(page) -> list[str]:
    """Principle Group rows: a sticky group row's ground reaches up to the section bar (no strip of text between them
    when the bar's measured height is rounded) and sideways at least as far as an arrived card's tint spreads (no tint
    beside the row). Read from the computed shadows, so it holds at every width and theme without scrolling."""
    found = page.evaluate("""() => {
        const probe = document.createElement('div'); probe.className = 'card arrived'; probe.style.position = 'absolute'; document.body.appendChild(probe);
        const tint = getComputedStyle(probe).boxShadow; probe.remove();
        return [tint, [...document.querySelectorAll('.group-nav')].filter(n => n.getClientRects().length).map(n => { const s = getComputedStyle(n);
            return [n.textContent.trim().slice(0, 30), s.boxShadow, s.backgroundColor]; })]; }""")
    return row_cover_errors(*found)


def shadow_layers(value: str) -> list[dict]:
    """A computed box-shadow ("rgb(23, 25, 26) 0px -8px 0px 8px, ...") as layers: colour, inset, x, y, blur, spread."""
    if not value or value == "none":
        return []
    layers = []
    for layer in re.split(r",(?![^(]*\))", value):
        colour = (re.search(r"rgba?\([^)]*\)", layer) or [""])[0]
        numbers = [float(n) for n in re.findall(r"(-?[\d.]+)px", layer.replace(colour, ""))] + [0.0] * 4
        layers.append({"colour": colour, "inset": "inset" in layer, "x": numbers[0], "y": numbers[1], "blur": numbers[2], "spread": numbers[3]})
    return layers


def row_cover_errors(tint_shadow: str, rows: list) -> list[str]:
    """One outer shadow layer in the row's own ground must cover, as one rectangle, the arrival tint's spread on both
    sides and at least a pixel above the row: separate layers leave the upper corners open."""
    tint = max([layer["spread"] for layer in shadow_layers(tint_shadow) if not layer["inset"]] or [0])
    errors = []
    for text, shadow, ground in rows:
        covers = [layer for layer in shadow_layers(shadow) if not layer["inset"] and layer["colour"] == ground
                  and layer["blur"] == 0 and layer["x"] - layer["spread"] <= -tint and layer["x"] + layer["spread"] >= tint
                  and layer["y"] - layer["spread"] <= -1 and layer["y"] + layer["spread"] >= 0]  # solid, down to the row's own bottom
        if not covers:
            errors.append(f"group row cover: {text!r} has no ground layer covering {tint:g}px on both sides and above the row as one rectangle")
    return errors


TERM_SCOPES = "details.card > summary, .card, #record"  # a folded card's summary row is its own view


def term_scope(node):
    """The nearest card summary row, card or record panel holding node (the page script's SCOPE)."""
    for parent in node.parents:
        if parent.name == "summary" and parent.parent is not None and parent.parent.name == "details" and "card" in parent.parent.get("class", []):
            return parent
        if "card" in (parent.get("class") or []) or parent.get("id") == "record":
            return parent
    return None


def spelt_out(glossary: dict, label: str, state: str | None) -> str | None:
    """The text that spells a term out just before it, lower-cased: its expansion's name up to the first sentence end,
    then " (" ("ai security institute (" before AISI), as the page script reads it; None without an expansion."""
    expansion = glossary.get(label)
    if isinstance(expansion, dict):
        expansion = expansion.get(state)
    return re.split(r"[;(]|\.(?=\s|$)", expansion)[0].strip().lower() + " (" if expansion else None


def check_terms_once(html, data: dict) -> list[str]:
    """Principle Tooltip cues, on the saved page: in each card (its summary row and its body each) and the record
    panel, a glossary term's first use is marked unless it spells the term out ("... Institute (AISI)"), and no later
    use is. Expansions per state come from the card's data-j, as the page script reads them."""
    soup = html if isinstance(html, BeautifulSoup) else BeautifulSoup(html, "html.parser")
    glossary = data["dataset"].get("glossary") or {}
    if not glossary:
        return []
    keys = sorted(glossary, key=len, reverse=True)
    term = re.compile(r"(^|[^A-Za-z0-9-])(" + "|".join(map(re.escape, keys)) + r")(?![A-Za-z0-9-])")
    errors = []
    for box in soup.select(TERM_SCOPES):
        host = box if box.has_attr("data-j") else box.find_parent(attrs={"data-j": True})
        state = host["data-j"] if host else None
        seen: set[str] = set()
        name = box.get("id") or (box.find_parent(class_="card") or box).get("id") or "record"
        for node in box.descendants:
            if term_scope(node) is not box:
                continue
            if getattr(node, "name", None) == "abbr" and "g" in node.get("class", []) and "val" not in node.get("class", []):
                label = node.get_text().strip()
                if label in glossary and label in seen:
                    errors.append(f"terms once: {name} marks {label!r} again after its first use")
                elif label in glossary and isinstance(node.previous_sibling, NavigableString) and (spelt := spelt_out(glossary, label, state)) and str(node.previous_sibling).lower().endswith(spelt):
                    errors.append(f"terms once: {name} marks {label!r} where the text spells it out")
                seen.add(label)
                continue
            if not isinstance(node, NavigableString) or isinstance(node, Comment) or node.find_parent(["abbr", "a", "button", "option", "select", "script", "style"]) is not None or node.find_parent(class_=("conf", "original-text")) is not None:
                continue
            text = str(node)
            for match in term.finditer(text):
                label, start = match.group(2), match.start(2)
                expansion = glossary[label]
                if isinstance(expansion, dict):
                    expansion = expansion.get(state)
                if not expansion or label in seen:
                    continue
                seen.add(label)
                if not text[:start].lower().endswith(spelt_out(glossary, label, state)):
                    errors.append(f"terms once: {name} leaves the first use of {label!r} unmarked")
    return errors


TEXT_CONTRAST = 4.5  # WCAG 1.4.3 AA: body and meta text
LARGE_TEXT_CONTRAST = 3.0  # at least 24px, or 18.66px bold (WCAG large text)
# How a colour painted inside an element reaches the screen: source-over onto the element's own background, the
# element's opacity applied to that whole group, then the same through each ancestor, onto the white canvas.
# layers: [[r, g, b, a] background, opacity, has a background image] from the element outwards; colours as 0-255 with
# alpha 0-1. painted() returns the opaque [r, g, b] seen on screen; imageShows() whether a background image paints
# where anything inside does not yet cover it (then there is no single background). Pure, so tests run them without
# a browser.
PAINT_JS = """
  const sourceOver = (top, bottom) => { const a = top[3] + bottom[3] * (1 - top[3]);
    return a ? [0, 1, 2].map(i => (top[i] * top[3] + bottom[i] * bottom[3] * (1 - top[3])) / a).concat(a) : [0, 0, 0, 0]; };
  const painted = (colour, layers) => { let c = colour;
    for (const [background, opacity] of layers) { c = sourceOver(c, background); c = [c[0], c[1], c[2], c[3] * opacity]; }
    return sourceOver(c, [255, 255, 255, 1]).slice(0, 3); };
  const imageShows = layers => { let cover = 0;  // how much of what lies below the content so far is hidden by it
    for (const [background, opacity, image] of layers) { if (image && cover < 1) return true; cover = (cover + background[3] * (1 - cover)) * opacity; }
    return false; };
  // Screen-reader-only text: clipped to nothing (inset(50%), rect(0 0 0 0) on a positioned box) or held in a box of
  // 1px or less that hides its overflow. Any clip that leaves part of the box shows text, which is then measured.
  const clipHides = (clipPath, clip, position, width, height, overflowX, overflowY) => /^inset\\(50%\\)$/.test(clipPath)
    || (/^rect\\(0px,? 0px,? 0px,? 0px\\)$/.test(clip) && /absolute|fixed/.test(position))
    || (width <= 1 && /hidden|clip/.test(overflowX)) || (height <= 1 && /hidden|clip/.test(overflowY));  // each axis by its own overflow
"""


def check_text_contrast(page) -> list[str]:
    """Every visible text on the page against what is painted behind it, at least 4.5:1, or 3:1 for large text: the
    text's colour and the backgrounds behind it composited as the browser paints them (PAINT_JS), opacity groups
    and uniform tint gradients included. Measured on the rendered colours, so a token that drifts fails even where other checks only compare an
    element with the token. Skipped: text where a background image or gradient shows through (no single background;
    imageShows), screen-reader-only text clipped to nothing, svg text, and genuinely disabled controls and their labels
    (WCAG exempts inactive components). In both themes: the colours are the theme's."""
    found = page.evaluate("limits => {" + CONTRAST_JS + PAINT_JS + SURFACE_PAINT_JS + """
      const rgba = c => { const [r, g, b, a] = rgb(c); return [r, g, b, a / 255]; };
      const clippedAway = node => { for (let n = node; n; n = n.parentElement) { const s = getComputedStyle(n), r = n.getBoundingClientRect();
        if (clipHides(s.clipPath, s.clip, s.position, r.width, r.height, s.overflowX, s.overflowY)) return true; }
        return false; };
      const inactive = node => { const control = node.closest('button, input, select, textarea, option, optgroup'), label = node.closest('label');
        return !!(control && control.matches(':disabled')) || !!(label && label.control && label.control.matches(':disabled')) || !!node.closest('[aria-disabled="true"]'); };
      const bad = new Map();
      for (const node of document.querySelectorAll('body *')) {
        if (!node.getClientRects().length || node.closest('svg') || inactive(node)) continue;
        if (![...node.childNodes].some(c => c.nodeType === 3 && c.textContent.trim())) continue;
        const s = getComputedStyle(node);
        if (s.visibility !== 'visible' || clippedAway(node)) continue;
        const layers = [];
        for (let n = node; n; n = n.parentElement) {
          const t = getComputedStyle(n), fill = surface(t);
          layers.push([fill || rgba(t.backgroundColor), parseFloat(t.opacity), fill === null]);
        }
        if (imageShows(layers)) continue;
        const ink = painted(rgba(s.color), layers), ground = painted([0, 0, 0, 0], layers);
        if (layers.some(([, opacity]) => opacity === 0)) continue;  // not shown
        const css = c => `rgb(${c.map(Math.round).join(', ')})`;
        const size = parseFloat(s.fontSize), large = size >= 24 || (size >= 18.66 && Number(s.fontWeight) >= 700);
        const got = ratio(css(ink), css(ground)), need = large ? limits[1] : limits[0];
        if (got + 1e-6 < need) {
          const what = node.tagName.toLowerCase() + (node.classList.length ? '.' + [...node.classList].join('.') : '');
          const key = what + '|' + css(ink) + '|' + css(ground);
          if (!bad.has(key)) bad.set(key, `text contrast: ${what} "${node.textContent.trim().replace(/\\s+/g, ' ').slice(0, 40)}" is ${got.toFixed(2)}:1 (${css(ink)} on ${css(ground)}; needs ${need}:1)`);
        }
      }
      return [...bad.values()];
    }""", [TEXT_CONTRAST, LARGE_TEXT_CONTRAST])
    return found[:20] + ([f"text contrast: and {len(found) - 20} more kinds of text below their contrast"] if len(found) > 20 else [])


def check_status_contrast(page) -> list[str]:
    """Principle Status: every mark's edge is at least 3:1 on the ground, tile text at least 4.5:1 on what is behind
    it (never on a hatch), no status fill is near-black (at least 2:1 against black, Salvatore's rule), and planned
    differs from established by more than fill (it has a centre on the ground)."""
    errors = []
    marks = status_marks(page)
    for m in marks:
        where = f"{m['status']} {m['kind']}"
        if m.get("missing"):
            if m["kind"] == "swatch":
                errors.append(f"status contrast: no legend swatch for {m['status']}")
            continue
        if m["kind"] == "dot" and (m["status"] == "planned") != (m["centre"] > 1.5):
            errors.append(f"status contrast: {m['status']} dot {'lacks' if m['status'] == 'planned' else 'has'} a solid centre (planned is a ring with a solid centre)")
        if m["edge"] < 3 or m["edgeWidth"] < 1:
            errors.append(f"status contrast: {where} edge {m['edge']:.2f}:1 at {m['edgeWidth']}px on the ground (needs 3:1, at least 1px)")
        for tag, ratio in m["text"]:
            if ratio < 4.5:
                errors.append(f"status contrast: {where} text ({tag}) {ratio:.2f}:1 on its background" + (" (on a pattern)" if ratio == 0 else "") + " (needs 4.5:1)")
        if m["status"] == "planned" and m["kind"] == "swatch" and not (m.get("squareCentre") and m.get("centreFill")):
            errors.append("status contrast: planned swatch lacks a square established-colour centre (a square mark has a square centre)")
        if m["status"] == "planned" and m["kind"] == "dot" and m["centre"] > 1.5 and not m.get("centreRound"):
            errors.append("status contrast: planned dot lacks a round centre (a round mark has a round centre)")
    swatch = {m["status"]: m for m in marks if m["kind"] == "swatch" and not m.get("missing")}
    if "planned" in swatch and "established" in swatch and not (swatch["planned"].get("squareCentre") and swatch["planned"]["centre"] > 1.5 and swatch["planned"]["image"] == "none" and swatch["established"]["image"] == "none" and not swatch["established"]["ring"]):
        errors.append("status contrast: planned is not drawn as a square centre inside a page-colour ring, distinct from established's solid fill")
    errors.extend(check_planned_swatches(page))
    dashless = page.evaluate("() => [...document.querySelectorAll('.sw.st-none_identified')].filter(n => n.getClientRects().length && !/linear-gradient/.test(getComputedStyle(n).backgroundImage)).length")
    if dashless:
        errors.append(f"status contrast: {dashless} none-identified swatches lack their centre dash (a status mark, not an empty checkbox)")
    # Map land must read as land, not a stray tint (owner, 5 Nov 2026): the region's land at least 1.25:1 on the sea, while the marks stay ahead of it.
    land = page.evaluate("() => {" + CONTRAST_JS + " const svg = [...document.querySelectorAll('#worldmap svg')].find(n => n.getClientRects().length); const a = svg && svg.querySelector('.land-in'), b = svg && svg.querySelector('.sea'); return a && b ? ratio(getComputedStyle(a).fill, getComputedStyle(b).fill) : null; }")
    if land is not None and land < 1.25:
        errors.append(f"map: region land is only {land:.2f}:1 on the sea (needs 1.25:1 so it reads as land)")
    return errors


def check_planned_swatches(page) -> list[str]:
    """Every visible square planned swatch (legend, cards, groups): a square centre
    in the established colour inside a page-colour ring that leaves it visible."""
    errors = []
    errors.extend(page.evaluate("() => {" + CONTRAST_JS + """
      const ground = rgb(token('--ground')).join(), est = rgb(token('--st-est')).join(), bad = new Set();
      document.querySelectorAll('.sw.st-planned').forEach(n => {
        if (!n.getClientRects().length) return; // hidden content is measured when shown
        const c = getComputedStyle(n), w = n.getBoundingClientRect().width, ring = c.boxShadow.match(/^(rgba?\\([^)]*\\)) 0px 0px 0px ([\\d.]+)px inset$/);
        const square = ['borderTopLeftRadius', 'borderTopRightRadius', 'borderBottomRightRadius', 'borderBottomLeftRadius'].every(k => !c[k].includes('%') && parseFloat(c[k]) < .25 * w);
        const ok = square && ring && rgb(ring[1]).join() === ground && parseFloat(ring[2]) >= 1 && n.clientWidth - 2 * parseFloat(ring[2]) >= 2 && c.backgroundImage === 'none' && rgb(c.backgroundColor).join() === est;
        if (!ok) bad.add((n.closest('[id]') || {}).id || 'page');
      });
      return [...bad].map(id => 'status contrast: a planned swatch in ' + id + ' lacks its square centre inside a page-colour ring');
    }"""))
    return errors


def check_status_states(page) -> list[str]:
    """A hovered or keyboard-focused map dot keeps its whole status mark: the highlight draws the same mark (class,
    fill, stroke colour and width, hatch, planned centre) larger, with an ink ring around it; focus also shows a ring
    on the target. The same holds for the selected state's highlight."""
    errors = []
    page.mouse.move(0, 0)
    page.evaluate("scrollTo(0, 0)")
    for status in STATUS_ORDER:
        page.keyboard.press("Shift")  # keyboard modality, so programmatic focus is focus-visible
        for how in ("hover", "focus", "select"):
            result = page.evaluate(
                """([status, how]) => {
              const svg = [...document.querySelectorAll('#worldmap svg')].find(n => n.getClientRects().length);
              const dot = svg && svg.querySelector(':scope > g > .dot.st-' + status); if (!dot) return '';
              let t = dot.nextElementSibling; while (t && !t.matches('.tgt')) t = t.nextElementSibling;
              const look = n => { const s = getComputedStyle(n); return [n.getAttribute('class'), s.fill, s.stroke, s.strokeWidth, n.getAttribute('fill')].join(' | '); };
              const centre = n => n.nextElementSibling && n.nextElementSibling.matches('.dot-centre') ? look(n.nextElementSibling) : '';
              const box = t.getBoundingClientRect(), at = { bubbles: true, clientX: box.left + box.width / 2, clientY: box.top + box.height / 2 };
              if (how === 'hover') t.dispatchEvent(new MouseEvent('mousemove', at));
              else if (how === 'focus') t.focus();
              else t.dispatchEvent(new MouseEvent('click', { bubbles: true }));
              const g = svg.querySelector(how === 'select' ? '.hl-sel' : '.hl-temp'), mark = g && g.querySelector('.dot'), ring = g && g.querySelector('.ring');
              const ink = (() => { const p = document.createElement('span'); p.style.color = 'var(--ink)'; document.body.appendChild(p); const c = getComputedStyle(p).color; p.remove(); return c; })();
              let error = '';
              if (!mark || look(mark) !== look(dot) || centre(mark) !== centre(dot)) error = 'its highlight changes the status mark';
              else if (!(parseFloat(mark.getAttribute('r')) > parseFloat(dot.getAttribute('r')))) error = 'its highlight is not larger than the dot';
              else if (!ring || getComputedStyle(ring).stroke !== ink || parseFloat(ring.getAttribute('r')) <= parseFloat(mark.getAttribute('r'))) error = 'its highlight has no ink ring around the mark';
              else if (how === 'focus' && (document.activeElement !== t || getComputedStyle(t).stroke === 'none')) error = 'keyboard focus shows no ring on the target';
              if (how === 'hover') t.parentElement.dispatchEvent(new MouseEvent('mouseleave'));
              else if (how === 'focus') t.blur();
              else { document.querySelector('#record .record-close').click(); if (document.activeElement) document.activeElement.blur(); }
              if (svg.querySelector('.hl-temp, .hl-sel')) error = error || 'the highlight stays after the pointer, focus or record leaves';
              return error;
            }""",
                [status, how],
            )
            if result:
                errors.append(f"status states: a {how} {status} map dot: {result}")
    page.mouse.move(0, 0)
    page.evaluate("scrollTo(0, 0)")
    return errors


# Short names, written independently of the page's SHORT_NAME: always on the phone drawing for the first four; on either
# drawing a state may show its short name instead of its full one (where the full name has no unambiguous place).
MAP_SHORT_NAMES = {"United Kingdom": "UK", "United States": "US", "Netherlands": "NL", "South Korea": "S. Korea", "Germany": "DE"}
MAP_PHONE_SHORT = {"United Kingdom", "United States", "Netherlands", "South Korea"}


def check_map_labels(page, label: str, names: dict[str, str]) -> list[str]:
    """In both drawings (wide, and phone with its short names) every state with a status in the region view has its name on the map (names: id -> name), and no
    name covers another state's dot or another name. Every name, with or without a leader line, lies clearly nearer its
    own dot than any other, edge to edge (box to circle): every other dot at least 1.5 times as far plus 1. A reader
    matches a name to the nearest dot, so a leader line does not excuse a name sitting next to another state's dot. A
    leader line runs from the name's own dot to the name and passes no other dot."""
    got = page.evaluate("""() => {
      const svg = [...document.querySelectorAll('#worldmap svg')].find(n => n.getClientRects().length); if (!svg) return { dots: [], labels: [], narrow: false };
      const dots = [...svg.querySelectorAll('.tgt')].map(t => { let d = t.previousElementSibling; while (d && !d.matches('.dot')) d = d.previousElementSibling;
        return [t.dataset.j, +d.getAttribute('cx'), +d.getAttribute('cy'), +d.getAttribute('r')]; });
      const leaders = Object.fromEntries([...svg.querySelectorAll('.lab-leader')].map(l => [l.dataset.j, ['x1', 'y1', 'x2', 'y2'].map(a => +l.getAttribute(a))]));
      return { dots, narrow: svg.classList.contains('map-narrow'), labels: [...svg.querySelectorAll('.lab')].map(t => { const b = t.getBBox();
        return [t.dataset.j, [...t.querySelectorAll('tspan')].map(s => s.textContent).join(' ') || t.textContent, [b.x, b.y, b.x + b.width, b.y + b.height], leaders[t.dataset.j] || null, t.getClientRects().length > 0 && b.width > 0]; }) };
    }""")
    errors = []
    shown = {jid: text for jid, text, _, _, visible in got["labels"] if visible}
    for jid, name in names.items():
        want = {MAP_SHORT_NAMES[name]} if got["narrow"] and name in MAP_PHONE_SHORT else {name, MAP_SHORT_NAMES.get(name, name)}
        if shown.get(jid) not in want:
            errors.append(f"map labels {label}: {jid} is labelled {shown.get(jid)!r}, expected {' or '.join(map(repr, sorted(want)))}")
    for jid in set(shown) - set(names):
        errors.append(f"map labels {label}: an unexpected name {shown[jid]!r} for {jid}")

    def seg_distance(line, x, y):
        x1, y1, x2, y2 = line
        dx, dy = x2 - x1, y2 - y1
        t = max(0.0, min(1.0, ((x - x1) * dx + (y - y1) * dy) / (dx * dx + dy * dy or 1)))
        return math.hypot(x1 + t * dx - x, y1 + t * dy - y)

    for k, (jid, text, (x0, y0, x1, y1), leader, _) in enumerate(got["labels"]):
        def dist(x, y):
            return math.hypot(max(x0 - x, 0, x - x1), max(y0 - y, 0, y - y1))
        own = next(((x, y, r) for j, x, y, r in got["dots"] if j == jid), None)
        for _, other, (a0, b0, a1, b1), _, _ in got["labels"][k + 1:]:
            if x0 < a1 and a0 < x1 and y0 < b1 and b0 < y1:
                errors.append(f"map labels {label}: {text!r} overlaps {other!r}")
        if leader:
            if own is None or math.hypot(leader[0] - own[0], leader[1] - own[1]) > own[2] + 2 or dist(leader[2], leader[3]) > 4:
                errors.append(f"map labels {label}: the leader line of {text!r} does not run from its own dot to the name")
        for j, x, y, r in got["dots"]:
            if j == jid:
                continue
            if dist(x, y) < r:
                errors.append(f"map labels {label}: {text!r} covers the dot of {j}")
            elif leader and seg_distance(leader, x, y) < r:
                errors.append(f"map labels {label}: the leader line of {text!r} crosses the dot of {j}")
            elif own is None or dist(x, y) - r < 1.5 * (dist(own[0], own[1]) - own[2]) + 1:
                errors.append(f"map labels {label}: {text!r} is not clearly nearer its own dot than the dot of {j} (edge to edge: {dist(x, y) - r:.1f} against {dist(own[0], own[1]) - own[2] if own else float('nan'):.1f} to its own{', despite its leader line' if leader else ''})")
    return errors


def check_first_screen(page, data: dict, theme_invariant: bool = True) -> list[str]:
    """The first screen against an independent oracle from aisi.json: per region the headline, the open accordion
    group and its institute rows, the others' name lines, one dot per state on or near its own country with its
    status mark; World by default; region chips move to their view; the list's World view row shows only outside World; the EU's merged border; search by keyboard; the record
    beside the map with its one link down (card or table row). Without theme_invariant (the dark passes) only the
    World view on load and its dots: the rest (region switches, dot clicks, the EU border, search, records, other
    widths) reads text, classes, focus and geometry, which do not depend on the theme; it ends where the full run
    ends (Europe selected, at the top) for the steps that follow."""
    errors = []
    page.mouse.move(0, 0)
    page.evaluate("scrollTo(0, 0)")
    status = {j["id"]: j["status"] for j in data["jurisdictions"]}
    institute_names = expected_base_names(data)
    head_names = {j['id']: j['name'] + (', ' + institute_names[j['institute_id']] if j.get('institute_id') else '')
                  for j in data['jurisdictions']}
    snapshot = """() => {
      const svg = [...document.querySelectorAll('#worldmap svg')].find(n => n.getClientRects().length);
      const groups = [...document.querySelectorAll('#regions .region')];
      const dots = svg ? [...svg.querySelectorAll('.tgt')].map(t => {
        let d = t.previousElementSibling; while (d && !d.matches('.dot')) d = d.previousElementSibling;
        const shape = svg.querySelector('.land-in[data-j="' + t.dataset.j + '"]'), r = parseFloat(d.getAttribute('r'));
        const x = parseFloat(t.getAttribute('cx')), y = parseFloat(t.getAttribute('cy'));
        let near = true;
        if (shape) { const b = shape.getBBox(), m = Math.max(3 * r, 12); near = x >= b.x - m && x <= b.x + b.width + m && y >= b.y - m && y <= b.y + b.height + m; }
        return [t.dataset.j, d.getAttribute('class'), near, t.getAttribute('aria-label')];
      }) : null;
      return { headline: document.querySelector('#headline .hl-version.current').textContent.replace(/\u00a0/g, ' '), mapTop: document.getElementById('worldmap').getBoundingClientRect().top + scrollY, svgs: document.querySelectorAll('#worldmap svg').length,
        shown: [...document.querySelectorAll('#worldmap svg')].filter(n => n.getClientRects().length).length, dots,
        open: groups.filter(g => g.classList.contains('open') && g.querySelector('.region-head').getAttribute('aria-expanded') === 'true' && !g.querySelector('.region-rows').hidden).map(g => g.dataset.region),
        rows: Object.fromEntries(groups.map(g => [g.dataset.region, [...g.querySelectorAll(':scope > .region-rows .pick')].map(b => b.dataset.pick)])),
        apart: Object.fromEntries(groups.map(g => { const box = g.querySelector('.region-elsewhere'); return [g.dataset.region, box ? { shown: box.getClientRects().length > 0, sub: !!box.querySelector('.region-sub')?.textContent.trim(), rows: [...box.querySelectorAll('.pick')].map(b => b.dataset.pick) } : null]; })),
        briefs: Object.fromEntries(groups.map(g => [g.dataset.region, g.querySelector('.region-brief').getClientRects().length > 0])),
        counts: Object.fromEntries(groups.map(g => [g.dataset.region, g.querySelector('.region-head .rcount').textContent.trim()]).concat([['World', (document.querySelector('.region-world .region-head .rcount') || {}).textContent?.trim()]])),
        controls: groups.every(g => document.getElementById(g.querySelector('.region-head').getAttribute('aria-controls')) === g.querySelector('.region-rows')),
        names: Object.fromEntries(groups.map(g => [g.dataset.region, g.querySelector('.region-head').getAttribute('aria-label')])) };
    }"""

    tops = []

    def expect_region(region, label):
        got = page.evaluate(snapshot)
        tops.append(got["mapTop"])
        # Narrow screens bring the map back into view on a region switch: below the sticky section bar, not under it.
        if page.evaluate("matchMedia('(max-width: 1100px)').matches && document.getElementById('worldmap').getBoundingClientRect().top < document.querySelector('.secnav').getBoundingClientRect().bottom - 0.5"):
            errors.append(f"first screen {label}: the map's top lies under the sticky section bar")
        if abs(got["mapTop"] - tops[0]) > 0.5:
            errors.append(f"first screen {label}: the map moved by {got['mapTop'] - tops[0]:.1f}px when the region changed")
        if got["headline"] != expected_headline(data, region):
            errors.append(f"first screen {label}: headline {got['headline']!r}, expected {expected_headline(data, region)!r}")
        if got["svgs"] != 2 or got["shown"] != 1:
            errors.append(f"first screen {label}: expected one map drawing per layout with exactly one shown, got {got['svgs']} and {got['shown']}")
        want_open = list(LIST_REGIONS) if region == "World" else [region]
        if got["open"] != want_open:
            errors.append(f"first screen {label}: open regions {got['open']}, expected {want_open}")
        if not got["controls"]:
            errors.append(f"first screen {label}: a region heading does not control its list")
        for name in LIST_REGIONS + ("World",):
            counted = [j for j in region_pool(data, name) if j["kind"] == "state"]
            want = f"{sum(j['status'] == 'established' for j in counted)} of {len(counted)}"
            if got["counts"].get(name) != want:
                errors.append(f"first screen {label}: {name} heading counts {got['counts'].get(name)!r}, expected {want!r} (states only)")
        for name in LIST_REGIONS:
            pool = [j for j in region_pool(data, name) if j["kind"] == "state"]  # states only, as the heading count
            n = sum(j["status"] == "established" for j in pool)
            spoken = f"{name}, {n} of {len(pool)} states {'has' if n == 1 else 'have'} an institute"
            if got["names"][name] != spoken:
                errors.append(f"first screen {label}: the {name} heading is named {got['names'][name]!r}, expected {spoken!r}")
            if got["rows"][name] != expected_region_rows(data, name):
                errors.append(f"first screen {label}: {name} rows {got['rows'][name]} differ from the data {expected_region_rows(data, name)}")
            if got["briefs"][name] == (name in want_open):
                errors.append(f"first screen {label}: {name} {'shows' if name in want_open else 'lacks'} its muted names line")
        for name in LIST_REGIONS:  # states on the map but outside the counts: listed apart under their subheading, shown only in their region's view
            apart = sorted((j for j in region_map_pool(data, name) if j not in region_pool(data, name)), key=lambda j: (STATUS_ORDER.index(j["status"]), j["name"], j["id"]))
            want = {"shown": region == name, "sub": True, "rows": [j["id"] for j in apart]} if apart else None
            if got["apart"][name] != want:
                errors.append(f"first screen {label}: {name} lists {got['apart'][name]} apart, expected {want}")
        pool = [j for j in region_map_pool(data, region) if region != "World" or j["status"] != "none_identified"]
        dots = got["dots"] or []
        if sorted(d[0] for d in dots) != sorted(j["id"] for j in pool):
            errors.append(f"first screen {label}: map dots differ from the region's states: extra {sorted({d[0] for d in dots} - {j['id'] for j in pool})}, missing {sorted({j['id'] for j in pool} - {d[0] for d in dots})}")
        for jid, cls, near, aria in dots:
            if cls != f"dot st-{status[jid]}":
                errors.append(f"first screen {label}: dot {jid} is drawn as {cls!r}, not its status {status[jid]}")
            if not near:
                errors.append(f"first screen {label}: dot {jid} sits away from its country")
            if not aria or not aria.endswith(", " + STATUS_LABELS[status[jid]]):
                errors.append(f"first screen {label}: dot {jid} lacks its accessible name and status")
        if region != "World":
            errors.extend(check_map_labels(page, f"{region} at {page.evaluate('innerWidth')}px", {j["id"]: j["name"] for j in pool if j["status"] != "none_identified"}))

    def head(region):
        page.locator(f'#regions .region-head[data-region="{region}"]').first.click()
        page.wait_for_timeout(50)

    def pointer_picks(label):
        """Real pointer clicks at every dot's centre open that state's record (hit circles overlap where states are close)."""
        ids = page.evaluate("() => [...[...document.querySelectorAll('#worldmap svg')].find(n => n.getClientRects().length).querySelectorAll('.tgt')].map(t => t.dataset.j)")
        wrong = []
        for jid in ids:
            x, y = page.evaluate("""j => { const t = [...document.querySelectorAll('#worldmap svg')].find(n => n.getClientRects().length).querySelector('.tgt[data-j="' + j + '"]');
              t.scrollIntoView({ block: 'center' }); const r = t.getBoundingClientRect(); return [r.left + r.width / 2, r.top + r.height / 2]; }""", jid)
            page.mouse.click(x, y)
            got = page.evaluate("() => { const r = document.getElementById('record'); return r.hidden ? null : r.dataset.j; }")
            # The record's heading takes focus; a surviving dot tip names the state, while the focused head's tip names both state and institute.
            aside = page.evaluate("j => { const a = document.activeElement, t = document.getElementById('tip'), d = [...document.querySelectorAll('#worldmap svg')].find(n => n.getClientRects().length).querySelector('.tgt[data-j=\"' + j + '\"]'); return [a && a.id === 'record-name' ? null : (a && (a.dataset.j || a.id || a.tagName)), t && !t.hidden ? t.firstChild.textContent : null, d.getAttribute('aria-controls') === 'record' && d.getAttribute('aria-expanded') === 'true']; }", jid)
            name = next(j["name"] for j in data["jurisdictions"] if j["id"] == jid)
            # Opening the record can scroll another tooltip trigger under the resting pointer (a region list short name); its own tip is not the dot's.
            under = page.evaluate("([x, y]) => { const n = document.elementFromPoint(x, y), o = n && n.closest('abbr.g, [data-tip], [aria-description]'); return o && !o.closest('#worldmap') ? (o.dataset.tip || o.getAttribute('aria-description')) : null; }", [x, y])
            if under is not None and aside[1] == under:
                aside[1] = None
            if got != jid:
                wrong.append(f"{jid}->{got}")
            elif aside[0] or aside[1] not in (name, head_names[jid], None) or not aside[2]:
                wrong.append(f"{jid}: focus on {aside[0]}, tooltip {aside[1]!r} or dot not expanded/controlling the record ({aside[2]})")
            elif page.evaluate("innerWidth > 1100") and not page.evaluate("document.getElementById('regions').inert"):
                wrong.append(f"{jid}: the covered list stays operable")
            if got:
                page.locator("#record .record-close").click()
                back = page.evaluate("() => document.activeElement && document.activeElement.matches('.tgt') ? document.activeElement.dataset.j : null")
                if back != jid:
                    wrong.append(f"{jid}: closing returns focus to {back}")
        page.mouse.move(0, 0)
        if wrong:
            errors.append(f"first screen {label}: a click at a dot's centre opens another record or none: {wrong[:8]}")
        if page.evaluate("document.getElementById('regions').inert"):
            errors.append(f"first screen {label}: the list stays inert after the record closes")
        page.evaluate("scrollTo(0, 0)")

    expect_region("World", "on load")
    chips = "() => [...document.querySelectorAll('#region-chips button')].map(c => [c.dataset.region, c.getAttribute('aria-pressed')])"
    if page.evaluate(chips) != [[r, str(r == "World").lower()] for r in ("World",) + LIST_REGIONS]:
        errors.append(f"first screen: region chips are not World (pressed) then the list's regions: {page.evaluate(chips)}")
    if not theme_invariant:
        head("Europe")
        expect_region("Europe", "Europe")
        page.evaluate("scrollTo(0, 0)")
        return errors
    head("Europe")
    expect_region("Europe", "Europe")
    pointer_picks("Europe")
    # A click on a country's shape, away from every dot, acts like a click on its dot: record, focus and tooltip.
    fr = next(j for j in data["jurisdictions"] if j["name"] == "France")
    page.locator("#worldmap").scroll_into_view_if_needed()
    spot = page.evaluate("""j => { const svg = [...document.querySelectorAll('#worldmap svg')].find(n => n.getClientRects().length), shape = svg.querySelector('.land-in[data-j="' + j + '"]');
      const b = shape.getBBox(), pt = svg.createSVGPoint(), m = svg.getScreenCTM(), hits = [...svg.querySelectorAll('.tgt')].map(t => [+t.getAttribute('cx'), +t.getAttribute('cy'), +t.getAttribute('r')]);
      for (let i = 1; i < 20; i++) for (let k = 1; k < 20; k++) { pt.x = b.x + b.width * i / 20; pt.y = b.y + b.height * k / 20;
        if (shape.isPointInFill(pt) && hits.every(([x, y, r]) => Math.hypot(x - pt.x, y - pt.y) > r + 2)) { const q = pt.matrixTransform(m); return [q.x, q.y]; } }
      return null; }""", fr["id"])
    if not spot:
        errors.append("first screen: no point on France's shape away from the dots to click")
    else:
        page.mouse.click(*spot)
        got = page.evaluate("j => [document.getElementById('record').dataset.j, document.activeElement && document.activeElement.id, (document.getElementById('tip') || {}).hidden === false ? document.getElementById('tip').firstChild.textContent : null]", fr["id"])
        if got[:2] != [fr["id"], "record-name"] or got[2] not in (fr["name"], head_names[fr['id']], None):
            errors.append(f"first screen: a click on France's shape does not act like its dot (record, focus, tooltip: {got})")
        page.locator("#record .record-close").click()
        page.mouse.move(0, 0)
        page.evaluate("scrollTo(0, 0)")
    for region in LIST_REGIONS[1:]:
        head(region)
        expect_region(region, region)
    if not page.locator(".region-world").is_visible():
        errors.append("first screen: the region list has no World view row while another region is selected")
    head("World")
    expect_region("World", "World view")
    pointer_picks("World view")
    if page.locator(".region-world").is_visible():
        errors.append("first screen: the region list still shows its World view row while World is selected")
    head("Europe")
    expect_region("Europe", "back to Europe")
    # The World view row hides itself when used: keyboard focus moves to the World tab, not to the page.
    page.locator(".region-world .region-head").focus()
    page.keyboard.press("Enter")
    page.wait_for_timeout(150)
    if page.evaluate("""!!document.activeElement && document.activeElement.matches('#region-chips button[data-region="World"]')""") is not True:
        errors.append("first screen: Enter on the World view row leaves focus nowhere; it should move to the World tab")
    page.locator('#region-chips button[data-region="Europe"]').click()
    page.wait_for_timeout(100)
    # Region chips move to their view only: a pressed chip clicked again stays; World is a chip like the others.
    for region, label in (("Asia", "Asia chip"), ("Asia", "Asia chip again"), ("World", "World chip"), ("World", "World chip again")):
        page.locator(f'#region-chips button[data-region="{region}"]').click()
        page.wait_for_timeout(50)
        expect_region(region, label)
        if page.evaluate(f"document.querySelector('#region-chips button[data-region=\"{region}\"]').getAttribute('aria-pressed')") != "true":
            errors.append(f"first screen {label}: the {region} chip is not pressed")
    head("Europe")
    # A window shorter than the map: a region picked from the list below still brings the map's top into view below the bar.
    if page.evaluate("matchMedia('(max-width: 1100px)').matches"):
        size = page.viewport_size
        page.set_viewport_size({"width": size["width"], "height": 500})
        page.locator('#regions .region-head[data-region="Asia"]').first.scroll_into_view_if_needed()
        head("Asia")
        if page.evaluate("document.getElementById('worldmap').getBoundingClientRect().top < document.querySelector('.secnav').getBoundingClientRect().bottom - 0.5"):
            errors.append("first screen: in a 500px-high window, a region switch from the list leaves the map's top hidden")
        head("Europe")
        page.set_viewport_size(size)
        page.evaluate("scrollTo(0, 0)")

    # The EU: listed under Europe; selecting it draws the merged outer border of the member states.
    eu = next(j for j in data["jurisdictions"] if j["kind"] == "supranational")
    eu_row = page.locator(f'#regions .region-rows .pick[data-pick="{eu["id"]}"]')
    if not eu_row.count():
        return errors + ["first screen: the EU AI Office is not listed under Europe"]
    eu_row.click()
    members = [j["id"] for j in data["jurisdictions"] if j.get("eu_member")]
    # One outline: as wide as the union, with fewer rings than the member countries drawn separately (inner borders dropped).
    border = page.evaluate("""ids => { const svg = [...document.querySelectorAll('#worldmap svg')].find(n => n.getClientRects().length), rings = p => (p.getAttribute('d').match(/M/g) || []).length;
      const p = svg.querySelector('.hl-sel .hl-shape'); if (!p) return null;
      return [p.getBBox().width / svg.viewBox.baseVal.width, rings(p), ids.reduce((n, id) => n + rings(svg.querySelector('.land-in[data-j="' + id + '"]')), 0)]; }""", members)
    if not border or border[0] < 0.3 or border[1] >= border[2]:
        errors.append(f"first screen: selecting the EU does not draw the merged EU border ({border})")
    if page.locator("#record-name .head-state").text_content() != eu["name"] or page.evaluate("document.activeElement && document.activeElement.id") != "record-name":
        errors.append("first screen: the EU row does not open its record with focus on it")
    page.keyboard.press("Escape")
    if page.locator("#record").is_visible():
        errors.append("first screen: Escape does not close the record")
    elif page.evaluate("document.activeElement && document.activeElement.dataset.pick") != eu["id"]:
        errors.append("first screen: closing the record does not return focus to the row that opened it")
    # A record opened from another region's names line: that region opens, so focus falls back to the state's row.
    far = expected_region_rows(data, "Asia")[0]
    page.locator(f'#regions .region[data-region="Asia"] .region-brief [data-pick="{far}"]').click()
    page.keyboard.press("Escape")
    if page.evaluate("document.activeElement.dataset.pick || document.activeElement.tagName") != far:
        errors.append("first screen: closing a record opened from another region's names line leaves focus nowhere")
    head("Europe")

    # Search: own listbox, keyboard, status mark and region per option, empty state.
    box = page.locator("#map-search")
    box.fill("ger")  # Germany first (names starting with the text lead), then Algeria, Niger, Nigeria
    options = page.evaluate("[...document.querySelectorAll('#map-search-list [role=option]')].map(o => [o.querySelector('.sname').textContent, !!o.querySelector('.sw[class*=st-]'), o.querySelector('.small').textContent])")
    names = page.evaluate("[...document.querySelectorAll('#map-search-list [role=option]')].map(o => o.getAttribute('aria-label') || '')")
    if not names or not all(any(", " + label + ", " in n for label in STATUS_LABELS.values()) for n in names):
        errors.append(f"search: options do not name their status to assistive technology ({names[:3]})")
    if len(options) < 2 or options[0][0] != "Germany" or not all(o[1] and o[2] for o in options) or box.get_attribute("aria-expanded") != "true":
        errors.append(f"search: 'ger' does not list Germany first, then others, with status marks and regions ({options})")
    # One ranking with the command palette: names starting with the text, then a word in a name starting so, then text inside a name.
    box.fill("gu")
    ranked = page.evaluate("[...document.querySelectorAll('#map-search-list [role=option] .sname')].map(o => o.textContent)")
    if ranked[:4] != ["Guatemala", "Guinea", "Guinea-Bissau", "Guyana"] or "Equatorial Guinea" not in ranked or ("Antigua and Barbuda" in ranked and ranked.index("Antigua and Barbuda") < ranked.index("Equatorial Guinea")):
        errors.append(f"search: 'gu' does not list name starts, then word starts (Equatorial Guinea), before text inside a name (Antigua and Barbuda): {ranked}")
    box.fill("ger")
    box.press("ArrowDown")
    if box.get_attribute("aria-activedescendant") != "map-search-1" or page.locator("#map-search-1").get_attribute("aria-selected") != "true":
        errors.append("search: ArrowDown does not move the active option")
    box.press("ArrowUp")
    if box.get_attribute("aria-activedescendant") != "map-search-0":
        errors.append("search: ArrowUp does not move the active option back")
    box.press("Enter")
    if page.locator("#record-name .head-state").text_content() != "Germany" or page.locator("#map-search-list").is_visible() or box.get_attribute("aria-expanded") != "false":
        errors.append("search: Enter does not open the active option's record and close the list")
    if page.locator("#record-name .head-state").text_content() != "Germany":
        # The record steps below assume Germany's record; with another one open they would fail on something else
        # (a citation that is not there) and hide the search errors above, so the search errors are the result.
        return errors + [f"search: 'ger' then Enter opened {page.locator('#record-name').text_content()!r}, not Germany; later first-screen steps skipped"]
    de = next(j for j in data["jurisdictions"] if j["name"] == "Germany")
    link = page.locator("#record .record-more")
    if not (link.text_content() or "").strip() or link.get_attribute("href") != f"#c-{de['id'].lower()}":
        errors.append(f"record: Germany's link down reads {link.text_content()!r} to {link.get_attribute('href')!r}")
    page.locator("#record .cn").first.focus()
    page.keyboard.press("Escape")
    if not page.locator("#record").is_visible() or page.evaluate("() => { const t = document.getElementById('tip'); return !!t && !t.hidden; }"):
        errors.append("record: the first Escape on a citation does not close only its tooltip")
    page.keyboard.press("Escape")
    if page.locator("#record").is_visible():
        errors.append("record: the second Escape does not close the record")
    box.fill("zzzq")
    if page.locator("#map-search-list").is_visible() or not page.locator("#map-search-empty").is_visible():
        errors.append("search: no match shows no empty state")
    box.press("Escape")
    if page.locator("#map-search-empty").is_visible():
        errors.append("search: Escape does not close the suggestions")
    box.fill("")

    # A state without a card: its link goes to its row in the table, filtered to it.
    nocard = sorted((j for j in data["jurisdictions"] if j["kind"] != "supranational" and not j["europe_scope"] and j["status"] == "none_identified"), key=lambda j: j["name"])[0]
    box.fill(nocard["name"])
    box.press("Enter")
    more = page.locator("#record .record-more")
    if not (more.text_content() or "").strip():
        errors.append(f"record: {nocard['id']} (no card) offers {more.text_content()!r}")
    more.click()
    page.wait_for_timeout(100)
    if not page.locator(f'#world-rows tr[data-j="{nocard["id"]}"]').is_visible() or not page.evaluate("() => { const r = document.querySelector('#world-scroll').getBoundingClientRect(); return r.top < innerHeight && r.bottom > 0; }"):
        errors.append(f"record: the table link does not bring {nocard['id']}'s row into view")
    page.fill("#world-filter", "")
    page.select_option("#world-status", "pos")
    page.evaluate("scrollTo(0, 0)")
    page.locator("#record .record-close").click()
    if page.locator("#record").is_visible():
        errors.append("record: the close button does not close it")
    head("Europe")
    # The map stays put across region switches also at widths where headlines wrap differently (Codex probe widths).
    size = page.viewport_size
    for width in (641, 320):
        page.set_viewport_size({"width": width, "height": size["height"]})
        page.wait_for_timeout(50)
        seen = set()
        for region in (*LIST_REGIONS, "World"):  # the World row hides while World is shown, so no second World click
            head(region)
            seen.add(round(page.evaluate("document.getElementById('worldmap').getBoundingClientRect().top + scrollY"), 1))
            if width == 320 and region != "World":  # the phone drawing at its narrowest width
                errors.extend(check_map_labels(page, f"{region} at 320px", {j["id"]: j["name"] for j in region_pool(data, region) if j["status"] != "none_identified"}))
        if len(seen) != 1:
            errors.append(f"first screen {width}px: the map moves when the region changes ({sorted(seen)})")
    page.set_viewport_size(size)
    head("Europe")
    page.evaluate("scrollTo(0, 0)")
    return errors



def theme_name_has_mode(name: str | None, mode: str) -> bool:
    return re.search(rf"\b{mode}\b", name or "", re.I) is not None


def theme_state_matches(state: list, scheme: str, active: str, mode: str) -> bool:
    """The applied scheme and paint are the active theme's, and the toggle's name says the chosen mode (system, light
    or dark) in any wording: the state, not the words, is checked."""
    return state[:2] == [scheme, GROUNDS[active]] and theme_name_has_mode(state[2], mode)


def check_theme_default(browser, base_url: str, dist: Path) -> list[str]:
    """Principle Theme: the system scheme by default; the toggle (keyboard reachable) moves through follows system,
    light and dark, stores a light or dark choice and clears it for system, is remembered across reloads, works
    before hydration, and Method, the API index and the 404 page follow both the choice and the system; invalid or
    blocked storage follows the system without errors; the theme is set before the stylesheet applies; without
    scripting the toggle is hidden. Contexts start unseeded (no themed_page), so persistence is real."""
    errors = []
    ground = "getComputedStyle(document.body).backgroundColor"
    label = "document.getElementById('theme-toggle').getAttribute('aria-label')"
    stored = f"localStorage.getItem('{THEME_KEY}')"

    def state(page):
        return page.evaluate(f"[getComputedStyle(document.documentElement).colorScheme, {ground}, {label}]")

    for system in ("light", "dark"):
        context = browser.new_context(viewport={"width": 1440, "height": 1000}, color_scheme=system)
        try:
            page = context.new_page()
            page.on("pageerror", lambda e: errors.append(f"theme: JavaScript error: {e}"))
            hydrated(page, base_url)
            got = state(page)
            if not theme_state_matches(got, "light dark", system, "system"):
                errors.append(f"theme: system {system} with nothing stored does not give the {system} page ({got})")
        finally:
            context.close()

    context = browser.new_context(viewport={"width": 1440, "height": 1000}, color_scheme="dark")
    try:
        page = context.new_page()
        page_errors: list = []
        page.on("pageerror", lambda e: page_errors.append(e))
        hydrated(page, base_url)
        toggle = page.locator("#theme-toggle")
        if not toggle.is_visible():
            errors.append("theme: the toggle is not shown")
            return errors  # nothing below can run without it
        page.evaluate("scrollTo(0, 0)")
        for _ in range(8):
            page.keyboard.press("Tab")
            if page.evaluate("document.activeElement.id") == "theme-toggle":
                break
        else:
            errors.append("theme: the toggle is not reachable by keyboard")
        page.mouse.move(0, 0)
        page.evaluate("document.activeElement.blur()")
        # The cycle: follows system -> light -> dark -> follows system, each stored (system clears the key).
        names = []  # the toggle's name in each mode of the cycle: each must tell its mode apart from the others
        for scheme, name, key in (("light", "light", "light"), ("dark", "dark", "dark"), ("light dark", "follows system", None)):
            toggle.focus()
            page.keyboard.press("Enter")
            page.evaluate("document.activeElement.blur()")
            got, kept = state(page), page.evaluate(stored)
            names.append(got[2])
            active = "light" if scheme == "light" else "dark"  # system is dark in this context
            if not theme_state_matches(got, scheme, active, "system" if key is None else key) or kept != key:
                errors.append(f"theme: the toggle does not move to {name} and store it ({got}, stored {kept})")
        if len(set(names)) != 3 or any(sum(theme_name_has_mode(n, m) for m in ("light", "dark", "system")) != 1 for n in names):
            errors.append(f"theme: the toggle's names do not each say one mode ({names})")
        toggle.focus()
        page.keyboard.press("Enter")
        toggle.focus()
        page.keyboard.press("Enter")
        hydrated(page, base_url)
        if not theme_state_matches(state(page), "dark", "dark", "dark"):
            errors.append(f"theme: a dark choice is not kept after a reload ({state(page)})")
        page.evaluate(f"localStorage.setItem('{THEME_KEY}', 'light')")
        hydrated(page, base_url)
        if not theme_state_matches(state(page), "light", "light", "light"):
            errors.append(f"theme: a light choice is not kept after a reload ({state(page)})")
        # Before the script takes over: the saved page's toggle shows the stored state and works.
        held = Held(page, PAGE_DATA)
        page.goto(base_url, wait_until="load")
        if not theme_name_has_mode(page.evaluate(label), "light"):
            errors.append("theme: the saved page's toggle does not show the stored light theme")
        page.locator("#theme-toggle").click(timeout=5_000)
        held.release()
        wait_hydrated(page)
        if not theme_state_matches(state(page), "dark", "dark", "dark") or page.evaluate(stored) != "dark":
            errors.append(f"theme: a toggle before the script takes over is lost ({state(page)})")
        api = sorted(dist.glob("api/v*/index.html"))
        others = {"Method": "method.html", "404": "404.html"} | ({"API index": api[-1].relative_to(dist).as_posix()} if api else {})
        if not api:
            errors.append("theme: no API index page to check")
        for theme in ("dark", "light", None):  # None: nothing stored, so the dark system decides
            page.evaluate(f"t => t ? localStorage.setItem('{THEME_KEY}', t) : localStorage.removeItem('{THEME_KEY}')", theme)
            for name, path in others.items():
                page.goto(base_url + path)
                if page.evaluate(ground) != GROUNDS[theme or "dark"]:
                    errors.append(f"theme: {name} does not follow the {theme or 'system dark'} theme")
                if name == "API index":
                    # Paths are code: monospace on a subtle fill distinct from the ground, text and links at least 4.5:1 on it.
                    bad = page.evaluate("""() => { const rgb = c => c.match(/[\\d.]+/g).slice(0, 3).map(Number);
                      const lum = c => { const [r, g, b] = rgb(c).map(v => { v /= 255; return v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4; }); return 0.2126 * r + 0.7152 * g + 0.0722 * b; };
                      const ratio = (a, b) => { const [x, y] = [lum(a), lum(b)].sort((p, q) => q - p); return (x + 0.05) / (y + 0.05); };
                      const ground = getComputedStyle(document.body).backgroundColor, items = [...document.querySelectorAll('main li')];
                      const out = items.length ? [] : ['no items'];
                      for (const li of items) { const codes = li.querySelectorAll('code'); if (!codes.length) out.push('no code path: ' + li.textContent.slice(0, 40));
                        for (const c of codes) { const s = getComputedStyle(c);
                          if (!/mono/i.test(s.fontFamily) || s.backgroundColor === ground || /rgba\\(0, 0, 0, 0\\)/.test(s.backgroundColor) || ratio(s.color, s.backgroundColor) < 4.5)
                            out.push(c.textContent + ' ' + s.fontFamily.slice(0, 20) + ' ' + s.backgroundColor + ' ' + ratio(s.color, s.backgroundColor).toFixed(2)); } }
                      return out; }""")
                    errors.extend(f"api index: in {theme or 'system dark'} a path is not readable code ({b})" for b in bad)
        errors.extend(f"theme: JavaScript error: {e}" for e in page_errors)
    finally:
        context.close()

    # Invalid and inaccessible storage: the system decides, no errors, and the toggle still switches the open page.
    for name, script in (
        ("invalid", f"try {{ localStorage.setItem('{THEME_KEY}', 'purple'); }} catch (e) {{}}"),
        ("inaccessible", "Object.defineProperty(window, 'localStorage', { get() { throw new DOMException('blocked', 'SecurityError'); } });"),
    ):
        context = browser.new_context(viewport={"width": 1000, "height": 900}, color_scheme="dark")
        try:
            page = context.new_page()
            page_errors = []
            page.on("pageerror", lambda e: page_errors.append(e))
            page.add_init_script(script)
            hydrated(page, base_url)
            if not theme_state_matches(state(page), "light dark", "dark", "system"):
                errors.append(f"theme: {name} storage does not follow the system ({state(page)})")
            page.locator("#theme-toggle").click(timeout=5_000)
            if not theme_state_matches(state(page), "light", "light", "light"):
                errors.append(f"theme: with {name} storage the toggle does not switch ({state(page)})")
            errors.extend(f"theme: {name} storage: JavaScript error: {e}" for e in page_errors)
        finally:
            context.close()

    # First paint: stored dark, system light, data held back. The theme is on the root when the first stylesheet
    # element arrives, and the first frame with a body is already dark.
    context = browser.new_context(viewport={"width": 390, "height": 900}, color_scheme="light")
    try:
        page = context.new_page()
        page.add_init_script(f"""try {{ localStorage.setItem('{THEME_KEY}', 'dark'); }} catch (e) {{}}
          window.__firstPaint = {{}};
          new MutationObserver((records, observer) => {{ for (const r of records) for (const n of r.addedNodes) if (n.nodeName === 'STYLE' || (n.nodeName === 'LINK' && n.rel === 'stylesheet')) {{ window.__firstPaint.style = document.documentElement.getAttribute('data-theme'); observer.disconnect(); return; }} }}).observe(document, {{ childList: true, subtree: true }});
          (function frame() {{ requestAnimationFrame(() => {{ if (document.body) window.__firstPaint.ground = getComputedStyle(document.body).backgroundColor; else frame(); }}); }})();""")
        held = Held(page, PAGE_DATA)  # the saved page alone
        page.goto(base_url, wait_until="load")
        page.wait_for_timeout(200)
        first = page.evaluate("window.__firstPaint")
        held.release(abort=True)
        if first.get("style") != "dark" or first.get("ground") != GROUNDS["dark"]:
            errors.append(f"theme: stored dark is not set before first paint ({first})")
    finally:
        context.close()

    context = browser.new_context(viewport={"width": 1000, "height": 900}, java_script_enabled=False)
    try:
        page = context.new_page()
        page.goto(base_url)
        if page.locator("#theme-toggle").is_visible():
            errors.append("theme: without scripting the toggle is shown but cannot work")
    finally:
        context.close()
    return errors


def check_link_states(page) -> list[str]:
    """Keyboard focus is visible; citation hit areas remain usable on focus and hover."""
    errors = []
    for selector in ('.secnav a[href="#map"]', ".cn", "a.standalone", "#regions .pick .pname", "#corr a"):
        # Citations have a zero-height box (line-height 0), which Playwright treats as
        # invisible; select them by width and hover by coordinates instead.
        located = page.locator(selector)
        link = (
            located.filter(visible=True).first
            if selector != ".cn"
            else located.nth(
                located.evaluate_all(
                    "ns => ns.findIndex(n => n.getBoundingClientRect().width > 0)"
                )
            )
        )
        if not link.count():
            errors.append(f"missing link-state fixture: {selector}")
            continue
        link.scroll_into_view_if_needed()
        page.mouse.move(0, 0)
        page.keyboard.press("Shift")
        (link.locator("xpath=..") if selector.endswith(".pname") else link).focus()
        if link.evaluate("n => getComputedStyle(n).textDecorationLine") != "underline":
            errors.append(f"{selector} has wrong focus decoration")
        if selector.startswith(".secnav") and not link.evaluate(
            "n => { const p=document.createElement('span'); p.style.color='var(--ink)'; document.body.appendChild(p); const ok=getComputedStyle(n).color===getComputedStyle(p).color; p.remove(); return ok; }"
        ):
            errors.append("nav does not darken on focus")
        if selector == "#corr a" and not link.evaluate("n => getComputedStyle(n).textDecorationColor === getComputedStyle(n).color"):
            errors.append("inline link underline does not strengthen on focus")
        if selector == ".cn":
            errors.extend(check_citations(page))
            link.evaluate("n => n.blur()")
            box = link.bounding_box()
            page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
            errors.extend(check_citations(page))
        link.evaluate("n => n.blur()")
    page.mouse.move(0, 0)
    page.evaluate("scrollTo(0, 0)")
    return errors


def check_wrapped_rows(page) -> list[str]:
    """Force multi-line text in existing row types, then restore the DOM."""
    page.evaluate("""() => {
      window.rowFixtures = [];
      const targets = '.meta > span:first-child, .card .top .name, .fund-identity h4, .dlist .dl-title, .dlist .d, .card.mini > summary .find, .card:not(.mini) .row .v, .legend-definition, .fund-staff .fund-value, .fund-budget .fund-value, #world-rows td[data-col="inst"], .development-text';
      for (const selector of targets.split(', ')) {
        const node = document.querySelector(selector);
        if (!node) continue;
        const saved = [...node.childNodes];
        const fixture = document.createElement('span');
        fixture.textContent = 'First line'; fixture.appendChild(document.createElement('br'));
        fixture.appendChild(document.createTextNode('Second line wraps'));
        const swatch = node.querySelector(':scope > .sw');
        node.replaceChildren(...(swatch ? [swatch, fixture] : [fixture]));
        window.rowFixtures.push({node, saved});
      }
    }""")
    try:
        return check_baselines_and_rhythm(page)
    finally:
        page.evaluate("""() => {
          window.rowFixtures.forEach(({node, saved}) => node.replaceChildren(...saved));
          delete window.rowFixtures;
        }""")


def check_value_qualifiers(page) -> list[str]:
    """Qualifiers remain separate from values in cards, the table and funding."""
    return page.evaluate(r"""() => {
      // Folded records (funding disclosures) count too: open them, measure, restore.
      const folds = [...document.querySelectorAll('details:not([open])')]; folds.forEach(d => d.open = true);
      const errors = [];
      let tracked = 0;  // card and table fields that carry qualifiers: none found means the check saw nothing
      const fields = [...document.querySelectorAll('.card .row .v, #world-rows td, .fund-value, .funding-native, .fund-context, p[data-record]')].filter(n => n.getClientRects().length);
      const name = n => (n.closest('[data-j]') || {}).dataset?.j + ' ' + (n.closest('.row')?.querySelector('.k')?.textContent || n.closest('td')?.dataset.col || n.className);
      for (const field of fields) {
        for (const q of field.querySelectorAll('.qualifiers')) {
          if (!q.getClientRects().length) continue;
          // Nothing but citations and a notes line follows the qualifier line within its value.
          const holder = q.closest('.qualified, .budget-value') || q;
          for (let n = holder.nextSibling; n; n = n.nextSibling) if (!(n.nodeType === 1 && n.matches('.cites, .cite-glue, .record-notes, .qualifiers, .activity-kind')) && n.textContent.trim()) { errors.push(`text follows the qualifier line: ${name(field)}`); break; }
        }
        // Inline qualifiers: a confidence term, or a trailing as-of date after a comma, outside a qualifier line.
        if ([...field.querySelectorAll('.conf')].some(c => !c.closest('.qualifiers, .funding-metadata, .fund-context'))) errors.push(`confidence written inline: ${name(field)}`);
        const leadText = n => { const copy = n.cloneNode(true); copy.querySelectorAll('.cites, .qualifiers, .vh, .small').forEach(x => x.remove()); return copy.textContent; };
        // Fields that carry qualifiers keep their lead free of them: no separator dot, confidence, advisory, scope or as-of wording.
        const key = field.closest('.row')?.dataset.track || field.closest('td')?.dataset.col;
        if (['2', '3', '9', '10', 'since', 'budget', 'staff'].includes(key)) { tracked++; if (/·|\bconfidence\b|advisory|supranational|\bas of\b|\breported\b/.test(leadText(field))) errors.push(`qualifier written inline: ${name(field)}`); }
        const lead = field.querySelector('.value-line, .budget-amount') || (field.matches('.card .v, #world-rows td') ? field : null);
        if (lead && /, (?:\d{1,2} )?(?:(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec) )?\d{4}\s*$/.test(leadText(lead))) errors.push(`as-of date written inline: ${name(field)}`);
      }
      if (!tracked) errors.push('no card or table field with qualifiers found to check (tracks 2, 3, 9, 10, since, budget, staff)');
      folds.forEach(d => d.open = false);
      return errors;
    }""")


def expected_base_names(data: dict) -> dict[str, str]:
    """A state-first identity already disambiguates the institute's recorded base name."""
    return {i["id"]: re.sub(r" \((planned|proposed)\)$", "", i["name"]) for i in data["institutes"]}


def expected_display_names(data: dict) -> dict[str, str]:
    """Hand-worked display name per institute: the name without a status suffix, plus the country where two share it."""
    base = expected_base_names(data)
    uses = Counter(name.casefold() for name in base.values())
    country = {j["id"]: j["name"] for j in data["jurisdictions"]}
    return {
        i["id"]: base[i["id"]] + (f" ({country[i['jurisdiction_id']]})" if uses[base[i["id"]].casefold()] > 1 else "")
        for i in data["institutes"]
    }


def check_card_names(html, data: dict) -> list[str]:
    """Cards name the state; a grouped full card also announces its status."""
    soup = BeautifulSoup(html, 'html.parser')
    states = {j['id']: j for j in data['jurisdictions']}
    errors = []
    for card in soup.select('.card'):
        jid = card.get('data-j')
        state = states.get(jid)
        name = card.select_one(':scope > .top > .name, :scope > summary > .name')
        if not state or name is None or name.get_text() != state['name']:
            errors.append(f'{jid}: card heading must name the state alone')
            continue
        mini = 'mini' in card.get('class', [])
        if not mini and (tip_of(name) or name.get('title')) != state['name']:
            errors.append(f'{jid}: card tooltip must name the state alone')
        ids = [name.get('id')]
        if not mini and card.find_parent(class_=('statusgroup', 'eu-level')):
            status = card.select_one(':scope > .top > .vh')
            if status is None or status.get_text() != ', ' + STATUS_LABELS[state['status']]:
                errors.append(f'{jid}: grouped card must retain its status')
            ids.append(status.get('id') if status else None)
        if not all(ids) or card.get('aria-labelledby', '').split() != ids or card.has_attr('aria-describedby'):
            errors.append(f'{jid}: card aria-labelledby must include its state and any grouped status')
    return errors


def check_institute_names(html, data: dict) -> list[str]:
    """One display name per body, one recorded short form for tight places, never a bare generic acronym.

    Same-named bodies carry their country, tight places (table cells) show the recorded short form, the short
    form names the full display name on hover; the Institute field shows that display name, while state-first
    funding heads show the full base name without a repeated country suffix.
    """
    soup = BeautifulSoup(html, "html.parser")
    errors = check_card_names(html, data)
    names = expected_display_names(data)
    shorts = {i["id"]: i["short_name"] for i in data["institutes"]}
    if len({n.casefold() for n in names.values()}) != len(names):
        errors.append("two institutes share a display name")
    if len({s.casefold() for s in shorts.values()}) != len(shorts):
        errors.append("two institutes share a short name")
    owner = {i["id"]: i["jurisdiction_id"] for i in data["institutes"]}
    by_state = {}
    for iid, jid in owner.items():
        by_state.setdefault(jid, []).append(iid)
    # Cards show the display name in full, with no appended acronym.
    by_id = Keyed(soup, "[id]", "id")
    for iid, jid in owner.items():
        card = by_id.one(f"c-{jid.lower()}", ".row[data-track='2'] .v")
        if card is None:
            errors.append(f"{jid} card has no Institute row")
            continue
        text = " ".join("".join(t for t in card.find_all(string=True) if not t.find_parent(class_=("vh", "cites"))).split())  # no qualifier: the EU's 'supranational' is said by its heading
        if text != names[iid]:
            errors.append(f"{jid} card names its institute {text!r}, expected {names[iid]!r}")
        local = next(i.get("name_local") for i in data["institutes"] if i["id"] == iid)
        if local and local != names[iid] and not original_tip_matches(card.select_one(".original-text"), names[iid], local):
            errors.append(f"{jid} Institute: the shown name must carry name_local in a focusable tooltip")
    # Tight places show the recorded short form; hovering gives the full name.
    rows = {str(r["data-j"]): r for r in soup.select("table.world tbody tr[data-j]")}
    kinds = {j["id"]: j["kind"] for j in data["jurisdictions"]}
    for jid, ids in by_state.items():
        if kinds[jid] == "supranational":
            continue  # the EU is not a state: its entry is outside the table
        if len(ids) != 1:
            errors.append(f"{jid} has several institutes; the table cell rule needs a decision")
            continue
        iid = ids[0]
        cell = rows[jid].select_one("td[data-col=inst]") if jid in rows else None
        local = next(i.get("name_local") for i in data["institutes"] if i["id"] == iid)
        hover = names[iid] if names[iid] != shorts[iid] else local if local and local != shorts[iid] else None
        mark = cell.select_one("abbr") if cell else None
        shown = " ".join("".join(t for t in cell.find_all(string=True) if not t.find_parent(class_=("vh", "cites"))).split()) if cell else ""
        if shown != shorts[iid] or (mark is None) != (hover is None):
            errors.append(f"{jid} table cell does not show the short form {shorts[iid]!r} (marked for hover only when it has a name to give)")
        elif mark is not None and tip_of(mark) != hover:
            errors.append(f"{jid} short form {shorts[iid]!r} does not give {hover!r} on hover")
        # The region list is a tight place too: its short form, cut off where long, gives the same name on hover or focus.
        pick = soup.select_one(f'#regions .region-rows .pick[data-pick="{jid}"]')
        short = pick.parent.select_one(":scope > .small") if pick else None
        if pick is None or short is None:
            errors.append(f"{jid} region list row or its short form {shorts[iid]!r} is missing")
        else:
            mark = short.select_one("abbr.g")
            if short.get_text(strip=True) != shorts[iid] or (mark is None) != (hover is None) or (mark is not None and (tip_of(mark) != hover or mark.get("tabindex") != "0")):
                errors.append(f"{jid} region list short form {shorts[iid]!r} does not give {hover!r} on hover and focus")
    # Compact Funding rows keep the short institute name on their opener; records retain the full name.
    fund_rows = soup.select("#fund-rows .fund-item")
    if not fund_rows:
        errors.append("no funding rows found to check institute names in")
    for row in fund_rows:
        jid = str(row.get("data-j"))
        iid = row.get("data-institute")
        opener = row.select_one(".fund-row")
        if iid not in names:
            errors.append(f"{jid} funding row: unknown institute {iid!r}")
            continue
        if opener is None or any(name not in value for name in (shorts[iid], names[iid]) for value in (opener.get("aria-label", ""), tip_of(opener))):
            errors.append(f"{jid} funding row does not keep short form {shorts[iid]!r} on its focusable opener")
    countries = {j["id"]: j["name"] for j in data["jurisdictions"]}
    base_names = expected_base_names(data)
    records = soup.select("#fund-rows .fund-detail[data-institute]")
    if not records:
        errors.append("no funding records found to check institute names in")
    for record in records:
        iid = record.get("data-institute")
        detail = record.select_one(":scope > summary .fund-head-name")
        full = "".join(t for t in detail.find_all(string=True) if t.find_parent(class_="head-separator") or not t.find_parent(class_=("vh", "cites"))).strip() if detail else None
        expected = f"{countries[owner[iid]]}, {base_names[iid]}" if iid in names else None
        if expected is None or full != expected:
            errors.append(f"{iid} funding detail does not name the state before {names.get(iid)!r} in full")
        if detail is not None and (tip_of(detail) or detail.get('title')) != expected:
            errors.append(f"{iid} funding detail tooltip does not name the state and institute")
    return errors


LEADERSHIP_ROWS = ("Head", "Reports to", "Partners")  # card labels, in card order, directly after Form
CARD_PARTNERS = 3  # the card line shows three, then "and N more", when two or more would be hidden; the oracle's own copy
MISSING_TEXT = "Not published"  # dataset.method/missing-values, leadership and reporting-line.


def row_sources(data: dict, n: int, fields: list[str]) -> list[str]:
    """Sources whose excerpts support any of the given fields of institutes[n], read from the excerpts themselves."""
    pointers = {f"/institutes/{n}/{field}" for field in fields}
    # An excerpt bound to a leaf below the field (an instrument's title) supports the field too.
    return sorted(
        {
            s["id"]
            for s in data["sources"]
            for e in s.get("excerpts", [])
            if any(p == q or p.startswith(q + "/") for p in e["supports"] for q in pointers)
        }
    )


def partners_entries(data: dict, institute: dict) -> list:
    """The stated partner entries of an institute (the oracle's own accessor)."""
    return institute.get("national_partners", {}).get("entries", [])


# Hand-worked Head roles on the institute's own card, where the published role ends in that institute's name (rule: nothing restates its context).
OWN_CARD_HEAD_ROLES = {"AU": "General Manager", "CA": "Executive Director", "SG": "Executive Director", "KR": "Director", "NO": "Director"}


def expected_head_role(role: str, institute: dict) -> str:
    return OWN_CARD_HEAD_ROLES.get(institute["jurisdiction_id"], role)


def expected_leadership(data: dict) -> dict[str, dict[str, tuple[str, list[str], list[str], list[tuple[str, str, list[str]]]]]]:
    """Hand-worked rows: label -> (text or state word, partner texts, row sources, fold-out (name, how, sources) items)."""
    vocabularies = data["dataset"]["vocabularies"]
    expected: dict = {}
    for n, institute in enumerate(data["institutes"]):
        rows: dict = {}
        head = institute.get("head")
        if head:
            if head["state"] == "stated":
                person = next(p for p in institute["people"] if p["id"] == head["person_id"])
                rows["Head"] = (f"{person['name']}, {expected_head_role(person['role'], institute)}", [], row_sources(data, n, ["head/person_id"]), [])
            else:
                rows["Head"] = ("NA" if head["state"] == "not_applicable" else MISSING_TEXT, [], [], [])
        reports = institute.get("reports_to")
        if reports:
            if reports["state"] == "stated":
                text = reports.get("name_en") or reports["name"]
                rows["Reports to"] = (text, [], row_sources(data, n, ["reports_to/name"]), [])
            else:
                rows["Reports to"] = ("NA" if reports["state"] == "not_applicable" else MISSING_TEXT, [], [], [])
        partners = institute.get("national_partners")
        if partners:
            if partners["state"] == "stated":
                # Cyber agency first, then by relation; authored order breaks ties (the sort is stable).
                entries = sorted(
                    enumerate(partners["entries"]),
                    key=lambda pair: (not pair[1].get("cyber_agency"), PARTNER_RELATIONS.index(pair[1]["relation"])),
                )
                members = [(k, e) for k, e in entries if e["relation"] == "member"]
                others = [(k, e) for k, e in entries if e["relation"] != "member"]
                has_cyber = any(e.get("cyber_agency") for _, e in entries)
                for label, bodies in (("Members", members), ("Partners", others)):
                    if (label == "Members" and not members) or (label == "Partners" and members and not others and has_cyber):
                        continue
                    shown = bodies[:CARD_PARTNERS] if len(bodies) > CARD_PARTNERS + 1 else bodies
                    texts = []
                    for _, e in (bodies if label == "Members" else shown):  # the members fold lists every member; the line shows three
                        name = e.get("acronym") or e.get("name_en") or e["name"]
                        description = e["what"]
                        if label == "Partners" and e["relation"] != "works_with":
                            description += ", " + vocabularies["partner_relation"][e["relation"]]["label"]
                        texts.append(f"{name} ({description})")
                    hows = [
                        (e.get("acronym") or e.get("name_en") or e["name"], e["how"],
                         row_sources(data, n, [f"national_partners/entries/{k}/{field}" for field in ("name", "how")]))
                        for k, e in bodies
                    ]
                    cites = row_sources(data, n, [f"national_partners/entries/{k}/{field}" for k, _ in shown for field in ("name", "relation", "how", "cyber_agency", "what")])
                    if label == "Partners" and len(shown) < len(bodies):
                        texts.append(f"and {len(bodies) - len(shown)} more")  # nothing cut without a count
                    if label == "Partners" and not has_cyber:
                        texts.append(vocabularies["partner_cyber_none_published"])  # the tail is worded in the vocabulary only
                    rows[label] = ("", texts, cites, hows, len(bodies) - len(shown))
            else:
                rows["Partners"] = ("NA" if partners["state"] == "not_applicable" else MISSING_TEXT, [], [], [])
        expected[institute["id"]] = rows
    return expected


FORMER_MARK = re.compile(r"\b(?:then|former|formerly)\b", re.IGNORECASE)
TEXT_BLOCKS = "p, li, td, div.v, span.meta-item, .how"


def former_forms(institute: dict) -> set[str]:
    """Hand-worked former names of an institute: each recorded name and its bracketed short form, minus the current ones."""
    current = {institute["name"], institute.get("acronym"), institute.get("short_name")}
    forms = set()
    for entry in institute.get("names", []):
        m = re.fullmatch(r"(.+?) \(([^()]+)\)", entry["name"])
        forms.update([m.group(1), m.group(2)] if m else [entry["name"]])
    return {f for f in forms if f and f not in current and not any(f in c for c in current if c)}


def check_former_names(html, data: dict) -> list[str]:
    """Former names are marked as former: in an institute's own page text, a sentence that uses a former name says
    'then', 'former' or 'formerly', or gives the current short form after 'now'.

    Scope is the text the page writes about the institute's state (the block's own or closest data-j, else its card
    id), not source entries (a list item led by its source link), which quote the source. A former form inside another
    institute's name (AI Safety Institute in Japan AI Safety Institute) is that body's name, not a use.
    """
    soup = BeautifulSoup(html, "html.parser")
    for node in soup.select("script, style, template, details.srcs"):
        node.decompose()
    errors = []
    blocks = []
    for b in soup.select(TEXT_BLOCKS):
        first = next((c for c in b.children if getattr(c, "name", None) or str(c).strip()), None)
        if b.name == "li" and getattr(first, "name", None) == "a" and "ext" in (first.get("class") or []):
            continue  # a source entry: its title and publisher quote the source
        host = b if b.has_attr("data-j") else b.find_parent(attrs={"data-j": True})
        card = b.find_parent(id=re.compile(r"^c-[a-z]{2}$"))
        owner = host.get("data-j") if host else card.get("id")[2:].upper() if card else None
        blocks.append((owner, " ".join(b.get_text(" ").split())))  # nested blocks are also checked whole; errors are deduplicated
    for institute in data["institutes"]:
        forms = former_forms(institute)
        if not forms:
            continue
        jid, short = institute["jurisdiction_id"], institute["short_name"]
        others = [o["name"] for o in data["institutes"] if o["id"] != institute["id"] and any(f in o["name"] for f in forms)]
        pattern = re.compile(r"(?<![A-Za-z])(?:" + "|".join(re.escape(f) for f in sorted(forms, key=len, reverse=True)) + r")(?![A-Za-z])")
        now = re.compile(r"\bnow\s+" + re.escape(short) + r"(?![\w-])", re.IGNORECASE)
        for owner, text in blocks:
            if owner != jid:
                continue
            text = text.replace("U.S.", "US").replace("( ", "(").replace(" )", ")")
            for sentence in re.split(r"(?<=[.!?])\s+", text):
                inside = [(m.start(), m.end()) for name in others for m in re.finditer(re.escape(name), sentence)]
                uses = [m for m in pattern.finditer(sentence) if not any(s0 <= m.start() and m.end() <= e0 for s0, e0 in inside)]
                if uses and not FORMER_MARK.search(sentence) and not now.search(sentence):
                    error = f"{jid}: former name used without marking it as former: {sentence[:120]!r}"
                    if error not in errors:
                        errors.append(error)
    return errors


NO_RATE_TIP = "Shown in the national currency only, because the ECB publishes no euro reference rate for it."  # rule currency-rounding


def check_no_rate_amounts(html, data: dict) -> list[str]:
    """An amount in a currency without an ECB rate row is shown in that currency only, says why on hover and is never compared."""
    soup = BeautifulSoup(html, "html.parser")
    rated = {"EUR"} | {r["currency"] for r in data["dataset"].get("exchange_rates", [])}
    errors = []
    for institute in data["institutes"]:
        display = institute.get("display", {})
        for record in institute.get("funding", []) or []:
            currency = record.get("currency")
            if not currency or currency in rated:
                continue
            identity = record["id"]
            if record.get("eur_per_year_est") is not None or display.get("comparison_funding") == identity:
                errors.append(f"{identity}: {currency} has no ECB rate, so the amount is never converted or compared")
            amounts = soup.select(f'.budget-value[data-funding-id="{identity}"] .budget-amount, .funding-record[data-record-id="{identity}"] .budget-amount')
            if not amounts:
                errors.append(f"{identity}: {currency} amount is not shown")
            for amount in amounts:
                if "EUR" in amount.get_text() or currency not in amount.get_text():
                    errors.append(f"{identity}: amount must be in {currency} only, got {amount.get_text()!r}")
                if tip_of(amount) != NO_RATE_TIP or amount.get("tabindex") != "0":
                    errors.append(f"{identity}: {currency} amount must say on hover and focus why there is no euro figure")
    return errors


def check_euro_amounts(html, data: dict) -> list[str]:
    """Every shown amount in a currency with an ECB rate is also in euros, "~EUR 52m (JPY 8.8bn)", and its value cites
    the rate's source beside the amount's own (rule currency-rounding); a compared figure keeps its annual label."""
    soup = BeautifulSoup(html, "html.parser")
    errors = []
    for institute in data["institutes"]:
        compared = institute.get("display", {}).get("comparison_funding")
        summary = soup.select_one(f'.fund-detail[data-institute="{institute["id"]}"] .fund-budget-summary')
        if summary and (summary.get("data-source-funding-id") != compared or summary.select_one("[data-record-id], [data-funding-id]") or summary.has_attr("data-record-id") or summary.has_attr("data-funding-id")):
            errors.append(f"{institute['id']}: annual budget summary must name its source funding record separately from the record's own amount")
        for record in institute.get("funding", []) or []:
            identity, wanted_sources = record["id"], rate_sources(record, data)
            if not wanted_sources:
                continue
            if summary and identity == compared:
                figure = summary.select_one(".fund-value strong")
                annual = {**record, "amount": expected_annual_native(record)}
                expected_annual = expected_shown_funding_value(annual, data)
                if figure is None or figure.get_text(strip=True) != expected_annual:
                    errors.append(f"{identity}: annual budget summary must show {expected_annual!r}")
                cited = {a.get("data-source-id") for a in summary.select(".fund-value .cn")}
                if not set(wanted_sources) <= cited:
                    errors.append(f"{identity}: annual budget summary does not cite the amount and exchange-rate sources")
            expected = expected_shown_funding_value(record, data)
            figures = soup.select(f'.funding-record[data-record-id="{identity}"] .budget-amount, .related-funding-item[data-funding-id="{identity}"] .related-funding-eur')
            if identity != compared:
                figures += soup.select(f'.budget-value[data-funding-id="{identity}"] .budget-amount')
            native = re.compile(rf"\b{re.escape(record['currency'])} \d")
            for line in soup.select(f'.related-funding-item[data-funding-id="{identity}"]'):
                if line.select_one(".related-funding-eur") is None:
                    errors.append(f"{identity}: related funding does not lead with its euro figure: {line.get_text(strip=True)!r}")
                # The stated amount is always on the line, in the bracket or in the source wording (rule currency-rounding).
                if record["currency"] != "EUR" and not native.search(" ".join(line.get_text(" ").split())):
                    errors.append(f"{identity}: related funding never states the {record['currency']} amount: {line.get_text(strip=True)!r}")
            for figure in figures:
                after = figure.next_sibling
                # The source wording already gives the native amount, so omit its bracket after the euro figure.
                want = re.sub(r" \([^()]*\)$", "", expected) if "related-funding-eur" in figure.get("class", []) and isinstance(after, str) and after.startswith(": ") and native.search(after) else expected
                if figure.get_text(strip=True) != want:
                    errors.append(f"{identity}: amount shown as {figure.get_text(strip=True)!r}, expected {want!r}")
                value = figure.find_parent(lambda n: n.name == "td" or bool(set(n.get("class", [])) & {"v", "fund-value", "funding-native", "related-funding-item"}))
                cited = {a.get("data-source-id") for a in value.select(".cn")} if value else set()
                if not set(wanted_sources) <= cited:
                    errors.append(f"{identity}: euro figure {expected!r} does not cite the amount and exchange-rate sources")
    return errors


def check_leadership_rows(html, data: dict) -> list[str]:
    """Members, Head, Reports to and Partners: match the data, in order after Form, with the stated words and citations.

    The rows are absent for an institute without the fields (the page does not change before the data lands) and present on
    every card once any institute has them, except Members and Partners follow their entry and tail rules. Texts, body order,
    labels and the muted tail are worked out here from the data and its vocabularies, not the page's own selection.
    """
    soup = BeautifulSoup(html, "html.parser")
    expected = expected_leadership(data)
    errors = []
    for row in soup.select('.card .row'):
        if row.select_one('.k') and row.select_one('.k').get_text(strip=True) and not (row.select_one('.v') and row.select_one('.v').get_text(strip=True)):
            errors.append("card label without a value")
    any_rows = any(rows for rows in expected.values())
    by_id = Keyed(soup, "[id]", "id")
    for institute in data["institutes"]:
        jid = institute["jurisdiction_id"]
        card = by_id.one(f"c-{jid.lower()}")
        if card is None:
            continue  # an institute without a card is the card checks' concern
        rows = expected[institute["id"]]
        missing_fields = {"head", "reports_to", "national_partners"} - institute.keys()
        if any_rows and missing_fields:
            errors.append(f"{institute['id']}: data lacks {sorted(missing_fields)}; every institute needs all three rows once any has them")
        tracks = [r.get("data-track") for r in card.select(":scope > .row")]
        shown = [track for track in tracks if track in {CARD_TRACKS[label] for label in LEADERSHIP_ROWS}]
        want = [label for label in LEADERSHIP_ROWS if label in rows]
        wanted_tracks = [CARD_TRACKS[label] for label in want]
        if shown != wanted_tracks:
            errors.append(f"{jid}: card rows {shown} differ from the data's {wanted_tracks}")
            continue
        members = card.select('.members')
        if len(members) != int("Members" in rows) or any(node.find_parent(class_="row") != card.select_one('.row[data-track="4"]') for node in members):
            errors.append(f"{jid}: members must be in Form, with no separate Members row")
        for label in (["Members"] if "Members" in rows else []) + want:
            text, items, cites, hows, *cut = rows[label]
            row = card.select_one(f':scope > .row[data-track="{CARD_TRACKS["Form" if label == "Members" else label]}"]')
            heading = row.select_one(".k")
            if not heading or not heading.get_text(strip=True):
                errors.append(f"{jid}: {label} row label is missing or empty")
            value = row.select_one(".v")
            if value is None:
                continue
            if label == "Members":
                value = value.select_one('.members')
                if value is None:
                    errors.append(f"{jid}: members must be in Form")
                    continue
                names = copy.copy(value.select_one('.member-names'))
                if names:
                    for mark in names.select('.cites'):
                        mark.decompose()
                hidden = cut[0] if cut else 0  # the line shows three, then "and N more" (principle: Folds)
                line_names = [name for name, _, _ in hows][:len(hows) - hidden] + ([f"and {hidden} more"] if hidden else [])
                if not names or [" ".join(item.get_text().split()) for item in names.select(':scope > .meta-item')] != line_names:
                    errors.append(f"{jid} Members: short names differ")
            reports = institute.get("reports_to") or {}
            if label == "Reports to" and reports.get("state") == "stated" and reports.get("name_en"):
                if not original_tip_matches(value.select_one(".original-text"), reports["name_en"], reports["name"]):
                    errors.append(f"{jid} Reports to: the English name must carry the original in a focusable tooltip")
            if label in ("Members", "Partners") and not text:
                # Citation glue can split a name into spans, so retain text within each item and the gaps between items.
                clone = copy.copy(value.select_one('.member-list') if label == "Members" else value)
                if clone is None:
                    errors.append(f"{jid} {label}: itemised list missing")
                    continue
                for mark in clone.select(".cites, .partner-how"):
                    mark.decompose()
                got = " ".join(" ".join(li.get_text().split()) for li in clone.select('li')) if label == "Members" else " ".join(clone.get_text().split())
                if got != " ".join(items):
                    errors.append(f"{jid} {label}: shows {got!r}, expected {' '.join(items)!r}")
                tail = " ".join("".join(n.get_text() for n in value.select(".partners-none")).split())
                has_cyber = any(e.get("cyber_agency") for e in partners_entries(data, institute))
                want_tail = data["dataset"]["vocabularies"]["partner_cyber_none_published"] if label == "Partners" and not has_cyber else ""
                if tail != want_tail:
                    errors.append(f"{jid} {label}: the muted tail reads {tail!r}, expected {want_tail or 'none'!r}")
                ids = sorted({a.get("data-source-id") for a in value.select('.member-names .cn' if label == "Members" else '.cn') if a.find_parent(class_="partner-how") is None})
                if ids != cites:
                    errors.append(f"{jid} {label}: citations {ids}, expected {cites}")
                if label == "Members":
                    entries = sorted(enumerate(partners_entries(data, institute)), key=lambda pair: (not pair[1].get('cyber_agency'), PARTNER_RELATIONS.index(pair[1]['relation'])))
                    members = [(index, entry) for index, entry in entries if entry['relation'] == 'member']
                    wanted = [row_sources(data, data['institutes'].index(institute), [f'national_partners/entries/{index}/{field}' for field in ('name', 'what')]) for index, _ in members]
                    actual = [sorted({a.get('data-source-id') for a in li.select('.cn')}) for li in value.select('.member-list > li')]
                    if actual != wanted:
                        errors.append(f"{jid} Members: gloss citations {actual}, expected {wanted}")
                # Groups and names follow first occurrence in priority order; every name keeps its own sources.
                folded = []
                fold_cites = []
                for li in value.select(".partner-how li"):
                    names = []
                    # Citation glue is nested inside the name, so remove only citation text.
                    clone = copy.copy(li)
                    for name in clone.select(".pname"):
                        fold_cites.append(sorted({a.get("data-source-id") for a in name.select(".cn")}))
                        for mark in name.select(".cites"):
                            mark.decompose()
                        names.append(" ".join(name.get_text().split()))
                    how = clone.select_one(".phow")
                    folded.append((names, " ".join(how.get_text().split()) if how else ""))
                grouped = {}
                for name, how, source_ids in hows:
                    grouped.setdefault(how, []).append((name, source_ids))
                want_fold, want_cites = [], []
                for how, members in grouped.items():
                    if how in GENERIC_PARTNER_HOW:
                        sentence = ""
                    elif len(members) > 1:
                        sentence = PLURAL_PARTNER_HOW.get(how, how)
                        if how not in PLURAL_PARTNER_HOW:
                            errors.append(f"{jid} {label}: shared explanation needs explicit plural wording: {how!r}")
                    else:
                        sentence = how
                    want_fold.append(([name for name, _ in members], sentence[:1].lower() + sentence[1:]))
                    want_cites.extend(sorted(set(ids)) for _, ids in members)
                if folded != want_fold:
                    errors.append(f"{jid} {label}: fold-out shows {folded}, expected {want_fold}")
                if fold_cites != want_cites:
                    errors.append(f"{jid} {label}: fold-out citations {fold_cites}, expected {want_cites}")
                fold_selector = '.member-details' if label == "Members" else 'details.partner-how'
                if len(value.select(fold_selector)) != int(bool(hows)):
                    errors.append(f"{jid} {label}: expected {'one fold-out' if hows else 'no fold-out'}")
                fold = value.select_one(fold_selector)
                if fold is not None:
                    if label == "Members":
                        if fold.name != 'details' or not fold.select_one(':scope > summary'):
                            errors.append(f"{jid} Members: card needs one fold with a summary")
                        if fold.select('details'):
                            errors.append(f"{jid} Members: nested fold")
                        if value.select_one('.member-list').parent != fold or value.select_one('.partner-how') is None or value.select_one('.partner-how').parent != fold:
                            errors.append(f"{jid} Members: list and how must share one fold")
                    if any(a.find_parent("li") is None for a in fold.select(".cn")):
                        errors.append(f"{jid} {label}: fold-out citation outside a partner item")
                    if fold.has_attr("open"):  # arrival opens only the fold that holds its target (Folds, part 4)
                        errors.append(f"{jid} {label}: the fold-out must start closed")
                continue
            clone = copy.copy(value)
            for mark in clone.select(".cites, .vh"):
                mark.decompose()
            got = " ".join(clone.get_text().split())
            ids = sorted(a.get("data-source-id") for a in value.select(".cn"))
            if text == "NA":
                if not value.select_one(".missing.na") or got != "\u2013" or "Not applicable" not in value.get_text():
                    errors.append(f"{jid} {label}: not applicable must be the muted dash with Not applicable for screen readers, got {got!r}")
            elif got != text:
                errors.append(f"{jid} {label}: shows {got!r}, expected {text!r}")
            if ids != cites:
                errors.append(f"{jid} {label}: citations {ids}, expected {cites}")
    return errors


SHOWN_AUTHORITIES = 3  # AI Act authorities shown before the fold (owner, 15 November)


def designation_problem(body: dict, line: str) -> str | None:
    """Whether an AI Act authority's line states its designation as the data has it: designated by its basis;
    proposed, with the latest formal step and its year where designation_step has them; otherwise neither."""
    text, state = line.lower(), body.get("designation_state")
    if state == "designated":
        basis = (body.get("designation_basis") or "").replace("_", " ") or "an act"
        if f"designated by {basis}" not in text or re.search(r"\bnot\b", text) or "proposed" in text:
            return f"does not say it is designated by {basis}"
    elif state == "proposed":
        step = body.get("designation_step")
        if "proposed" not in text or "designated by" in text or (step and (step["text"].lower() not in text or step["date"][:4] not in text)):
            return "does not say it is proposed" + (f" with its latest step {step['text']!r} and its date" if step else "")
    elif "designated" in text or "proposed" in text:
        return "claims a designation the data does not record"
    return None


def authority_key(body: dict):
    """Pattern 4's order: the single point of contact first, then the most roles, ties by name."""
    roles = body.get("authority_roles") or []
    return ("single_point_of_contact" not in roles, -len(roles), body.get("name_en") or body["name"])


def shown_original(body: dict) -> str:
    """The original name in the English name's tooltip: a closing "(ABBR)" the English name also ends
    with is said once, in the English name."""
    match = re.search(r"\s*(\([^()]+\))$", body["name"])
    return body["name"][: match.start()] if match and body.get("name_en", "").endswith(match.group(1)) else body["name"]


def check_how_we_checked(html: str, data: dict) -> list[str]:
    """Migrated notes retain their generated paragraphs, three before a closed fold, without citations or reading logs.

    Legacy notes can still name documents read; that path remains unchanged until those records migrate.
    """
    soup = BeautifulSoup(html, "html.parser")
    errors = []
    for j in data["jurisdictions"]:
        if "claims" not in j:
            continue
        card = soup.find(id=f"c-{j['id'].lower()}")
        if card is None:
            continue
        blocks = card.select(".how")
        expected = [" ".join(p.split()) for p in re.split(r"\n\s*\n", j.get("notes", "")) if p.strip()]
        if "mini" in card.get("class", []) and len(blocks) != bool(expected):
            errors.append(f"{j['id']}: How we checked block missing or duplicated")
        for block in blocks:
            heading = block.select_one(":scope > .gl")
            if not heading or not heading.get_text(strip=True):
                errors.append(f"{j['id']}: How we checked heading is empty")
            shown = [" ".join(p.get_text().split()) for p in block.select(":scope > p:not(.gl)")]
            folds = block.select("details")
            folded = [" ".join(p.get_text().split()) for fold in folds for p in fold.select("p")]
            if shown != expected[:3] or folded != expected[3:]:
                errors.append(f"{j['id']}: How we checked paragraphs differ from notes or the three-paragraph limit")
            hidden = max(0, len(expected) - 3)
            if len(folds) != bool(hidden) or any(fold.has_attr("open") for fold in folds):
                errors.append(f"{j['id']}: How we checked needs one closed fold only for extra paragraphs")
            elif hidden:
                summary = folds[0].find("summary", recursive=False)
                if summary is None or re.findall(r"\d+", summary.get_text()) != [str(hidden)]:
                    errors.append(f"{j['id']}: How we checked fold count differs from its hidden paragraphs")
            if block.select(".cn, .cites"):
                errors.append(f"{j['id']}: generated How we checked has citation marks")
            if re.search(r"\b(?:were|was) read\b|\d{4}-\d{2}-\d{2}-[a-z]", block.get_text(" "), re.I):
                errors.append(f"{j['id']}: How we checked names a document read or a research pass id")
            footer = card.select_one(".foot .source-after")
            if expected and footer and re.search(r"\bChecked\b", footer.get_text()):
                errors.append(f"{j['id']}: footer repeats the check date from How we checked")
    return errors


def check_nonidentified_folds(html, data: dict) -> list[str]:
    """None-identified fold-outs: the closest body, the AI Act authorities and other bodies, each group recognised by
    its members, so the owner may reword a heading (test strategy D1); a heading must have text.

    A migrated closest body shows its activity and other role claims (expired ones marked) under its name and stage;
    when the summary names it (pattern 2, which gives the stage too) the name and stage are left out, the finding
    cites the body's sources, and the group is omitted when nothing else remains. Legacy notes stay as before. The AI
    Act authorities are one group in pattern 4's order, each line naming its roles in the published words
    (dataset.display.authority_roles); with more than four, the first three show and the rest fold behind a summary
    giving their number. An English name carries the original in its own focusable tooltip; without an English
    form, the original stands as it is. Each body with sources cites them inline, and each role text is shown.
    """
    from summaries import shown_lead

    soup = BeautifulSoup(html, "html.parser")
    errors = check_how_we_checked(html, data)
    words = (data.get("dataset", {}).get("display") or {}).get("authority_roles") or {}
    folds = Keyed(soup, "details.card.mini[id]", "id")
    for j in data["jurisdictions"]:
        card = folds.one(f"c-{j['id'].lower()}")
        if card is None:
            if j.get("status") == "none_identified" and j.get("europe_scope"):
                errors.append(f"{j['id']} none-identified entry is missing")
            continue
        if card.select("p.related"):
            errors.append(f"{j['id']}: the fold-out carries a line about the bodies as a group; it was removed (owner, 15 November)")
        closest = j.get("closest_body")
        bodies = [b for b in j.get("related_bodies", [])
                  if not closest or (b["id"] != closest.get("body_id") if "claims" in j
                                     else b["name"] != closest["name"] and not b["name"].startswith(closest["name"] + " ("))]
        expected = {}
        lead = shown_lead(j) if "claims" in j else None
        closest_claims = [c for c in j.get("claims", []) if c["kind"] == "function" and closest
                          and c.get("body_id") == closest.get("body_id") and c is not lead]
        closest_sources = (closest.get("display", {}).get("source_links", closest.get("source_ids", [])) if closest else [])
        in_summary = bool(closest and "claims" in j and closest["name"] in j.get("summary", ""))
        active = any(c["kind"] == "function" and c.get("basis") == "activity" and not c.get("expired")
                     and c.get("body_id") == closest.get("body_id") for c in j.get("claims", [])) if closest else False
        if in_summary:  # the name's marks move to the finding paragraph that names the body
            naming = [p for p in card.select(":scope > p") if closest["name"] in " ".join(p.get_text().split())]
            marked = {a.get("data-source-id") for a in naming[0].select("a.cn")} if naming else set()
            if not set(closest_sources) <= marked:
                errors.append(f"{j['id']}: the finding paragraph that names the closest body lacks citation marks for {sorted(set(closest_sources) - marked)}")
        if closest and (not in_summary or closest_claims or active):
            expected["closest"] = [closest]

        def shown_names(key, members):
            return [] if key == "closest" and in_summary else [b.get("name_en") or b["name"] for b in members]
        if authorities := sorted((b for b in bodies if b["kind"] == "ai_act_authority"), key=authority_key):
            expected["authorities"] = authorities
        if rest := [b for b in bodies if b["kind"] != "ai_act_authority"]:
            expected["other"] = rest
        groups, found = {}, []
        for group in card.select(".body-group"):
            names = []
            for n in group.select("li .body-name"):
                n = copy.copy(n)
                for extra in n.select(".cites, .body-roles"):
                    extra.extract()  # the numerals and an authority's roles are not part of the name
                names.append(" ".join(n.get_text().split()))
            found.append(names)
            matches = [key for key, members in expected.items() if names == shown_names(key, members)]
            if len(matches) != 1 or matches[0] in groups:
                errors.append(f"{j['id']} fold-out group lists {names}, which matches no expected group or repeats one")
            if len(matches) == 1 and matches[0] not in groups:
                groups[matches[0]] = group
                heading = group.select_one(".gl")
                if not heading or not heading.get_text().strip():
                    errors.append(f"{j['id']} fold-out groups: the {matches[0]} heading is empty")
        if list(groups) != list(expected):
            errors.append(f"{j['id']} fold-out groups list {found}, expected {[shown_names(k, m) for k, m in expected.items()]} in this order")
            continue
        for key, members in expected.items():
            group = groups[key]
            items = group.select("li")
            if key == "authorities":
                fold = group.select_one("details.more")
                hidden = len(members) - SHOWN_AUTHORITIES if len(members) > SHOWN_AUTHORITIES + 1 else 0
                in_fold = len(fold.select("li")) if fold else 0
                summary = fold.summary.get_text() if fold and fold.summary else ""
                numbers = [int(n) for n in re.findall(r"\d+", summary)]
                folded = []
                for n in (fold.select("li .body-name") if fold else []):
                    n = copy.copy(n)
                    for extra in n.select(".cites, .body-roles"):
                        extra.extract()
                    folded.append(" ".join(n.get_text().split()))
                wanted = [b.get("name_en") or b["name"] for b in members[len(members) - hidden:]] if hidden else []
                if in_fold != hidden or folded != wanted or (hidden and numbers != [hidden]):
                    errors.append(f"{j['id']} AI Act authorities: {folded} folded under {summary.strip()!r}, expected {wanted} under their number {hidden}")
            for li, b in zip(items, members):
                cited = bool(li.select(".cn"))
                if key == "closest" and "claims" in j and not in_summary:
                    missing_sources = set(closest_sources) - {a.get("data-source-id") for a in li.select("a.cn")}
                    if missing_sources:
                        errors.append(f"{j['id']}: closest body lacks citation marks for {sorted(missing_sources)}")
                want = b.get("name_en") or b["name"]
                if b.get("name_en") and not (key == "closest" and in_summary):
                    name = li.select_one(".body-name > span")
                    if not original_tip_matches(name, want, shown_original(b)):
                        errors.append(f"{j['id']} body {want!r}: the English name alone must carry the original name in a focusable tooltip, without a repeated closing abbreviation")
                if key == "authorities":
                    # The citation glue stays inside one .body-roles span. Read every span in order, without the
                    # marks, so a split line is still checked for its words as well as its structure.
                    spans = li.select(".body-roles")
                    if len(spans) != 1:
                        errors.append(f"{j['id']} body {want!r}: expected exactly one .body-roles span, found {len(spans)}")
                    line = " ".join("".join(t for s in spans for t in s.find_all(string=True) if not t.find_parent(class_="cites")).split())
                    missing = [words.get(r, r) for r in (b.get("authority_roles") or []) if words.get(r, r) not in line]
                    if not line or missing:
                        errors.append(f"{j['id']} body {want!r}: its line {line!r} does not name its roles {missing}")
                    if problem := designation_problem(b, line):
                        errors.append(f"{j['id']} body {want!r}: its line {line!r} {problem}")
                for marks in li.select(".cites"):
                    marks.extract()
                for extra in li.select("details.quote, .translation, .claim > .reread"):
                    extra.extract()
                dets = [" ".join(d.get_text().split()) for d in li.select(".det")]
                if "claims" in j:
                    body_id = b.get("body_id") if key == "closest" else b["id"]
                    want_dets = [" ".join(c["text"].split()) for c in j["claims"] if c["kind"] == "function"
                                 and c.get("body_id") == body_id and c is not lead]
                    if key == "closest" and b.get("stage", "exists") != "exists" and not in_summary:
                        stage = b["stage"].replace("_", " ").capitalize() + "."
                        # An activity line follows the stage directly under the name; without it the existing order stays.
                        if b.get("active_in"):
                            want_dets.insert(0, stage)
                        else:
                            want_dets.append(stage)
                    if dets != want_dets:
                        errors.append(f"{j['id']} body {want!r}: role sentences {dets!r}, expected {want_dets!r}")
                elif key == "closest":
                    note = " ".join((b.get("note") or "").split())
                    shown_note = note if note and note != f"{b['name']}." else ""
                    stage = (b.get("stage") or "exists").replace("_", " ")
                    want_dets = [shown_note] if shown_note else []
                    if stage != "exists":
                        want_dets.append(stage)
                    got = [d for d in dets]
                    if len(got) != len(want_dets) or (shown_note and got[0] != shown_note) or (stage != "exists" and got[-1].rstrip(".").lower() != stage):
                        errors.append(f"{j['id']} closest body shows {dets!r}: its note (unless only its name), then its stage ({stage!r}) only where it does not exist yet, and no reason")
                elif (role := " ".join((b.get("role") or "").split())) and role not in dets:
                    errors.append(f"{j['id']} body {want!r}: its role text is nowhere in the entry")
                if b.get("source_ids") and not cited:
                    errors.append(f"{j['id']} body {want!r} has no inline citation")
    return errors


MEDIAN_EXCEPTION = "except the medians in the counts row"  # full rules
READER_MEDIAN_EXCEPTION = "except the medians"  # reader summaries: readers never see "counts row"


def check_method_pages(html: str, data: dict, dist: Path) -> list[str]:
    """Independent rendered contracts for summaries, links and verbatim full rules."""
    soup = BeautifulSoup(html, "html.parser")
    errors = []
    summaries = [s for s in data["dataset"]["method_summaries"] if s["summary"] != data["dataset"].get("definition")]
    if len(soup.select("#rules > li")) != len(summaries):
        errors.append("Method: reader summary count differs from the dataset")
    for summary in summaries:
        sid = summary["id"]
        item = soup.find(id="method-summary-" + sid)
        text = item.select_one(".method-copy") if item else None
        want = summary["summary"]
        if text is None or not text.get_text(strip=True) or " ".join(text.get_text().split()) != " ".join(want.split()):
            errors.append(f"Method summary {sid}: reader copy differs")
        links = [(a.get("href"), a.get_text()) for a in item.select("a")] if item else []
        rules = {r["id"]: r for r in data["dataset"]["method"]}
        names = [None] if len(summary["rules"]) == 1 else [rule_label(rules[rid]) if rid in rules else rid for rid in summary["rules"]]
        if len(links) != len(summary["rules"]) or any(href != "method.html#" + rid or not label.strip() or (name is not None and label != name) for (href, label), rid, name in zip(links, summary["rules"], names)):
            errors.append(f"Method summary {sid}: full-rule links differ")
    for selector in ("footer", "#method-details"):
        if not soup.select(f'{selector} a[href="method.html"]'):
            errors.append(f"Full method: missing link in {selector}")
    path = dist / "method.html"
    if not path.is_file():
        return errors + ["Full method: method.html is missing"]
    full = BeautifulSoup(path.read_text(encoding="utf-8"), "html.parser")
    ids = [node.get("id") for node in full.select("main section")]
    if ids != [r["id"] for r in data["dataset"]["method"]]:
        errors.append("Full method: rule anchors differ from the dataset")
    # The ~ convention excepts the counts-row medians (check_figures_row); every text stating it says so.
    marks = [(f"Method summary {s['id']}", s["summary"], READER_MEDIAN_EXCEPTION) for s in summaries] + [(f"Full method rule {r['id']}", r["text"], MEDIAN_EXCEPTION) for r in data["dataset"]["method"]]
    for where, text, exception in marks:
        if "~ marks" in text and exception not in text:
            errors.append(f"{where}: says what ~ marks without '{exception}'")
    for rule in data["dataset"]["method"]:
        section = full.find(id=rule["id"])
        parts = [p.get_text() for p in section.select(".section-body > p")] if section else []
        if " ".join(parts) != rule["text"]:
            errors.append(f"Full method rule {rule['id']}: text differs from the full rule")
    # The main page's section bar: the title back to the tracker and the theme toggle.
    bar = full.select_one("header nav.secnav .measure")
    kids = [n for n in bar.children if getattr(n, "name", None)] if bar else []
    if [(n.name, n.get("href") or n.get("id")) for n in kids] != [("a", "index.html#method-sec"), ("button", "theme-toggle")] or "navtitle" not in kids[0].get("class", []):
        errors.append("Full method: the header is not the section bar with the title link back to the tracker and the theme toggle")
    return errors


def check_generic_sources(html, data: dict) -> list[str]:
    """Population and region sources apply to every entry: out of card source lists, named once in Method."""
    soup = BeautifulSoup(html, "html.parser")
    sources = {s["id"]: s for s in data["sources"]}
    generic = {
        sid
        for j in data["jurisdictions"]
        for field in GENERIC_FIELDS
        for sid in (j.get(field) or {}).get("source_ids", [])
    }
    errors = []
    if not generic:
        errors.append("no generic population or region sources found in the data")
    lists = soup.select(".srcs li[data-source-id]")
    if not [li for li in lists if li.get("data-source-id") not in generic]:
        errors.append("no source lists found to check")
    for li in lists:
        sid = li.get("data-source-id")
        host = li.find_parent(attrs={"data-j": True})
        jid = host.get("data-j") if host else None
        # A source that also supports the entry itself belongs in its list.
        own = {
            s
            for record in [*data["jurisdictions"], *data["institutes"]]
            if jid is not None and jid in (record["id"], record.get("jurisdiction_id"))
            for s in entry_sources(record)
        }
        if sid in generic and sid not in own:
            errors.append(f"{jid}: source list repeats generic source {sid}")
    box = soup.select_one("#generic-sources")
    links = {a.get("href") for a in box.select("a")} if box else set()
    for sid in sorted(generic):
        if source_href(sources[sid]) not in links:
            errors.append(f"Method does not name generic source {sid}")
    text = box.get_text().replace(" (opens in a new tab)", "") if box else ""
    if not (text.startswith(GENERIC_LEAD) and text.endswith(GENERIC_TAIL)):
        errors.append("Method generic sources line differs from its copy")
    return errors


GENERIC_LEAD = "Population and region sources apply to every entry: "
GENERIC_TAIL = ". Entry source lists leave them out; the downloads include them."


def reread_failed(source: dict) -> bool:
    """A source whose quotations' worst re-read state is a failure: offline, unreadable or quote not found."""
    return any(e.get("reread", {}).get("state") in {"blocked_or_failed", "source_gone", "quote_gone"} for e in source.get("excerpts", []))


def reread_worst(source: dict) -> dict | None:
    """The worst failed re-read state of a source's quotations, ordered as the page's Re-read line orders them."""
    states = [e["reread"] for e in source.get("excerpts", []) if e.get("reread")]
    rank = {"confirmed": 0, "not_attempted": 0, "blocked_or_failed": 1, "source_gone": 2, "quote_gone": 2}
    tie = {"source_gone": 0, "quote_gone": 1, "blocked_or_failed": 2}
    top = max((rank[r["state"]] for r in states), default=0)
    if not top:
        return None
    return min((r for r in states if rank[r["state"]] == top), key=lambda r: (r["since"], tie[r["state"]], r["last_found"]))


def source_href(source: dict) -> str:
    """Archived copies (principle): an offline source with an archived copy links to the copy wherever it is linked."""
    worst = reread_worst(source)
    return source["archive_url"] if source.get("archive_url") and worst and worst["state"] == "source_gone" else source["url"]


def check_archive_links(html, data: dict) -> list[str]:
    """Archived copies (principle): " · archived copy" ends a source's line where the source could not be re-read,
    after its caveat; a record's source list also gives every other archived copy; a claim's own sources and Method's
    inline links give none otherwise. A source without an archive_url shows none. An offline source with an
    archived copy links to the copy (source_href) in its citation marks and source-list titles.

    The oracle is the data: each source list item names its source id, and the Method
    inline links follow display.generic_sources, then each other overview's source_links.
    Superscript citations never carry an archive link.
    """
    soup = BeautifulSoup(html, "html.parser")
    sources = {s["id"]: s for s in data["sources"]}
    errors, expected = [], 0

    def visible(node) -> str:
        return "".join(t for t in node.find_all(string=True) if not t.find_parent(class_="vh"))

    def ends(parent, link, sid: str, where: str, record: bool) -> None:
        nonlocal expected
        src = sources[sid]
        archive = src.get("archive_url") or ""
        want = bool(archive) and (record or reread_failed(src))
        after = [n for n in link.find_next_siblings() if n.find_parent() is parent]
        copies = [n for n in after if "archive-copy" in (n.get("class") or [])]
        if not want:
            if copies:
                errors.append(f"{where}: source {sid} shows an archived copy link it should not")
            return
        expected += 1
        if len(copies) != 1:
            errors.append(f"{where}: source {sid} lacks its archived copy link")
            return
        copy, a = copies[0], copies[0].select_one("a.archive")
        gap = copy.find_previous_sibling()
        if a is None or a.get("href") != archive or not visible(a).strip():
            errors.append(f"{where}: source {sid} archived copy link differs from data")
        elif visible(copy) != visible(a) or gap is None or "sep-gap" not in gap.get("class", []):
            errors.append(f"{where}: source {sid} archived copy is not one \" \u00b7 \" phrase after its line")
        tail = [n for n in after if "reread-tail" in (n.get("class") or [])]
        preceding = [n for n in copy.find_previous_siblings() if 'sep-gap' not in (n.get('class') or [])]
        if (tail and (not preceding or preceding[0] is not tail[0])) or any(
                n.find_parent() is parent for n in copy.find_next_siblings() if not {'reread', 'sep-gap'}.intersection(n.get("class") or [])):
            errors.append(f"{where}: source {sid} archived copy does not end its source line")

    items = soup.select("li[data-source-id]")
    if not items:
        errors.append("no source lists found to check archive links")
    for li in items:
        sid, scope = str(li["data-source-id"]), li.find_parent(attrs={"data-source-scope": True})
        host = scope or li.find_parent(attrs={"data-j": True})
        where = f"{(host.get('data-source-scope') or host.get('data-j')) if host else 'page'} source list"
        link = li.select_one("a[href]")
        if sid in sources and link is not None:
            ends(li, link, sid, where, "fn-sources" not in (li.parent.get("class") or []))
    display = data["dataset"]["display"]
    lists = [display["generic_sources"]] + [o["display"]["source_links"] for o in data["dataset"].get("other_overviews") or []]
    spans = soup.select(".inline-links")
    if len(spans) != len(lists):
        errors.append(f"Method: {len(spans)} inline source groups, data has {len(lists)}")
    for span, ids in zip(spans, lists):
        links = [a for a in span.select("a") if "archive" not in (a.get("class") or []) and not a.find_parent(class_="archive-copy")]
        if [a.get("href") for a in links] != [source_href(sources[sid]) for sid in ids]:
            errors.append(f"Method: inline sources differ from data ({', '.join(ids)})")
            continue
        for n, (link, sid) in enumerate(zip(links, ids)):
            nxt = links[n + 1] if n + 1 < len(links) else None
            src = sources[sid]
            want = bool(src.get("archive_url")) and reread_failed(src)
            between = []
            for sib in link.find_next_siblings():
                if sib is nxt:
                    break
                between.append(sib)
            copies = [b for b in between if "archive-copy" in (b.get("class") or [])]
            if want:
                expected += 1
            if bool(copies) != want:
                errors.append(f"Method: source {sid} {'lacks its' if want else 'shows an'} archived copy link")
    shown = soup.select("a.archive")
    if len(shown) != expected:
        errors.append(f"{len(shown)} archived copy links on the page, {expected} expected from source lists and Method")
    if any(a.find_parent(class_="cn") or "cn" in (a.get("class") or []) for a in shown):
        errors.append("a superscript citation carries an archived copy link")
    # An offline source with an archived copy is linked to the copy wherever it is linked: its marks and source-list titles.
    for sid, src in sources.items():
        href = source_href(src)
        if href == src["url"]:
            continue
        links = soup.select(f'a.cn[data-source-id="{sid}"]') + [li.select_one("a[href]") for li in soup.select(f'li[data-source-id="{sid}"]')]
        if any(a is None or a.get("href") != href for a in links):
            errors.append(f"offline source {sid}: a citation mark or source-list link does not go to its archived copy")
    return errors


def expected_reread_line(source: dict) -> str | None:
    """The re-read sentence the page principle (Re-read line) asks for, from the source's quotations' own states."""
    states = [e["reread"] for e in source.get("excerpts", []) if e.get("reread")]
    if not states:
        return None
    worst = reread_worst(source)
    if not worst:
        return f"Source last re-read {expected_date(max(r['last_found'] for r in states))}."
    since, found = expected_date(worst["since"]), expected_date(worst["last_found"])
    if worst["state"] == "quote_gone":
        gone = sum(r["state"] == "quote_gone" for r in states)
        count = f": {gone} of {len(states)} quotes" if any(r["state"] in {"confirmed", "not_attempted"} for r in states) else ""
        return f"Not found in the source on {since}{count}; last found {found}; under review."
    if worst["state"] == "source_gone":
        return f"Source offline since {since}; last read {found}."
    return f"Source could not be re-read since {since}; last read {found}."


def expected_reread_tail(source: dict) -> str | None:
    """The short caveat that ends the source line of a source that could not be re-read: " · last read {date}"."""
    if not reread_failed(source):
        return None
    states = [e["reread"] for e in source.get("excerpts", []) if e.get("reread")]
    rank = {"blocked_or_failed": 1, "source_gone": 2, "quote_gone": 2}
    tie = {"source_gone": 0, "quote_gone": 1, "blocked_or_failed": 2}
    top = max(rank.get(r["state"], 0) for r in states)
    worst = min((r for r in states if rank.get(r["state"], 0) == top), key=lambda r: (r["since"], tie[r["state"]], r["last_found"]))
    word = "quote last found" if worst["state"] == "quote_gone" else "last read"
    return f"{word} {expected_date(worst['last_found'])}"


def check_reread_lines(html, data: dict) -> list[str]:
    """Each source list item carries its re-read state once: a source that could not be re-read ends its line with
    " · last read {date}" (the full sentence as its tip and for screen readers), any other source with a re-read
    state ends with its own re-read line; none says verified."""
    soup = BeautifulSoup(html, "html.parser")
    sources = {s["id"]: s for s in data["sources"]}
    errors, items = [], soup.select("li[data-source-id]")
    for li in items:
        sid = str(li["data-source-id"])
        if sid not in sources:
            continue
        src = sources[sid]
        lines, tails = li.select(".reread"), li.select(".reread-tail")
        expected, tail = expected_reread_line(src), expected_reread_tail(src)
        if tail:
            shown = "".join(t for t in tails[0].find_all(string=True) if not t.find_parent(class_="vh")) if len(tails) == 1 else None
            full = tails[0].select_one(".vh") if len(tails) == 1 else None
            gap = tails[0].find_previous_sibling() if len(tails) == 1 else None
            if lines or shown != tail or gap is None or "sep-gap" not in gap.get("class", []):
                errors.append(f"source {sid}: re-read caveat {shown!r}, expected {tail!r} and no separate line")
            elif tails[0].parent is not li or tails[0].get("data-tip") != expected or tails[0].has_attr("aria-description") or full is None or " ".join(full.get_text().split()) != expected:
                errors.append(f"source {sid}: the caveat's tip or screen reader sentence differs from {expected!r}")
            continue
        text = [" ".join(line.get_text().split()) for line in lines]
        if "fn-sources" in (li.parent.get("class") or []):
            expected = None  # "Source last re-read" belongs to a record's source list only
        if tails or text != ([expected] if expected else []):
            errors.append(f"source {sid}: re-read line {text or 'missing'}, expected {expected or 'none'}")
        elif lines and (lines[0].parent is not li or any(
                not isinstance(after, str) or after.strip() for after in lines[0].next_siblings)):
            errors.append(f"source {sid}: the re-read line is not the item's last part")
    if any("verified" in line.get_text().lower() for line in soup.select(".reread, .reread-tail")):
        errors.append("a re-read line says verified")
    return errors


def check_archive_wrap(page) -> list[str]:
    """"(archived copy)" never breaks across lines. Opens the source lists to measure, then restores them."""
    return page.evaluate("""() => {
      const folds = [...document.querySelectorAll('details:not([open])')];
      folds.forEach(d => d.open = true);
      const copies = [...document.querySelectorAll('.archive-copy')].filter(n => n.getClientRects().length);
      const broken = copies.filter(n => new Set([...n.getClientRects()].map(r => Math.round(r.top))).size > 1);
      folds.forEach(d => d.open = false);
      const errors = broken.slice(0, 5).map(n => 'archived copy breaks across lines after ' + (n.previousElementSibling || n.parentElement).textContent.slice(0, 40));
      if (!copies.length) errors.push('no archived copy links to measure');
      return errors;
    }""")


def check_fold_block_gaps(page) -> list[str]:
    """Consecutive blocks in an open fold (none-identified entries, funding records) never overlap.

    Opens every disclosure to measure, then restores the page.
    """
    return page.evaluate("""() => {
      const folds = [...document.querySelectorAll('details:not([open])')];
      folds.forEach(d => d.open = true);
      const errors = [];
      for (const box of document.querySelectorAll('.card.mini, .fund-detail-body > div:not(.srcs), .funding-record')) {
        const kids = [...box.children].filter(k => k.tagName !== 'SUMMARY' && k.getClientRects().length && getComputedStyle(k).position !== 'absolute');
        for (let i = 1; i < kids.length; i++) {
          const gap = kids[i].getBoundingClientRect().top - kids[i - 1].getBoundingClientRect().bottom;
          if (gap < -0.5) errors.push(`blocks overlap in ${box.closest('[data-j]')?.dataset.j || box.closest('.fund-item')?.querySelector('h3')?.textContent.trim() || box.className}: ${kids[i].tagName.toLowerCase()}.${kids[i].className} ${gap.toFixed(1)}px`);
        }
      }
      folds.forEach(d => d.open = false);
      return [...new Set(errors)];
    }""")


def section_targets(page) -> list[tuple[str, object]]:
    """Every status group (not just the first), then the figures row, the access groups, the toolbar, table and funding chart."""
    targets = []
    groups = page.locator(".statusgroup")
    for index in range(groups.count()):
        group = groups.nth(index)
        targets.append((f"cards-{index + 1}-{group.get_attribute('data-status')}", group))
    for name, selector in (
        ("figures", "#figures"),
        ("access", "#access"),
        ("toolbar", ".tbar"),
        ("table", "#world-scroll"),
        ("funding", "#fund-rows"),
    ):
        target = page.locator(selector).first
        if target.count():
            targets.append((name, target))
    return targets


# Elements with their own text inside a page-coordinate clip that would show but are skipped by content-visibility
# (hidden, invisible or transparent ones do not count: they are blank in any capture). Text directly inside a
# content-visibility element counts too: no script can tell whether its own contents are skipped, and the page keeps
# such text in child elements.
SKIPPED_IN_CLIP = """clip => [...document.querySelectorAll('main *')].filter(n =>
  [...n.childNodes].some(t => t.nodeType === 3 && t.data.trim())
  && n.checkVisibility && n.checkVisibility({ visibilityProperty: true, opacityProperty: true })
  && (!n.checkVisibility({ contentVisibilityAuto: true }) || getComputedStyle(n).contentVisibility === 'auto') && (() => { const r = n.getBoundingClientRect();
  return r.bottom + scrollY > clip.y && r.top + scrollY < clip.y + clip.height && r.right + scrollX > clip.x && r.left + scrollX < clip.x + clip.width; })()).length"""


def capture_sections(page, screenshots: Path, prefix: str, landmarks: bool = False) -> None:
    """Save useful crops without moving the viewport or affecting measurements; with landmarks,
    also the named page parts inside each crop (see save_landmarks)."""
    for name, target in section_targets(page):
        clip = target.evaluate(
            """(node, name) => {
          const r = node.getBoundingClientRect();
          const table = name === 'toolbar' ? node.closest('section')?.querySelector('#world-scroll') : null;
          const x = Math.max(0, r.left + scrollX);
          return {x, y:Math.max(0, r.top + scrollY),
            width:Math.min(r.right + scrollX - x, document.documentElement.clientWidth - x),
            height:Math.min(table ? table.getBoundingClientRect().top - r.top : r.height, 1200)};
        }""",
            name,
        )
        if clip["width"] > 0 and clip["height"] > 0:
            skipped = page.evaluate(SKIPPED_IN_CLIP, clip)
            if skipped:  # a full-page clip reaches beyond the viewport, where skipped content would come out blank
                raise RuntimeError(f"{prefix}-{name}: {skipped} elements with text are skipped inside the crop (principle: Off-screen sections); its capture would show them blank")
            page.screenshot(
                path=str(screenshots / f"{prefix}-{name}.png"),
                full_page=True,
                clip=clip,
            )
            if landmarks:
                save_landmarks(page, screenshots / f"{prefix}-{name}.json", clip, target)


# Plain names for page parts a reader recognises; other ids are named within their section.
LANDMARKS_JS = """(root, clip) => {
  const NAMED = {'page-top': 'top bar', top: 'page header', updated: 'updated line', headline: 'headline',
    figures: 'key figures', 'development-list': 'News list', worldmap: 'map', regions: 'region list',
    record: 'record beside the map', 'map-search': 'map search box', legend: 'map legend',
    mapnote: 'map note',
    'world-scroll': 'comparison table', 'world-count': 'table count line', 'world-status': 'table status filter',
    'fund-rows': 'funding chart',
    'fund-chart-h': 'funding chart heading', foot: 'page footer', content: 'main content',
    entries: 'European state cards', 'world-cards': 'worldwide state cards', 'world-rows': 'comparison table rows',
    'world-filter': 'table search box', 'world-region': 'table region filter', 'world-download': 'table download menu'};
  const text = n => (n ? n.textContent : '').replace(/\\s+/g, ' ').trim();
  const sectionOf = n => {
    const s = n.closest('section'); const h = s && s.querySelector('h2');
    return h ? {name: text(h), id: s.id || h.id} : null;
  };
  const cardName = c => text(c.querySelector('.name') || c.querySelector('summary')).replace(/\\s*\\(.*$/, '');
  const groupName = g => text(g.querySelector('h3')).replace(/\\s*\\(\\d+\\)$/, '')
    + (g.closest('#world-cards') ? ' worldwide cards' : ' European cards');
  const describe = n => {
    if (n.matches('.card .row')) {
      const c = n.closest('.card');
      const k = text(n.querySelector('.k'));
      return [`${cardName(c)} card, ${k} field`, c.id, 'state cards', cardName(c), `${k} field`];
    }
    if (n.matches('.card')) return [`${cardName(n)} card`, n.id, 'state cards', cardName(n)];
    if (n.matches('.statusgroup')) return [groupName(n), (n.querySelector('.card') || {}).id || 'europe'];
    if (n.matches('#world-rows tr')) {
      const name = text(n.querySelector('td'));
      return [`comparison table, ${name} row`, 'world-scroll', 'comparison table rows', name];
    }
    if (n.matches('#world-scroll thead')) return ['comparison table header', 'world-scroll'];
    if (n.matches('.fund-item')) {
      const name = text(n.querySelector('h4'));
      return [`funding chart, ${name} row`, 'fund-rows', 'funding chart rows', name];
    }
    if (n.matches('.development-row')) {
      const name = text(n.querySelector('.development-name'));
      return [`News, ${name} entry`, 'developments', 'News entries', name];
    }
    if (n.matches('.tbar')) return ['table toolbar', 'world'];
    const s = sectionOf(n);
    if (n.matches('section')) return s ? [`${s.name} section`, s.id] : [null, null];
    if (!n.id) return [null, null];
    if (NAMED[n.id]) return [NAMED[n.id], n.id];
    const words = text(n).slice(0, 40);
    const own = n.matches('h1, h2, h3') ? `${words} heading` : n.matches('a, button, summary') && words
      ? `"${words}" ${n.matches('a') ? 'link' : 'button'}` : n.id.replace(/-/g, ' ');
    return [s ? `${s.name} section, ${own}` : own, n.id];
  };
  const box = n => {
    const r = n.getBoundingClientRect();
    return [r.left + scrollX - clip.x, r.top + scrollY - clip.y, r.right + scrollX - clip.x, r.bottom + scrollY - clip.y].map(Math.round);
  };
  const shown = b => b[2] > Math.max(0, b[0]) && b[3] > Math.max(0, b[1]) && b[0] < clip.width && b[1] < clip.height;
  const landmarks = [];
  const parts = '[id], section, .card, .card .row, .statusgroup, #world-rows tr, #world-scroll thead, .fund-item, .development-row, .tbar';
  document.querySelectorAll(parts).forEach(n => {
    const b = box(n);
    if (!shown(b) || !n.checkVisibility({visibilityProperty: true})) return;
    const [label, anchor, family, name, part] = describe(n);
    if (label && anchor) landmarks.push(family ? {box: b, label, anchor, family, name, part: part || null} : {box: b, label, anchor});
  });
  const [label, anchor] = root ? describe(root) : ['first screen of the page', 'top'];
  // Where to link if this capture disappears from the page under review: its section heading.
  const section = root && sectionOf(root);
  return {crop: {label, anchor, section: section ? section.id : 'top'}, landmarks};
}"""


def save_landmarks(page, path: Path, clip: dict, target=None) -> None:
    """Named page parts inside one capture, boxed in capture pixels, so that a changed region can
    be described in plain words and linked to the nearest element id. A repeated part (a card, a
    card field, a table or chart row) also names its family, its card or row and the field."""
    if target is None:
        found = page.evaluate(f"(clip) => ({LANDMARKS_JS})(null, clip)", clip)
    else:
        found = target.evaluate(f"(node, clip) => ({LANDMARKS_JS})(node, clip)", clip)
    path.write_text(json.dumps(found, ensure_ascii=False), encoding="utf-8")


# Text rasterisation otherwise varies between Chrome launches: on one build, 3 of 54 views
# differed (largest channel difference 136) across 10 launches; with these flags, none did.
STABLE_RASTER = [
    "--disable-gpu",
    "--disable-lcd-text",
    "--disable-font-subpixel-positioning",
    "--font-render-hinting=none",
    "--force-color-profile=srgb",
    # Overlay scrollbars of inner scroll areas fade out after load and were caught mid-fade.
    "--hide-scrollbars",
]


def capture_states(chrome: str, dist: Path, out: Path) -> None:
    """Fixed, freshly loaded states for contact sheets: three widths, both themes, no checks."""
    from playwright.sync_api import sync_playwright

    out.mkdir(parents=True, exist_ok=True)
    handler = partial(QuietHandler, directory=str(dist))
    with ThreadingHTTPServer(("127.0.0.1", 0), handler) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with sync_playwright() as playwright:
                browser = LocalisedBrowser(playwright.chromium.launch(executable_path=chrome, headless=True, args=STABLE_RASTER, env=browser_env()))
                for width in (1440, 1000, 390):
                    for theme in ("light", "dark"):
                        page = themed_page(
                            browser,
                            theme,
                            viewport={"width": width, "height": 1000},
                            device_scale_factor=1,
                        )
                        hydrated(page, f"http://127.0.0.1:{server.server_port}/")
                        page.wait_for_selector("#world-rows tr")
                        page.evaluate("document.fonts.ready")
                        # The pointer never enters the page, so nothing is captured in a hover state.
                        page.wait_for_timeout(300)
                        page.screenshot(path=str(out / f"{width}-{theme}-hero.png"))
                        save_landmarks(page, out / f"{width}-{theme}-hero.json", {"x": 0, "y": 0, "width": width, "height": 1000})
                        capture_sections(page, out, f"{width}-{theme}", landmarks=True)
                        page.close()
                browser.close()
        finally:
            server.shutdown()
            thread.join(timeout=5)


def check_controls(page) -> list[str]:
    """Controls retain usable targets, names and room in the responsive toolbar."""
    return page.evaluate(
        "() => {"
        + BROWSER_GEOMETRY
        + """
      const errors = [];
      document.querySelectorAll('input[type="checkbox"]').forEach(node => {
        if (!visible(node)) return;
        const r = node.getBoundingClientRect(), p = pseudoBox(node);
        if (Math.abs(r.width-20) > 1 || Math.abs(r.height-20) > 1 || !p || p.height < 39 || p.width < 39) errors.push('checkbox lacks 20px visual / 40px effective target');
      });
      document.querySelectorAll('#col-opts label, .more > summary').forEach(node => {
        if (visible(node) && node.getBoundingClientRect().height < 39) errors.push(`control/disclosure label is below 40px: ${node.textContent.trim().slice(0,60)}`);
      });
      const tr = document.querySelector('.tbar').getBoundingClientRect();
      const region = document.querySelector('#world-region');
      if (!(region.getAttribute('aria-label') || '').trim()) errors.push('region filter lacks an accessible name');
      if (innerWidth <= 640) {
        const s = getComputedStyle(region), r = region.getBoundingClientRect();
        const canvas = document.createElement('canvas'), ctx = canvas.getContext('2d'); ctx.font = [s.fontWeight, s.fontSize, s.fontFamily].join(' ');
        if (s.color === 'rgba(0, 0, 0, 0)' || r.width-parseFloat(s.paddingLeft)-parseFloat(s.paddingRight)-2 < ctx.measureText(region.selectedOptions[0]?.textContent || '').width) errors.push('phone region filter has no room for a visible label');
        const statusBox = document.querySelector('#world-status').getBoundingClientRect();
        if (statusBox.left < tr.left-1 || statusBox.right > tr.right+1 || statusBox.height < 40) errors.push('phone status select does not fit the toolbar or is too short to tap');
      }
      // Downloads (principle): the table's Download is its foot row's icon alone, after the table, with its name on hover and for screen readers.
      const menu = document.querySelector('#world-download'), summary = menu?.querySelector(':scope > summary.ico.i-dl');
      if (!summary || menu.closest('.tbar') || !menu.closest('.section-foot') || !(menu.closest('.section-foot').compareDocumentPosition(document.querySelector('#world-scroll')) & Node.DOCUMENT_POSITION_PRECEDING)
          || !(summary.getAttribute('aria-label') || '').trim() || !(summary.dataset.tip || '').trim() || document.querySelector('#world-download .control-label').getBoundingClientRect().width > 1) errors.push('Download is not the icon alone in the foot row after the table, named on hover and for screen readers');
      if (innerWidth <= 640) {
        if ([...document.querySelector('.toolbar-actions').children].some(n => { const r = n.getBoundingClientRect(); return r.left < tr.left-1 || r.right > tr.right+1; })) errors.push('phone toolbar actions overflow');
      }
      const inline = document.querySelector('#world-count button');
      if (inline) {
        const hit = pseudoBox(inline);
        if (!hit || hit.height < 28) errors.push('Show all lacks its invisible hit area');
      }
      const body = getComputedStyle(document.body);
      const mutedProbe = document.createElement('span'); mutedProbe.style.color = 'var(--muted)'; document.body.appendChild(mutedProbe);
      const muted = getComputedStyle(mutedProbe).color; mutedProbe.remove();
      document.querySelectorAll('.missing').forEach(node => {
        if (!visible(node)) return;
        const s = getComputedStyle(node), structural = node.matches('.na');
        // "Not published" figures use body ink; a structural dash is muted. Funding's compact name lists
        // are separate muted controls.
        const quiet = structural;
        if (s.color !== (quiet ? muted : body.color)) errors.push(structural ? 'structural absence is not muted' : 'missing value does not use the body ink');
      });
      if (document.querySelector('#world-all')) errors.push('duplicate full-dataset toolbar action remains');
      const tabs = document.querySelector('#region-chips'), first = tabs && tabs.querySelector('button'), map = [...document.querySelectorAll('#worldmap svg')].find(visible), list = document.querySelector('#regions'), find = document.querySelector('#map-search');
      if (tabs && visible(tabs) && first && map && list && find) {
        const mr = map.closest('#worldmap').getBoundingClientRect(), lr = list.getBoundingClientRect(), fr = find.getBoundingClientRect();
        const beside = lr.left > mr.right; // the list beside the map, or stacked under it
        const hr = tabs.getBoundingClientRect();
        if (!beside && hr.right > fr.left + 1 && hr.bottom > fr.top + 1) errors.push('region tabs and the search overlap');
      }
      return errors;
    }"""
    )


def check_in_content_controls(page) -> list[str]:
    """The table header label shows an underline on hover and focus."""
    errors = []
    # The first column's header is always in view horizontally, so the table's sideways scroll is untouched.
    scroll_y = page.evaluate("scrollY")
    button = page.locator('table.world th[data-k="name"] button')
    button.scroll_into_view_if_needed()
    feedback = """n => { const label = n.querySelector('.sort-label');
      return !!label && getComputedStyle(label).textDecorationLine.includes('underline'); }"""
    button.hover()
    hovered = button.evaluate(feedback)
    page.mouse.move(0, 0)
    button.focus()
    page.keyboard.press("Shift")  # keyboard modality, so :focus-visible applies
    focused = button.evaluate(feedback)
    button.evaluate("n => n.blur()")
    for state, result in (("hover", hovered), ("focus", focused)):
        if not result:
            errors.append(f"table header control lacks {state} underline on its label")
    # Restore the scroll position, and with it the section navigation, for later checks.
    page.evaluate(
        "y => new Promise(done => { scrollTo(0, y); requestAnimationFrame(() => requestAnimationFrame(done)); })",
        scroll_y,
    )
    return errors


def check_structural_columns(page, all_inapplicable: bool) -> list[str]:
    """A column structurally inapplicable to every visible row is hidden; otherwise it shows."""
    return page.evaluate(
        """all => {
      const errors = [], table = document.querySelector('table.world');
      const rows = [...document.querySelectorAll('#world-rows > tr')];
      for (const col of ['inst', 'since', 'op', 'budget', 'staff']) {
        const head = table.querySelector(`thead [data-col="${col}"]`);
        const cells = rows.map(r => r.querySelector(`[data-col="${col}"]`));
        const shown = getComputedStyle(head).display !== 'none';
        // A reader's own column choice (hide-*) still hides a column that applies.
        const chosen = !table.classList.contains('hide-' + col);
        if (all ? shown : shown !== chosen || table.classList.contains('empty-' + col)) errors.push(`${col} column ${all ? 'shows although no visible row has an institute' : 'is hidden although some rows apply'}`);
        if (cells.some(c => !c || (getComputedStyle(c).display !== 'none') !== shown)) errors.push(`${col} cells and header disagree on visibility`);
        if (all && cells.some(c => !c.querySelector('.missing.na'))) errors.push(`${col} hidden although a visible row has a value`);
      }
      return errors;
    }""",
        all_inapplicable,
    )


STATUS_LABELS = {
    "established": "Established",
    "planned": "Planned",
    "proposed": "Proposed",
    "exploring": "Exploring",
    "none_identified": "None identified",
}


def check_status_filter(page, data: dict) -> list[str]:
    """The table's Status select and its count line, against the data (counts count states only)."""
    states = [j for j in data["jurisdictions"] if j["kind"] != "supranational"]
    by_status = Counter(j["status"] for j in states)
    total, none = len(states), by_status["none_identified"]
    errors: list[str] = []
    page.locator("#world-filter").fill("")
    page.locator("#world-region").select_option("")
    page.locator("#world-status").select_option("pos")
    if page.locator("select#world-status").count() != 1 or page.locator("#world-status button").count():
        errors.append("table status filter is not one select")
    expected = [f"Has or considers one ({total - none})", f"All ({total})"] + [
        f"{label} ({by_status[status]})" for status, label in STATUS_LABELS.items() if by_status[status]
    ]
    options = [" ".join(t.split()) for t in page.locator("#world-status option").all_inner_texts()]
    if options != expected:
        errors.append(f"status select options {options} differ from data {expected}")

    def line(filtered: bool = True) -> str:
        """The count line as rendered (hidden text does not count); a filtered view also shows one visible, named
        reset button, an unfiltered one none."""
        text = page.locator("#world-count").inner_text()
        buttons = page.locator("#world-count button").filter(visible=True)
        if buttons.count() != int(filtered):
            errors.append(f"count line shows {buttons.count()} reset buttons {'with a filter' if filtered else 'without one'}, expected {int(filtered)}")
        elif filtered:
            label = buttons.inner_text().strip()
            if not label:
                errors.append("count line action has no accessible name")
            text = text.replace(label, "")
        return " ".join(text.split())

    # The table lists Taiwan as a territory beside the 193 states, so its total names both (the headline counts states only).
    noun = "states and territories" if any(j["kind"] == "territory" for j in states) else "states"

    want = f"{total - none} of {total} {noun}"
    if line() != want:
        errors.append(f"count line {line()!r} differs from {want!r}")
    page.locator("#world-status").select_option("")
    if (unfiltered := line(filtered=False)) != f"{total} of {total} {noun}":
        errors.append(f"count line with all states is {unfiltered!r}")
    page.locator("#world-status").select_option("pos")
    # A region narrows the shown count; the line has the same words for every filter.
    region = next(
        j["region"]["name"]
        for j in states
        if j["status"] == "none_identified" and j.get("region")
    )
    in_region = [j for j in states if (j.get("region") or {}).get("name") == region]
    region_none = sum(j["status"] == "none_identified" for j in in_region)
    page.locator("#world-region").select_option(f"c:{region}")
    want = f"{len(in_region) - region_none} of {total} {noun}"
    if line() != want:
        errors.append(f"count line in {region} is {line()!r}, expected {want!r}")
    page.locator("#world-status").select_option("planned")
    planned = sum(j["status"] == "planned" and j in in_region for j in states)
    if line() != f"{planned} of {total} {noun}":
        errors.append(f"count line for one status is {line()!r}")
    page.locator("#world-count button").click()
    if page.locator("#world-status").input_value() != "" or page.locator("#world-region").input_value() != "":
        errors.append("Show all does not reset the Status and Region selects")
    page.locator("#world-status").select_option("pos")
    if not (page.locator("#world-status").get_attribute("aria-label") or "").strip():
        errors.append("status select lacks its accessible name")
    return errors


def make_format_fixture(data: dict) -> tuple[dict, list[dict]]:
    """Use production rendering for zero, unknown, qualifiers and date precision."""
    fixture = copy.deepcopy(data)
    # Pin the synthetic rate so these hand-worked labels survive real FX updates.
    rate = next(
        r for r in fixture["dataset"]["exchange_rates"] if r["currency"] == "GBP"
    )
    rate.update(units_per_eur=5 / 6, eur_per_unit=1.2)
    jurisdictions = {j["id"]: j for j in fixture["jurisdictions"]}
    institutes = [
        i
        for i in fixture["institutes"]
        if jurisdictions[i["jurisdiction_id"]]["status"] == "established"
        and jurisdictions[i["jurisdiction_id"]]["kind"] != "supranational"
    ][:6]
    if len(institutes) != 6:
        raise ValueError("format fixture requires six established state institutes")
    cases: list[dict] = [
        {
            "date": "2025",
            "since": "2025",
            "budget": "~EUR 0 (GBP 0) a financial year · adopted",
            "staff": "0 (0 technical) as of 2025",
            "figure": "~EUR 0 (GBP 0)",
            "published": "~EUR 0 (GBP 0) a financial year",
        },
        {
            "date": "2025-03",
            "since": "Mar 2025",
            "budget": "up to ~EUR 11m (GBP 9.0m) a year · 2025–2027 · adopted",
            "staff": "~12 (4+ technical) as of Mar 2025",
            "figure": "up to ~EUR 11m (GBP 9.0m)",
            "published": "up to ~EUR 11m (GBP 9.0m) a year · 2025–2027 · adopted",
            "technical": "~7 technical · as of Apr 2026",
        },
        {
            "date": "2025-03-07",
            "since": "7 Mar 2025",
            "budget": "Not published",
            "staff": "Not published",
            "missing_op": True,
        },
        {
            "date": "2025-04-09",
            "since": "9 Apr 2025",
            "budget": "Not published",
            "staff": "10+ (up to 4 technical) as of 9 Apr 2025",
            "published": "up to GBP 5m 2027 · proposed",
        },
        {
            "date": "2026-04",
            "since": "Apr 2026",
            "budget": "~EUR 2.8m a year · adopted",
            "staff": "~7 technical as of Apr 2026",
            "figure": "~EUR 2.8m",
            "published": "EUR 9m a year",
            "technical": "~7 technical · as of Apr 2026",
            "annual_once": True,
            "exact_amount": True,
        },
        {
            "date": "2026",
            "since": "2026",
            "budget": "Not published",
            "staff": "Not published",
        },
    ]
    for index, (institute, case) in enumerate(zip(institutes, cases)):
        jid = institute["jurisdiction_id"]
        case["jid"] = jid
        j = jurisdictions[jid]
        j["status_since"] = case["date"]
        j["status_confidence"] = "high"
        ids = institute["source_ids"][:2]
        j["timeline"] = [
            {"date": case["date"], "event": "designated", "source_ids": ids, "citations": {"date": ids, "event": ids}}
        ]
        record = {"source_ids": ids, "confidence": "high", "as_of": case["date"], "citations": {key: ids for key in ("description", "amount", "currency", "period", "commitment", "eur_per_year_est", "total", "total_qualifier", "technical", "technical_qualifier", "as_of", "confidence")}}
        j["citations"]["since"] = ids
        institute["funding"] = [
            {
                **record,
                "kind": "annual",
                "amount": 0 if index == 0 else 9000000,
                "annual_amount": 0 if index == 0 else 9000000,
                "currency": "GBP",
                "period": "per financial year" if index == 0 else "2025–2027",
                "eur_per_year_est": 0 if index == 0 else 10_800_000,  # GBP 9m * 1.2
                "description": "" if index == 0 else "up to £9m",
                "commitment": "adopted",
            }
        ]
        institute["staff"] = [
            {
                **record,
                "total": 0 if index == 0 else 12,
                "technical": 0 if index == 0 else 4,
                "description": "",
                "total_qualifier": "exact" if index == 0 else "about",
                "technical_qualifier": "exact" if index == 0 else "at_least",
            }
        ]
        if index == 1:
            institute["funding"][0]["amount_qualifier"] = "at_most"
            institute["staff"].append(
                {
                    **record,
                    "total": None,
                    "technical": 7,
                    "technical_qualifier": "about",
                    "description": "",
                    "as_of": "2026-04",
                }
            )
        elif index == 2:
            institute["funding"] = []
            institute["staff"] = []
            institute.pop("operating", None)
        elif index == 3:
            institute["funding"] = [
                {
                    **record,
                    "kind": "programme",
                    "amount": None,
                    "currency": "GBP",
                    "period": "2027",
                    "eur_per_year_est": None,
                    "description": "up to £5m",
                    "commitment": "proposed",
                }
            ]
            institute["staff"] = [
                {
                    **record,
                    "total": 10,
                    "total_qualifier": "at_least",
                    "technical": 4,
                    "technical_qualifier": "at_most",
                    "description": "",
                }
            ]
        elif index == 4:
            institute["funding"] = [
                {
                    **record,
                    "kind": "annual",
                    "amount": 9000000,
                    "currency": "EUR",
                    "period": "per year",
                    "annual_amount": 2833333.333333,
                    "eur_per_year_est": 2833330,
                    "description": "€9m a year",
                    "commitment": "adopted",
                },
                {
                    **record,
                    "kind": "one_off",
                    "amount": 1234567.89,
                    "currency": "EUR",
                    "period": "2026",
                    "eur_per_year_est": None,
                    "description": "",
                    "commitment": "adopted",
                },
            ]
            institute["staff"] = [
                {
                    **record,
                    "total": None,
                    "technical": 7,
                    "technical_qualifier": "about",
                    "description": "",
                }
            ]
        elif index == 5:
            institute["funding"] = [
                {
                    **record,
                    "kind": "none_disclosed",
                    "amount": None,
                    "currency": None,
                    "period": None,
                    "eur_per_year_est": None,
                    "description": "",
                    "commitment": "not_applicable",
                }
            ]
            institute["staff"] = []
    # A tracked date must not produce a citation when the table deliberately
    # displays no Since value and the jurisdiction has no numbered source list.
    cardless = next(
        j
        for j in fixture["jurisdictions"]
        if j["status"] == "none_identified" and not j["europe_scope"]
    )
    cardless["status_since"] = "2025"
    cardless["source_ids"] = institutes[0]["source_ids"][:1]
    cardless["timeline"] = [
        {"date": "2025", "event": "designated", "source_ids": cardless["source_ids"], "citations": {"date": cardless["source_ids"], "event": cardless["source_ids"]}}
    ]
    cases.append({"jid": cardless["id"], "cardless": True})
    for institute in institutes:
        for funding in institute.get("funding", []):
            if funding.get("eur_per_year_est") is not None:
                funding["coverage"] = "whole_institute"
                funding["annual_period"] = {"start": "2026-01-01", "end": "2026-12-31"}
    refresh_fixture_selections(fixture)
    return fixture, cases


def check_format_output(html: str, data: dict, cases: list[dict]) -> list[str]:
    """Assert user-visible outcomes; do not call the page's formatting helpers."""
    errors = check_source_alignment(html, data)
    soup = BeautifulSoup(html, "html.parser")
    for cite in soup.select(".cites"):
        cite.decompose()

    for case in cases:
        jid = case["jid"]
        card = soup.select_one(f'.card[data-j="{jid}"]')
        table = soup.select_one(f'#world-rows tr[data-j="{jid}"]')
        if case.get("cardless"):
            if card is not None or table is None:
                errors.append(
                    f"format fixture {jid}: cardless jurisdiction missing from All view"
                )
            elif not is_structural_absence(table.select_one('[data-col="since"]')):
                errors.append(
                    f"format fixture {jid}: undisplayed Since date leaked into All view"
                )
            continue
        funding = soup.select_one(f'.fund-detail[data-j="{jid}"]')
        if not card or not table or not funding:
            errors.append(f"format fixture {jid}: a view is missing")
            continue
        values = {
            row.get("data-track"): prose(row.select_one(".v"))
            for row in card.select(".row")
        }
        for label, key in (
            ("Status", "since"),
            ("Budget", "budget"),
            ("Staff", "staff"),
        ):
            actual = values.get(CARD_TRACKS[label], "")
            table_value = prose(table.select_one(f'[data-col="{key}"]'))
            if key == "budget" and case.get("commitment"):
                matches = (
                    case[key] in actual
                    and case["commitment"] in actual.lower()
                    and case["budget_period"] in actual
                )
            elif key == "since":
                # The card names the status before its date ("Established since 9 Apr 2025"); the table column holds the date alone.
                status = next(j["status"] for j in data["jurisdictions"] if j["id"] == jid)
                row = card.select_one(f'.row[data-track="{CARD_TRACKS[label]}"]')
                actual = prose(row.select_one(".v .value-line")) if row else ""
                matches = actual == f"{STATUS_LABELS[status]} since {case[key]}"
                actual = case[key] if matches else actual
            else:
                matches = actual == case[key]
            if not matches or table_value != actual:
                errors.append(
                    f"format fixture {jid}: {label} differs from {case[key]!r} in card/table"
                )
        if case.get("missing_op") and (
            values.get(CARD_TRACKS["Known activity"]) != "Not published"
            or prose(table.select_one('[data-col="op"]')) != "Not published"
        ):
            errors.append(
                f"format fixture {jid}: absent known activity is not unknown"
            )
        if (
            "figure" in case
            and prose(funding.select_one(".fund-budget-summary strong"))
            != case["figure"]
        ):
            errors.append(
                f"format fixture {jid}: funding figure loses zero or precision"
            )
        if "published" in case and not any(
            case["published"] in prose(p)
            for p in funding.select('[data-record="funding"]')
        ):
            errors.append(
                f"format fixture {jid}: published funding loses its period or upper bound"
            )
        if "technical" in case and not any(
            prose(p) == case["technical"]
            for p in funding.select(".fund-staff-summary .fund-context")
        ):
            errors.append(f"format fixture {jid}: latest technical-only record is lost")
        records = [prose(p.select_one(".funding-native") or p) for p in funding.select('[data-record="funding"]')]
        if case.get("annual_once") and (
            not records
            or len(re.findall(r"\b(?:a year|per year|annual)\b", records[0])) != 1
        ):
            errors.append(f"format fixture {jid}: annual unit is repeated")
        if case.get("exact_amount") and not any(
            "~EUR 1.2m" in text for text in records
        ):
            errors.append(f"format fixture {jid}: source amount is not shown at two significant figures")
    for jurisdiction in data["jurisdictions"]:
        if jurisdiction.get("institute_id"):
            continue
        jid = jurisdiction["id"]
        table = soup.select_one(f'#world-rows tr[data-j="{jid}"]')
        if table is None:
            continue
        # No institute record: nothing exists yet to publish, so the absence is structural.
        for column in ("inst", "budget", "staff", "op"):
            if not is_structural_absence(table.select_one(f'[data-col="{column}"]')):
                errors.append(
                    f"format fixture {jid}: {column} absence is not a structural dash"
                )
    return errors


def check_format_edges(browser, base_url: str, data: dict) -> list[str]:
    fixture, cases = make_format_fixture(data)
    page = browser.new_page(viewport={"width": 390, "height": 1000})
    page_errors = []
    page.on("pageerror", lambda e: page_errors.append(str(e)))
    refresh_fixture_selections(fixture)
    page.route(PAGE_DATA, lambda route: route.fulfill(json=fixture))
    try:
        hydrated(page, base_url)
        page.wait_for_selector("#world-rows > tr")
        page.locator("#world-status").select_option("")
        page.locator(".fund-detail").evaluate_all("folds=>folds.forEach(n=>n.open=true)")
        return check_format_output(page.content(), fixture, cases) + page_errors
    finally:
        page.close()


def check_light_fallback(page) -> list[str]:
    """An older browser without light-dark(): disable the CSS supports branch (not JS CSS.supports). With dark stored,
    colour tokens, native controls and the toggle must all stay light, so no dark control sits on a light page."""
    sample = """removeModern => {
      const sheet = [...document.styleSheets].find(s => s.ownerNode.tagName === 'STYLE');
      const index = [...sheet.cssRules].findIndex(r => r.conditionText && r.conditionText.includes('light-dark('));
      if (index < 0) return null;
      const tokens = [...sheet.cssRules[index].cssRules[0].style].filter(p => p.startsWith('--'));
      if (removeModern) sheet.deleteRule(index);
      const probe = document.createElement('span'); probe.hidden = true; document.body.appendChild(probe);
      const colours = tokens.map(token => { probe.style.color = 'var(' + token + ')'; return [token, getComputedStyle(probe).color]; });
      probe.remove(); return colours;
    }"""
    set_theme(page, "light")
    modern = page.evaluate(sample, False)
    set_theme(page, "dark")
    fallback = page.evaluate(sample, True)
    errors = []
    if not modern or modern != fallback:
        errors.append("unsupported light-dark(): colour tokens lose the light palette")
    if (
        page.evaluate("getComputedStyle(document.documentElement).colorScheme")
        != "light"
    ):
        errors.append(
            "unsupported light-dark(): native controls do not use the light scheme"
        )
    if page.locator("#theme-toggle").is_visible():
        errors.append("unsupported light-dark(): the theme toggle is shown but cannot switch the palette")
    return errors


# Expected selections for this dataset, independent of the page's selection algorithm.
EXPECTED_FUNDING_IDS = {
    "GB": "funding-ddcdd902bdc5873816ddd014c1742ad6",
    "US": "funding-443aa6a31d205192966af69e21022fb1",
    "CA": "funding-360a59a37fde5cc8ba0a03ce3a00ea5f",
    "KR": "funding-d0f61d15548153b9830f74f6e14ec324",
    "AU": "funding-a7c0488859f35293a4b593ba44e40d12",
}
EXPECTED_BUDGET_IDS = {
    "gb-aisi": EXPECTED_FUNDING_IDS["GB"], "fr-inesia": None,
    "de-aisi": None, "es-isia": None, "no-ki-norge": "funding-07dc8b2c007b4b80a356aef8e5afa53c",
    "eu-ai-office": None, "us-caissi": EXPECTED_FUNDING_IDS["US"],
    "ca-caisi": EXPECTED_FUNDING_IDS["CA"],
    "jp-aisi": "funding-7d0b36a48cc65315865a6d6ec83ddeb6",
    "kr-aisi": EXPECTED_FUNDING_IDS["KR"], "sg-aisi": None,
    "au-aisi": EXPECTED_FUNDING_IDS["AU"], "my-aisafe": None,
    "in-aisi": None, "ke-ai-safety-institute": "funding-b60809279d92488ebf5dcea4813d9da9",
}


def check_budget_output(html, data, expected=None):
    """Hand-worked institute identities, independent of the producer selection."""
    expected = EXPECTED_BUDGET_IDS if expected is None else expected
    soup = BeautifulSoup(html, "html.parser")
    errors = []
    jurisdictions = {row["id"]: row for row in data["jurisdictions"]}
    cards, world_rows = Keyed(soup, ".card[data-j]"), Keyed(soup, "#world-rows tr[data-j]")
    for institute in data["institutes"]:
        identity, jid = institute["id"], institute["jurisdiction_id"]
        if identity not in expected:
            errors.append(f"{identity}: Budget oracle needs reviewed expectation")
            continue
        wanted = expected[identity]  # Cards and the world table retain every institute's Budget.
        card = cards.one(jid)
        budget = card.select_one(f'.row[data-track="{CARD_TRACKS["Budget"]}"] .v') if card else None
        table = world_rows.one(jid, '[data-col="budget"]')
        for view, node in (("card", budget), ("table", table)):
            if view == "table" and jurisdictions[jid].get("kind") == "supranational":
                continue
            selected = node.select_one(".budget-value") if node else None
            actual = selected.get("data-funding-id") if selected else None
            if node is None or actual != wanted:
                errors.append(f"{jid}: {view} Budget selected wrong funding ID")
            if wanted is None and node and not node.select_one(".missing"):
                errors.append(f"{jid}: {view} Budget must be Not published")
            if selected and (not selected.select_one(".budget-amount") or not selected.select_one(".budget-qualifiers")):
                errors.append(f"{jid}: {view} Budget must separate amount and qualifiers")
            if jid == "JP" and selected and selected.select_one(".budget-amount") and selected.select_one(".budget-amount").get_text(strip=True) != "~EUR 52m (JPY 8.8bn)":
                errors.append("JP: Budget must show latest own JPY 8.8bn allocation, as ~EUR 52m (JPY 8.8bn)")
        if card and any(row.get("coverage") == "not_institute_specific" for row in institute.get("funding", [])):
            if not card.select_one(f'.row[data-track="{CARD_TRACKS["Related funding"]}"]'):
                errors.append(f"{jid}: wider funding must use a separate Related funding line")
        if jurisdictions[jid].get("status") != "established":
            continue
        chart = soup.select_one('.fund-list[data-kind="budget"]')
        row = chart.select_one(f'.fund-item[data-institute="{identity}"]') if chart else None
        inline = chart.select_one(f'.fund-name-row[data-institute="{identity}"]') if chart else None
        comparison = EXPECTED_FUNDING_IDS.get(jid)
        if comparison:
            if row is None or row.get("data-funding-id") != comparison or not row.select_one(".fund-budget .fund-value strong") or not row.select_one(".fund-budget .fund-figure abbr.g[aria-description]"):
                errors.append(f"{jid}: funding Budget comparison loses its selected amount or qualifiers")
        else:
            if inline is None or inline.get("data-funding-id") != wanted:
                errors.append(f"{jid}: funding Budget selected wrong funding ID")
            if wanted is None and inline and not inline.find_parent(class_="fund-missing-line"):
                errors.append(f"{jid}: funding Budget must be Not published")
            if inline and inline.select_one(".budget-amount, strong"):
                errors.append(f"{jid}: funding Budget without a bar must describe its amount in words")
    return errors


def funding_groups(data: dict) -> tuple[set, set, set]:
    """Hand-worked Budget groups: compared, other own amounts, and Not published."""
    institutes = {i["id"]: i["jurisdiction_id"] for i in established_funding_institutes(data)}
    own = {institutes[iid] for iid, fid in EXPECTED_BUDGET_IDS.items() if fid is not None and iid in institutes}
    everyone = set(institutes.values())
    bars = set(EXPECTED_FUNDING_IDS) & everyone
    nobar = own - bars
    return bars, nobar, everyone - bars - nobar


def expected_funding_rows(data: dict) -> dict[str, list[str]]:
    """Independent descending rank for each scale, then jurisdiction name and permanent ID."""
    jurisdictions = {j["id"]: j for j in data["jurisdictions"]}
    rows = {"budget": [], "staff": []}
    for inst in established_funding_institutes(data):
        jid = inst["jurisdiction_id"]
        budget = next((r for r in inst.get("funding", []) if r["id"] == EXPECTED_FUNDING_IDS.get(jid)), None)
        staff, _ = latest_staff_total(inst)
        values = {"budget": budget.get("eur_per_year_est") if budget else None,
                  "staff": staff.get("total") if staff and staff.get("as_of") else None}
        for kind, amount in values.items():
            if isinstance(amount, (int, float)) and math.isfinite(amount):
                rows[kind].append(((-amount, jurisdictions[jid]["name"], inst["id"]), inst["id"]))
    return {kind: [iid for _, iid in sorted(items)] for kind, items in rows.items()}


def check_funding_outside(html, data: dict, *, saved: bool = False) -> list[str]:
    """Both charts cover each established institute once, with each figure's complete accessible explanation."""
    soup = BeautifulSoup(html, "html.parser")
    errors = []
    year = data["dataset"]["as_of"][:4]
    heading = soup.select_one("#fund-chart-h")
    if not heading or year not in heading.get_text():
        errors.append(f"funding chart heading must name the reference year {year}")
    scope = " ".join(n.get_text(" ", strip=True) for n in soup.select("#fund-chart-h, #fund-how")).lower()
    if scope.count("established institutes") != 1 or "planned" not in scope or "proposed" not in scope:
        errors.append("funding scope must say once that established institutes are compared and planned and proposed ones remain in their records")
    if soup.select_one("#fund-rows .fund-block, #fund-rows [data-band]"):
        errors.append("funding charts must rank figures without status bands")
    if soup.select_one("#fund-rows .sw, #fund-rows .fund-status"):
        errors.append("funding comparison must not repeat status marks")
    by_id = {i["id"]: i for i in established_funding_institutes(data)}
    expected_rows = expected_funding_rows(data)
    details = soup.select("#fund-rows details.fund-detail[data-institute]")
    if Counter(d.get("data-institute") for d in details) != Counter(by_id.keys()):
        errors.append("funding must retain one canonical records fold per established institute")
    for detail in details:
        summary = detail.select_one(":scope > summary")
        if summary is None or "Funding and staff records" not in summary.get_text():
            errors.append(f"{detail.get('data-institute')}: missing native Funding and staff records fallback")
    for kind in ("budget", "staff"):
        charts = soup.select(f'#fund-rows > .fund-list[data-kind="{kind}"]')
        if len(charts) != 1:
            errors.append(f"funding {kind}: expected one chart")
            continue
        chart = charts[0]
        head = chart.select_one(f".fund-{kind}-head h3")
        if head is None or head.get_text(strip=True) != kind.title():
            errors.append(f"funding {kind}: missing chart heading")
        rows = chart.select(".fund-chart-rows > .fund-item[data-institute]")
        actual = [str(r.get("data-institute")) for r in rows]
        if actual != expected_rows[kind]:
            errors.append(f"funding {kind} ranked rows {actual}, expected {expected_rows[kind]}")
        openers = chart.select("button.fund-row, button.fund-name-row")
        coverage = Counter(str(o.get("data-institute")) for o in openers)
        if coverage != Counter(by_id.keys()):
            errors.append(f"funding {kind} must list each institute once: {dict(coverage)}, expected {sorted(by_id)}")
        if kind == "budget":
            other = {iid for iid in by_id if EXPECTED_BUDGET_IDS.get(iid) and iid not in expected_rows[kind]}
        else:
            other = {iid for iid, inst in by_id.items() if inst.get("staff") and iid not in expected_rows[kind]
                     and any(any(r.get(field) is not None for field in ("total", "technical", "target")) or r.get("description") for r in inst["staff"])}
        missing = set(by_id) - set(expected_rows[kind]) - other
        for selector, want, label in ((".fund-other-line .fund-name-row", other, "other published amounts"),
                                      (".fund-missing-line .fund-name-row", missing, "Not published")):
            got = [o.get("data-institute") for o in chart.select(selector)]
            if Counter(got) != Counter(want):
                errors.append(f"funding {kind} {label} list {sorted(got)}, expected {sorted(want)}")
        missing_line = chart.select_one(".fund-missing-line")
        if missing and (missing_line is None or not missing_line.get_text(" ", strip=True).startswith("Not published:")):
            errors.append(f"funding {kind}: missing Not published label")
        if len(chart.select(".fund-missing-line")) != bool(missing):
            errors.append(f"funding {kind}: Not published must be one plain names line when needed")
        other_lines = chart.select(".fund-other-line")
        if len(other_lines) != bool(other) or any(not line.get_text(" ", strip=True).startswith("Not compared:") for line in other_lines):
            errors.append(f"funding {kind}: Not compared must be one plain names line when needed")
        for line in chart.select(".fund-missing-line, .fund-other-line"):
            toggles, lists = line.select(":scope > .fund-names-toggle"), line.select(":scope > ul.fund-name-list")
            if len(toggles) != 1 or len(lists) != 1:
                errors.append(f"funding {kind}: each names line needs one disclosure and one plain list")
                continue
            toggle, listing = toggles[0], lists[0]
            if not listing.has_attr("hidden") or listing.get("role") != "list" or toggle.find_next_sibling() is not listing or not listing.get("id") or len(soup.select(f'[id="{listing.get("id")}"]')) != 1:
                errors.append(f"funding {kind}: names disclosure must control its own list, closed by default")
            if saved:
                visible = [n for n in [line, *line.find_all(True)] if n is not listing and listing not in n.parents]
                if toggle.name != "span" or any(n.name in {"a", "button", "input", "select", "textarea", "summary"}
                        or any(n.has_attr(a) for a in ("role", "tabindex", "contenteditable", "aria-expanded", "aria-controls")) for n in visible):
                    errors.append(f"funding {kind}: saved names line must be plain text without control semantics or focus")
            elif toggle.name != "button" or toggle.get("aria-expanded") != "false" or toggle.get("aria-controls") != listing.get("id"):
                errors.append(f"funding {kind}: names disclosure must control its own list, closed by default")
            if toggle.select("a, button, summary, [tabindex]"):
                errors.append(f"funding {kind}: names disclosure contains an interactive control")
            buttons = listing.select(":scope > li.fund-name-item > button.fund-name-row")
            if len(buttons) != len(listing.find_all("li", recursive=False)) or not buttons or len(buttons) != len(line.select(".fund-name-row")) or any(len(row.select("a, button, summary, [tabindex]")) != 1 for row in listing.find_all("li", recursive=False)):
                errors.append(f"funding {kind}: names list needs one institute button per row")
            names = toggle.select_one(".fund-missing-names, .fund-other-names")
            label = "Not published: " if "fund-missing-line" in line.get("class", []) else "Not compared: "
            if names is None or names.find(True) or names.get_text() != label + ", ".join(b.get_text(strip=True) for b in buttons):
                errors.append(f"funding {kind}: closed summary must retain all names and reasons as plain text")
            for button in buttons:
                inst = by_id.get(str(button.get("data-institute")))
                state = next((j["name"] for j in data["jurisdictions"] if inst and j["id"] == inst["jurisdiction_id"]), None)
                if not inst or button.get("data-j") != inst["jurisdiction_id"] or not state or not (button.get_text() == state or button.get_text().startswith(state + " (")):
                    errors.append(f"funding {kind}: list row must retain its institute identity and state name")
        for opener in openers:
            iid = str(opener.get("data-institute"))
            detail = soup.find(id=opener.get("aria-controls"))
            if iid not in by_id or detail is None or detail.get("data-institute") != iid or opener.get("aria-expanded") != "false" or "Funding and staff records" not in (opener.get("aria-label") or ""):
                errors.append(f"{iid}: {kind} opener does not name and control its records")
            if opener.select("a, button, [tabindex]"):
                errors.append(f"{iid}: {kind} opener contains an interactive control")
            if "fund-row" in opener.get("class", []) and opener.get_text(strip=True):
                errors.append(f"{iid}: whole-row button contains visual or interactive content")
        for row in rows:
            iid = str(row.get("data-institute"))
            if iid not in by_id:
                continue
            if row.name != "li" or row.get("data-j") != by_id[iid]["jurisdiction_id"]:
                errors.append(f"{iid}: funding row has the wrong identity or is not a list item")
            if len(row.select(f":scope > .fund-{kind} .bar-value")) != 1 or len(row.select(f":scope > .fund-{kind} .fund-value strong")) != 1:
                errors.append(f"{iid}: every printed {kind} figure must have exactly one bar")
            figure = row.select_one(".fund-figure > strong > abbr.g.val")
            if figure is None or figure.get("tabindex") != "0" or not figure.get("aria-description") or tip_of(figure) != figure.get("aria-description"):
                errors.append(f"{iid}: {kind} figure needs its focusable tooltip and accessible description")
                continue
            if row.select_one(".fund-qualifier"):
                errors.append(f"{iid}: {kind} row must keep qualifications in the figure's explanation")
            description = figure["aria-description"]
            text = figure.get_text(strip=True)
            if re.search(r"\b(?:about|at least)\b", text):
                errors.append(f"{iid}: {kind} figure must use bound markers, with bound words in its explanation")
            if not description.endswith(".") or " · " in description:
                errors.append(f"{iid}: {kind} explanation must be one sentence")
            if kind == "budget":
                funding = next((f for f in by_id[iid].get("funding", []) if f["id"] == row.get("data-funding-id")), {})
                commitment = funding.get("commitment")
                if commitment not in {"adopted", "announced"} or not description.endswith(", " + commitment + "."):
                    errors.append(f"{iid}: Budget explanation must state its commitment word")
                if not description.startswith("A year, "):
                    errors.append(f"{iid}: Budget explanation must start with A year")
                period = funding.get("period") or ""
                annual = funding.get("annual_period") or {}
                if funding.get("annual_basis_code") == "allocation_current_year":
                    start, end = annual.get("start", "")[:4], annual.get("end", "")[:4]
                    separator = "/" if re.search(r"\d{4}/\d{2}\b", period) else "-"
                    label = start if start == end else start + separator + end[2:]
                    period_present = bool(start and end) and label + " allocation" in description
                elif period and not re.fullmatch(r"(?:per|a|each) (?:financial )?year|annual(?:ly)?", period, re.I):
                    period_present = period in description
                else:
                    years = {annual.get(edge, "")[:4] for edge in ("start", "end")} - {""}
                    period_present = bool(years) and all(year in description for year in years)
                if not period_present:
                    errors.append(f"{iid}: Budget explanation must retain its funding period")
                if ("average over " in description) != (funding.get("annual_basis_code") == "period_mean"):
                    errors.append(f"{iid}: Budget explanation must identify a period mean")
                bound = {"about": "about", "at_least": "at least", "at_most": "up to", "more_than": "more than", "less_than": "less than", "nearly": "nearly"}.get(funding.get("amount_qualifier"))
                if bound and bound not in description:
                    errors.append(f"{iid}: Budget bound {bound!r} must be stated in words in its explanation")
                symbol = {"about": "~", "at_least": "+", "at_most": "up to "}.get(funding.get("amount_qualifier"))
                if symbol and symbol not in text:
                    errors.append(f"{iid}: Budget figure must retain its {symbol!r} bound marker")
            if kind == "staff":
                total, _ = latest_staff_total(by_id[iid])
                if total is None or not total.get("as_of"):
                    errors.append(f"{iid}: Staff bar must represent a published dated total only")
                else:
                    bound = {"about": "about", "at_least": "at least", "at_most": "up to", "more_than": "more than", "less_than": "less than", "nearly": "nearly"}.get(total.get("total_qualifier"))
                    if bound and bound not in description:
                        errors.append(f"{iid}: Staff bound {bound!r} must be stated in words")
                    if not description.startswith("As at " + expected_date(total["as_of"])):
                        errors.append(f"{iid}: Staff explanation needs its recorded date")
                    if text != expected_staff_total(total):
                        errors.append(f"{iid}: Staff figure must retain its count and bound markers")
        for line in chart.select(".fund-other-line"):
            words = line.get_text(" ", strip=True)
            if re.search(r"\d", words) or line.select(".bar-value, .budget-amount, strong"):
                errors.append(f"funding {kind}: unscaled amounts must use words pointing to the records")
            for button in line.select(".fund-name-row"):
                iid = str(button.get("data-institute"))
                record = next((r for r in by_id.get(iid, {}).get("funding", []) if r["id"] == EXPECTED_BUDGET_IDS.get(iid)), {})
                if kind == "budget" and record.get("kind") == "one_off" and "(one-off amount)" not in button.get_text():
                    errors.append(f"{iid}: Not compared must identify the one-off amount")
    download = soup.select_one('#funding-download a[href="data/funding.csv"], a#funding-download[href="data/funding.csv"]')
    guidance = soup.select_one("#funding-download")
    if not download or not download.has_attr("download") or not guidance or "staff.csv" not in (guidance.get_text(" ") + tip_of(guidance)):
        errors.append("funding download must offer funding.csv and its staff.csv guidance")
    return errors


def label_ladder_selectors(data: dict) -> list[str]:
    """Data-backed minima, including the EU even if its entire group is removed."""
    selectors = set()
    for j in data["jurisdictions"]:
        full = full_card_expected(j)
        if j.get("kind") == "supranational":
            group, heading = ".eu-level", ".eu-level .eu-head > h3"
        elif full or j.get("europe_scope"):
            section = "#entries" if j.get("europe_scope") else "#world-cards"
            group = f'{section} .statusgroup[data-status="{j["status"]}"]'
            heading = group + " > h3"
        else:
            continue
        selectors.add(heading)
        if full:
            selectors.add(heading + ":visible")
            card = f'{group} .card:not(.mini)[data-j="{j["id"]}"]'
            selectors.update((card, card + " .top .name"))
    for institute in established_funding_institutes(data):
        for kind, ranked in expected_funding_rows(data).items():
            chart = f'#fund-rows .fund-list[data-kind="{kind}"]'
            selectors.add(chart + ' h3:visible')
            if institute["id"] in ranked:
                row = chart + f' .fund-item[data-institute="{institute["id"]}"]'
                selectors.update((row + ' .fund-identity h4', row + ' > .fund-row[aria-label]'))
            else:
                inline = chart + f' .fund-name-row[data-institute="{institute["id"]}"]'
                selectors.add(inline)
    return sorted(selectors)


def label_ladder_match_errors(data: dict, counts: dict[str, int]) -> list[str]:
    return [error for selector in label_ladder_selectors(data)
            for error in zero_match_errors("label_ladder", selector, counts.get(selector, 0))]


def check_label_ladder(page) -> list[str]:
    """Cards retain their accessible status and required labels."""
    data = checked_page_data(page)
    result = page.evaluate("""selectors => {
      const errors = [], visible = n => n.getClientRects().length > 0;
      const counts = Object.fromEntries(selectors.map(s => [s, [...document.querySelectorAll(s.replace(/:visible$/, ''))].filter(n => !s.endsWith(':visible') || visible(n)).length]));
      // A grouped card's accessible name includes its status after the state.
      for (const card of document.querySelectorAll('.statusgroup .card:not(.mini)')) {
        const said = card.querySelector(':scope > .top > .vh'), ids = (card.getAttribute('aria-labelledby') || '').split(' ');
        const want = ', ' + card.closest('.statusgroup').querySelector(':scope > h3 .smark > span').textContent.replace(/ \\(\\d+\\)$/, '');
        if (!said || said.textContent !== want || !ids.includes(said.id)) errors.push(`card in a status group lacks its accessible status name: ${card.id}`);
      }
      // The EU entry's dated Status row says its status, so its title row shows none either; the accessible name keeps it.
      for (const card of document.querySelectorAll('.eu-level .card:not(.mini)')) {
        const said = card.querySelector(':scope > .top > .vh'), ids = (card.getAttribute('aria-labelledby') || '').split(' ');
        const row = card.querySelector('.row[data-track="3"]'), shown = row ? row.querySelector('.v').textContent.trim().split(' since ')[0] : null;
        if (!said || !shown || said.textContent !== ', ' + shown || !ids.includes(said.id)) errors.push(`the EU entry's accessible status ${said && said.textContent} does not match its Status row ${shown}`);
      }
      return {errors, counts};
    }""", label_ladder_selectors(data))
    return label_ladder_match_errors(data, result["counts"]) + result["errors"]


def check_funding_weights(page) -> list[str]:
    """Both charts give figures equal type and tracks; every printed row figure has a bar."""
    return page.evaluate("""() => {
      const errors=[], type=n=>{const s=getComputedStyle(n);return [s.fontSize,s.fontWeight,s.fontFamily,s.lineHeight].join('|');};
      const rows=[...document.querySelectorAll('#fund-rows .fund-item')], figure=document.querySelector('.fund-budget .fund-value strong');
      if (!figure || !rows.length) return ['funding: no charted figure or rows to compare'];
      const probe=document.createElement('span');probe.style.font='var(--type-body)';document.body.appendChild(probe);
      const bodySize=parseFloat(getComputedStyle(probe).fontSize);probe.remove();
      if(parseFloat(getComputedStyle(figure).fontSize)>bodySize+.5) errors.push('funding: amount is larger than body type');
      const widths=[],heights=[];
      for(const row of rows) {
        for(const n of row.querySelectorAll(':scope > .fund-measure .fund-value strong'))
          if(type(n)!==type(figure)) errors.push(row.dataset.institute+': Budget and Staff amounts do not share compact type');
        for(const measure of row.querySelectorAll(':scope > .fund-measure')) {
          const bar=measure.querySelector('.fund-bar'),value=measure.querySelector('.fund-value');
          if(!bar || !value || !bar.firstElementChild) {errors.push(row.dataset.institute+': printed figure lacks its bar');continue;}
          const b=bar.getBoundingClientRect(),v=value.getBoundingClientRect();widths.push(b.width);heights.push(b.height);
          if(v.left<b.right-1 && v.top<b.bottom-1 && v.bottom>b.top+1) errors.push(row.dataset.institute+': figure overlaps its bar track');
        }
      }
      if(!widths.length || Math.max(...widths)-Math.min(...widths)>1 || Math.max(...heights)-Math.min(...heights)>1) errors.push('funding: Budget and Staff do not share bar-track dimensions in every row');
      return errors;
    }""")


def funding_bar_width_errors(samples: list[dict]) -> list[str]:
    """Data / scale in pixels, allowing only the owner's 2px visibility floor, never a CSS-derived oracle."""
    errors = []
    if not samples:
        return ["funding: no Budget or Staff bar proportions measured"]
    for sample in samples:
        key = sample["key"]
        if not all(isinstance(sample.get(k), (int, float)) and math.isfinite(sample[k]) for k in ("value", "scale", "track", "width", "stated_value", "stated_scale")):
            errors.append(f"{key}: non-finite bar measurement")
            continue
        value, scale, track = sample["value"], sample["scale"], sample["track"]
        if scale <= 0 or track <= 0 or value < 0:
            errors.append(f"{key}: invalid bar amount, scale or track")
            continue
        want = 0 if value == 0 else min(track, max(2, value / scale * track))
        if abs(sample["width"] - want) > .75:
            errors.append(f"{key}: bar width {sample['width']:.2f}px, expected {want:.2f}px from amount / scale with minimum clamp")
        if not math.isclose(sample["stated_value"], value, rel_tol=1e-9, abs_tol=1e-9) or not math.isclose(sample["stated_scale"], scale, rel_tol=1e-9, abs_tol=1e-9):
            errors.append(f"{key}: bar amount or scale metadata differs from the independently selected record")
    return errors


def check_funding_bars(page, data: dict, expected_ids=None) -> list[str]:
    errors = []
    expected_ids = EXPECTED_FUNDING_IDS if expected_ids is None else expected_ids
    included = {i["id"] for i in established_funding_institutes(data)}
    for institute in data["institutes"]:
        jid = institute["jurisdiction_id"]
        row = page.locator(f'.fund-list[data-kind="budget"] .fund-item[data-institute="{institute["id"]}"]')
        if jid not in expected_ids or institute["id"] not in included:
            if row.count():
                errors.append(f"{jid}: unexpected comparison bar")
            continue
        records = {record["id"]: record for record in institute.get("funding", [])}
        funding = records.get(expected_ids[jid])
        if funding is None:
            errors.append(f"{jid}: expected funding ID is absent")
            continue
        if row.count() != 1:
            errors.append(f"{jid}: expected one funding row")
            continue
        if row.get_attribute("data-funding-id") != funding["id"]:
            errors.append(f"{jid}: wrong comparison record")
        figure = row.locator(".fund-budget .fund-value strong").inner_text()
        stated, currency = funding.get("annual_amount"), funding.get("currency")
        bracket = ""
        if currency and currency != "EUR" and stated is not None:
            kept = half_up_two(stated, funding.get("amount_qualifier"))
            bracket = f" ({'~' if kept != Decimal(str(stated)) else ''}{currency} {short_amount(kept)})"
        detail_figure = page.locator(f'.fund-detail[data-institute="{institute["id"]}"] .fund-budget-summary strong').inner_text()
        if (bracket and not detail_figure.endswith(bracket)) or (not bracket and "(" in detail_figure):
            errors.append(f"{jid}: original-currency figure differs from the data in the opened records")
        if "(" in figure:
            errors.append(f"{jid}: chart figure retains the native-currency note")
        match = re.fullmatch(r"(?:up to |nearly |more than |less than )?~?EUR ([0-9.,]+)(k|m|bn)?\+?", figure)
        amount = Decimal(match[1].replace(",", "")) * {None: 1, "k": 10**3, "m": 10**6, "bn": 10**9}[match[2]] if match else None
        raw = expected_annual_amount(funding, data)
        rounded = half_up_two(raw, funding.get("amount_qualifier"))
        if amount != rounded:
            errors.append(f"{jid}: comparison figure differs from selected record")
        marked = currency != "EUR" or rounded != raw or funding.get("amount_qualifier") == "about"
        if ("~EUR " in figure) != marked:
            errors.append(f"{jid}: comparison approximation marker differs from selected record")
        if (figure.endswith("+")) != (funding.get("amount_qualifier") == "at_least") or figure.startswith("up to ") != (funding.get("amount_qualifier") == "at_most"):
            errors.append(f"{jid}: comparison bound marker differs from selected record")
        pending = row.locator(".fund-budget .bar-value.pending").count() == 1
        if pending != (funding["commitment"] != "adopted"):
            errors.append(f"{jid}: bar fill differs from commitment")
        value_text = " ".join(page.locator(f'.fund-detail[data-institute="{institute["id"]}"] .fund-budget-summary').inner_text().split())
        if ("average over period" in value_text) != (funding.get("annual_basis_code") == "period_mean"):
            errors.append(f"{jid}: period mean marker differs from data")
    budget_values = {
        inst["id"]: float(record["eur_per_year_est"])
        for inst in established_funding_institutes(data) for record in inst.get("funding", [])
        if record["id"] == expected_ids.get(inst["jurisdiction_id"]) and record.get("eur_per_year_est") is not None
    }
    staff_values = {}
    for inst in established_funding_institutes(data):
        total, _ = latest_staff_total(inst)
        if total is not None and total.get("as_of") and isinstance(total.get("total"), (int, float)) and math.isfinite(total["total"]):
            staff_values[inst["id"]] = total["total"]
    samples = []
    for kind, values, unit in (("budget", budget_values, 10_000_000), ("staff", staff_values, 50)):
        scale = max(unit, math.ceil(max(values.values(), default=0) / unit) * unit)
        head_scale = page.locator(f'.fund-{kind}-head .fund-scale')
        expected_scale = f"0 to EUR {short_amount(Decimal(scale))} a year" if kind == "budget" else f"0 to {scale} people"
        if head_scale.count() != 1 or " ".join(head_scale.inner_text().split()) != expected_scale:
            errors.append(f"funding {kind}: head must state its independent zero-based scale {expected_scale!r}")
        measured = page.locator(f"#fund-rows .fund-{kind} .bar-value").evaluate_all("""bars => bars.map(bar => {
          const track=bar.parentElement;
          return {institute:bar.closest('.fund-item').dataset.institute, track:track.getBoundingClientRect().width, width:bar.getBoundingClientRect().width,
            stated_value:Number(track.dataset.value), stated_scale:Number(track.dataset.scale)};
        })""")
        if Counter(s["institute"] for s in measured) != Counter(values.keys()):
            errors.append(f"funding {kind}: bar identities differ from published eligible records")
        for sample in measured:
            if sample["institute"] in values:
                samples.append({**sample, "key": sample["institute"] + " " + kind, "value": values[sample["institute"]], "scale": scale})
    errors.extend(funding_bar_width_errors(samples))
    return errors


def refresh_fixture_selections(fixture):
    """Rebuild fixture evidence at its current positions, then use the producer.

    Fixtures explicitly supply field bindings. Original excerpt positions cannot
    survive their array replacements or removals, so these synthetic supports
    describe only the fixture, never a claim about the original source text.
    """
    if __package__:
        from .display import derive_display
    else:
        from display import derive_display

    sources = {source["id"]: source for source in fixture["sources"]}
    for source in sources.values():
        source["excerpts"] = []

    def bind(record, path=""):
        if isinstance(record, list):
            for index, item in enumerate(record):
                bind(item, f"{path}/{index}")
        elif isinstance(record, dict):
            for field, ids in record.get("citations", {}).items():
                if field not in record or field in ("citations", "display", "source_ids"):
                    continue
                pointer = f"{path}/{field.replace('~', '~0').replace('/', '~1')}"
                for sid in ids:
                    sources[sid]["excerpts"].append({"text": "Synthetic fixture evidence.", "supports": [pointer]})
            for field, value in list(record.items()):
                if field not in ("citations", "display", "sources"):
                    bind(value, f"{path}/{field.replace('~', '~0').replace('/', '~1')}")

    for jurisdiction in fixture["jurisdictions"]:
        for index, event in enumerate(jurisdiction.get("timeline", [])):
            event.setdefault("id", f"fixture-{jurisdiction['id']}-{index}")
    for institute in fixture["institutes"]:
        for collection in ("funding", "staff"):
            for index, record in enumerate(institute.get(collection, [])):
                record.setdefault("id", f"fixture-{institute['id']}-{collection}-{index}")
    # Fixtures replace outputs, timelines and statuses: recompute only the generated intervals from them,
    # never rematerialise the facts the fixture deliberately overrides.
    if __package__:
        from .published import intervals
    else:
        from published import intervals
    jurisdictions = {j["id"]: j for j in fixture["jurisdictions"]}
    for institute in fixture["institutes"]:
        timeline = [e for e in jurisdictions[institute["jurisdiction_id"]].get("timeline", []) if e["event"] != "first_output"]
        institute["intervals"] = intervals(institute, timeline, fixture["dataset"]["as_of"])
    bind(fixture)
    refreshed = derive_display(fixture)
    fixture.clear()
    fixture.update(refreshed)


REVEAL_FUNDING_LIST_JS = """button => {
  const list=button.closest('.fund-name-list');
  if(list?.hidden) list.previousElementSibling.click();
}"""


FUNDING_RECORD_PLACEMENT_JS = """button => {
  const detail=document.getElementById(button.getAttribute('aria-controls'));
  const row=button.closest('.fund-item,.fund-name-item');
  return detail.open && detail.parentElement===row &&
    Math.abs(detail.getBoundingClientRect().top-button.getBoundingClientRect().bottom)<=1;
}"""


FUNDING_SUMMARY_LINES_JS = """() => [...document.querySelectorAll('.fund-names-toggle[aria-expanded="false"]')].map(button => {
  const names=button.querySelector('.fund-missing-names, .fund-other-names'), tops=[], bottoms=[];
  const walker=document.createTreeWalker(names,NodeFilter.SHOW_TEXT);
  while(walker.nextNode()) {
    const range=document.createRange();range.selectNodeContents(walker.currentNode);
    for(const r of range.getClientRects()) if(r.width&&r.height) {
      bottoms.push(r.bottom);
      if(!tops.some(t=>Math.abs(t-r.top)<.5)) tops.push(r.top);
    }
  }
  return {key:button.getAttribute('aria-controls'),kind:button.closest('.fund-list').dataset.kind,
    fontSize:parseFloat(getComputedStyle(names).fontSize),lineTops:tops.sort((a,b)=>a-b),textBottom:Math.max(...bottoms)};
})"""


def funding_summary_spacing_errors(summaries: list[dict]) -> list[str]:
    """Ordinary wrapped leading and at most 1.6em from its last ink box to the next summary.

    At 14px this allows 22.4px, including the next line centred in its 44px target.
    Two single-line targets need more space; the inter-summary bound is for a wrapped predecessor.
    """
    errors = [f"{s['key']}: wrapped names have a reserved hit gap between text lines"
              for s in summaries if any(b - a > 1.6 * s["fontSize"] + .1
                                        for a, b in zip(s["lineTops"], s["lineTops"][1:]))]
    for previous, current in zip(summaries, summaries[1:]):
        if previous['kind'] == current['kind'] and len(previous['lineTops']) > 1 and current['lineTops']:
            gap = current['lineTops'][0] - previous['textBottom']
            if gap < 0 or gap > 1.6 * previous['fontSize'] + .1:
                errors.append(f"{previous['key']}: gap to the next summary's text must be at most 1.6 times the font size")
    return errors


FUNDING_LAYOUT_JS = r"""() => {
  const list=document.getElementById('fund-rows');
  const alpha=color=>{if(color==='transparent')return 0;if(color.startsWith('rgba('))return Number(color.slice(5,-1).split(',')[3]);const m=color.match(/\/\s*([\d.]+)(%)?\s*\)$/);return m?Number(m[1])/(m[2]?100:1):1;};
  const rect=n=>{const r=n.getBoundingClientRect();return {left:r.left,right:r.right,top:r.top,bottom:r.bottom,width:r.width,height:r.height};};
  const visible=n=>{if(n.closest('.vh, template')||!n.getClientRects().length||getComputedStyle(n).visibility==='hidden')return false;for(let p=n.closest('details:not([open])');p;p=p.parentElement.closest('details:not([open])'))if(!p.querySelector(':scope > summary')?.contains(n))return false;return true;};
  const section=list.closest('.section-body'),bounds=rect(section),style=getComputedStyle(section);
  bounds.left+=parseFloat(style.paddingLeft)+parseFloat(style.borderLeftWidth);bounds.right-=parseFloat(style.paddingRight)+parseFloat(style.borderRightWidth);
  const overflow=[],outside=(r,name)=>{if(r.width&&r.height&&(r.left<bounds.left-.75||r.right>bounds.right+.75))overflow.push(name+': descendant crosses section content edge');};
  for(const n of [list,...list.querySelectorAll('*')]) {
    if(!visible(n))continue;
    for(const r of n.getClientRects())outside(r,n.className);
    // Text its own box clips with an ellipsis (a one-line header name) is cut where the box ends; the box itself is measured above.
    const cs=getComputedStyle(n);if(cs.textOverflow==='ellipsis'&&cs.overflowX!=='visible')continue;
    for(const text of n.childNodes)if(text.nodeType===Node.TEXT_NODE&&text.textContent.trim()){const range=document.createRange();range.selectNodeContents(text);for(const r of range.getClientRects())outside(r,n.className+' text');}
  }
  if(list.scrollWidth>list.clientWidth+1||section.scrollWidth>section.clientWidth+1)overflow.push('sideways Funding scroll');
  const cell=(n,sel)=>{const c=n?.querySelector(sel);return c&&visible(c)?rect(c):null;};
  const lines=n=>{if(!n)return 0;const range=document.createRange();range.selectNodeContents(n);return new Set([...range.getClientRects()].filter(r=>r.width&&r.height).map(r=>Math.round(r.top))).size;};
  const commitmentVisible=q=>{
    if(!q||!visible(q)||Number(getComputedStyle(q).opacity)===0)return false;
    const word=q.textContent.trim();
    if(!['adopted','announced'].includes(word))return false;
    // The Budget key retains both commitment words; measure their glyphs, not just the containing box.
    let node=null,at=-1;
    if(document.createTreeWalker){const walker=document.createTreeWalker(q,NodeFilter.SHOW_TEXT);
      while(walker.nextNode()){const k=walker.currentNode.textContent.lastIndexOf(word);if(k>=0){node=walker.currentNode;at=k;}}
    } else if(q.firstChild?.nodeType===Node.TEXT_NODE){node=q.firstChild;at=q.textContent.lastIndexOf(word);}
    if(!node||at<0)return false;
    const range=document.createRange();range.setStart(node,at);range.setEnd(node,at+word.length);
    const boxes=[...range.getClientRects()].filter(r=>r.width&&r.height),bounds=rect(q);
    return boxes.length>0&&boxes.every(r=>{
      // A glyph box is a few px taller than a tight line box, so the line's own box gets that much slack above and below.
      if(r.left<bounds.left-.75||r.right>bounds.right+.75||r.top<bounds.top-6||r.bottom>bounds.bottom+6)return false;
      for(let n=q;n&&n!==list;n=n.parentElement){const s=getComputedStyle(n),b=rect(n);
        if(/hidden|clip/.test(s.overflowX)&&(r.left<b.left-.75||r.right>b.right+.75))return false;
        if(/hidden|clip/.test(s.overflowY)&&(r.top<b.top-.75||r.bottom>b.bottom+.75))return false;
      }return true;
    });
  };
  const heads=Object.fromEntries(['budget','staff'].map(kind=>{
    const h=list.querySelector('.fund-'+kind+'-head'),s=getComputedStyle(h);
    const key=[...h.querySelectorAll('.key-item')];
    return [kind,{box:rect(h),bar:rect(h.querySelector('.fund-bar-head')),figure:rect(h.querySelector('.fund-figure-head')),opaque:alpha(s.backgroundColor)===1&&Number(s.opacity)===1,
      commitmentVisible:kind!=='budget'||h.closest('.fund-list[data-head-covered]')!==null||key.length===2&&key.every(commitmentVisible)}];
  }));
  const rows=[...list.querySelectorAll('.fund-item')].map(row=>{
    const identity=row.querySelector('.fund-identity'),button=row.querySelector('.fund-row'),bs=getComputedStyle(button),ns=getComputedStyle(identity);
    const detail=row.querySelector('.fund-detail[open]'),kind=row.closest('.fund-list').dataset.kind;
    const m=row.querySelector('.fund-measure'),v=m.querySelector('.fund-value'),ms=getComputedStyle(m);
    const joined=!!detail&&button.getAttribute('aria-expanded')==='true'&&bs.backgroundColor===getComputedStyle(detail).backgroundColor&&alpha(bs.backgroundColor)===1&&Math.abs(rect(detail).top-rect(button).bottom)<=1;
    return {key:kind+':'+row.dataset.institute,kind,button:rect(button),border:parseFloat(bs.borderBottomWidth),joined,
      painted:!['none','hidden'].includes(bs.borderBottomStyle)&&alpha(bs.borderBottomColor)>0,last:row===row.parentElement.lastElementChild,
      identity:rect(identity),identityLines:lines(identity.querySelector('h4')),detail:cell(row,'.fund-detail[open]'),maxHeight:Math.max(44,rect(identity.querySelector('h4')).height+parseFloat(ns.paddingTop)+parseFloat(ns.paddingBottom),parseFloat(getComputedStyle(v).lineHeight)+parseFloat(ms.paddingTop)+parseFloat(ms.paddingBottom)),
      measures:[{kind,box:rect(m),bar:rect(m.querySelector('.fund-bar')),figure:rect(v),amountLines:lines(v.querySelector('strong')),qualifierLines:v.querySelector('.fund-qualifier')?1:0}]};
  });
  const targets=[...list.querySelectorAll('.fund-row, .fund-name-row, .fund-names-toggle')].filter(visible).map(n=>({key:n.closest('.fund-list').dataset.kind+':'+(n.dataset.institute||n.getAttribute('aria-controls')),box:rect(n)}));
  const coverage=Object.fromEntries(['budget','staff'].map(kind=>[kind,[...list.querySelectorAll('.fund-list[data-kind='+kind+'] :is(.fund-row,.fund-name-row)')].filter(visible).map(n=>n.dataset.institute)]));
  return {list:rect(list),heads,rows,targets,coverage,panels:[...list.querySelectorAll('.fund-detail[open]')].map(rect),overflow:[...new Set(overflow)],oneLineNames:parseFloat(getComputedStyle(document.documentElement).fontSize)<=16.01};
}"""


def funding_layout_errors(got: dict, closed: dict | None = None) -> list[str]:
    """Full-width charts, equal tracks, readable commitment key and compact one-line rows at every width."""
    errors = list(got["overflow"])
    if not got["rows"]:
        return errors + ["funding: no rows measured"]
    def same(a, b):
        return abs(a - b) <= .75

    def full(box):
        return all(same(box[edge], got["list"][edge]) for edge in ("left", "right"))
    targets = got.get("targets", [])
    for index, target in enumerate(targets):
        box = target["box"]
        if box["height"] < 44 - .75 or not full(box):
            errors.append(f"{target['key']}: Funding target must span the full list and be at least 44px high")
        for other in targets[index + 1:]:
            b = other["box"]
            if min(box["right"], b["right"]) - max(box["left"], b["left"]) > .5 and min(box["bottom"], b["bottom"]) - max(box["top"], b["top"]) > .5:
                errors.append(f"Funding targets overlap: {target['key']} / {other['key']}")
    if any(not full(panel) for panel in got.get("panels", [])):
        errors.append("funding: opened records beneath a bar or list row must span the full list")
    for kind, head in got["heads"].items():
        if not full(head["box"]) or not head["opaque"]:
            errors.append(f"funding {kind}: sticky head needs an opaque ground across the full list")
        if kind == "budget" and not head["commitmentVisible"]:
            errors.append("funding Budget key must show both commitment words without clipping")
    if got["heads"]["staff"]["box"]["top"] < got["heads"]["budget"]["box"]["bottom"]-.75:
        errors.append("funding: charts must stack Budget before Staff")
    tracks = []
    for row in got["rows"]:
        key = row["key"]
        if not full(row["button"]) or (not row["last"] and not row.get("joined", False) and (row["border"] < 1 or not row["painted"])):
            errors.append(f"{key}: row hairline/control does not span the full list")
        if row["detail"] and not full(row["detail"]):
            errors.append(f"{key}: opened detail does not span the shared tracks")
        if row["button"]["height"] < 44 - .75:
            errors.append(f"{key}: row must retain its 44px hit target")
        if row["button"]["height"] > row["maxHeight"] + 2:
            errors.append(f"{key}: row exceeds its one-line content and hit target")
        if got["oneLineNames"] and row["identityLines"] != 1:
            errors.append(f"{key}: name must fit on one line at normal text size")
        for measure in row["measures"]:
            kind = measure["kind"]
            b, v = measure["bar"], measure["figure"]
            tracks.append(b["width"])
            if measure["amountLines"] != 1 or measure["qualifierLines"]:
                errors.append(f"{key}: figure must be one line with no qualifier line")
            if not (v["top"] - .75 <= (b["top"] + b["bottom"]) / 2 <= v["bottom"] + .75):
                errors.append(f"{key}: bar and figure must share one line")
            head = got["heads"][kind]
            if not head["bar"] or not head["figure"] or not same(b["left"], head["bar"]["left"]) or not same(v["left"], head["figure"]["left"]):
                errors.append(f"{key}: bar-left/figure-left differ from the head cells")
    if tracks and max(tracks)-min(tracks) > .75:
        errors.append("funding: bar tracks differ across rows or measures")
    if closed:
        for kind in ("budget", "staff"):
            for cell in ("bar", "figure"):
                before, after = closed["heads"][kind][cell], got["heads"][kind][cell]
                if before and (not after or not all(same(before[k], after[k]) for k in ("left", "width"))):
                    errors.append(f"funding {kind}: {cell} track moves when hovered/opened")
        old_rows = {r["key"]: r for r in closed["rows"]}
        for row in got["rows"]:
            for old, new in zip(old_rows[row["key"]]["measures"], row["measures"], strict=True):
                if any(not same(old[cell][edge], new[cell][edge]) for cell in ("bar", "figure") for edge in ("left", "width")):
                    errors.append(f"{row['key']} {new['kind']}: row tracks move when hovered/opened")
    return errors


def check_funding_layout_states(page) -> list[str]:
    """Exercise both summaries and their list rows, plus the ranked bars and shared records."""
    errors = []
    saved = page.evaluate("() => ({y:scrollY,lists:[...document.querySelectorAll('.fund-names-toggle[aria-expanded=true]')].map(n=>n.getAttribute('aria-controls')),open:[...document.querySelectorAll('.fund-row[aria-expanded=true],.fund-name-row[aria-expanded=true]')].map(n=>({kind:n.closest('.fund-list').dataset.kind,id:n.dataset.institute}))})")
    settle = "() => new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r)))"
    try:
        page.locator('.fund-detail[open] .close-control').evaluate_all("nodes=>nodes.forEach(n=>n.click())")
        page.locator('.fund-names-toggle[aria-expanded=true]').evaluate_all("nodes=>nodes.forEach(n=>n.click())")
        page.mouse.move(0, 0)
        opener = page.locator('#fund-rows .fund-row').first
        opener.scroll_into_view_if_needed()
        page.evaluate(settle)
        closed = page.evaluate(FUNDING_LAYOUT_JS)
        errors += ["closed: " + e for e in funding_layout_errors(closed)]
        summaries = page.evaluate(FUNDING_SUMMARY_LINES_JS)
        errors += funding_summary_spacing_errors(summaries)
        if page.viewport_size["width"] in (320, 390) and not any("missing" in s["key"] and len(s["lineTops"]) > 1 for s in summaries):
            errors.append("funding: phone spacing check must exercise wrapped Not published text")
        opener.hover(position={"x": 2, "y": 2})
        page.evaluate(settle)
        errors += ["hovered: " + e for e in funding_layout_errors(page.evaluate(FUNDING_LAYOUT_JS), closed)]
        for kind in ("budget", "staff"):
            figure = page.locator(f'.fund-list[data-kind="{kind}"] .fund-figure abbr.g').first
            if not figure.count():
                errors.append(f"funding {kind}: no figure tooltip to exercise")
                continue
            for action in ("hover", "focus", "tap"):
                page.keyboard.press("Escape")
                if action == "hover":
                    figure.hover()
                elif action == "focus":
                    figure.evaluate("n=>{n.blur();n.focus();}")
                else:
                    figure.evaluate("n=>{n.dispatchEvent(new PointerEvent('pointerdown',{bubbles:true,pointerType:'touch'}));n.click();}")
                page.evaluate(settle)
                shown = figure.evaluate("n=>{const tip=document.querySelector('#tip');return !!tip&&!tip.hidden&&tip.textContent.trim()===n.getAttribute('aria-description');}")
                if not shown:
                    errors.append(f"funding {kind}: {action} does not show the figure's accessible explanation")
                if page.locator('.fund-detail[open]').count():
                    errors.append(f"funding {kind}: {action} on the figure also opens records")
                page.keyboard.press("Escape")
        toggles = page.locator('.fund-names-toggle').all()
        for count, toggle in enumerate(toggles, 1):
            toggle.evaluate("n=>n.click()")
            if page.locator('.fund-names-toggle[aria-expanded=true]').count() != count or not toggle.evaluate("n=>!document.getElementById(n.getAttribute('aria-controls')).hidden"):
                errors.append("funding: each names disclosure must open its own list independently")
        page.evaluate(settle)
        expanded = page.evaluate(FUNDING_LAYOUT_JS)
        errors += ["lists open: " + e for e in funding_layout_errors(expanded, closed)]
        errors += check_hit_targets(page, '#fund-rows')
        expected = Counter(i["id"] for i in established_funding_institutes(checked_page_data(page)))
        for kind in ("budget", "staff"):
            if Counter(expanded["coverage"][kind]) != expected:
                errors.append(f"funding {kind}: opened lists and bars must reach every expected institute exactly once")
        selectors = [f'.fund-list[data-kind="{kind}"] .fund-row' for kind in ("budget", "staff")]
        selectors += ['#' + toggle.get_attribute('aria-controls') + ' .fund-name-row' for toggle in toggles]
        for selector in selectors:
            button = page.locator(selector).first
            if not button.count():
                continue
            button.evaluate("n=>n.click()")
            page.mouse.move(0, 0)
            page.evaluate(settle)
            errors += ["open: " + e for e in funding_layout_errors(page.evaluate(FUNDING_LAYOUT_JS), closed)]
            if page.locator('.fund-detail[open]').count() != 1:
                errors.append("funding: only one records panel may open across both charts")
            if not button.evaluate(FUNDING_RECORD_PLACEMENT_JS):
                errors.append("funding: records must open directly beneath their bar or list row")
            for close in ("button", "escape"):
                if close == "button":
                    page.locator('.fund-detail[open] .close-control').evaluate_all("nodes=>nodes.forEach(n=>n.click())")
                else:
                    page.keyboard.press("Escape")
                if page.locator('.fund-detail[open]').count() or not button.evaluate("n=>document.activeElement===n&&n.getAttribute('aria-expanded')==='false'"):
                    errors.append(f"funding: {close} must close records and restore focus to their exact opener")
                button.evaluate("n=>n.click()")  # leave one open for the next opener to replace
        for toggle in toggles:
            listing = '#' + toggle.get_attribute('aria-controls')
            button = page.locator(listing + ' .fund-name-row').first
            if button.get_attribute('aria-expanded') != 'true':
                button.evaluate("n=>n.click()")
            toggle.evaluate("n=>n.click()")
            if page.locator('.fund-detail[open]').count() or not toggle.evaluate("n=>document.activeElement===n&&n.getAttribute('aria-expanded')==='false'&&document.getElementById(n.getAttribute('aria-controls')).hidden"):
                errors.append("funding: closing a list must close its records and focus its disclosure")
            if button.get_attribute('aria-expanded') != 'false':
                errors.append("funding: collapsing a list must reset its record opener")
            toggle.evaluate("n=>n.click()")
        if len(toggles) > 1:
            owner, other = toggles[:2]  # two distinct summaries in DOM order, only for the independence check
            button = page.locator('#' + owner.get_attribute('aria-controls') + ' .fund-name-row').first
            button.evaluate("n=>n.click()")
            other.evaluate("n=>n.click()")
            if owner.get_attribute('aria-expanded') != 'true' or button.get_attribute('aria-expanded') != 'true' or page.locator('.fund-detail[open]').count() != 1:
                errors.append("funding: closing another list must preserve the open records")
    finally:
        page.evaluate("state=>{document.querySelectorAll('.fund-detail[open] .close-control').forEach(n=>n.click());document.querySelectorAll('.fund-names-toggle').forEach(n=>{if((n.getAttribute('aria-expanded')==='true')!==state.lists.includes(n.getAttribute('aria-controls')))n.click();});for(const o of state.open)document.querySelector('.fund-list[data-kind='+o.kind+'] button[data-institute='+o.id+']')?.click();scrollTo(0,state.y);}", saved)
    return errors


def check_funding_large_text(browser, base_url: str) -> list[str]:
    """Exercise summary line spacing, opened lists and shared tracks at 320/390px and enlarged text."""
    errors = []
    cases = [(width, scale) for width in (320, 390) for scale in (100, 110, 125)]
    cases += [(width, 100) for width in (1000, 1440)]
    cases += [(width, 125) for width in (641, 700, 760, 1000, 1440)]
    for width, scale in cases:
        page = browser.new_page(viewport={"width": width, "height": 900})
        # Playwright supplies (route, request); keep the captured scale keyword-only.
        page.route(base_url, lambda route, *, scale=scale: route.fulfill(response=(r := route.fetch()), body=r.text().replace("</head>", f"<style>html {{ font-size: {scale}% !important; }}</style></head>", 1)))
        hydrated(page, base_url)
        rem = page.evaluate("parseFloat(getComputedStyle(document.documentElement).fontSize)")
        if not math.isclose(rem, 16 * scale / 100, abs_tol=.01):
            errors.append(f"{width}px: the large-text check ran at a {rem}px root font, not {scale}%")
        for theme in ("light", "dark"):
            set_theme(page, theme)
            errors += [f"{width}px, {theme}, text {scale}%: {e}" for e in check_funding_layout_states(page)]
        page.close()
    return errors


def check_published_amount_fallback(browser, base_url: str, data: dict) -> list[str]:
    """Related funding records without a description show their amounts as display.money writes them: a
    single-digit multiple keeps its second figure ("CHF 7.0m", "CHF 8.0k", never "CHF 7m"), thousands as k."""
    fixture = copy.deepcopy(data)
    records = [i["display"]["funding_records"][k] for i in fixture["institutes"] for k in i["display"].get("related_funding_ids", [])]
    cases = [(7000000, "CHF 7.0m", "CHF 7m"), (640000, "CHF 640k", "CHF 640,000"), (8000, "CHF 8.0k", "CHF 8,000")][:len(records)]
    if len(cases) < 3:
        return [f"related funding: {len(records)} records, three needed for the amount fixture"]
    for record, (amount, _, _) in zip(records, cases):
        record.pop("description", None)
        record["amount"], record["currency"] = amount, "CHF"
    page = browser.new_page(viewport={"width": 1440, "height": 1000})
    page.route(PAGE_DATA, lambda route: route.fulfill(json=fixture))
    hydrated(page, base_url)
    page.locator(".fund-detail").evaluate_all("folds => folds.forEach(n=>n.open=true)")
    text = page.evaluate("document.body.textContent")
    page.close()
    return [f"related funding without a description: {amount} not shown as {want!r} ({'shown as ' + repr(old) if old in text else 'absent'})" for amount, want, old in cases if want not in text or old in text]


def check_access_edges(browser, base_url: str, data: dict) -> list[str]:
    """Earlier access (no institute has it today) joins the privileged group and says the year of its latest event."""
    fixture = copy.deepcopy(data)
    institute = next(i for i in fixture["institutes"] if i["model_access"]["value"] == "recent_access")
    institute["model_access"]["value"] = "earlier_access"
    errors = []
    for width in (390, 1440):
        page = browser.new_page(viewport={"width": width, "height": 1000})
        page_errors: list[str] = []
        page.on("pageerror", lambda e, errs=page_errors: errs.append(str(e)))
        page.route(PAGE_DATA, lambda route: route.fulfill(json=fixture))
        hydrated(page, base_url)
        page.wait_for_selector("#access .group-block")
        soup = BeautifulSoup(page.content(), "html.parser")
        note = soup.select_one(f'#access .group-block[data-group="privileged"] li[data-state="{institute["jurisdiction_id"]}"] .group-note')
        if not note or f"evidence from {institute['model_access']['latest_event'][:4]}" not in note.get_text():
            errors.append("earlier access is not in the privileged group with its evidence year")
        errors.extend(check_access_groups(soup, fixture))
        errors.extend(check_source_alignment(page.content(), fixture))
        errors.extend(page_errors)
        page.close()
    return errors


def check_news_layout(page, data) -> list[str]:
    """D, E and H: stacked quiet labels, contextual glossary marks and footer links. Blocking, not a report: a
    wrapped label, a missing or wrong expansion and misplaced links are the owner's News rules, not cosmetic drift."""
    return page.evaluate(r"""G => {
      const errors = [], rows = [...document.querySelectorAll('#development-list > li')];
      if (!rows.length) return errors;
      const hidden = rows.map(row => row.hidden);
      rows.forEach(row => row.hidden = false);
      const css = node => getComputedStyle(node), rect = node => node.getBoundingClientRect();
      const body = css(document.body), tokens = Object.keys(G || {}).sort((a, b) => b.length - a.length);
      const pattern = tokens.length ? new RegExp('(^|[^A-Za-z0-9-])(' + tokens.map(k => k.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')).join('|') + ')(?![A-Za-z0-9-])', 'g') : null;
      for (const row of rows) {
        const label = row.querySelector('.development-label'), text = row.querySelector('.development-text');
        if (!label || !text) { errors.push('News entry needs a label and sentence'); continue; }
        const l = rect(label), t = rect(text), ls = css(label), ts = css(text);
        const date = label.querySelector('.development-date'), name = label.querySelector('.development-name');
        if (!date || !name || label.textContent !== date.textContent + ' ' + name.textContent) errors.push('News label is not date · states');
        const range = document.createRange(); range.selectNodeContents(label);
        const boxes = [...range.getClientRects()];
        if (ls.whiteSpace !== 'nowrap' || Math.abs(l.height - parseFloat(ls.lineHeight)) > 1 || boxes.some(b => Math.abs(b.top - boxes[0].top) > 1)) errors.push('News label wraps or is taller than one line');
        if (parseFloat(ls.fontSize) >= parseFloat(ts.fontSize) || Number(ls.fontWeight) > Number(ts.fontWeight) || Number(ls.fontWeight) !== 400 || ls.color === ts.color) errors.push('News label is not small, muted and normal weight');
        if (name && (css(name).fontSize !== ls.fontSize || css(name).fontWeight !== ls.fontWeight || css(name).color !== ls.color)) errors.push('News state name differs from its quiet label');
        if (Math.abs(l.left - t.left) > 1 || t.top < l.bottom - 1) errors.push('News sentence is not below its label on the same left edge');
        if (ts.fontSize !== body.fontSize || ts.color !== body.color) errors.push('News sentence is not body size and colour');
        if (pattern) {
          const copy = text.cloneNode(true); copy.querySelectorAll('.cites').forEach(n => n.remove());
          const line = copy.textContent, expected = [], seen = new Set(); let match; pattern.lastIndex = 0;
          while ((match = pattern.exec(line))) {
            const key = match[2], definition = typeof G[key] === 'object' ? G[key][row.dataset.j] : G[key];
            if (!definition || seen.has(key)) continue;
            seen.add(key);
            const prefix = line.slice(0, match.index + match[1].length).toLowerCase();
            if (prefix.endsWith(definition.split(/[;(]|\.(?=\s|$)/)[0].trim().toLowerCase() + ' (')) continue;
            expected.push([key, definition]);
          }
          const actual = [...text.querySelectorAll('abbr.g[data-gl]')].map(a => [a.textContent, a.getAttribute('aria-description')]);
          if (JSON.stringify(actual) !== JSON.stringify(expected)) errors.push('News glossary marks differ from definitions for state ' + row.dataset.j);
        }
      }
      const links = [...document.querySelectorAll('#developments .news-links a')], more = document.getElementById('developments-more');
      if (links.length !== 2 || links[0].id !== 'news-feed' || links[1].id !== 'news-download') errors.push('News foot row needs the feed, then the download at its right end');
      else {
        if (Math.abs((rect(links[0]).top + rect(links[0]).bottom) - (rect(links[1]).top + rect(links[1]).bottom)) > 4 || links.some(a => a.getClientRects().length !== 1)) errors.push('News links do not share one line');
        if (links.some(a => !(rows.at(-1).compareDocumentPosition(a) & Node.DOCUMENT_POSITION_FOLLOWING) || !more || !(more.compareDocumentPosition(a) & Node.DOCUMENT_POSITION_FOLLOWING) || rect(a).top < rect(rows.at(-1)).bottom - 1 || (!more.hidden && rect(a).top < rect(more).bottom - 1))) errors.push('News links must follow the entries and control');
      }
      rows.forEach((row, n) => row.hidden = hidden[n]);
      return [...new Set(errors)];
    }""", data["dataset"].get("glossary", {}))


def check_development_edges(browser, base_url: str, data: dict) -> list[str]:
    """Exercise short text and closure events without a current institute card."""
    fixture = copy.deepcopy(data)
    jurisdiction = next(
        j
        for j in fixture["jurisdictions"]
        if j["status"] == "none_identified" and not j.get("institute_id")
    )
    fixture["dataset"].setdefault("glossary", {})["NEWS"] = {jurisdiction["id"]: "National Evaluation Working Service"}
    common = {
        "date": fixture["dataset"]["as_of"],
        "source_ids": jurisdiction["source_ids"][:1],
        "citations": {field: jurisdiction["source_ids"][:1] for field in ("date", "event", "description", "short", "news")},
    }
    jurisdiction["timeline"] = [
        {
            **common,
            "id": "fixture-closed",
            "event": "closed",
            "short": "Fixture short summary.",
            "news": "The NEWS programme reports the closure.",
            "description": jurisdiction["summary"],
        },
        {
            **common,
            "id": "fixture-withdrawn",
            "event": "withdrawn",
            "description": jurisdiction["summary"],
        },
    ]
    errors = []
    for width in (390, 1440):
        page = themed_page(browser, "light", viewport={"width": width, "height": 1000})
        page_errors: list[str] = []
        page.on("pageerror", lambda e, errs=page_errors: errs.append(str(e)))
        refresh_fixture_selections(fixture)
        page.route(PAGE_DATA, lambda route: route.fulfill(json=fixture))
        hydrated(page, base_url)
        page.wait_for_selector("#development-list > li")
        page.evaluate("document.fonts.ready")
        if (
            page.locator('[data-events="fixture-closed fixture-withdrawn"]').count()
            != 1
        ):
            errors.append("development edge fixture was not rendered")
        errors.extend(check_summary_data(page.content(), fixture))
        errors.extend(check_news_layout(page, fixture))
        errors.extend(check_source_alignment(page.content(), fixture))
        errors.extend(check_wrapped_rows(page))
        errors.extend(page_errors)
        page.close()
    return errors


def card_claims(j: dict, institutes: dict) -> list[dict]:
    """Expected card claims, from the published fields; institute functions belong to Compare."""
    if j["status"] != "none_identified":
        return [c for r in (j, institutes.get(j.get("institute_id"), {})) for c in r.get("claims", [])
                if c["kind"] == "fact" and not c.get("expired")]
    from summaries import shown_lead

    lead = shown_lead(j)
    closest_id = (j.get("closest_body") or {}).get("body_id")
    bodies = {b["id"] for b in j.get("related_bodies", [])} | {closest_id}
    return [c for c in j.get("claims", []) if c is lead or
            c["kind"] == "development" and not c.get("expired") or c["kind"] == "function" and c.get("body_id") in bodies]


def claim_bindings(data: dict, claim_id: str) -> list[tuple[dict, int, dict]]:
    """Independently resolve a claim's required published leaves, once per bound excerpt."""
    for collection in ("jurisdictions", "institutes"):
        for ri, record in enumerate(data.get(collection, [])):
            for ci, claim in enumerate(record.get("claims", [])):
                if claim["id"] != claim_id:
                    continue
                leaves = ("text", "act", "date") if claim["kind"] == "development" else ("text",)
                pointers = {f"/{collection}/{ri}/claims/{ci}/{leaf}" for leaf in leaves}
                return [(s, ei, e) for s in data.get("sources", []) for ei, e in enumerate(s.get("excerpts", []))
                        if pointers.intersection(e.get("supports", []))]
    return []


def expected_quote_label(data: dict, claim_id: str) -> str:
    """The Quote fold's label: "Quote", or "Quote, translated from {languages}" naming, in order, the plain language of
    every translated excerpt and anchor it holds (principle: Claims; plain names as in summaries.LANGUAGE_NAMES)."""
    from summaries import LANGUAGE_NAMES
    sources, names = {s["id"]: s for s in data.get("sources", [])}, []
    for s, _, e in claim_bindings(data, claim_id):
        for span in [e, *e.get("anchors", [])]:
            lang = sources.get(span.get("source_id"), s).get("language") if span.get("source_id") else s.get("language")
            name = span.get("gloss") and LANGUAGE_NAMES.get(lang, lang or "an unknown language")
            if name and name not in names:
                names.append(name)
    if not names:
        return "Quote"
    return "Quote, translated from " + (names[0] if len(names) < 2 else ", ".join(names[:-1]) + " and " + names[-1])


def check_quote_translations(soup) -> list[str]:
    """Every fold shows Quote, names its languages accessibly and once inside; originals stay focusable."""
    errors = []
    if soup.select(".translation"):
        errors.append("a translation marker follows an individual quote")
    for fold in soup.select('details.quote'):
        summary = fold.find('summary', recursive=False)
        name = summary.get('aria-label', '') if summary else ''
        if summary is None or summary.get_text(strip=True) != 'Quote':
            errors.append('a quote fold does not show the word Quote')
        if name != 'Quote' and not name.startswith('Quote, translated from '):
            errors.append('a quote fold lacks its accessible name')
        translated = name.replace('Quote, translated', 'Translated', 1) if name.startswith('Quote, translated from ') else ''
        if fold.get('data-translated', '') != translated:
            errors.append('a quote fold translation differs from its accessible name')
        body = fold.select_one(':scope > .quote-body')
        if body is not None:
            markers = body.select('.quote-translation')
            if ([m.get_text(strip=True) for m in markers] != ([translated] if translated else [])
                    or any(m.parent is not body for m in markers)):
                errors.append('an opened quote must name its translation once inside the fold')
    for quote in soup.select(".quote-text[aria-description]"):
        if quote.get("tabindex") != "0":
            errors.append("translated quote lacks keyboard focus")
        fold = quote.find_parent("details", class_="quote")
        if fold is None or quote.find_parent(class_="claim-text") is not None:
            continue
        summary = fold.find("summary", recursive=False)
        if summary is None or not summary.get('aria-label', '').startswith("Quote, translated from "):
            errors.append("a Quote fold holding a translated quote does not say so in its accessible name")
    return errors


def check_claim_cards(html: str, data: dict) -> list[str]:
    """Saved sentences and citations, empty quote disclosures, and unchanged legacy body text.

    The lead's quoted English is deliberately saved. No disclosure body or anchor is saved, even hidden.
    Legacy body text remains under check_nonidentified_folds in the names content family.
    """
    soup = BeautifulSoup(html, "html.parser")
    institutes = {i["id"]: i for i in data.get("institutes", [])}
    from summaries import shown_lead

    errors = check_quote_translations(soup)
    for j in data.get("jurisdictions", []):
        if j["status"] == "none_identified" and not j.get("europe_scope"):
            continue  # these jurisdictions have table rows, not cards
        expected = card_claims(j, institutes)
        lead = shown_lead(j) if j["status"] == "none_identified" else None
        card = soup.select_one(f'#c-{j["id"].lower()}')
        if card is None:
            if expected:
                errors.append(f"{j['id']}: missing claims card")
            continue
        blocks = card.select(".claim[data-claim-id]")
        ids = [b["data-claim-id"] for b in blocks]
        wanted = [c["id"] for c in expected]
        if Counter(ids) != Counter(wanted) or (j["status"] != "none_identified" and ids != wanted):
            errors.append(f"{j['id']}: claims {ids}, expected {wanted} once each, facts in authored order")
        for c in expected:
            block = next((b for b in blocks if b["data-claim-id"] == c["id"]), None)
            if block is None:
                continue
            line = block.select_one(".claim-text")
            if line is None:
                errors.append(f"{c['id']}: missing claim sentence")
                continue
            plain = copy.copy(line)
            for extra in plain.select(".cites, .claim-date"):
                extra.decompose()
            text = " ".join(plain.get_text().split())
            if c["kind"] == "development":
                expected_text = f"On {expected_date(c['date'])} {c['act']}, which {c['text'].rstrip('.')}."
            else:
                expected_text = " ".join(c["text"].replace("“", '"').replace("”", '"').split())
            if text != expected_text:
                errors.append(f"{c['id']}: sentence {text!r}, expected {expected_text!r}")
            if c is lead:
                bindings = claim_bindings(data, c["id"])
                if len(bindings) == 1 and bindings[0][2].get("gloss"):
                    quotes = line.select(".quote-text[aria-description]")
                    if len(quotes) != 1 or quotes[0].get("aria-description") != bindings[0][2]["text"]:
                        errors.append(f"{c['id']}: translated lead lacks its bound original")
            fields = ("text", "act", "date") if c["kind"] == "development" else ("text",)
            cited = {n.get("data-source-id") for n in line.select("a.cn")}
            want_sources = {s for leaf in fields for s in c.get("citations", {}).get(leaf, [])}
            if not cited or cited != want_sources:
                errors.append(f"{c['id']}: citations {cited}, expected {want_sources}")
            folds = block.select("details.quote")
            if len(folds) != 1 or folds[0].get("data-quote-claim") != c["id"]:
                errors.append(f"{c['id']}: missing or mismatched quote disclosure")
            elif (label := (folds[0].find("summary", recursive=False) or {}).get('aria-label')) != (want_label := expected_quote_label(data, c["id"])):
                errors.append(f"{c['id']}: Quote label {label!r}, expected {want_label!r}")
            if c["kind"] == "function" and c.get("expired") and c is not lead:
                warning = block.select_one(":scope > .reread")
                if warning is None or not warning.get_text(strip=True):
                    errors.append(f"{c['id']}: expired role lacks its re-read warning")
        for fold in card.select("details.quote"):
            summary = fold.find("summary", recursive=False)
            if fold.has_attr("open") or not summary or not summary.get_text(strip=True) or len(fold.find_all(recursive=False)) != 1 or fold.get_text() != summary.get_text():
                errors.append(f"{j['id']}: quote content is saved or disclosure is open/empty")
        if card.select(".quote-body, .quote-excerpt, .quote-anchor"):
            errors.append(f"{j['id']}: quote content is in the saved page")
        if j["status"] == "none_identified" and "claims" not in j and card.select(".claim, details.quote"):
            errors.append(f"{j['id']}: legacy card gained claims")
        for body in card.select("[data-body-id]"):
            actual = [n["data-claim-id"] for n in body.select(".claim")]
            want = [c["id"] for c in expected if c.get("body_id") == body["data-body-id"] and c is not lead]
            if actual != want:
                errors.append(f"{j['id']}: body claims out of authored order: {actual}, expected {want}")
    return errors


def claim_quote_fixture(data: dict) -> tuple[dict, list[str]]:
    """One status card and a migrated card, even before a data migration reaches this branch.

    Synthetic quotes cover two anchors, mixed translations, all development leaves, a retained expired lead,
    a closest body's different display name, authority roles, other bodies and a marked expired role.
    """
    fixture = copy.deepcopy(data)
    status = min((j for j in fixture["jurisdictions"] if j["status"] != "none_identified"), key=lambda j: j["id"])
    migrated = min((j for j in fixture["jurisdictions"] if j["status"] == "none_identified" and j.get("europe_scope")), key=lambda j: j["id"])
    source_id = "fixture-claim-source"
    state = {"state": "source_gone", "last_found": "2026-09-01", "since": "2026-09-02"}
    source = {"id": source_id, "url": "https://example.org/claim-fixture", "title": "Synthetic claim source", "publisher": "Fixture", "language": "de", "excerpts": []}
    fixture["sources"].append(source)
    anchor_source = {"id": "fixture-anchor-source", "url": "https://example.org/anchor-fixture", "title": "Synthetic unit membership", "language": "fr"}
    fixture["sources"].append(anchor_source)

    def add(record, kind, text, **extra):
        claim = {"id": f"fixture-claim-{record['id']}-{len(record.setdefault('claims', []))}", "kind": kind,
                 "text": text, "source_ids": [source_id, anchor_source["id"]], "citations": {leaf: [source_id] for leaf in ("text", "act", "date")}, **extra}
        record["claims"].append(claim)
        ri = fixture["jurisdictions"].index(record)
        pointer = f"/jurisdictions/{ri}/claims/{len(record['claims']) - 1}"
        source["excerpts"].append({"text": "Das Institut prüft Modelle.", "gloss": "The institute tests models.", "supports": [pointer + "/text"], "reread": state,
                                   "anchors": [{"kind": "heading", "text": "Aufgaben", "gloss": "Tasks"}, {"kind": "definition", "text": "The Institute"},
                                               {"kind": "membership", "text": "Le laboratoire fait partie de l'institut.", "gloss": "The lab is part of the institute.", "source_id": anchor_source["id"]}]})
        if kind == "development":
            source["excerpts"].append({"text": "The government adopted the instrument on 1 September 2026.",
                                       "supports": [pointer + "/act", pointer + "/date"], "reread": state,
                                       "anchors": [{"kind": "provision", "text": "Artikel 1", "gloss": "Article 1"}]})
        return claim

    add(status, "fact", "A synthetic fact is recorded.")
    migrated["claims"] = []
    lead = add(migrated, "function", 'It says it "tests models".', body_id="fixture-closest", lead=True, expired=True, reread=state)
    add(migrated, "function", "It publishes research.", body_id="fixture-closest")
    add(migrated, "function", "It supports regulators.", body_id="fixture-authority")
    add(migrated, "function", "It trains people.", body_id="fixture-other")
    add(migrated, "function", "This expired role stays marked.", body_id="fixture-other", expired=True, reread={**state, "state": "quote_gone"})
    add(migrated, "development", "assigns model testing", act="the government adopted the instrument", date="2026-09-01")
    migrated["closest_body"] = {"name": "Fixture closest body", "body_id": "fixture-closest", "stage": "exists", "display": {"source_links": [source_id]}}
    migrated["related_bodies"] = [{"id": bid, "name": name, "kind": kind, "display": {"source_links": [source_id]}, "source_ids": [source_id],
                                  "authority_roles": ["single_point_of_contact"], "designation_state": "designated", "designation_basis": "law"}
                                 for bid, name, kind in [("fixture-closest", "Different legal name", "research"), ("fixture-authority", "Fixture authority", "ai_act_authority"), ("fixture-other", "Fixture other body", "research")]]
    migrated["summary"] = ('No qualifying institute identified as of November 2026. Closest body: Fixture closest body. '
                            + lead["text"] + ' Source offline since 2 September 2026; last read 1 September 2026.')
    return fixture, [status["id"], migrated["id"]]


def multiline_quotes(data: dict) -> list[dict]:
    return [dict(span, key=f"{source['id']}/{i}/{n}")
            for source in data['sources'] for i, excerpt in enumerate(source.get('excerpts', []))
            for n, span in enumerate([excerpt, *excerpt.get('anchors', [])])
            if any(re.search(r'[\r\n]', span.get(field, '')) for field in ('text', 'gloss'))]


QUOTE_BREAKS_JS = r"""spans => {
  const errors=[], host=document.createElement('div');host.className='quote-body';document.body.appendChild(host);
  for(const span of spans) {
    const quote=window.quoteNode(span.text,span.gloss), expected='"'+(span.gloss || span.text)+'"';host.appendChild(quote);
    const shown=quote.querySelector('.quote-text'), breaks=[...shown.querySelectorAll('br')];
    if(shown.textContent!==expected) errors.push(span.key+': stored quote text changed');
    if(breaks.length!==(expected.match(/\r\n|\r|\n/g)||[]).length) errors.push(span.key+': a stored line break was lost');
    // Check real glyph positions on each side of every break, including statutory list items.
    for(const br of breaks) {
      let before=br.previousSibling, after=br.nextSibling;
      while(before && (before.nodeType!==Node.TEXT_NODE || !before.textContent.trim())) before=before.previousSibling;
      while(after && (after.nodeType!==Node.TEXT_NODE || !after.textContent.trim())) after=after.nextSibling;
      if(!before || !after) continue;
      const a=document.createRange(), b=document.createRange();
      const end=before.textContent.trimEnd().length, start=after.textContent.search(/\S/);
      a.setStart(before,end-1);a.setEnd(before,end);b.setStart(after,start);b.setEnd(after,start+1);
      if(b.getBoundingClientRect().top<=a.getBoundingClientRect().top+1) errors.push(span.key+': words on either side of a stored break share a line');
    }
    if(span.gloss && /[\r\n]/.test(span.text)) {
      shown.focus({preventScroll:true});
      const tip=document.getElementById('tip');
      if(!tip || tip.hidden || tip.textContent!==span.text || getComputedStyle(tip).whiteSpace!=='pre-line') errors.push(span.key+': original-language tip loses line breaks');
    }
    quote.remove();
  }
  host.remove();return errors;
}"""


def check_claim_quotes(browser, base_url: str, data: dict) -> list[str]:
    """Open every disclosure on selected real cards and fixture cards, with keyboard and touch originals."""
    from summaries import LANGUAGE_NAMES
    errors = []
    institutes = {i["id"]: i for i in data["institutes"]}
    selected = []
    for status in (True, False):
        candidates = [j for j in data["jurisdictions"] if (j["status"] != "none_identified") == status
                      and (status or j.get("europe_scope")) and card_claims(j, institutes)]
        if candidates:
            selected.append(min(candidates, key=lambda j: j["id"])["id"])
    fixture, fixture_ids = claim_quote_fixture(data)
    for payload, ids in ((data, selected), (fixture, fixture_ids)):
        for touch in (False, True):
            page = browser.new_page(viewport={"width": 390 if touch else 1440, "height": 1000}, has_touch=touch, is_mobile=touch)
            try:
                if payload is fixture:
                    page.route(PAGE_DATA, lambda route: route.fulfill(json=fixture))
                hydrated(page, base_url)
                if payload is fixture:
                    errors.extend(check_claim_cards(page.content(), fixture))
                else:
                    spans = multiline_quotes(data)
                    if not spans:
                        errors.append('no multiline quotation or anchor to check')
                    errors.extend(page.evaluate(QUOTE_BREAKS_JS, spans))
                for jid in ids:
                    card = page.locator(f"#c-{jid.lower()}")
                    card.evaluate("n => { if (n.tagName === 'DETAILS') n.open = true; n.querySelectorAll('details.more').forEach(d => d.open = true); }")
                    from summaries import shown_lead

                    jurisdiction = next(j for j in payload["jurisdictions"] if j["id"] == jid)
                    lead = shown_lead(jurisdiction) if jurisdiction["status"] == "none_identified" else None
                    if lead:
                        bound = claim_bindings(payload, lead["id"])
                        original = bound[0][2] if len(bound) == 1 else None  # text has exactly one binding
                        quote = card.locator(f'.claim[data-claim-id="{lead["id"]}"] > .claim-text .quote-text')
                        if original and original.get("gloss") and (quote.count() != 1 or quote.get_attribute("aria-description") != original["text"]):
                            errors.append(f"{jid}: saved lead does not carry its bound original")
                    for fold in card.locator("details.quote").all():
                        identity = fold.get_attribute("data-quote-claim")
                        expected = claim_bindings(payload, identity)
                        if fold.locator(".quote-body").count():
                            errors.append(f"{identity}: quote loaded before opening")
                        fold.locator("summary").tap() if touch else fold.locator("summary").click()
                        fold.locator(".quote-body").wait_for(state="attached")
                        blocks = fold.locator(".quote-excerpt")
                        if blocks.count() != len(expected) or not expected:
                            errors.append(f"{identity}: excerpt count {blocks.count()}, expected {len(expected)}")
                        for source, ei, excerpt in expected:
                            block = fold.locator(f'.quote-excerpt[data-source-id="{source["id"]}"][data-excerpt-index="{ei}"]')
                            if block.count() != 1:
                                errors.append(f"{identity}: missing bound excerpt {source['id']}/{ei}")
                                continue
                            spans = [excerpt, *excerpt.get("anchors", [])]
                            shown = block.locator(".quote-text").all_text_contents()
                            if shown != ['"' + (span.get("gloss") or span["text"]) + '"' for span in spans]:
                                errors.append(f"{identity}: quote or anchor text differs")
                            failed = (excerpt.get("reread") or {}).get("state") in {"blocked_or_failed", "source_gone", "quote_gone"}
                            if block.locator(".quote-anchor").count() != len(spans) - 1 or bool(block.locator(".reread").count()) != failed:
                                errors.append(f"{identity}: missing anchors, or a quote's re-read line where it has no failed state (or none where it has)")
                            if not block.locator(".quote-anchor").evaluate_all("nodes => nodes.every(n => n.firstChild.nodeType === Node.TEXT_NODE && n.firstChild.textContent.trim())"):
                                errors.append(f"{identity}: anchor kind label is missing")
                            for line, anchor in zip(block.locator(".quote-anchor").all(), excerpt.get("anchors", [])):
                                if "source_id" in anchor:
                                    named = next(s for s in payload["sources"] if s["id"] == anchor["source_id"])
                                    if not line.inner_text().endswith(" (" + (named.get("title_en") or named["title"]) + ")"):  # Naming: English shown
                                        errors.append(f"{identity}: other-copy anchor source title is missing")
                                    if anchor.get("gloss") and (named.get("language") or "") and LANGUAGE_NAMES.get(named["language"], named["language"]) not in (fold.locator("summary").get_attribute('aria-label') or ''):
                                        errors.append(f"{identity}: other-copy anchor language is missing from the Quote label")
                            for node, span in zip(block.locator(".quote-text").all(), spans):
                                if not span.get("gloss"):
                                    if node.get_attribute("aria-description"):
                                        errors.append(f"{identity}: English original gained a translation tooltip")
                                if span.get("gloss") and node.get_attribute("aria-description") != span["text"]:
                                    errors.append(f"{identity}: original differs from the bound span")
                        fold.evaluate("n => n.open = false")
                        fold.locator("summary").tap() if touch else fold.locator("summary").click()
                        if fold.locator(".quote-body").count() != 1:
                            errors.append(f"{identity}: quote duplicated on reopening")
                    # The saved lead has no marker; each loaded fold names all its translated languages once.
                    errors.extend(f"{jid}: {e}" for e in check_quote_translations(BeautifulSoup(card.inner_html(), "html.parser")))
                    for node in card.locator(".quote-text[aria-description]").all():
                        original = node.get_attribute("aria-description")
                        node.scroll_into_view_if_needed()
                        page.keyboard.press("Escape")
                        if touch:
                            node.tap()
                        else:
                            node.focus()
                        tip = page.locator("#tip")
                        if not tip.is_visible() or tip.inner_text() != original:
                            errors.append(f"{jid}: original unreachable by {'touch' if touch else 'keyboard'}")
            finally:
                page.close()
    return errors


def click_control_text(page, control) -> None:
    """Use real mouse-down/up on text: scripted clicks miss the Chrome 155 crash."""
    control.scroll_into_view_if_needed()
    text_point = control.evaluate(
        """control => {
            const text = [...control.childNodes].find(n => n.nodeType === Node.TEXT_NODE && n.textContent.trim());
            if (!text) throw new Error("Control has no direct label text");
            const range = document.createRange();
            range.selectNodeContents(text);
            const r = range.getBoundingClientRect();
            return {x: r.left + r.width / 2, y: r.top + r.height / 2};
        }"""
    )
    page.mouse.move(**text_point)
    page.mouse.down()
    page.evaluate("new Promise(requestAnimationFrame)")
    page.mouse.up()


def interaction_pass(browser, base_url: str, dist: Path, data: dict, screenshots: Path | None, width: int, theme: str) -> list[str]:
    """Layout and behaviour of the hydrated page at one width in one theme, step after step on one page: later steps
    rely on the filters, scroll position and open disclosures that earlier ones leave."""
    errors = []
    label = f"{width}px {theme}"
    page = themed_page(
        browser,
        theme,
        viewport={"width": width, "height": 1000},
    )
    page_errors: list[Exception] = []
    page.on("pageerror", lambda e: page_errors.append(e))

    def require(condition, message, context=label):
        if not condition:
            errors.append(f"{context}: {message}")

    lap = step_clock()  # per-step seconds for the job's timings line (timed_job)
    anchor = (width, theme) == (1440, "light")  # the one state for checks that depend on neither

    hydrated(page, base_url, map_drawn=True)
    page.wait_for_selector("#world-rows tr")
    require(
        page.locator("#headline[role=alert]").count() == 0,
        "render failed",
    )
    page.evaluate("document.fonts.ready")
    settle(page, "(() => { const f = document.getElementById('figures'); if (!f) return false; const r = f.getBoundingClientRect(); return r.width > 0 && r.height > 0 && f.checkVisibility({ visibilityProperty: true }); })()")
    require(
        page.locator("#figures").is_visible(),
        "figures row is hidden",
    )
    lap("load")
    if anchor:
        for error in check_claim_quotes(browser, base_url, data):
            require(False, "claim quotes: " + error)
        lap("claim quotes")
    for error in check_hit_targets(page):
        require(False, error)
    for check in (
        check_baselines_and_rhythm,
        check_citations,
        check_design_roles,
        check_source_footers,
        check_card_grid,
        check_external_markers,
        check_status_contrast,
        check_status_states,
        check_tooltip_cues,
        check_group_row_cover,
    ):
        for error in check(page):
            require(False, error)
    lap("layout and style")
    for error in check_source_keyboard(page):
        require(False, error)
    for error in check_wrapped_rows(page):
        require(False, "wrapped rows: " + error)
    for error in check_fold_block_gaps(page):
        require(False, error)
    for error in check_value_qualifiers(page):
        require(False, error)
    if width == 390:
        page.set_viewport_size({"width": 320, "height": 1000})
        for error in check_controls(page):
            errors.append(f"320px {theme}: {error}")
        page.set_viewport_size({"width": width, "height": 1000})
    for check in (check_controls, check_in_content_controls):
        for error in check(page):
            require(False, error)
    lap("geometry and controls")
    for error in check_status_filter(page, data):
        require(False, error)
    for error in check_funding_weights(page):
        require(False, error)
    for error in check_funding_layout_states(page):
        require(False, error)
    for error in check_label_ladder(page):
        require(False, error)
    for error in check_download_layout(page):
        require(False, error)
    for error in check_version_history(page, data) + check_legend_place(page, width) + check_info_lines(page, width):
        require(False, error)
    lap("filter, weights, ladder, downloads, history")
    # The page's content (facts, counts, sources, rows, names) is checked once, on the saved page, in check.py's
    # content step (tests/content; test strategy D2), not here in every width and theme. What remains reads layout,
    # style or behaviour.
    for error in check_funding_bars(page, data) + check_access_layout(page, width) + check_access_tips(page):
        require(False, error)
    for error in check_archive_wrap(page):
        require(False, error)
    lap("funding bars, access, archive, names")
    require(
        page.locator(".cards").evaluate_all(
            """(grids, statuses) => grids.every(grid => {
          const group = grid.closest('.statusgroup');
          if (grid.closest('.eu-level')) return true;
          return group && group.querySelector(':scope > h3 .st-' + group.dataset.status)
            && [...grid.querySelectorAll('.card')].every(card => statuses[card.dataset.j] === group.dataset.status);
        })""",
            {j["id"]: j["status"] for j in data["jurisdictions"]},
        ),
        "card columns mix statuses or lack a status heading",
    )
    require(
        page.evaluate(
            "document.documentElement.scrollWidth <= innerWidth + 1"
        ),
        "page overflows horizontally",
    )
    # One tooltip system and one external marker: no native title tooltips survive rendering, and no arrow glyph.
    require(
        page.evaluate(
            "() => !document.querySelector('body [title]:not(svg *)')"
        ),
        "a native title tooltip survives rendering",
    )
    # State summaries reach keyboard users: a linked name and an unlinked one both show the tooltip on focus, and Escape dismisses it.
    scroll = page.evaluate("[scrollX, scrollY]")
    page.locator("#world-status").select_option("")
    for name_sel in ("#world-rows td:first-child a[aria-description]", "#world-rows td:first-child strong[aria-description]"):
        name = page.locator(name_sel).first
        if not name.count():
            require(False, f"no state name tooltip for {name_sel}")
            continue
        page.mouse.move(0, 0)  # a pointer left over the page would hover whatever scrolls under it
        name.focus()
        require(
            page.evaluate("s => { const n = document.querySelector(s), t = document.getElementById('tip'); return n === document.activeElement && t && !t.hidden && t.textContent === (n.dataset.tip || n.getAttribute('aria-description')) && !!n.getAttribute('aria-description'); }", name_sel),
            f"state summary tooltip not shown on keyboard focus: {name_sel}",
        )
        page.keyboard.press("Escape")
        require(page.evaluate("() => document.getElementById('tip').hidden"), "Escape does not dismiss the state summary tooltip")
        page.evaluate("document.activeElement.blur()")
    page.locator("#world-status").select_option("pos")
    page.evaluate("([x, y]) => scrollTo(x, y)", scroll)
    page.wait_for_timeout(300)
    page.evaluate("scrollTo(0, 0)")  # the nav strip follows the section in view; at the top it starts at its first item
    settle(page, "document.querySelector('.secnav .measure').scrollLeft === 0")
    require(
        page.evaluate(
            "() => !document.body.textContent.includes('\\u2197') && ![...document.querySelectorAll('[data-tip], [data-tip-sub], [data-exp], [aria-description]')].some(n => [...Object.values(n.dataset), n.getAttribute('aria-description') || ''].some(v => v.includes('\\u2197')))"
        ),
        "an arrow glyph is used instead of the drawn external marker",
    )
    require(
        page.locator(".secnav .measure").evaluate(
            "e => e.scrollLeft === 0"
        ),
        "nav does not start at its first item",
    )
    require(
        page.locator('.secnav a[href="#map"]').evaluate(
            "e => e.getBoundingClientRect().left >= e.parentNode.getBoundingClientRect().left - 1"
        ),
        "first nav item is clipped",
    )
    require(
        page.locator(
            'table.world th[data-col="since"]'
        ).is_visible()
        == (width > 640),
        "phone column defaults are incorrect",
    )
    require(
        page.locator('table.world th[data-col="inst"]').evaluate(
            "e => e.getBoundingClientRect().width >= 175"  # 11rem: the owner tightened minimums so the default table fits at 1100px
        ),
        "Institute column is too narrow",
    )
    for error in check_activity_tooltip(page):
        require(False, error)
    lap("grids, tooltips, nav, columns, activity")
    # The first screen's region switches, dot clicks, search and records do not depend on the theme: in light only.
    for error in check_first_screen(page, data, theme_invariant=theme == "light"):
        require(False, error)
    lap("first screen")
    # The bar shows the page title once the h1 is under it; Back to top reveals the h1 fully.
    page.evaluate(
        "() => { const h = document.querySelector('#top h1'), n = document.querySelector('.secnav'); window.scrollTo(0, h.getBoundingClientRect().bottom + scrollY - n.offsetHeight + 2); }"
    )
    settle(page, "document.querySelector('.secnav').classList.contains('scrolled')")
    require(
        page.locator(".secnav").evaluate(
            "n => n.classList.contains('scrolled')"
        ),
        "nav title does not appear once the h1 scrolls under the bar",
    )
    if width <= 460:  # phones: the five items alone, no title (check_group_rows)
        require(not page.locator(".secnav .navtitle").is_visible(), "nav title shows on a phone")
    else:
        page.locator(".secnav .navtitle").click()
        settle(page, "document.querySelector('#top h1').getBoundingClientRect().top >= document.querySelector('.secnav').getBoundingClientRect().bottom")
        require(
            page.evaluate(
                "() => document.querySelector('#top h1').getBoundingClientRect().top >= document.querySelector('.secnav').getBoundingClientRect().bottom"
            ),
            "Back to top leaves the h1 under the bar",
        )
    page.evaluate("window.scrollTo(0, 0)")
    page.wait_for_timeout(150)

    lap("nav title")
    if screenshots:
        screenshots.mkdir(parents=True, exist_ok=True)
        page.screenshot(
            path=str(
                screenshots / f"after-{width}-{theme}-full.png"
            ),
            full_page=True,
        )
        page.screenshot(
            path=str(
                screenshots / f"after-{width}-{theme}-hero.png"
            )
        )
        capture_sections(
            page, screenshots, f"closed-{width}-{theme}"
        )

    version_link = page.locator('#updated a[href="#changes"]')
    version_link.scroll_into_view_if_needed()
    for state in ("hover", "focus"):
        if state == "hover":
            version_link.hover()
        else:
            page.mouse.move(0, 0)
            page.keyboard.press("Shift")
            version_link.focus()
        require(
            not page.locator("#tip").is_visible(),
            "version link shows a tooltip on " + state + " (its text says where it goes)",
        )
    version_link.click()
    require(
        page.locator("#changes").get_attribute("open") is not None
        and page.evaluate("location.hash") == "#changes",
        "version link does not open the Data changelog",
    )
    page.locator("#changes").evaluate("n => n.open = false")
    if width in (390, 1440) and theme == "light":
        errors.extend(check_news_layout(page, data))
    if page.locator("#development-list > li.earlier").count():
        more = page.locator("#developments-more")
        n = page.locator("#development-list > li.earlier").count()
        after_last = "() => { const items = [...document.querySelectorAll('#development-list > li')].filter(li => !li.hidden), b = document.getElementById('developments-more'); return b.getBoundingClientRect().top >= items.at(-1).getBoundingClientRect().bottom - 1; }"
        require([int(value) for value in re.findall(r"\b\d+\b", more.inner_text())] == [n] and more.get_attribute("aria-expanded") == "false" and page.evaluate(after_last), "developments control is not collapsed after the list with the earlier-item count")
        collapsed_name = more.inner_text().strip()
        more.click()
        require(
            page.locator("#development-list > li.earlier").first.is_visible()
            and bool(more.inner_text().strip()) and more.inner_text().strip() != collapsed_name
            and more.get_attribute("aria-expanded") == "true"
            and page.evaluate(after_last),
            "developments control does not extend the list in place with 'Show fewer' at its end",
        )
        more.click()
        require(not page.locator("#development-list > li.earlier").first.is_visible(), "Show fewer does not collapse the earlier developments")
        # Keyboard: Enter expands and moves focus to the first revealed item in view, below the sticky section bar; Space collapses with focus on the control in view.
        in_view = "n => { const r = n.getBoundingClientRect(), nav = document.querySelector('.secnav').getBoundingClientRect(); return n === document.activeElement && r.top >= Math.max(0, nav.bottom) && r.bottom <= innerHeight; }"
        more.focus()
        page.keyboard.press("Enter")
        require(page.locator("#development-list > li.earlier").first.evaluate(in_view), "keyboard expansion leaves focus off the first revealed development or off-screen")
        more.focus()
        page.keyboard.press("Space")
        require(more.get_attribute("aria-expanded") == "false" and more.evaluate(in_view), "keyboard collapse leaves focus off the control or off-screen")
        page.evaluate("document.activeElement.blur()")
    with page.expect_download() as news_download:
        page.locator("#news-download").click()
    require(
        Path(news_download.value.path()).read_bytes()
        == (dist / "data" / "news.csv").read_bytes(),
        "News download differs from the built news.csv",
    )
    page.evaluate(
        "() => { history.replaceState(null, '', location.pathname); scrollTo(0, 0); }"
    )
    for error in check_rules_render(page):
        require(False, error)
    for error in check_link_states(page):
        require(False, error)

    lap("screenshots, version link, developments, events, rules, links")
    # A heading inside a sticky label stops moving. Its section does not.
    for target in ("map", "testing", "set-up", "funding", "europe", "world"):
        page.locator(
            f'section[aria-labelledby="{target}"]'
        ).evaluate(
            "e => scrollTo(0, e.getBoundingClientRect().top + scrollY + Math.min(220, e.offsetHeight / 2))"
        )
        settle(page, "t => document.querySelector(`.secnav a[data-sections~=\"${t}\"]`).getAttribute('aria-current') === 'location'", target)
        require(
            page.locator(
                f'.secnav a[data-sections~="{target}"]'
            ).get_attribute("aria-current")
            == "location",
            f"scrollspy lags inside {target}",  # the item of its group
        )
        require(
            page.locator(".secnav a.active").evaluate(
                "e => { const r=e.getBoundingClientRect(), p=e.parentNode.getBoundingClientRect(); return r.left >= p.left-1 && r.right <= p.right+1; }"
            ),
            f"active nav link is clipped at {target}",
        )
    page.evaluate(
        "scrollTo(0, document.documentElement.scrollHeight)"
    )
    settle(page, "document.querySelector('.secnav a[data-sections~=\"method\"]').getAttribute('aria-current') === 'location'")
    require(
        page.locator('.secnav a[data-sections~="method"]').get_attribute("aria-current")
        == "location",
        "last section is not active at page bottom",
    )
    page.locator('.secnav a[href="#europe"]').evaluate(
        "e => e.click()"
    )
    settle(page, "document.querySelector('.secnav a[href=\"#europe\"]').getAttribute('aria-current') === 'location'")
    require(
        page.locator('.secnav a[href="#europe"]').get_attribute(
            "aria-current"
        )
        == "location",
        "nav click does not reach the section",
    )

    lap("scrollspy")
    for error in check_design_roles(page):
        require(False, error)

    # At this scroll position the first card citations can be hit-tested.
    for error in check_citations(page):
        require(False, error)

    require(
        page.locator("main abbr, main strong, main a").evaluate_all(
            "nodes => nodes.every(n => n.textContent.trim())"
        ),
        "citation glue left an empty text or focus target",
    )
    require(
        page.locator(".conf").evaluate_all(
            "nodes => nodes.every(n => ['indirect or secondary', 'secondary sources', 'press report', 'our estimate', 'low confidence', 'medium confidence'].includes(n.textContent))"
        ),
        "citation glue split a confidence label",
    )

    lap("design roles, citations, glue")
    # Expanded lists stay in the shared footer track.
    for grid in page.locator(".cards:not(.minis)").all():
        paired = grid.locator(".card-row:has(> .card:nth-child(2))")
        row = paired.first if paired.count() else grid.locator(".card-row").first
        snapshot = "e => ({tops: [...e.querySelectorAll('.top, .row')].map(n => n.getBoundingClientRect().top + scrollY), bottom: e.getBoundingClientRect().bottom + scrollY})"
        before = row.evaluate(snapshot)
        fold = row.locator("details.srcs").first
        fold.evaluate("e => e.open = true")
        after = row.evaluate(snapshot)
        require(
            len(before["tops"]) == len(after["tops"])
            and all(
                abs(a - b) < 1
                for a, b in zip(before["tops"], after["tops"])
            ),
            "opening Sources moves comparison fields",
        )
        require(
            after["bottom"] > before["bottom"],
            "Sources does not grow its comparison row",
        )
        require(
            fold.evaluate(
                "e => e.querySelector('ol').getBoundingClientRect().bottom <= e.closest('.card-row').getBoundingClientRect().bottom + 1"
            ),
            "Sources overflows the shared footer track",
        )
        for error in check_card_grid(page):
            require(False, "expanded comparison row: " + error)
        fold.evaluate("e => e.open = false")
    fund_openers = page.locator('.fund-list[data-kind="budget"] button[data-institute]').all()
    require(bool(fund_openers), 'no Funding record controls to exercise')
    for opener in fund_openers:
        listing = opener.locator('xpath=ancestor::ul[contains(@class,"fund-name-list")]')
        hidden = bool(listing.count() and listing.evaluate('n=>n.hidden'))
        opener.evaluate(REVEAL_FUNDING_LIST_JS)
        opener.evaluate('n=>n.click()')
        require(opener.evaluate(FUNDING_RECORD_PLACEMENT_JS),
                'funding records must open immediately beneath their whole-row control')
        page.locator('.fund-detail[open] .close-control').evaluate('n=>n.click()')
        if hidden:
            listing.evaluate('n=>n.previousElementSibling.click()')

    lap("expanded sources")
    # Production bindings: filters, column preference, CSV, glossary, downloads.
    # Search, the Download menu by keyboard, the filtered CSV, Show all and the region filter do not depend on width or theme: once, at 1440 light. Each popover's place in the viewport is checked per width below (toolbar menus).
    if anchor:
        page.locator("#world-filter").fill("United Kingdom")
        require(
            page.locator("#world-rows > tr").count() == 1,
            "world search fails",
        )
        menu = page.locator("#world-download")
        trigger = menu.locator("summary")
        trigger.focus()
        trigger.press("Enter")
        require(
            menu.get_attribute("open") is not None,
            "Download does not open by keyboard",
        )
        require(
            menu.locator(".popover").evaluate(
                "n => { const r=n.getBoundingClientRect(); return r.left >= 0 && r.right <= innerWidth; }"
            ),
            "Download choices are outside the viewport",
        )
        trigger.press("Tab")
        require(
            page.locator("#world-csv").evaluate(
                "n => n === document.activeElement"
            ),
            "CSV choice is not next in tab order",
        )
        page.keyboard.press("Escape")
        require(
            menu.get_attribute("open") is None
            and trigger.evaluate("n => n === document.activeElement"),
            "Escape does not close Download and restore focus",
        )
        trigger.click()
        page.locator("#world-filter").click()
        require(
            menu.get_attribute("open") is None,
            "outside click does not close Download",
        )
        trigger.click()
        with page.expect_download() as download:
            click_control_text(page, page.locator("#world-csv"))
        require(
            menu.get_attribute("open") is None,
            "CSV choice leaves Download open",
        )
        csv_text = Path(download.value.path()).read_text()
        csv_rows = list(csv.DictReader(io.StringIO(csv_text)))
        for error in check_operating_output(
            page.content(), data, csv_rows
        ):
            require(False, error)
        require(
            len(csv_rows) == 1 and csv_rows[0]["id"] == "GB",
            "filtered CSV export differs from view",
        )
        trigger.click()
        # The link as assistive technology sees it: a role query with a non-empty computed name (it leaves out anything
        # hidden from the accessibility tree), narrowed to the structural target.
        named = re.compile(r"\S")  # a non-empty computed accessible name, however it is given
        data_link = menu.get_by_role("link", name=named).or_(menu.get_by_role("menuitem", name=named)).and_(menu.locator('a[href="#data"]'))
        require(data_link.count() == 1, "Download menu's link to the data section is not one named, accessible link")
        data_link.click()
        require(
            page.evaluate("location.hash") == "#data"
            and menu.get_attribute("open") is None,
            "Full dataset does not close Download and navigate to Data",
        )
        page.locator("#world-count button").click()
        require(
            page.locator("#world-filter").input_value() == "",
            "Show all does not clear search",
        )
        total = sum(
            j["kind"] != "supranational" for j in data["jurisdictions"]
        )
        require(
            page.locator("#world-rows > tr").count() == total
            and page.locator('#world-status option[value=""]').inner_text()
            == f"All ({total})",
            "All status count or table total differs from data",
        )
        region = page.locator(
            '#world-region option[value^="c:"]'
        ).first.get_attribute("value")
        if region is None:
            raise RuntimeError("region filter has no continent option")
        page.locator("#world-region").select_option(region)
        expected_region = {
            j["id"]
            for j in data["jurisdictions"]
            if j["kind"] != "supranational"
            and (j.get("region") or {}).get("name") == region[2:]
        }
        actual_region = set(
            page.locator("#world-rows > tr").evaluate_all(
                "rows => rows.map(r => r.dataset.j)"
            )
        )
        require(
            actual_region == expected_region,
            "region filter differs from data",
        )
        require(
            region[2:]
            in (
                page.locator("#world-region").get_attribute(
                    "aria-label"
                )
                or ""
            ),
            "selected region is absent from accessible label",
        )
    page.locator("#world-region").select_option("")
    page.locator("#world-status").select_option("none_identified")
    expected_none = sum(
        j["kind"] != "supranational"
        and j["status"] == "none_identified"
        for j in data["jurisdictions"]
    )
    require(
        page.locator("#world-rows > tr").count() == expected_none,
        "status filter fails",
    )
    for error in check_structural_columns(page, True):
        require(False, "None identified: " + error)
    page.locator("#world-status").select_option("pos")
    for error in check_structural_columns(page, False):
        require(False, "default status view: " + error)
    page.locator("details.cols > summary").click()
    require(
        page.locator("#col-opts").evaluate(
            "e => { const r=e.getBoundingClientRect(); return r.left >= 0 && r.right <= innerWidth; }"
        ),
        "Columns menu is outside the viewport",
    )
    since = (
        page.locator("#col-opts label")
        .filter(has_text="Status since")
        .locator("input")
    )
    since.set_checked(width <= 640)
    require(
        page.locator(
            'table.world th[data-col="since"]'
        ).is_visible()
        == (width <= 640),
        "Columns cannot override defaults",
    )
    page.locator("details.cols > summary").click()
    hint = page.locator("#world-scroll-hint")
    overflow = page.locator("#world-scroll").evaluate("e => e.scrollWidth > e.clientWidth + 1")
    require(hint.is_visible() == overflow, "scroll hint does not reflect table overflow")
    if hint.is_visible():
        require(bool(hint.inner_text().strip()), "visible scroll hint is empty")
    if width <= 640:
        require(
            page.locator(".dlist li").first.evaluate(
                "e => e.querySelector('.d').getBoundingClientRect().top >= e.querySelector('.dl-title').getBoundingClientRect().bottom"
            ),
            "phone download description is not below its title",
        )
    term = page.locator("abbr.g").filter(visible=True).first
    term.scroll_into_view_if_needed()
    page.evaluate("new Promise(requestAnimationFrame)")
    # The glossary tip on focus and Escape: once, at 1440 light; its placement (clamped at narrow widths) runs in every state.
    if anchor:
        term.focus()
        require(
            page.locator("#tip").is_visible(),
            "glossary tooltip does not open on focus",
        )
        page.keyboard.press("Escape")
        require(
            not page.locator("#tip").is_visible(),
            "tooltip does not dismiss with Escape",
        )
    # Tooltip geometry after a previous tooltip elsewhere: 4px from the trigger, centred unless clamped.
    # A tooltip left near the right edge must not narrow the next one during measurement.
    page.evaluate("() => { const t = document.getElementById('tip'); t.style.left = (document.documentElement.clientWidth - 40) + 'px'; }")
    page.mouse.move(0, 0)
    term.scroll_into_view_if_needed()
    term.hover()
    bad_tip = page.evaluate(
        """t => {
            const tip = document.getElementById('tip');
            const r = t.getBoundingClientRect(), b = tip.getBoundingClientRect();
            const vw = document.documentElement.clientWidth;
            const below = Math.abs(b.top - (r.bottom + 4)) < 1.5, above = Math.abs(r.top - 4 - b.bottom) < 1.5 || Math.abs(b.top - 4) < 1.5;
            const centred = Math.abs((b.left + b.right) / 2 - (r.left + r.right) / 2) < 1.5;
            const clamped = Math.abs(b.left - 16) < 1.5 || Math.abs(b.right - (vw - 16)) < 1.5;
            return tip.hidden ? 'hidden' : (below || above) && (centred || clamped) ? '' : JSON.stringify({r, b});
        }""",
        term.element_handle(),
    )
    require(not bad_tip, "tooltip is not 4px from its trigger and centred or clamped: " + bad_tip)
    # The aisi.json download and the funding counts do not depend on width or theme: once, at 1440 light.
    if anchor:
        with page.expect_download() as download:
            page.locator('.dlist a[download="aisi.json"]').click()
        require(
            json.loads(Path(download.value.path()).read_text())[
                "dataset"
            ]["version"]
            == data["dataset"]["version"],
            "raw dataset download differs from build",
        )
        require(
            all(page.locator(f'#fund-rows .fund-list[data-kind="{kind}"] button[data-institute]').count() == len(established_funding_institutes(data)) for kind in ('budget', 'staff')),
            "funding section omits an established institute",
        )

        for record_type in ("funding", "staff"):
            expected = sum(
                len(i.get(record_type, []))
                for i in established_funding_institutes(data)
            )
            require(
                page.locator(f"[data-record={record_type}]").count()
                == expected,
                f"funding folds lost {record_type} records",
            )
    lap("filters, menus, CSV, glossary")
    page.locator("details").evaluate_all(
        "nodes => nodes.forEach(n => { if (!n.closest('.toolbar-actions')) n.open = true; })"
    )
    require(
        page.locator(
            "details:not(.cols):not(.download-menu)"
        ).evaluate_all(
            "nodes => nodes.every(n => [n, ...n.querySelectorAll('*')].every(e => getComputedStyle(e).userSelect !== 'none'))"
        ),
        "descriptive disclosure text is not selectable",
    )
    # The expanded content's geometry, text placement and keyboard order do not depend on the theme: in light only
    # (3 widths). Its colours (design roles, swatches, source footers, external markers, controls) are checked in both themes.
    if theme == "light":
        for error in check_wrapped_rows(page):
            require(False, "expanded wrapped rows: " + error)
        for error in check_hit_targets(page):
            require(False, "expanded content: " + error)
    for error in check_planned_swatches(page):
        require(False, "expanded content: " + error)
    for check in (
        *((check_baselines_and_rhythm, check_citations) if theme == "light" else ()),
        check_design_roles,
        check_source_footers,  # compares each footer's colour with the theme's --muted, so in both themes
        *((check_card_grid,) if theme == "light" else ()),
        check_external_markers,
        check_text_contrast,  # every text against its real background, every fold open
    ):
        for error in check(page):
            require(False, "expanded content: " + error)
    if theme == "light":
        for error in check_source_keyboard(page):
            require(False, "expanded content: " + error)
    for error in check_controls(page):
        require(False, "expanded content: " + error)
    # Toolbar disclosures are exclusive, so measure each separately.
    for menu_width in (320, width) if width == 390 else (width,):
        page.set_viewport_size(
            {"width": menu_width, "height": 1000}
        )
        for selector in ("details.cols", "#world-download"):
            disclosure = page.locator(selector)
            disclosure.locator("summary").click()
            require(
                disclosure.get_attribute("open") is not None,
                f"{selector}: menu did not open",
            )
            require(
                disclosure.locator(".popover").evaluate(
                    "n => getComputedStyle(n).userSelect === 'none'"
                ),
                f"{selector}: control popover allows text selection",
            )
            require(
                disclosure.locator(".popover").evaluate(
                    "n => { const r=n.getBoundingClientRect(); return r.left >= 0 && r.right <= innerWidth; }"
                ),
                f"{selector} at {menu_width}px: popover is outside the viewport",
            )
            if selector == "details.cols":
                checkbox = disclosure.locator("input").first
                was_checked = checkbox.is_checked()
                choice = disclosure.locator("label").first
                click_control_text(page, choice)
                require(
                    checkbox.is_checked() != was_checked
                    and disclosure.get_attribute("open")
                    is not None,
                    "clicking Columns label text fails or closes the menu",
                )
                click_control_text(page, choice)
            for check in (
                check_hit_targets,
                check_controls,
            ):
                for error in check(page):
                    require(
                        False,
                        f"{selector} at {menu_width}px: {error}",
                    )
            last_choice = disclosure.locator(
                "input, button, a"
            ).last
            last_choice.focus()
            last_choice.press("Tab")
            require(
                disclosure.get_attribute("open") is None,
                f"{selector}: tabbing away leaves menu open",
            )
    require(
        page.locator("table.world th:visible").evaluate_all(  # the sortable table's headers; the functions table has none
            "nodes => nodes.every(th => { const b=th.querySelector('button').getBoundingClientRect(); return Math.abs(th.clientWidth-b.width)<2 && Math.abs(th.clientHeight-b.height)<2; })"  # inside the header rule
        ),
        "sort button does not fill its header cell",
    )
    if screenshots:
        page.screenshot(
            path=str(screenshots / f"expanded-{width}-{theme}.png"),
            full_page=True,
        )
        capture_sections(
            page, screenshots, f"expanded-{width}-{theme}"
        )
    for browser_error in page_errors:
        require(False, f"JavaScript error: {browser_error}")
    page.close()
    lap("expanded content, toolbar menus, rest")
    return errors


# pinFade(node): pause a jumped-to heading's focus-fade at its start (the tint held), so a check reads the tint at a
# fixed point of the fade, not whenever it happens to run; true when there was a fade to pin.
PIN_FOCUS_FADE = """
  const pinFade = n => { const a = n.getAnimations().find(a => a.animationName === 'focus-fade'); if (a) { a.pause(); a.currentTime = 0; } return !!a; };
"""


def shared_links(browser, base_url: str) -> list[str]:
    """Section links and shared #links on a tall page, then the stylesheet at 320 px and its older-browser fallback."""
    errors = []
    # Short final sections must retain an explicit nav choice at a clamped scroll position.
    page = browser.new_page(viewport={"width": 1440, "height": 1600}, reduced_motion="no-preference")  # the fade is read at its start
    page_errors = []
    page.on("pageerror", lambda e: page_errors.append(e))
    hydrated(page, base_url)
    page.wait_for_selector("#world-rows tr")
    page.locator('.secnav a[href="#data"]').evaluate("e => e.click()")
    settle(page, "document.querySelector('.secnav a[href=\"#data\"]').getAttribute('aria-current') === 'location' && document.activeElement.id === 'data'")
    if (
        page.locator('.secnav a[href="#data"]').get_attribute(
            "aria-current"
        )
        != "location"
    ):
        errors.append(
            "tall viewport: clicking Data selects another section"
        )
    if page.evaluate("document.activeElement.id") != "data":
        errors.append("section navigation does not transfer keyboard focus")
    # Focus: a jump focuses the section heading with the tint and no ring; keyboard focus on text controls is the tint plus a 2px
    # underline in the focus colour at 3:1 or more on the ground, in both themes (principle: Focus).
    for scheme in ("light", "dark"):
        set_theme(page, scheme)
        page.locator('.secnav a[href="#functions"]').focus(timeout=5_000)
        page.keyboard.press("Enter")
        settle(page, "document.activeElement.id === 'functions'")
        # The tint is read at the start of its fade, paused there: sampled later, a slow run would see it faded.
        got = page.evaluate("() => {" + CONTRAST_JS + PIN_FOCUS_FADE + " const n = document.activeElement, faded = pinFade(n), c = getComputedStyle(n); return [n.id, n.tagName, c.outlineStyle, rgb(c.backgroundColor).join() === rgb(token('--highlight-fill')).join(), faded]; }")
        if got != ["functions", "H2", "none", True, True]:
            errors.append(f"{scheme}: after a jump to Functions the focused heading is {got} (expected the #functions h2 with the tint, no outline and its fade)")
        # The palette's Definition destination is a paragraph, not a control: the same tint, no ring.
        got = page.evaluate("() => {" + CONTRAST_JS + PIN_FOCUS_FADE + " const n = document.getElementById('definition'); n.tabIndex = -1; n.focus({ preventScroll: true }); pinFade(n); const c = getComputedStyle(n); return [c.outlineStyle, rgb(c.backgroundColor).join() === rgb(token('--highlight-fill')).join()]; }")
        if got != ["none", True]:
            errors.append(f"{scheme}: the focused Definition paragraph shows {got} (expected the tint and no outline)")
        for selector in ('.secnav a[href="#news"]', '#world-rows td:first-child a'):
            page.keyboard.press("Shift")
            page.locator(selector).first.focus(timeout=5_000)
            look = page.evaluate("s => {" + CONTRAST_JS + """
              const n = document.activeElement, c = getComputedStyle(n), ground = token('--ground');
              // The tint is the --highlight-fill token, the underline the --focus token, and the underline holds 3:1 against the tint it sits on.
              return { visible: n.matches(':focus-visible'), outline: c.outlineStyle, tint: rgb(c.backgroundColor).join() === rgb(token('--highlight-fill')).join(), line: c.textDecorationLine,
                colour: rgb(c.textDecorationColor).join() === rgb(token('--focus')).join(), thick: parseFloat(c.textDecorationThickness), edge: ratio(c.textDecorationColor, c.backgroundColor) }; }""", selector)
            if not (look["visible"] and look["outline"] == "none" and look["tint"] and look["colour"] and "underline" in look["line"] and look["thick"] >= 2 and look["edge"] >= 3):
                errors.append(f"{scheme}: keyboard focus on {selector} is not the tint with a 2px focus-colour underline at 3:1: {look}")
        # Region tabs (owner, 5 Nov 2026): focus is the tint alone; the underline marks the selected tab only, focused or not.
        for pressed in ("false", "true"):
            page.keyboard.press("Shift")
            page.locator(f'.region-chips button[aria-pressed="{pressed}"]').first.focus(timeout=5_000)
            look = page.evaluate("() => {" + CONTRAST_JS + """
              const n = document.activeElement, c = getComputedStyle(n);
              return { visible: n.matches(':focus-visible'), outline: c.outlineStyle, tint: rgb(c.backgroundColor).join() === rgb(token('--highlight-fill')).join(),
                line: c.textDecorationLine, inset: /inset/.test(c.boxShadow) }; }""")
            if not (look["visible"] and look["outline"] == "none" and look["tint"] and look["line"] == "none" and look["inset"] == (pressed == "true")):
                errors.append(f"{scheme}: a focused {'selected' if pressed == 'true' else 'unselected'} region tab is not the tint {'with' if pressed == 'true' else 'without'} the selection underline: {look}")
    # Without motion the jumped-to heading keeps the tint while focused and has no fade.
    page.emulate_media(reduced_motion="reduce")
    page.locator('.secnav a[href="#data"]').focus(timeout=5_000)
    page.keyboard.press("Enter")
    settle(page, "document.activeElement.id === 'data'")
    got = page.evaluate("() => {" + CONTRAST_JS + " const n = document.activeElement, c = getComputedStyle(n); return [n.id, rgb(c.backgroundColor).join() === rgb(token('--highlight-fill')).join(), n.getAnimations().some(a => a.animationName === 'focus-fade')]; }")
    if got != ["data", True, False]:
        errors.append(f"reduced motion: after a jump to Data the focused heading is {got} (expected #data with the tint and no fade)")
    page.emulate_media(reduced_motion="no-preference")
    set_theme(page, "light")
    for target in ("c-fr", "world", "funding", "method", "changes"):
        hydrated(page, base_url + "#" + target)
        page.wait_for_selector("#world-rows tr")
        page.evaluate("document.fonts.ready")
        page.wait_for_timeout(100)
        visible_target = page.locator("#" + target).evaluate(
            "e => { const t=e.tagName === 'H2' ? e.closest('section') : e; const r=t.getBoundingClientRect(); return r.top >= document.querySelector('.secnav').getBoundingClientRect().bottom - 1 && r.top < innerHeight; }"
        )
        if not visible_target:
            errors.append(
                f"shared link #{target} does not arrive below the nav"
            )
    # Test the shipped stylesheet at narrow width before mutating it
    # for the older-browser palette fallback check.
    page.set_viewport_size({"width": 320, "height": 1000})
    page.locator("details.srcs").evaluate_all(
        "nodes => nodes.forEach(n => n.open = true)"
    )
    page.evaluate("new Promise(requestAnimationFrame)")
    for theme in ("light", "dark"):
        set_theme(page, theme)
        errors.extend(check_external_markers(page))
    try:
        page.emulate_media(forced_colors="active")
        errors.extend(check_external_markers(page))
    finally:
        page.emulate_media(forced_colors="none")
    errors.extend(check_light_fallback(page))
    errors.extend(
        f"shared-link JavaScript error: {error}" for error in page_errors
    )
    page.close()
    return errors


def prefixed(prefix: str, check, browser, base_url: str, *args) -> list[str]:
    return [prefix + error for error in check(browser, base_url, *args)]


@contextmanager
def chrome_session(chrome: str, dist: Path):
    """A local server for the built site and headless Chrome with fresh preferences: what every browser job runs
    against. Playwright is imported here so the DOM helpers and their unit tests stay usable without it."""
    from playwright.sync_api import sync_playwright

    handler = partial(QuietHandler, directory=str(dist))
    with ThreadingHTTPServer(("127.0.0.1", 0), handler) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with sync_playwright() as playwright:
                browser = LocalisedBrowser(playwright.chromium.launch(executable_path=chrome, headless=True, env=browser_env()))
                try:
                    yield browser, f"http://127.0.0.1:{server.server_port}/"
                finally:
                    browser.close()
        finally:
            server.shutdown()
            thread.join(timeout=5)


# Functions table (Compare): independent oracles use the complete published data, never page claims' indices.
FN_NAMES = {"activity": "Active", "mandate": "Assigned", "both": "Active",
            "not_found": "not found", "not_coded": "not yet coded"}
FN_STATUS_WORD = re.compile(r"\b(?:planned|proposed|exploring|announced)\b", re.I)


def function_display_name(institute: dict) -> str:
    """Full recorded name, with only a trailing parenthesised status omitted in this display."""
    return re.sub(r"\s*\((?:planned|proposed|exploring|announced)\)$", "", institute["name"], flags=re.I)


def function_month(day: str) -> str:
    return f"{calendar.month_abbr[int(day[5:7])]} {day[:4]}" if len(day) > 4 else day


def function_summary(vocab: dict, counts: dict, total: int) -> str:
    """Independent count buckets: vocabulary order within each tie, never split the third-place tie."""
    buckets = defaultdict(list)
    for code in vocab:
        if counts[code]:
            buckets[counts[code]].append(vocab[code]["short"])
    if not buckets:
        return ""
    def joined(names):
        return " and ".join(names) if len(names) < 3 else ", ".join(names[:-1]) + " and " + names[-1]
    top = max(buckets)
    if len(buckets[top]) > 3:
        return f"Most common: {joined(buckets[top])} ({top} of {total} institutes each)."
    named = []
    for count in sorted(buckets, reverse=True):
        if len(named) + len(buckets[count]) > 3:
            break
        for name in buckets[count]:
            named.append(f"{name} ({count}" + (f" of {total} institutes)" if not named else ")"))
    return f"Most common: {joined(named)}."


def function_rows(data: dict) -> list[dict]:
    states = {j["id"]: j for j in data["jurisdictions"]}
    return sorted(data["institutes"], key=lambda i: (states[i["jurisdiction_id"]]["status"] != "established",
                                                   int((states[i["jurisdiction_id"]].get("status_since") or "9999")[:4]),  # no date: last in its block
                                                   states[i["jurisdiction_id"]]["name"], i["id"]))



FN_GROUP_LABELS = {"Testing and science": "Testing and science", "Rules": "Rules and standards", "Informing government": "Informing government", "Ecosystem": "Ecosystem and adoption"}


def fn_group_short_name(group: str) -> str:
    """The phone head's form of a group label: "&" for "and", nothing else (owner, 18 Nov 2026)."""
    return fn_group_name(group).replace(" and ", "\u00a0& ", 1)  # a no-break space keeps "Testing &" on one line


def fn_group_name(group: str) -> str:
    """Short page labels (owner, 18 Nov 2026); unknown groups keep their recorded names."""
    return FN_GROUP_LABELS.get(group, group)

def function_columns(data: dict) -> list[dict]:
    """All current codes, in first-occurring group order then vocabulary order, independent of carriers."""
    vocab = {c: v for c, v in data["dataset"]["vocabularies"]["function"].items() if not v.get("deprecated")}
    return [{"name": group, "codes": [c for c, v in vocab.items() if v["group"] == group]}
            for group in dict.fromkeys(v["group"] for v in vocab.values())]


def function_values(institutes: list[dict], codes) -> dict:
    """A missing row is not yet coded, never an implicit not-found result."""
    result = {}
    for institute in institutes:
        rows = {row["code"]: row for row in institute.get("functions", [])}
        result[institute["id"]] = {code: rows.get(code, {"code": code, "value": "not_coded", "claim_ids": []}) for code in codes}
    return result


def function_quotes(data: dict) -> dict:
    """Bind by the original claim position, independently of the page projection."""
    pointers = defaultdict(list)
    for source in data.get("sources", []):  # a fixture without sources has no function claims to bind
        for excerpt in source.get("excerpts", []):
            for pointer in excerpt.get("supports", []):
                pointers[pointer].append({"excerpt": excerpt, "source": source})
    result = {}
    for n, institute in enumerate(data["institutes"]):
        for k, claim in enumerate(institute.get("claims", [])):
            if claim["kind"] == "function":
                bound = pointers[f"/institutes/{n}/claims/{k}/text"]
                if len(bound) != 1:
                    raise ValueError(f"function claim {claim['id']}: expected one bound quote, got {len(bound)}")
                result[claim["id"]] = {"claim": claim, **bound[0]}
    return result


def function_status_parts(row: dict, quotes: dict) -> list[str]:
    """Assignment date comes from the oldest dated bound mandate, not another cited source."""
    claims = [quotes[cid] for cid in row["claim_ids"]]
    mandates = [q for q in claims if q["claim"]["basis"] == "mandate"]
    dates = [q["source"]["published"] for q in mandates if q["source"].get("published")]
    parts = ["assigned" + (" " + function_month(min(dates)) if dates else "")] if mandates else []
    if any(q["claim"]["basis"] == "activity" and not q["claim"].get("expired") for q in claims):
        parts.append("active" + (" since at least " + function_month(row["since"]) if row.get("since") else ""))
    return parts if row["value"] in ("mandate", "activity", "both") else [FN_NAMES[row["value"]].lower()]


def functions_structure(html: str, data: dict) -> list[str]:
    """Saved and hydrated table: order, values, accessible controls and absence of pre-rendered evidence."""
    from reader_text import functions_copy_errors, status_parentheses

    soup = BeautifulSoup(html, "html.parser")
    section = soup.select_one('section[aria-labelledby="functions"]')
    if section is None:
        return ["Functions section is missing"]
    errors = functions_copy_errors(html)
    def need(ok, message):
        if not ok:
            errors.append(message)
    institutes = function_rows(data)
    states = {j["id"]: j for j in data["jurisdictions"]}
    vocab = {c: v for c, v in data["dataset"]["vocabularies"]["function"].items() if not v.get("deprecated")}
    lookup = function_values(institutes, vocab)
    quotes = function_quotes(data)
    counts = {code: sum(lookup[i["id"]][code]["value"] in ("mandate", "activity", "both") for i in institutes) for code in vocab}
    grouped = function_columns(data)
    groups = [group["name"] for group in grouped]
    columns = [code for group in grouped for code in group["codes"]]
    group = section.parent
    need(group.select_one(":scope > section") == section, "Functions is not first in Compare")
    nav = group.select_one('.group-nav a')
    need(nav and nav.get("href") == "#functions", "Compare group row does not start with Functions")
    need(soup.select_one('.secnav a[href="#functions"][data-sections="functions testing set-up funding"]'), "Compare bar target differs")
    headers = section.select('.fn-table thead th[data-code]')
    need([h.get("data-code") for h in headers] == columns, "Functions columns/order differ")
    need(len(section.select('.fn-table thead th[scope="col"]')) == len(columns) + 1, "Functions column scopes/count differ")
    for n, (header, code) in enumerate(zip(headers, columns)):
        v = vocab[code]
        heading = header.select_one('abbr.g[tabindex="0"]')
        need(heading and heading.get_text() == v["short"][:1].upper() + v["short"][1:] and tip_of(heading) == f"{v['label']}: {v['definition']}", f"{code}: function label/test differs")
        need(("fn-group-start" in header.get("class", [])) == (n > 0 and v["group"] != vocab[columns[n - 1]]["group"]), f"{code}: head group gap differs")
    group_heads = section.select('.fn-table th[scope="colgroup"]')
    need([h.get_text() for h in group_heads] == [fn_group_name(g) for g in groups], "Functions vocabulary groups/order differ")
    for header, group_name in zip(group_heads, groups):
        need(header.get("colspan") == str(sum(vocab[c]["group"] == group_name for c in columns)), f"{group_name}: column group span differs")
    group_rows = section.select('.fn-group th')
    need([h.get_text() for h in group_rows] == ["Established", "Not yet established"], "Functions not-yet-established blocks/order differ")
    need(len(section.select('.fn-table > tbody')) == 2, "Functions needs two row blocks")
    need(all(h.get("scope") == "rowgroup" and h.find_parent("tbody") and h.get("colspan", "1") == "1" for h in group_rows), "Functions group scope/span differs")
    for header in group_rows:
        cells = header.parent.select(':scope > td[data-code]')
        need([td.get("data-code") for td in cells] == columns, "Functions block band columns/order differ")
        for n, (td, code) in enumerate(zip(cells, columns)):
            need(td.get("aria-hidden") == "true" and not td.get_text() and not td.find(True), f"{code}: block band cell is not decorative/empty")
            need("fn-column" in td.get("class", []) and ("fn-group-start" in td.get("class", [])) ==
                 (n > 0 and vocab[code]["group"] != vocab[columns[n - 1]]["group"]), f"{code}: block band group gap differs")
    colgroups = section.select('.fn-table > colgroup')
    need([cg.get("span", "1") for cg in colgroups] == ["1"] + [str(sum(vocab[c]["group"] == g for c in columns)) for g in groups] + ["1"], "Functions column groups do not match the group heads")
    for tr in section.select('.fn-table thead tr, .fn-table tbody tr'):
        pads = tr.select(":scope > .fn-pad")
        last = tr.find_all(["td", "th"], recursive=False)[-1:]
        need(len(pads) == 1 and last == pads and pads[0].name == "td" and pads[0].get("aria-hidden") == "true" and not pads[0].get_text(strip=True) and not pads[0].find(True),
             "Functions spacer column: each head and body row ends with one empty aria-hidden td.fn-pad")
    rows = section.select('.fn-table tbody tr[data-institute]')
    need([r.get("data-institute") for r in rows] == [i["id"] for i in institutes], "Functions rows/order differ")
    wanted_counts = Counter(lookup[i["id"]][c]["value"] for i in institutes for c in columns)
    need(Counter(td.get("data-value") for td in section.select('.fn-table td[data-value]')) == wanted_counts, "Functions value counts differ")
    for row, institute in zip(rows, institutes):
        place = states[institute["jurisdiction_id"]]["name"]
        header = row.select_one('th[scope="row"]')
        heading = header.select_one('abbr.g[tabindex="0"]') if header else None
        need(header and header.get_text() == place and heading and tip_of(heading) == function_display_name(institute), f"{institute['id']}: row name/institute tip or count differs")
        need(not heading or not FN_STATUS_WORD.search(tip_of(heading)), f"{institute['id']}: status word in row tip")
        preceding = row.find_previous_sibling("tr", class_="fn-group")
        block_name = "Established" if states[institute["jurisdiction_id"]]["status"] == "established" else "Not yet established"
        need(preceding and preceding.get_text() == block_name, f"{institute['id']}: wrong row block")
        cells = row.select(":scope > td:not(.fn-pad)")
        need([td.get("data-code") for td in cells] == columns, f"{institute['id']}: wrong cell count/order")
        for n, (td, code) in enumerate(zip(cells, columns)):
            expected = lookup[institute["id"]][code]
            value = expected["value"]
            key = f"{institute['id']}/{code}"
            need(td.get("data-value") == value, key + ": value differs")
            need(("fn-group-start" in td.get("class", [])) == (n > 0 and vocab[code]["group"] != vocab[columns[n - 1]]["group"]), key + ": body group gap differs")
            button = td.select_one("button.fn-cell")
            if value in ("mandate", "activity", "both"):
                status = ", ".join(function_status_parts(expected, quotes))
                need(button and button.get("type") == "button" and button.get("aria-label") == f"{place}, {vocab[code]['label']}: {status}"
                     and button.get("aria-expanded") == "false" and bool(button.get("aria-controls")), key + ": coded control/name differs")
            else:
                need(not td.select("button") and td.get_text() == FN_NAMES[value] and td.select_one('.vh'), key + ": absent value must be unbuttoned and named")
            if value in ("mandate", "activity", "both"):
                mark = "activity" if value == "both" else value
                need(td.select_one(f'.fn-mark.fn-{mark}[aria-hidden="true"]'), key + ": mark differs")
            else:
                need(not td.select('.fn-mark'), key + ": absent cell has a visible mark element")
            need(not td.select("[data-tip], [data-tip-sub], [data-tip-ext], [data-exp]"), key + ": evidence/tooltip attributes on a cell")
    need(not section.select('.fn-empty'), "Functions obsolete .fn-empty element is present")
    need("No public text found at any institute yet for" not in " ".join(section.get_text(" ", strip=True).split()),
         "Functions obsolete folded-functions sentence is present")
    # Independent data-derived sentence oracle; the implementation lives only in JavaScript.
    summary = section.select_one('.fn-summary')
    marked = [c for c in columns if counts[c]]
    need(bool(summary) == bool(marked), "Functions summary presence differs")
    if marked and summary:
        need(summary.get_text() == function_summary(vocab, counts, len(institutes)), "Functions summary facts differ")
        need(not any(i["name"] in summary.get_text() for i in institutes), "Functions summary names an institute")
    legend = section.select_one('ul.fn-legend.fn-desktop-only[role="list"]')
    legend_values = ["activity", "mandate", "not_found"] + (["not_coded"] if wanted_counts["not_coded"] else [])
    need(legend and [(li.get("data-value"), li.get_text()) for li in legend.select("li[data-value]")] == [(v, "Blank: no public text found; it may still be done" if v == "not_found" else FN_NAMES[v]) for v in legend_values], "Functions legend differs from its two marks and absent values")
    need(not section.select('.fn-intro, .fn-blank'), "Functions obsolete lede or blank sentence is present")
    order = section.select_one('.fn-order')
    need(order and order.get_text() == "Rows: by the year the institute's current status began, oldest first.", "Functions row order explanation missing")
    provisional = section.select_one('.fn-provisional')
    need(provisional and provisional.get_text() == "The list of functions is provisional.", "Functions provisional line missing")
    how = section.select_one('details.fn-how')
    need(how and not how.has_attr("open") and how.select_one('summary') and how.summary.get_text() == "How this is coded",
         "Functions coding fold missing or open by default")
    box = section.select_one('#functions-table')
    foot = box.find_all(recursive=False)[-1] if box else None  # Downloads: the fold sits in the section's foot row, the last block
    need(box and how and foot is how.parent and "section-foot" in (foot.get("class") or []) and foot.select_one(":scope > .section-foot-actions > a.section-dl[download]"),
         "Functions coding fold must follow the table and legends, in the foot row with the download")
    need(not summary or box and summary is box.find(recursive=False), "Functions head must start with the most-common line")
    need(how and order and provisional and how.find_all(recursive=False)[-2:] == [order, provisional]
         and order.parent is how and provisional.parent is how
         and len(section.select('.fn-order, .fn-provisional')) == 2,
         "Functions row order and provisional lines must end the coding fold")
    need(section.get_text().lower().count("provisional") == 1, "Functions provisional caveat repeated")
    need(not status_parentheses(section.get_text(" ")), "Functions status word alone in parentheses")
    need(not FN_STATUS_WORD.search(section.get_text(" ")), "Functions labels contain a status word")
    need(not section.select('.fn-detail-row, .fn-claim, .quote-body, blockquote'), "Functions detail was saved before a click")
    need(not any(text in section.get_text() for q in function_quotes(data).values() for text in (q["excerpt"]["text"], q["excerpt"].get("gloss")) if text), "Functions quote was pre-rendered")
    scroll = section.select_one('.fn-scroll[role="region"][tabindex="0"][aria-labelledby="functions"]')
    need(scroll and scroll.get("aria-describedby") and soup.find(id=scroll["aria-describedby"]), "Functions scroll region lacks its name/hint")
    need(section.select_one('.fn-table caption.vh'), "Functions table lacks its hidden caption")
    return errors


def functions_forms_structure(html: str, data: dict) -> list[str]:
    """Both saved forms and their media-specific copy must exist before hydration."""
    soup = BeautifulSoup(html, "html.parser")
    box = soup.select_one('#functions-table')
    if box is None:
        return ["Functions section is missing"]
    errors = []
    for selector in ('.fn-scroll', '.fn-scroll > .fn-table', '.fn-phone', '.fn-legend.fn-desktop-only', '.fn-legend.fn-phone-only'):
        if len(box.select(selector)) != 1:
            errors.append(f"Functions needs exactly one saved {selector}")
    how = box.select_one('.fn-how')
    for form, tail in (("desktop", "Select a mark to see the quote and source."),
                       ("phone", "Select a country to see its functions, then a function for its quote and source.")):
        instructions = box.select('.fn-use.fn-' + form + '-only')
        if len(instructions) != 1 or instructions[0].parent is not how or instructions[0].get_text() != tail:
            errors.append(f"Functions {form} instructions missing from coding fold")
    desktop_controls = {n.get('aria-controls') for n in box.select('.fn-scroll .fn-cell')}
    phone_controls = {n.get('aria-controls') for n in box.select('.fn-phone .fn-phone-row')}
    if desktop_controls & phone_controls:
        errors.append("Functions forms share detail controls")
    if box.select('.fn-detail, .fn-detail-row, .fn-phone-detail, .fn-phone-claims'):
        errors.append("Functions detail was saved before a click")
    return errors


FN_FORM_VISIBILITY = """() => {
  const errors=[], box=document.querySelector('#functions-table'), narrow=innerWidth<=640;
  if(!box) return ['functions section is missing'];
  const check=(selector, shown) => {
    const nodes=[...box.querySelectorAll(selector)];
    if(nodes.length!==1) { errors.push('expected one '+selector); return; }
    const node=nodes[0], display=getComputedStyle(node).display;
    if(shown ? display==='none' || !node.getClientRects().length : display!=='none') errors.push(selector+' must be '+(shown?'displayed':'display: none'));
  };
  check('.fn-scroll', !narrow); check('.fn-phone', narrow);
  for(const kind of ['.fn-legend']) {
    check(kind+'.fn-desktop-only', !narrow); check(kind+'.fn-phone-only', narrow);
  }
  if(box.querySelector('.fn-pip-note')) errors.push('obsolete phone pip note is present');
  const hint=box.querySelector('#fn-scroll-hint');
  if(narrow && (!hint || getComputedStyle(hint).display!=='none')) errors.push('phone exposes desktop scroll hint');
  const hidden=box.querySelector(narrow?'.fn-scroll':'.fn-phone');
  if(hidden && [...hidden.querySelectorAll('.fn-detail, .fn-phone-claims')].some(n=>n.getClientRects().length)) errors.push('hidden form has visible detail');
  return errors;
}"""


TEXT_RECTS_JS = r"""
// Range rectangles for actual text, excluding hidden text and citation superscripts when requested.
const textRects = (root, exclude = '') => {
  if (!root) return [];
  const rects = [], walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  for (let node; (node = walker.nextNode());) {
    const parent = node.parentElement;
    if (!node.textContent.trim() || !parent.getClientRects().length ||
        parent.closest('.vh, [hidden]') || (exclude && parent.closest(exclude)) ||
        getComputedStyle(parent).visibility !== 'visible') continue;
    const range = document.createRange(); range.selectNodeContents(node);
    rects.push(...[...range.getClientRects()].filter(r => r.width && r.height));
  }
  return rects;
};
"""


# These controls deliberately span more than their text. Keep the exceptions narrow and explained.
CLICK_WIDTH_ALLOW = {
    '.card.mini > summary': 'Europe mini cards use the whole finding row as their disclosure control.',
    '.fund-detail[open] > summary': 'The open funding fold uses its summary as a full-width sticky identity beside its close.',
    'th[data-k] > button': 'Sortable table headers use the full cell as their target.',
    '#changes > summary': 'Version history fills the row so its label and latest version can truncate.',
    '.dlist a': 'Each Data download is one row link covering the file name and its description.',
    '.fn-phone-row': 'A grouped Functions row is one full-width control over its name and strips.',
    '.fund-row': 'A Funding row is one full-width control; identity, figures and citations are operable siblings.',
    '.fund-names-toggle': 'Each Funding names line is one full-width disclosure.',
    '.fund-name-row': 'Each institute in an opened Funding names list is one full-width records control.',
}

CLICK_WIDTH_JS = r"""allowed => {
  /* TEXT_RECTS */
  const probe = document.createElement('span'); probe.style.cssText = 'display:block;height:var(--hit)';
  document.body.appendChild(probe); const minHit = probe.getBoundingClientRect().height; probe.remove();
  return [...document.querySelectorAll('a, button, summary')].filter(n => n.getClientRects().length &&
    getComputedStyle(n).visibility === 'visible' && !n.closest('.vh, [hidden]')).map(n => {
    const rects = textRects(n);
    // A graphic is content too. Empty icon controls still have their box tested against the 60px limit.
    for (const graphic of n.querySelectorAll('svg, img, .fn-mark')) if (graphic.getClientRects().length) rects.push(graphic.getBoundingClientRect());
    // Padding is part of a control, and an icon or chevron drawn by ::before/::after is content: measure the content box,
    // and let a drawn pseudo-element extend the content to that edge.
    const rect = n.getBoundingClientRect(), style = getComputedStyle(n);
    const box = {left: rect.left + parseFloat(style.borderLeftWidth) + parseFloat(style.paddingLeft), right: rect.right - parseFloat(style.borderRightWidth) - parseFloat(style.paddingRight)};
    const drawn = pseudo => { const s = getComputedStyle(n, pseudo); return s.content !== 'none' && s.content !== 'normal' && s.display !== 'none' && !/absolute|fixed/.test(s.position) && parseFloat(s.width) > 0; }; // in-flow icons only, not hit-area layers
    // A drawn pseudo-element adds only its own width, margins and the gap beside the text, never the rest of the box,
    // so a stretched link with an icon right after its text still shows its empty width.
    let left = rects.length ? Math.min(...rects.map(r => r.left)) : null, right = rects.length ? Math.max(...rects.map(r => r.right)) : null;
    const gapX = parseFloat(style.columnGap) || 0, span = pseudo => { const s = getComputedStyle(n, pseudo); return parseFloat(s.width) + (parseFloat(s.marginLeft) || 0) + (parseFloat(s.marginRight) || 0); };
    if (drawn('::before')) { const w = span('::before'); if (left === null) { left = box.left; right = box.left + w; } else left -= gapX + w; }
    if (drawn('::after')) { const w = span('::after'); if (right === null) { right = box.right; left = box.right - w; } else right += gapX + w; }
    const content = left === null ? 0 : right - left;
    return {name: (n.id || n.className || n.tagName) + ': ' + (n.textContent.trim() || n.getAttribute('aria-label') || '').slice(0, 80),
      width: box.right - box.left, content, allowed: allowed.find(selector => n.matches(selector)) || null,
      correction: n.matches('a.correction'), height: rect.height, minHit};
  });
}""".replace("/* TEXT_RECTS */", TEXT_RECTS_JS)


def clickable_width_errors(controls: list[dict]) -> list[str]:
    errors = []
    for control in controls:
        if control['width'] - control['content'] > 60 and control.get('allowed') not in CLICK_WIDTH_ALLOW:
            errors.append(f"Clickable box exceeds its content by {control['width'] - control['content']:.1f}px: {control['name']}")
        if control.get('correction') and control['height'] < control['minHit'] - .5:
            errors.append('Correction link lost its hit height: ' + control['name'])
    return errors


def check_clickable_widths(browser, base_url: str) -> list[str]:
    """Audit every section at the desktop widths of the pointer report, including expanded records and sources."""
    page = browser.new_page(viewport={"width": 1440, "height": 1000})
    hydrated(page, base_url)
    page.evaluate("document.fonts.ready")
    page.add_style_tag(content='.offscreen, .card-row, .fund-item { content-visibility: visible !important; }')
    errors = []
    for width in (1000, 1440, 1920):
        page.set_viewport_size({"width": width, "height": 1000})
        for theme in ('light', 'dark'):
            set_theme(page, theme)
            for opened in (False, True):
                page.locator('main details:not(.cols, .download-menu)').evaluate_all('(nodes, open) => nodes.forEach(n => n.open = open)', opened)
                page.locator('.fund-names-toggle').evaluate_all('(nodes, open) => nodes.forEach(n => { if ((n.getAttribute("aria-expanded") === "true") !== open) n.click(); })', opened)
                page.evaluate('() => new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)))')
                if not opened and theme == 'light':
                    # The audit must still see a stretched link whose icon sits right after its text.
                    probe = page.evaluate('''() => { const s = document.createElement('style'); s.id = 'width-probe-style';
                      s.textContent = '#width-probe::after { content: ""; display: inline-block; width: 8px; height: 8px; margin-left: 4px; }';
                      const a = document.createElement('a'); a.id = 'width-probe'; a.href = '#'; a.textContent = 'Probe'; a.style.cssText = 'display:block;width:400px;cursor:pointer';
                      document.head.appendChild(s); document.querySelector('main').prepend(a); return true; }''')
                    flagged = any(e.startswith('Clickable box exceeds') and 'width-probe' in e for e in clickable_width_errors(page.evaluate(CLICK_WIDTH_JS, list(CLICK_WIDTH_ALLOW))))
                    page.evaluate("() => { document.getElementById('width-probe')?.remove(); document.getElementById('width-probe-style')?.remove(); }")
                    if probe and not flagged:
                        errors.append(f'{width}px: the width audit missed a stretched link with an icon after its text')
                controls = page.evaluate(CLICK_WIDTH_JS, list(CLICK_WIDTH_ALLOW))
                if not any(c['allowed'] == '.fund-row' for c in controls):
                    errors.append('Clickable width audit did not reach Funding row controls')
                if opened and not any(c['correction'] for c in controls):
                    errors.append('Clickable width audit did not reach correction links')
                errors += [f'{width}px {theme} {"expanded" if opened else "collapsed"}: ' + e for e in clickable_width_errors(controls)]
    page.close()
    return errors


FN_CLAIM_INDENT_JS = r"""detail => {
  if (detail.matches('.fn-detail-row')) detail = detail.querySelector('.fn-detail');
  const inline = detail.matches('.fn-phone-claims'), errors = [];
  const name = inline ? detail.parentElement.querySelector('.fn-function-label > .fn-name') : detail.querySelector('.fn-function-head');
  if (!name) return ['claim block has no parent function name'];
  const range = document.createRange(); range.selectNodeContents(name);
  const edge = range.getBoundingClientRect().left;
  const roots = inline ? [detail] : [...detail.querySelectorAll(':scope > .fn-claim, :scope > .fn-earlier')];
  for (const root of roots) for (const node of [root, ...root.querySelectorAll('*')]) {
    if (node.closest('.vh') || getComputedStyle(node).visibility !== 'visible') continue;
    const rects = [...node.getClientRects()].filter(r=>r.width>0 && r.height>0);
    if (rects.some(r=>r.left < edge - .5)) errors.push('opened claim child starts left of its function name: ' + node.className);
  }
  if (inline && Math.abs(detail.getBoundingClientRect().left - edge) > .5) errors.push('opened claim block does not align with its function name');
  return errors;
}"""


FN_CLAIMS_CHECK_JS = "const claimIndentErrors = " + FN_CLAIM_INDENT_JS + ";\n" + r"""
// Archived copies: an offline source (its worst quotation state source_gone, as the Re-read line orders states) links to its archived copy.
const REREAD_RANK = {confirmed: 0, not_attempted: 0, blocked_or_failed: 1, source_gone: 2, quote_gone: 2}, REREAD_TIE = {source_gone: 0, quote_gone: 1, blocked_or_failed: 2};
const sourceHref = s => {
  const states = (s.excerpts || []).map(e => e.reread).filter(Boolean), top = Math.max(0, ...states.map(r => REREAD_RANK[r.state]));
  const worst = top ? states.filter(r => REREAD_RANK[r.state] === top).sort((a, b) => a.since.localeCompare(b.since) || REREAD_TIE[a.state] - REREAD_TIE[b.state] || a.last_found.localeCompare(b.last_found))[0] : null;
  return s.archive_url && worst?.state === 'source_gone' ? s.archive_url : s.url;
};
const functionMonth = day => day.length > 4 ? ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'][+day.slice(5, 7) - 1] + ' ' + day.slice(0, 4) : day; // the page's fmtDate at month precision
const checkHeading = (detail, cell, country, institute, code, key) => {
  const heading = detail.querySelector('.fn-function-head'), context = detail.querySelector(':scope > .fn-detail-context');
  if (detail.matches('.fn-phone-claims')) {
    const button = detail.parentElement.querySelector('button.fn-function-label');
    need(!heading && !context && detail.firstElementChild?.matches('.fn-claim'), key + ': inline claims repeat their title or status before the evidence');
    need(detail.getAttribute('role') === 'region' && !!button?.id && detail.getAttribute('aria-labelledby') === button.id, key + ': inline claims lost their accessible name');
    return;
  }
  need(heading?.matches('h4') && heading.textContent === data.dataset.vocabularies.function[code].label, key + ': grid panel must head the claims with the function label');
  const identity=detail.querySelector('.fn-detail-head');
  need(!!identity?.id && detail.getAttribute('aria-labelledby') === identity.id, key + ': grid region lost its state and institute name');
  const parts = [...context?.querySelectorAll(':scope > .fn-context-status') || []];
  need(context?.textContent === [institute, ...cell.status_parts].join(' '), key + ': grid status must name the institute short form and only the known status parts');
  need(JSON.stringify(parts.map(n => n.textContent)) === JSON.stringify(cell.status_parts) && parts.every(n => n.matches('.trailing-qualifier') &&
       n.previousElementSibling?.matches('.sep-gap') && getComputedStyle(n).whiteSpace === 'nowrap'), key + ': grid status parts/dates/separators differ');
  need(context && (heading?.closest('.fn-detail-top') || heading)?.nextElementSibling === context &&
       context.nextElementSibling?.matches('.fn-claim'), key + ': heading/context/claim order differs');
  const probe = document.createElement('span'); probe.style.cssText = 'font:var(--type-label);color:var(--muted)'; detail.appendChild(probe);
  if (context) need(['fontFamily', 'fontSize', 'fontWeight', 'lineHeight', 'color'].every(k => getComputedStyle(context)[k] === getComputedStyle(probe)[k]), key + ': context is not muted list-level type');
  probe.remove();
};
const checkEntry = (item, q, key, preview, panelOrder) => {
  const sentence = item?.querySelector(':scope > .meta-item > .claim-text'), cites = sentence?.querySelector('.cites');
  const assigned = sentence?.querySelector(':scope > .fn-assigned'), text = sentence?.cloneNode(true);
  text?.querySelector('.fn-assigned')?.remove(); text?.querySelector('.cites')?.remove();
  const day = item?.querySelector(':scope > .meta-item > .fn-entry-date');
  need(text?.textContent.replace(/\u00a0/g, ' ') === q.claim.text, key + ': claim text differs');
  need(q.claim.basis === 'mandate' ? assigned?.textContent === 'Assigned: ' && assigned === sentence?.firstChild : !assigned, key + ': assignment marker differs');
  need(q.source.published ? day?.textContent.replace(/\u00a0/g, ' ') === functionMonth(q.source.published) : !day, key + ': entry date differs from its bound source');
  const parts = [...item?.querySelectorAll(':scope > .meta-item') || []].map(n => n.firstElementChild);
  need(JSON.stringify(parts.map(n => n?.className)) === JSON.stringify([...(q.source.published ? [day?.className] : []), sentence?.className]) &&
       parts.every((n, index) => !index || n.parentElement.previousElementSibling?.matches('.sep-gap')), key + ': entry must read date then cited sentence with separators');
  const links = [...cites?.querySelectorAll('a.cn') || []], institute = item?.closest('[data-institute]')?.dataset.institute;
  // The card's Sources order; a source the card does not cite takes the next number within this opened function,
  // as citations() numbers it (panelOrder carries those additions from one entry to the next).
  const order = panelOrder || [...(orders[data.institutes.find(i => i.id === institute)?.jurisdiction_id] || [])];
  const ids = [...new Set(q.claim.citations?.text || [])].filter(id => data.sources.some(s => s.id === id));
  ids.forEach(id => { if (!order.includes(id)) order.push(id); });
  const cited = order.filter(id => ids.includes(id)).map(id => data.sources.find(s => s.id === id));
  need(JSON.stringify(links.map(a => a.href)) === JSON.stringify(cited.map(s => new URL(sourceHref(s), document.baseURI).href)) && links.every((a, n) =>
       a.target === '_blank' && a.dataset.sourceId === cited[n].id && a.textContent === String(order.indexOf(cited[n].id) + 1) &&
       a.getAttribute('aria-label')?.replace(/, opens in a new tab$/, '') === 'Source ' + a.textContent + ': ' + (cited[n].title_en || cited[n].title)), key + ': citation marks/links/names/card order differ');
  need(links.length === new Set(ids).size && cites?.textContent === links.map(a => a.textContent).join(',') && cites?.parentElement.matches('.cite-glue') &&
       cites === cites?.parentElement.lastChild && sentence?.textContent.endsWith(cites.textContent) && !item?.querySelector('.fn-sources, a.standalone'), key + ': citations must end the sentence in a glued, comma-tight group without publisher links');
  const fold = item?.querySelector(':scope > details.quote'), summary = fold?.querySelector(':scope > summary');
  const tail = item?.querySelector(':scope > .archive-copy') || item?.querySelector(':scope > .reread-tail') || sentence?.parentElement;
  if (preview) need(!item?.querySelector('details.quote') && tail === item?.lastElementChild, key + ': a preview line carries a Quote control or extra tail');
  else need(fold && !fold.open && !fold.querySelector('.quote-body') && fold.dataset.quoteClaim === q.claim.id && fold === item.lastElementChild &&
       summary.querySelector('.qualifier-text')?.textContent === 'Quote' && fold.previousElementSibling?.matches('.sep-gap') && fold.previousElementSibling.previousElementSibling === tail,
       key + ': the quote control is not a closed, lazy Quote fold after the cited entry separator');
  if (item && sentence && cites) {
    const probe = document.createElement('span'); probe.style.cssText = 'font:var(--type-meta);color:var(--muted)'; item.appendChild(probe);
    need([assigned, day, summary, item.querySelector(':scope > .reread-tail'), item.querySelector(':scope > .archive-copy')].filter(Boolean).every(n => ['fontSize', 'fontWeight', 'color'].every(k => getComputedStyle(n)[k] === getComputedStyle(probe)[k])), key + ': entry apparatus is not muted meta type');
    need(parseFloat(getComputedStyle(cites).fontSize) <= parseFloat(getComputedStyle(probe).fontSize) && parseFloat(getComputedStyle(cites).top) < 0, key + ': citation group is not superscript at most meta size');
    probe.style.color = 'var(--ink)'; need(getComputedStyle(sentence).color === getComputedStyle(probe).color, key + ': entry sentence is not ink'); probe.remove();
  }
  return {sentence, cites, fold, summary};
};
const checkClaims = async (detail, cell, key) => {
  const items = [...detail.querySelectorAll('.fn-claim')];
  const order = cell.claim_ids;
  need(JSON.stringify(items.map(n => n.dataset.claim)) === JSON.stringify(order), key + ': claim order differs from generated claim_ids');
  const activities = order.filter(id => quotes[id]?.claim.basis === 'activity'), earlierIds = activities.slice(3);
  const earlier = detail.querySelector(':scope > details.fn-earlier'), summary = earlier?.querySelector(':scope > summary');
  need(detail.querySelectorAll('details.fn-earlier').length === (earlierIds.length ? 1 : 0), key + ': earlier disclosure count differs');
  if (earlierIds.length) {
    need(earlier && !earlier.open && summary?.textContent === earlierIds.length + ' earlier' && earlier === detail.lastElementChild, key + ': earlier entries need one closed disclosure after the recent entries');
    need(JSON.stringify([...earlier?.querySelectorAll('.fn-claim') || []].map(n => n.dataset.claim)) === JSON.stringify(earlierIds), key + ': earlier disclosure claims differ');
    const probe = document.createElement('span'); probe.style.cssText = 'font:var(--type-meta);color:var(--muted)'; detail.appendChild(probe);
    if (summary) need(['fontFamily', 'fontSize', 'fontWeight', 'lineHeight', 'color'].every(k => getComputedStyle(summary)[k] === getComputedStyle(probe)[k]), key + ': earlier summary is not muted meta type');
    probe.remove();
  }
  for (const item of items) {
    const folded = earlierIds.includes(item.dataset.claim);
    need(folded ? item.parentElement === earlier : item.parentElement === detail && !!item.getClientRects().length, key + ': assignments and first three activities must be visible, earlier activities folded');
  }
  if (earlier && summary) {
    await new Promise(resolve => { earlier.addEventListener('toggle', resolve, {once:true}); summary.click(); });
    need(earlier.open && [...earlier.querySelectorAll('.fn-claim')].every(item => item.getClientRects().length), key + ': earlier entries do not open');
  }
  const owner = data.institutes.find(i => i.id === detail.closest('[data-institute]')?.dataset.institute);
  const panelOrder = [...(orders[owner?.jurisdiction_id] || [])]; // one numbering per opened function, as the page builds it
  for (const id of cell.claim_ids) {
    const item = items.find(n => n.dataset.claim === id), q = quotes[id];
    if (!item || !q) { errors.push(key + ': claim missing ' + id); continue; }
    const {fold, summary} = checkEntry(item, q, key, false, panelOrder);
    need(!detail.querySelector('.fn-basis, .fn-task'), key + ': replaced assignment/activity labels or task line remain');
    const name = summary?.getAttribute('aria-label') || '';
    const glossed = [q.excerpt, ...(q.excerpt.anchors || [])].some(span => span.gloss);
    need(glossed ? name.startsWith('Quote, translated from ') && !/Bokmål|Nynorsk/.test(name) : name === 'Quote', key + ': the quote control is not named "Quote", with the translation once, in a plain language name');
    if (!fold) continue;
    await new Promise(resolve => { fold.addEventListener('toggle', resolve, {once:true}); fold.querySelector('summary').click(); });
    need(fold.open && fold.querySelector('.quote-body')?.getClientRects().length, key + ': Quote does not open to visible evidence');
    const body = fold.querySelector('.quote-body');
    need(body && body.getBoundingClientRect().left > item.getBoundingClientRect().left + 4 && body.getBoundingClientRect().top >= summary.getBoundingClientRect().bottom - 1, key + ': the quote does not open beneath its entry, indented');
    need(glossed ? /^Translated from \S/.test(body?.querySelector(':scope > .quote-translation')?.textContent || '') : !body?.querySelector('.quote-translation'), key + ': an opened translated quote does not say what it was translated from');
    const block = item.querySelector('.quote-body > .quote-excerpt');
    need(item.querySelectorAll('.quote-excerpt').length === 1 && block?.dataset.sourceId === q.source.id, key + ': shared quote block differs');
    const shown = block?.querySelector(':scope > .quotation > .quote-text');
    need(shown?.textContent === '"' + (q.excerpt.gloss || q.excerpt.text) + '"', key + ': quote differs');
    need(!q.excerpt.gloss || shown?.getAttribute('aria-description') === q.excerpt.text, key + ': original differs');
    need(!item.querySelector('.translation'), key + ': a translation marker follows a quote');
    const anchors = [...(block?.querySelectorAll('.quote-anchor') || [])];
    need(anchors.length === (q.excerpt.anchors || []).length, key + ': anchor count differs');
    (q.excerpt.anchors || []).forEach((anchor, n) => {
      const quote = anchors[n]?.querySelector('.quote-text');
      need(quote?.textContent === '"' + (anchor.gloss || anchor.text) + '"' && (!anchor.gloss || quote?.getAttribute('aria-description') === anchor.text), key + ': anchor quote differs');
    });
    need(!!block?.querySelector('.reread') === ['blocked_or_failed', 'source_gone', 'quote_gone'].includes(q.excerpt.reread?.state), key + ': a quote shows a re-read line only for a failed state');
    const reread = block?.querySelector('.reread');
    if (reread && reread.classList.contains('failed')) need(getComputedStyle(reread).display === 'none' && item.querySelector(':scope > .reread-tail')?.getClientRects().length &&
         item.querySelector(':scope > .reread-tail')?.previousElementSibling?.previousElementSibling?.querySelector('.cites'), key + ': a source that could not be re-read does not say so after its citation marks');
    else if (reread) need(getComputedStyle(reread).display === 'block' && getComputedStyle(reread).color === getComputedStyle(summary).color, key + ': per-quote re-read line is not separate/muted');
  }
  errors.push(...claimIndentErrors(detail).map(error => key + ': ' + error));
};
"""


def function_phone_model(data: dict) -> dict:
    """Expected phone rows and groups from the published values, including a missing code's uncoded fallback."""
    institutes = function_rows(data)
    states = {j["id"]: j for j in data["jurisdictions"]}
    vocab = {code: value for code, value in data["dataset"]["vocabularies"]["function"].items() if not value.get("deprecated")}
    lookup = function_values(institutes, vocab)
    quotes = function_quotes(data)
    groups = function_columns(data)
    columns = [code for group in groups for code in group["codes"]]
    base_names = expected_base_names(data)
    return {"groups": groups, "rows": [
        {"id": i["id"], "country": states[i["jurisdiction_id"]]["name"], "name": function_display_name(i),
         "head_name": states[i["jurisdiction_id"]]["name"] + ', ' + base_names[i['id']],
         "block": "Established" if states[i["jurisdiction_id"]]["status"] == "established" else "Not yet established",
         "functions": [{"code": code, "label": vocab[code]["label"], "group": vocab[code]["group"], **lookup[i["id"]][code],
                        "status_parts": function_status_parts(lookup[i["id"]][code], quotes)} for code in columns]}
        for i in institutes]}


def functions_phone_structure(html: str, data: dict) -> list[str]:
    """Saved and hydrated phone form: independent row, group, pip and accessible-control oracle."""
    soup = BeautifulSoup(html, "html.parser")
    box = soup.select_one('#functions-table')
    if box is None:
        return ["Functions phone section is missing"]
    errors = []
    def need(ok, message):
        if not ok:
            errors.append(message)
    model = function_phone_model(data)
    groups, expected_rows = model["groups"], model["rows"]
    lists = box.select('.fn-phone')
    need(len(lists) == 1, "Functions needs exactly one phone list")
    if len(lists) != 1:
        return errors
    phone = lists[0]
    heads = phone.select('.fn-phone-head > span')
    need([head.get_text() for head in heads[:1]] == [""], "Functions phone head must start with the empty country head")
    need([(h.select_one('.fn-label-full').get_text() if h.select_one('.fn-label-full') else None) for h in heads[1:]] == [fn_group_name(g["name"]) for g in groups],
         "Functions phone group heads/order differ")
    need([(h.select_one('.fn-label-short').get_text() if h.select_one('.fn-label-short') else None) for h in heads[1:]] == [fn_group_short_name(g["name"]) for g in groups],
         "Functions phone head short labels differ ('&' for 'and', no other change)")
    blocks = phone.select(':scope > .fn-phone-block')
    need([block.select_one(':scope > h3').get_text() if block.select_one(':scope > h3') else None for block in blocks] == ["Established", "Not yet established"], "Functions phone blocks/order differ")
    rows = phone.select('.fn-phone-block > ul > li')
    need([row.get("data-institute") for row in rows] == [row["id"] for row in expected_rows], "Functions phone rows/order differ")
    for block, label in zip(blocks, ("Established", "Not yet established")):
        need(block.select_one(':scope > ul[role="list"]'), "Functions phone block list semantics differ")
        need([row.get("data-institute") for row in block.select(':scope > ul > li')] == [row["id"] for row in expected_rows if row["block"] == label], "Functions phone block membership differs")
    controls = []
    for row, expected in zip(rows, expected_rows):
        key = expected["id"]
        button = row.select_one(':scope > button.fn-phone-row')
        need(button is not None and row.select('button') == [button], key + ': phone row must be one button')
        if button is None:
            continue
        need(button.get("type") == "button" and button.get("aria-expanded") == "false" and button.get("aria-controls") and
             button.get("aria-label") == f"{expected['country']}, {expected['name']}: show functions", key + ': phone row control/name differs')
        controls.append(button.get("aria-controls"))
        need(not button.find() and not button.get_text(), key + ': phone row button must be empty')
        need(row.select('button, [tabindex], a[href], input, select, textarea, [contenteditable="true"]') == [button], key + ': phone row has other focus targets')
        country = row.select_one(':scope > span.fn-phone-country')
        need(country and country.get_text() == expected["country"], key + ': phone country label differs')
        need(country and country.get("aria-hidden") == "true", key + ': decorative phone country is exposed')
        need(not row.select('abbr, .g, [data-tip], [aria-description], [title]'), key + ': phone country has a tooltip')
        need(row.get_text(strip=True) == expected["country"] and not FN_STATUS_WORD.search(button.get("aria-label", "")), key + ': phone row has counts or status words')
        pip_groups = row.select(':scope > .fn-pips')
        need(len(pip_groups) == len(groups), key + ': phone pip groups/order differ')
        need(row.find_all(recursive=False) == [button, country, *pip_groups], key + ': phone row children/order differ')
        # The published model fixes block/row order, group order and column order within each group.
        # Decorative pips carry only their shape class; values come from that ordered model.
        for pips, group in zip(pip_groups, groups):
            need(pips.get("aria-hidden") == "true", key + ': decorative phone pip group is exposed')
            wanted = [row for row in expected["functions"] if row["group"] == group["name"]]
            actual = pips.select(':scope > .fn-pip')
            need(len(actual) == len(wanted), key + ': phone pip count/order differs')
            for pip, function in zip(actual, wanted):
                shape = "activity" if function["value"] == "both" else function["value"]
                need(set(pip.get("class", [])) == {"fn-pip", "fn-mark", "fn-" + shape} and pip.get("aria-hidden") == "true" and not pip.get_text(), key + '/' + function["code"] + ': phone pip mark differs')
    need(len(set(controls)) == len(controls), "Functions phone detail controls are not unique")
    need(not phone.select('.fn-phone-detail, .fn-phone-claims, .fn-claim'), "Functions phone detail is present before opening a row")
    legend = box.select('.fn-legend.fn-phone-only > li')
    wanted_values = ["activity", "mandate", "not_found"]
    if any(row["value"] == "not_coded" for institute in expected_rows for row in institute["functions"]):
        wanted_values.append("not_coded")
    need([li.get("data-value") for li in legend] == wanted_values, "Functions phone legend values/order differ")
    for item, value in zip(legend, wanted_values):
        label = "No public text found; it may still be done" if value == "not_found" else FN_NAMES[value]
        need(item.get_text() == label and item.select_one('.fn-pip.fn-' + value), "Functions phone legend label/mark differs")
    need(not box.select('.fn-pip-note') and "One pip per function" not in box.get_text(), "Functions obsolete phone pip note is present")
    need(not box.select('.fn-blank'), "Functions obsolete blank sentence is present")
    return errors


FN_PHONE_BOUNDS_JS = """
  const phoneBounds = node => {
    if (!node) return null;
    let rects;
    if (node.matches('.fn-pips')) {
      // The structure oracle fixes pip order; measure the painted run, not its stretched grid item.
      const pips = [...node.querySelectorAll(':scope > .fn-pip')];
      if (!pips.length) return null;
      rects = [pips[0].getBoundingClientRect(), pips[pips.length - 1].getBoundingClientRect()];
    } else {
      const range = document.createRange(); range.selectNodeContents(node);
      rects = [...range.getClientRects()].filter(r => r.width && r.height);
    }
    if (!rects.length) return null;
    return {left: Math.min(...rects.map(r => r.left)), right: Math.max(...rects.map(r => r.right)),
            top: Math.min(...rects.map(r => r.top)), bottom: Math.max(...rects.map(r => r.bottom))};
  };
"""


PHONE_WIDTHS = (320, 360, 390, 430, 640)


def check_functions_phone(browser, base_url: str, data: dict, widths=PHONE_WIDTHS, *, inject: bool = False) -> list[str]:
    """Phone words, gutters, alignment, values, evidence and focus; real keyboard and touch."""
    page = browser.new_page(viewport={"width": 360, "height": 1000})
    errors = []
    page.on("pageerror", lambda error: errors.append("Functions phone JavaScript: " + str(error)))
    if inject:
        page.route(PAGE_DATA, lambda route: route.fulfill(json=data))
    hydrated(page, base_url)
    page.evaluate("document.fonts.ready")
    payload = {"data": data, "quotes": function_quotes(data), "names": FN_NAMES, "model": function_phone_model(data), "groupLabels": FN_GROUP_LABELS,
               "orders": card_source_orders(BeautifulSoup(page.content(), "html.parser"), data)}
    for width in widths:
        page.set_viewport_size({"width": width, "height": 1000})
        page.wait_for_selector('.fn-phone')
        for theme in ("light", "dark"):
            set_theme(page, theme)
            for scale in (("100%",) if width == 320 else ("100%", "110%")):
                page.evaluate("scale => document.documentElement.style.fontSize = scale", scale)
                page.evaluate("() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))")
                prefix = f"phone {width}px {theme} {scale}: "
                errors += [prefix + error for error in page.evaluate(FN_FORM_VISIBILITY)]
                errors += [prefix + error for error in functions_phone_structure(page.content(), data)]
                errors += [prefix + error for error in page.evaluate(r"""async ({data, quotes, names, model, groupLabels, orders}) => {
                  const errors = [], box = document.querySelector('#functions-table'), phone = box.querySelector('.fn-phone');
                  const need = (ok, label) => { if (!ok) errors.push(label); };
                  if (!phone) return ['phone form is missing'];
                  /* FUNCTION_CLAIMS */
                  /* PHONE_BOUNDS */
                  const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);
                  const transparent = color => color === 'transparent' || color === 'rgba(0, 0, 0, 0)';
                  const heads = [...phone.querySelectorAll('.fn-phone-head > span')].slice(1); // structure check enforces the empty country head
                  const overflow = key => {
                    need(document.documentElement.scrollWidth <= innerWidth + 1, key + ': sideways page scroll');
                    for (const node of [phone, ...phone.querySelectorAll('.fn-phone-row, .fn-pips, .fn-phone-detail, .fn-phone-claims')]) {
                      if (node.scrollWidth > node.clientWidth + 1) { errors.push(key + ': sideways phone box scroll: ' + node.className); break; }
                    }
                    const section = phone.closest('section'), bounds = section.getBoundingClientRect(), style = getComputedStyle(section);
                    const left = bounds.left + parseFloat(style.borderLeftWidth) + parseFloat(style.paddingLeft);
                    const right = bounds.right - parseFloat(style.borderRightWidth) - parseFloat(style.paddingRight);
                    const gutter = parseFloat(getComputedStyle(section.closest('.gutter') || section).paddingLeft);
                    for (const node of [phone, ...phone.querySelectorAll('*')]) {
                      if (!node.getClientRects().length) continue;
                      const r = node.getBoundingClientRect();
                      need(r.right <= right + .5 && r.left >= left - .5, key + ': phone descendant crosses section content edge: ' + node.className);
                    }
                    for (const label of [...heads, ...phone.querySelectorAll('.fn-phone-country')]) {
                      const name = key + ': ' + label.textContent;
                      need(getComputedStyle(label).hyphens !== 'auto', name + ': automatic hyphenation');
                      need(label.scrollWidth <= label.clientWidth, name + ': label overflows');
                      const walker = document.createTreeWalker(label, NodeFilter.SHOW_TEXT);
                      while (walker.nextNode()) {
                        const text = walker.currentNode;
                        if (!text.parentElement.getClientRects().length) continue;  // the head's other label form, hidden at this width
                        for (const word of text.textContent.matchAll(/\S+/gu)) {
                          const range = document.createRange(); range.setStart(text, word.index); range.setEnd(text, word.index + word[0].length);
                          need(range.getClientRects().length === 1, name + ': word breaks across lines: ' + word[0]);
                        }
                      }
                    }
                    for (const row of phone.querySelectorAll('.fn-phone-block > ul > li')) {
                      const name = key + ': ' + row.dataset.institute;
                      const groups = [...row.querySelectorAll(':scope > .fn-pips')];
                      for (const [g, pips] of groups.entries()) {
                        const head = phoneBounds(heads[g]), run = phoneBounds(pips);
                        need(head && run && Math.abs(head.left - run.left) <= 1,  // both start just after the group line (owner, 18 Nov 2026)
                             name + ': visible pip run and head label do not start together: ' + g);
                      }
                      const last = groups[groups.length - 1]?.querySelector(':scope > .fn-pip:last-child');
                      need(last && document.documentElement.clientWidth - last.getBoundingClientRect().right >= Math.max(left, gutter) - .5,
                           name + ': last pip enters page gutter');
                    }
                  };
                  const pip = (node, value, key, detail = false) => {
                    if (!node) { errors.push(key + ': missing pip'); return; }
                    const style = getComputedStyle(node), rect = node.getBoundingClientRect();
                    const shape = value === 'both' ? 'activity' : value;
                    need(same([...node.classList].sort(), ['fn-mark', 'fn-pip', 'fn-' + shape].sort()), key + ': pip mark differs');
                    need(node.getAttribute('aria-hidden') === 'true', key + ': pip must be decorative; the status follows the name');
                    need(Math.abs(rect.width - 8) <= 1 && Math.abs(rect.height - (value === 'not_coded' ? 2 : 8)) <= 1, key + ': pip dimensions differ');
                    if (value === 'activity' || value === 'both') need(!transparent(style.backgroundColor), key + ': active pip is not filled');
                    else if (value === 'mandate' || value === 'not_found') need(transparent(style.backgroundColor) && parseFloat(style.borderLeftWidth) >= 1, key + ': pip outline missing');
                    else need(!transparent(style.backgroundColor) && style.backgroundColor === getComputedStyle(box.querySelector('.fn-legend.fn-phone-only')).color, key + ': uncoded dash is not muted');
                  };
                  const buttons = [...phone.querySelectorAll('.fn-phone-row')];
                  need(buttons.length === model.rows.length, 'phone row count differs');
                  for (const [n, button] of buttons.entries()) {
                    const expected = model.rows[n]; if (!expected) continue;
                    need(!button.children.length && !button.textContent, expected.id + ': phone row button must be empty');
                    need(!/grid|flex/.test(getComputedStyle(button).display), expected.id + ': phone row button uses grid or flex');
                    const row = button.parentElement, groups = [...row.querySelectorAll(':scope > .fn-pips')];
                    const rowStyle = getComputedStyle(row), buttonStyle = getComputedStyle(button), hit = button.getBoundingClientRect(), bounds = row.getBoundingClientRect();
                    need(rowStyle.display === 'grid' && rowStyle.gridTemplateColumns.startsWith('subgrid'), expected.id + ': phone list item is not a subgrid');
                    need(buttonStyle.gridRowStart === '1' && Math.abs(hit.left - bounds.left) <= 1 && Math.abs(hit.right - bounds.right) <= 1 &&
                         hit.height >= parseFloat(buttonStyle.minHeight), expected.id + ': phone button does not cover the row');
                    for (const node of row.querySelectorAll(':scope > .fn-phone-country, :scope > .fn-pips')) {
                      const style = getComputedStyle(node), align = node.matches('.fn-pips') ? 'stretch' : 'center';  // a strip spans its row to carry the group line
                      need(style.gridRowStart === '1' && style.alignSelf === align && style.position === 'relative' && Number(style.zIndex) > 0 &&
                           style.pointerEvents === 'none', expected.id + ': phone visual does not sit above/pass taps to the button');
                    }
                    need(groups.length === model.groups.length, expected.id + ': phone pip groups/order differ');
                    for (const [g, group] of model.groups.entries()) {
                      const pips = groups[g], key = expected.id + '/' + group.name;
                      if (!pips) continue;
                      need(Math.abs(parseFloat(getComputedStyle(pips).columnGap) - 2) < .5, key + ': phone pip gap differs');
                      const wanted = expected.functions.filter(f => f.group === group.name), marks = [...pips.children];
                      need(marks.length === wanted.length, key + ': phone pip count/order differs');
                      wanted.forEach((f, k) => pip(marks[k], f.value, expected.id + '/' + f.code));
                    }
                  }
                  const ring = box.querySelector('.fn-legend.fn-phone-only .fn-mandate'), empty = box.querySelector('.fn-legend.fn-phone-only .fn-not_found');
                  need(ring && empty && getComputedStyle(ring).borderLeftColor !== getComputedStyle(empty).borderLeftColor, 'not-found outline is not fainter than Assigned');
                  overflow('closed rows');
                  for (const [n, button] of buttons.entries()) {
                    const expected = model.rows[n]; if (!expected) continue;
                    const key = expected.id;
                    button.focus({preventScroll: true}); button.click();
                    const detail = phone.querySelector('.fn-phone-detail');
                    if (!detail) { errors.push(key + ': no phone detail'); continue; }
                    need(phone.querySelectorAll('.fn-phone-detail').length === 1 && button.parentElement.lastElementChild === detail &&
                         getComputedStyle(detail).gridRowStart === '2' &&
                         detail.id === button.getAttribute('aria-controls') && button.getAttribute('aria-expanded') === 'true' &&
                         detail.getAttribute('role') === 'region' && detail.getAttribute('aria-labelledby') === detail.querySelector('.fn-detail-head')?.id,
                         key + ': phone detail placement/state differs');
                    need(detail.querySelector('.fn-detail-head')?.textContent === expected.head_name, key + ': phone detail state and institute name differ');
                    const headings = [...detail.querySelectorAll(':scope > h4')], lists = [...detail.querySelectorAll(':scope > .fn-phone-functions')];
                    need(same(headings.map(h => h.textContent), model.groups.map(g => Object.prototype.hasOwnProperty.call(groupLabels, g.name) ? groupLabels[g.name] : g.name)) && lists.length === model.groups.length, key + ': phone detail groups/order differ');
                    for (const [g, group] of model.groups.entries()) {
                      const list = lists[g], heading = headings[g];
                      need(list && heading && heading.nextElementSibling === list, key + ': phone detail group heading placement differs');
                      need(list?.children.length === group.codes.length, key + ': phone detail group function count differs');
                    }
                    const functions = [...detail.querySelectorAll('.fn-phone-functions > li')];
                    need(functions.length === expected.functions.length, key + ': phone detail function count differs');
                    need(!detail.querySelector('.fn-claim'), key + ': claims visible before selecting a function');
                    overflow(key + ': open detail');
                    for (const [k, item] of functions.entries()) {
                      const f = expected.functions[k]; if (!f) continue;
                      const label = item.querySelector(':scope > .fn-function-label');
                      const name = label?.querySelector(':scope > .fn-name');
                      const marked = ['activity', 'mandate', 'both'].includes(f.value), labelKey = key + '/' + f.code;
                      need(label && name && label.textContent === [f.label, ...f.status_parts].join(' ') && (label.tagName === 'BUTTON') === marked, labelKey + ': function label/control differs');
                      const statuses = [...name?.querySelectorAll(':scope > .fn-function-status') || []], status = statuses.at(-1);
                      need(same(statuses.map(n => n.textContent), f.status_parts) && statuses.every(n => n.matches('.trailing-qualifier') &&
                           n.previousElementSibling?.matches('.sep-gap') && getComputedStyle(n).whiteSpace === 'nowrap'), labelKey + ': function status parts/dates/separators differ');
                      const newest = item.querySelector(':scope > .fn-latest'), latest = f.newest && quotes[f.newest];
                      need(!!newest === !!latest && !item.querySelector('.fn-task, .fn-basis'), labelKey + ': latest entry presence or obsolete task/basis label differs');
                      need(item.children.length === 1 + Number(!!latest), labelKey + ': function has extra preview lines');
                      if (latest) {
                        need(latest.claim.id === f.claim_ids.find(id => quotes[id].claim.basis === 'activity' && !quotes[id].claim.expired) && label?.nextElementSibling === newest, labelKey + ': latest preview is not the first live activity');
                        checkEntry(newest, latest, labelKey + ': latest', true);
                      }
                      need(!newest || !newest.hidden && newest.getClientRects().length, labelKey + ': preview must be visible before opening');
                      need(!marked || (statuses.every(n => !n.id) && !label.hasAttribute('aria-describedby')), labelKey + ': function button must include its status');
                      pip(label?.querySelector('.fn-pip'), f.value, labelKey, true);
                      if (!marked || !label) continue;
                      need(label.getAttribute('aria-expanded') === 'false' && label.getAttribute('type') === 'button', labelKey + ': unopened function control differs');
                      label.click();
                      const claims = item.querySelector(':scope > .fn-phone-claims');
                      need(claims && (newest || label)?.nextElementSibling === claims && claims.id === label.getAttribute('aria-controls') && label.getAttribute('aria-expanded') === 'true', labelKey + ': function claims placement/state differs');
                      need(!newest || newest.parentElement === item && newest.hidden && !newest.getClientRects().length, labelKey + ': preview must stay present but hidden while opened');
                      if (claims) {
                        checkHeading(claims, f, expected.country, null, f.code, labelKey);
                        const a = label.getBoundingClientRect(), s = (status || name).getBoundingClientRect(), bar = getComputedStyle(label, '::after');
                        const strong = parseFloat(getComputedStyle(document.documentElement).getPropertyValue('--rule-strong'));
                        need(!['none', 'normal'].includes(bar.content) && parseFloat(bar.width) >= strong &&
                             a.top + parseFloat(bar.height) >= s.bottom - 1, labelKey + ': selection bar does not span the name and status');
                        await checkClaims(claims, f, labelKey);
                      }
                      overflow(labelKey);
                      label.click();
                      need(!item.querySelector('.fn-phone-claims') && label.getAttribute('aria-expanded') === 'false', labelKey + ': same function does not close claims');
                      need(!newest || newest.parentElement === item && !newest.hidden && newest.getClientRects().length, labelKey + ': closing the function must restore its preview');
                    }
                    need(!/\b(planned|proposed|exploring|announced)\b/i.test([...detail.querySelectorAll('.fn-detail-head, h4, .fn-function-label')].map(n => n.textContent).join(' ')), key + ': status word in phone detail labels');
                    const escapeTarget = detail.querySelector('button.fn-function-label') || detail.querySelector('.close-control');
                    escapeTarget.focus({preventScroll: true});
                    escapeTarget.dispatchEvent(new KeyboardEvent('keydown', {key: 'Escape', bubbles: true}));
                    need(!phone.querySelector('.fn-phone-detail') && button.getAttribute('aria-expanded') === 'false' && document.activeElement === button, key + ': phone Escape/focus differs');
                  }
                  const [first, second] = buttons; // explicit interaction samples; every row/function is checked above
                  if (first) {
                    first.click(); first.click(); need(!phone.querySelector('.fn-phone-detail') && document.activeElement === first, 'same phone row does not close/return focus');
                    first.click(); phone.querySelector('.fn-phone-detail .close-control').click();
                    need(!phone.querySelector('.fn-phone-detail') && document.activeElement === first, 'phone Close does not return focus');
                    first.click();
                    const handled = new KeyboardEvent('keydown', {key: 'Escape', bubbles: true, cancelable: true}); handled.preventDefault(); first.dispatchEvent(handled);
                    need(!!phone.querySelector('.fn-phone-detail'), 'handled Escape closes the phone detail');
                    first.click();
                  }
                  if (first && second) {
                    first.click(); second.click();
                    need(phone.querySelectorAll('.fn-phone-detail').length === 1 && first.getAttribute('aria-expanded') === 'false' && second.getAttribute('aria-expanded') === 'true', 'another phone row leaves old detail open');
                    second.dispatchEvent(new KeyboardEvent('keydown', {key: 'Escape', bubbles: true}));
                  }
                  return errors;
                }""".replace("/* FUNCTION_CLAIMS */", FN_CLAIMS_CHECK_JS).replace("/* PHONE_BOUNDS */", FN_PHONE_BOUNDS_JS), payload)]
    page.evaluate("document.documentElement.style.fontSize = '100%'")
    if page.locator('.fn-phone-row').count():
        row = page.locator('.fn-phone-row').first
        for key in ("Enter", " "):
            row.focus()
            page.keyboard.press(key)
            if page.locator('.fn-phone-detail').count() != 1 or row.get_attribute('aria-expanded') != 'true':
                errors.append(f"Functions phone: real keyboard {key!r} does not open the detail")
            function = page.locator('.fn-phone-detail button.fn-function-label').first
            if function.count():
                function.focus()
                page.keyboard.press(key)
                if function.get_attribute('aria-expanded') != 'true' or not page.locator('.fn-phone-claims').count():
                    errors.append(f"Functions phone: real keyboard {key!r} does not open function claims")
            page.keyboard.press('Escape')
            if page.locator('.fn-phone-detail').count() or not row.evaluate('n => document.activeElement === n'):
                errors.append(f"Functions phone: real Escape after {key!r} does not close/return focus")
        page.evaluate("window.__fnNodes = [...document.querySelector('#functions-table').querySelectorAll('*')]")
        row.click()
        page.locator('#functions').evaluate("h => h.dispatchEvent(new KeyboardEvent('keydown', {key: 'Escape', bubbles: true}))")
        if page.locator('.fn-phone-detail').count() != 1:
            errors.append('Functions phone: Escape outside the list closes the detail')
        # CSS alone switches forms: base nodes and each form's independent detail survive.
        phone_detail = page.locator('.fn-phone-detail').element_handle()
        def cross_breakpoint(width):
            page.set_viewport_size({"width": width, "height": 1000})
            # A media query's change event fires at the next rendering step, not with the resize: let two frames pass before reading focus.
            page.evaluate("() => new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)))")
            page.wait_for_selector('.fn-phone' if width <= 640 else '.fn-scroll')
            errors.extend(f"breakpoint {width}px: " + error for error in page.evaluate(FN_FORM_VISIBILITY))
            if not page.evaluate("window.__fnNodes.every(n => document.querySelector('#functions-table').contains(n))"):
                errors.append(f"Functions {width}px: crossing the breakpoint rebuilt DOM nodes")
        cross_breakpoint(641)
        if not phone_detail or not phone_detail.evaluate("n => n.isConnected && !n.getClientRects().length"):
            errors.append('Functions phone: open detail was removed or remains visible at desktop width')
        institute = row.evaluate("n => n.closest('li[data-institute]').dataset.institute")
        # Prefer the same institute to catch ID collisions when both forms have an open detail.
        cell = page.locator(f'.fn-scroll tr[data-institute="{institute}"] .fn-cell').first
        if not cell.count():
            cell = page.locator('.fn-scroll .fn-cell').first
        desktop_detail = None
        if cell.count():
            cell.click()
            desktop_detail = page.locator('.fn-scroll .fn-detail').element_handle()
            if not page.evaluate("() => { const ids=[...document.querySelector('#functions-table').querySelectorAll('[id]')].map(n=>n.id); return new Set(ids).size===ids.length; }"):
                errors.append('Functions: open forms have duplicate detail IDs')
            page.locator('.fn-scroll .fn-detail .close-control').focus()
        cross_breakpoint(640)  # pageerror listener also catches exceptions with focus inside the now-hidden form
        # Since 18 Nov 2026 the view, not the width, chooses the form, so a resize never hides the focused control on the real page;
        # the old focus hand-over between forms is gone (this check pairs view and width itself, through fn_view_by_width).
        if not phone_detail or not phone_detail.evaluate("n => n.isConnected && !!n.getClientRects().length") or row.get_attribute('aria-expanded') != 'true':
            errors.append('Functions phone: returning to phone width lost its open detail')
        if desktop_detail and not desktop_detail.evaluate("n => n.isConnected && !n.getClientRects().length"):
            errors.append('Functions desktop: open detail was removed or remains visible at phone width')
        row.press('Escape')
        if page.locator('.fn-phone-detail').count() or not row.evaluate('n => document.activeElement === n'):
            errors.append('Functions phone: Escape after crossing the breakpoint does not close/return focus')
        if desktop_detail and not desktop_detail.evaluate("n => n.isConnected"):
            errors.append('Functions phone: Escape closed the hidden desktop detail')
        cross_breakpoint(641)  # the real page keeps its view on a resize (18 Nov 2026), so there is no focus hand-over to test
        if desktop_detail:
            if not desktop_detail.evaluate("n => n.isConnected && !!n.getClientRects().length") or cell.get_attribute('aria-expanded') != 'true':
                errors.append('Functions desktop: returning to desktop width lost its open detail')
            page.locator('.fn-scroll .fn-detail .close-control').click()
            if page.locator('.fn-scroll .fn-detail').count() or not cell.evaluate('n => document.activeElement === n'):
                errors.append('Functions desktop: Close after crossing the breakpoint does not close/return focus')
        cross_breakpoint(640)
        errors += ['phone breakpoint return: ' + error for error in functions_phone_structure(page.content(), data)]
    page.close()
    context = browser.new_context(viewport={"width": 390, "height": 900}, has_touch=True, is_mobile=True)
    touch = context.new_page()
    if inject:
        touch.route(PAGE_DATA, lambda route: route.fulfill(json=data))
    hydrated(touch, base_url)
    touch.evaluate("document.fonts.ready")
    row = touch.locator('.fn-phone-row').first
    if row.count():
        row.tap()
        if touch.locator('.fn-phone-detail').count() != 1:
            errors.append('Functions phone: touch row does not open its detail')
        else:
            function = touch.locator('.fn-phone-detail button.fn-function-label').first
            if function.count():
                function.tap()
                if function.get_attribute('aria-expanded') != 'true' or touch.locator('.fn-phone-claims').count() != 1:
                    errors.append('Functions phone: touch function does not open claims')
            touch.locator('.fn-phone-detail .close-control').tap()
            if touch.locator('.fn-phone-detail').count() or not row.evaluate('n => document.activeElement === n'):
                errors.append('Functions phone: touch Close does not close/return focus')
    for item in touch.locator('.fn-phone-block > ul > li').all():
        button = item.locator(':scope > button.fn-phone-row')
        for visual in item.locator(':scope > .fn-phone-country, :scope > .fn-pips').all():
            button.evaluate("n => n.scrollIntoView({block: 'center', behavior: 'instant'})")
            point = visual.evaluate("""node => {
              /* PHONE_BOUNDS */
              const r = phoneBounds(node);
              if (!r) return null;
              const x = (r.left + r.right) / 2, y = (r.top + r.bottom) / 2;
              return {x, y, hitsButton: document.elementFromPoint(x, y) === node.parentElement.querySelector(':scope > button.fn-phone-row')};
            }""".replace("/* PHONE_BOUNDS */", FN_PHONE_BOUNDS_JS))
            key = item.get_attribute('data-institute') + '/' + visual.get_attribute('class')
            if not point or not point['hitsButton']:
                errors.append(f'Functions phone {key}: visible centre does not hit the row button')
                continue
            # Locator.tap on a pointer-transparent label would wait for it to receive the event.
            touch.touchscreen.tap(point['x'], point['y'])
            detail = item.locator(':scope > .fn-phone-detail')
            if (touch.locator('.fn-phone-detail').count() != 1 or detail.count() != 1 or
                    detail.get_attribute('id') != button.get_attribute('aria-controls') or button.get_attribute('aria-expanded') != 'true'):
                errors.append(f'Functions phone {key}: tapping the visible centre does not open its row')
                button.press('Escape')
                continue
            button.tap()
            if touch.locator('.fn-phone-detail').count() or not button.evaluate('n => document.activeElement === n'):
                errors.append(f'Functions phone {key}: tapping the same row does not close/return focus')
    context.close()
    return errors


def browser_jobs(dist: Path, data: dict, screenshots: Path | None) -> list[tuple]:
    """Every browser check as (stage, check, arguments after browser and base URL), in the order its errors are
    reported. No job depends on another: each opens its own pages."""
    # Dark once, at the widest layout: check_theme_parity compares the themes, so a layout fault is reported once, not per theme.
    passes = [(interaction_pass, (dist, data, screenshots, width, theme)) for width, theme in ((1440, "light"), (1440, "dark"), (1000, "light"), (390, "light"))]
    return [
        ("hydration", hydration_paint, ()),
        ("hydration", hydration_edges, (dist,)),
        ("hydration", check_mixed_release, ()),
        ("origins", check_origins, (dist,)),
        ("origins", check_first_load, ()),
        ("interactions", check_recovery_page, (dist, screenshots)),
        *(("interactions", check, args) for check, args in passes),
        ("interactions", shared_links, ()),
        ("interactions", check_development_edges, (data,)),
        ("interactions", check_access_edges, (data,)),
        ("interactions", check_clickable_widths, ()),
        ("interactions", check_functions_phone, (data,)),  # one job: split per width, the five page loads cost more than they saved
        ("interactions", check_funding_large_text, ()),
        ("interactions", check_published_amount_fallback, (data,)),
        ("interactions", partial(prefixed, "access: ", check_access_fixes), ()),
        ("interactions", partial(prefixed, "group rows: ", check_group_rows), ()),
        ("interactions", check_theme_default, (dist,)),
        ("interactions", check_wide_layout, ()),
        ("interactions", check_content_grid, ()),
        ("interactions", partial(prefixed, "measure: ", check_text_measure), ()),
        ("interactions", check_theme_parity, ()),
        ("interactions", check_highlight_fill, ()),
        ("interactions", check_anchor_aliases, ()),
        ("interactions", check_format_edges, (data,)),
        ("interactions", check_card_grid_edges, (data,)),
        ("interactions", check_saved_card_heads, ()),
    ]


# Jobs that start before the others: hydration, which every later stage waits on, then the longest (about 15 to
# 35 s each on an M3 Max), longest first, so the parallel run ends soon after the slowest one.
FIRST = (hydration_paint, hydration_edges, interaction_pass, check_group_rows, check_wide_layout)


def start_order(check) -> int:
    check = check.args[1] if isinstance(check, partial) else check
    return FIRST.index(check) if check in FIRST else len(FIRST)


def run_browser_job(chrome: str, dist: Path, stage: str, check, args: tuple) -> list[str]:
    from playwright.sync_api import Error as BrowserError

    try:
        with chrome_session(chrome, dist) as (browser, base_url):
            return check(browser, base_url, *args)
    except BrowserError as exc:
        if stage != "interactions":
            raise
        raise RuntimeError(f"headless Chrome interaction check {job_id(stage, check, args)} failed{raised_at(exc)}: {exc}") from exc


def load_quarantine() -> dict[str, dict]:
    """Validate the register of demonstrated harness flakes and return entries active on this run's date."""
    try:
        entries = json.loads(QUARANTINE.read_text(encoding="utf-8"))
        if not isinstance(entries, list):
            raise ValueError("expected a list of entries")
        active, seen, expired = {}, set(), []
        run_date, known = today(), set(all_job_ids())
        fields = {"job", "owner", "issue", "expires", "reason"}
        for n, entry in enumerate(entries, 1):
            label = f"entry {n}"
            if not isinstance(entry, dict) or fields - entry.keys():
                raise ValueError(f"{label}: requires job, owner, issue, expires and reason")
            if any(not isinstance(entry[key], str) or not entry[key].strip() or any(ord(c) < 32 for c in entry[key]) for key in fields):
                raise ValueError(f"{label}: fields must be non-empty text without control characters")
            jid = entry["job"]
            if jid not in known:
                raise ValueError(f"{label}: unknown job {jid}")
            if jid in seen:
                raise ValueError(f"{label}: duplicate job {jid}")
            seen.add(jid)
            issue = urlsplit(entry["issue"])
            if not (re.fullmatch(r"#[1-9][0-9]*", entry["issue"]) or (issue.scheme in {"https", "http"} and issue.netloc and not any(c.isspace() for c in entry["issue"]))):
                raise ValueError(f"{jid}: issue must be an HTTP(S) URL or #N")
            try:
                expires = date.fromisoformat(entry["expires"])
                if expires.isoformat() != entry["expires"]:
                    raise ValueError("not YYYY-MM-DD")
            except ValueError as exc:
                raise ValueError(f"{jid}: bad expires date {entry['expires']!r}; expected YYYY-MM-DD") from exc
            if (expires - run_date).days > 30:
                raise ValueError(f"{jid}: expires more than 30 days after run date {run_date}")
            if expires < run_date:
                expired.append(entry)
            else:
                active[jid] = entry
    except (OSError, ValueError) as exc:
        raise ValueError(f"quarantine {QUARANTINE}: {exc}") from exc
    for entry in expired:
        print(f"warning: expired quarantine {entry['job']} (expired {entry['expires']}, owner {entry['owner']}, {entry['issue']}); findings block again", file=sys.stderr)
    return active


class JobsNotRun(Exception):
    """The completed checks passed, but quarantined jobs were skipped."""


def skipped_job(entry: dict) -> None:
    print(f"NOT RUN: {entry['job']}: quarantined until {entry['expires']} (owner {entry['owner']}, {entry['issue']})", file=sys.stderr)


def raised_at(exc: BaseException) -> str:
    """ at check_page.py:LINE (function), the innermost frame of this file that the exception passed, or ''."""
    import traceback

    ours = [f for f in traceback.extract_tb(exc.__traceback__) if Path(f.filename).name == "check_page.py"]
    return f" at check_page.py:{ours[-1].lineno} ({ours[-1].name})" if ours else ""


STEP_TIMES: dict[str, float] = {}  # per process: the running job's step seconds, collected by timed_job


def step_clock():
    """lap(name) records the seconds since the previous lap (or since the clock started) as step name."""
    last = [time.monotonic()]

    def lap(name: str) -> None:
        now = time.monotonic()
        STEP_TIMES[name] = round(STEP_TIMES.get(name, 0) + now - last[0], 3)
        last[0] = now
    return lap


def write_timings(path: Path, text: str, mode: str = "a") -> None:
    """Best effort: timings are a report aid, so failing to write them never changes what the gate reports."""
    try:
        with path.open(mode, encoding="utf-8") as out:
            out.write(text)
    except OSError as exc:
        print(f"warning: browser job timings not written to {path}: {exc}", file=sys.stderr)


# A watchdog, not a budget: CI's slowest job takes about 70 s alone and a small one-browser host about three times
# that, while every Playwright call has its own timeout; a job still running after JOB_LIMIT is stuck outside them.
JOB_LIMIT = 600
JOB_TIMEOUT_EXIT = 124  # the exit status of a process a job's watchdog ended, as timeout(1) uses


class WorkerCrashed(RuntimeError):
    """A browser job's worker process died without a result."""


def timed_job(timings: Path | None, origin: float, run, stage: str, check, args: tuple, limit: float = JOB_LIMIT) -> list[str]:
    """run(stage, check, args) under a watchdog, appending one line about it to the timings file (JSON lines) as it
    ends: the job's id, when it started (seconds after origin, the gate's or the shard's start on this machine, by the
    monotonic clock that all processes of one machine share) and how long it took, with its error count or the type of
    the exception it raised. Written by the job's own process, so jobs of a stage the report never reaches are timed
    too; a job whose worker died has no line.

    A job still running after limit seconds is stuck: its line says exception JobTimeout, stderr names the job and
    where every thread was, and the process exits with JOB_TIMEOUT_EXIT; the Playwright driver then closes its Chrome
    as the pipe closes. In a pool the gate reports WorkerCrashed; with one job in the gate's own process (--jobs 1)
    the gate itself ends that way."""
    started, line, owner = time.monotonic(), {"job": job_id(stage, check, args)}, threading.Lock()

    def finish() -> None:
        line.update(started=round(max(started - origin, 0), 3), seconds=round(time.monotonic() - started, 3))
        if STEP_TIMES:  # a job that marks its steps (interaction_pass)
            line["steps"] = dict(STEP_TIMES)
        if timings is not None:
            write_timings(timings, json.dumps(line) + "\n")  # one short append per line; parallel jobs append whole lines

    def overrun() -> None:
        if not owner.acquire(blocking=False):
            return  # the job ended at the same moment and writes its own line
        line.update(errors=None, exception="JobTimeout")
        finish()
        print(f"browser job {line['job']} ran over {limit:g} s and is stopped; where each thread was:", file=sys.stderr, flush=True)
        faulthandler.dump_traceback(all_threads=True)
        os._exit(JOB_TIMEOUT_EXIT)

    watchdog = threading.Timer(limit, overrun)
    watchdog.daemon = True
    STEP_TIMES.clear()
    watchdog.start()
    ended = {}
    try:
        errors = run(stage, check, args)
        ended.update(errors=len(errors), exception=None)
        return errors
    except BaseException as exc:
        ended.update(errors=None, exception=type(exc).__name__)
        raise
    finally:
        if owner.acquire(blocking=False):  # whoever takes owner first writes the one line
            watchdog.cancel()
            line.update(ended)
            finish()
        else:
            watchdog.join()  # the watchdog fired first and ends this process


def submit_jobs(pool, run, work: list[tuple], order: list[int]) -> dict:
    """Submit work[n] for n in order; after a worker death breaks the pool, the rest stay unsubmitted (pool_result)."""
    from concurrent.futures.process import BrokenProcessPool

    futures = {}
    for n in order:
        try:
            futures[n] = pool.submit(run, *work[n])
        except BrokenProcessPool:
            break
    return futures


def pool_result(future, job: tuple) -> list[str]:
    """A pool job's errors; a dead worker (which breaks the whole pool) is WorkerCrashed naming the awaited job."""
    from concurrent.futures.process import BrokenProcessPool

    try:
        if future is None:
            raise BrokenProcessPool("the job was never submitted")
        return future.result()
    except BrokenProcessPool as exc:
        raise WorkerCrashed(f"browser job {job_id(*job)} has no result: a worker process died while it was awaited or "
                            "before it started (possibly SIGKILL: out of memory or an external kill, or another abrupt "
                            f"exit; a job past its limit prints 'ran over {JOB_LIMIT:g} s' on stderr and its timings "
                            "line says JobTimeout)") from exc


def read_timings(path: Path) -> list[dict]:
    """The JSON object lines of a timings file; best effort like write_timings, so a damaged file loses only timings."""
    try:
        text = path.read_text(encoding="utf-8") if path.is_file() else ""
    except (OSError, ValueError) as exc:  # ValueError: bytes that are not UTF-8
        print(f"warning: browser job timings not read from {path}: {exc}", file=sys.stderr)
        return []
    lines = []
    unparsable = 0
    for line in text.splitlines():
        try:
            lines.append(json.loads(line))
        except ValueError:
            unparsable += 1
    if unparsable:
        print(f"warning: {unparsable} unparsable browser job timing lines in {path}", file=sys.stderr)
    return [line for line in lines if isinstance(line, dict)]  # a shard's lines are validated where it is merged


def drop_timings(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        print(f"warning: browser job timings working file {path} not removed: {exc}", file=sys.stderr)


def stop_pool(pool, finished: bool) -> None:
    """Shut the pool down. After an early end (a stage not reached, an exception, an interrupt) terminate the workers
    still running jobs nobody will read, rather than wait for them; their Playwright driver then closes its Chrome."""
    processes = [] if finished else list((pool._processes or {}).values())  # no public API before Python 3.14
    pool.shutdown(wait=finished, cancel_futures=True)
    for process in processes:
        process.terminate()
    for process in processes:
        process.join(timeout=5)
        if process.is_alive():
            process.kill()
            process.join()


def inherit_browser_slot(path: str) -> None:
    """Spawned worker initialiser: only the child's environment gets the parent's lock marker.

    Workers call jobs directly, never main(); the gate entry point owns their slot.
    """
    os.environ["AISI_BROWSER_LOCK_HELD"] = path


def run_stages(run, work: list[tuple], seeds: dict, jobs: int = 1, stages: list[str] | None = None) -> list[str]:
    """Report (stage, check, args) jobs, each run as run(stage, check, args), as the sequential gate does: stages in
    the order they first appear, each only when every earlier one passed; within a stage its seed's errors (a
    function of no arguments), then each job's in list order; the first exception in that order ends the run.

    With jobs > 1 every job starts at once in that many spawned processes (FIRST first) and results are read back
    in list order, so the report and the exception are the same; the results of a stage that is not reached are
    discarded. run and every check must pickle: module-level functions or partials of them.
    """
    stages = list(dict.fromkeys(stage for stage, _, _ in work)) if stages is None else stages
    pool, futures, finished = None, {}, False
    try:
        if jobs > 1:
            import multiprocessing
            from concurrent.futures import ProcessPoolExecutor

            pool = ProcessPoolExecutor(jobs, mp_context=multiprocessing.get_context("spawn"),
                                       initializer=inherit_browser_slot, initargs=(str(lock_path()),))
            futures = submit_jobs(pool, run, work, sorted(range(len(work)), key=lambda n: start_order(work[n][1])))
        errors = []
        for stage in stages:
            if errors:
                break
            found = seeds[stage]() if stage in seeds else []
            for n, job in enumerate(work):
                if job[0] == stage:
                    found += pool_result(futures.get(n), job) if pool else run(*job)
            errors += found
        else:
            finished = True
        return errors
    finally:
        if pool:
            stop_pool(pool, finished)


def check_browser(chrome: str, dist: Path, data: dict, screenshots: Path | None = None, jobs: int = 1,
                  timings: Path | None = None, only: list[str] | None = None, quarantine: dict | None = None,
                  limit: float = JOB_LIMIT) -> list[str]:
    """Hydration, then origins, then interactions (after the page's style-source check), each only when everything
    before it passed; a failed Playwright call in the interactions stops the gate. With --screenshots a parallel run
    may also write the screenshots of a stage it then does not report; with timings, each job's line (timed_job);
    with only, just the jobs of those ids (job_id), in the same stages and order."""
    work = browser_jobs(dist, data, screenshots)
    if only is not None:
        work = [job for job in work if job_id(*job) in only]
    quarantine = load_quarantine() if quarantine is None else quarantine
    skipped = [quarantine[job_id(*job)] for job in work if job_id(*job) in quarantine]
    for entry in skipped:
        skipped_job(entry)
    stages = list(dict.fromkeys(stage for stage, _, _ in work))
    work = [job for job in work if job_id(*job) not in quarantine]
    run = partial(timed_job, timings, time.monotonic(), partial(run_browser_job, chrome, dist), limit=limit)
    errors = run_stages(run, work, browser_seeds(dist), jobs, **({"stages": stages} if skipped else {}))
    if skipped and not errors:
        raise JobsNotRun
    return errors


def all_job_ids() -> list[str]:
    """Every browser job's id, in report order; ids do not depend on the site or the data."""
    return [job_id(*job) for job in browser_jobs(Path("site"), {}, None)]


def job_ids(text: str) -> list[str]:
    """--job: comma-separated job ids, each one that exists."""
    wanted, known = list(dict.fromkeys(i.strip() for i in text.split(",") if i.strip())), all_job_ids()
    if not wanted or (unknown := [i for i in wanted if i not in known]):
        raise argparse.ArgumentTypeError(f"unknown browser job {', '.join(unknown) if wanted else '(none given)'}; --list-jobs prints them")
    return wanted


def browser_seeds(dist: Path) -> dict:
    def style_checks():
        html = (dist / "index.html").read_text()
        return check_css_tokens(html)

    return {"interactions": style_checks}


# Sharding: CI splits the browser jobs over several machines (--shard K/N) and one job reads the results back
# (--merge) through the same run_stages, after the static checks. Findings, their order and exit codes are the local
# gate's; an exception from a browser job prints the same text when main() catches its type, and any other failure
# (a dead worker, a missing or foreign shard result) still fails, with its own message.

SHARD_FORMAT = 3  # 3: a skipped (quarantined) job's outcome carries not_run
CAUGHT = (OSError, subprocess.SubprocessError, ValueError, RuntimeError)  # what main() reports as "page check failed"

# Seconds per job on CI (run 44906932395, 21 Nov 2026, the jobs of a shard sharing its runner); shards are balanced by
# them, so they affect only how long each shard takes. A job not listed counts DEFAULT_COST.
JOB_COST = {
    "hydration/check_mixed_release": 19.0,
    "interactions/check_functions_phone": 176.3,
    "hydration/hydration_edges": 39.0,
    "hydration/hydration_paint": 42.0,
    "interactions/check_access_edges": 26.0,
    "interactions/check_access_fixes": 18.6,
    "interactions/check_anchor_aliases": 23.7,
    "interactions/check_card_grid_edges": 9.2,
    "interactions/check_clickable_widths": 16.6,
    "interactions/check_content_grid": 29.2,
    "interactions/check_development_edges": 33.3,
    "interactions/check_format_edges": 23.3,
    "interactions/check_funding_large_text": 165.9,
    "interactions/check_group_rows": 33.9,
    "interactions/check_highlight_fill": 5.5,
    "interactions/check_published_amount_fallback": 4.6,
    "interactions/check_recovery_page": 4.8,
    "interactions/check_saved_card_heads": 23.6,
    "interactions/check_text_measure": 10.3,
    "interactions/check_theme_default": 19.0,
    "interactions/check_theme_parity": 5.2,
    "interactions/check_wide_layout": 84.7,
    "interactions/interaction_pass/1000/light": 64.6,
    "interactions/interaction_pass/1440/dark": 39.7,
    "interactions/interaction_pass/1440/light": 106.0,
    "interactions/interaction_pass/390/light": 70.4,
    "interactions/shared_links": 10.2,
    "origins/check_first_load": 10.3,
    "origins/check_origins": 3.6,
}
DEFAULT_COST = 5.0


class RecordedError(RuntimeError):
    """A caught exception that a shard recorded: main() prints it as it printed the original."""

    def __init__(self, message: str, stderr: str | None = None):
        super().__init__(message)
        self.stderr = stderr


class ShardJobError(Exception):
    """Any other exception a shard recorded; like the original it is not caught by main()."""


def job_id(stage: str, check, args: tuple) -> str:
    """A browser job's stable name: stage, the check (a partial's wrapped one) and an interaction pass's width and theme."""
    inner = check.args[1] if isinstance(check, partial) else check
    name = f"{stage}/{inner.__name__}"
    return f"{name}/{args[3]}/{args[4]}" if inner is interaction_pass else name


def shard_numbers(ids: list[str], count: int) -> list[int]:
    """The shard (1 to count) of every job: longest first onto the least loaded shard, ties to the lower number."""
    loads, numbers = [0.0] * count, [0] * len(ids)
    for n in sorted(range(len(ids)), key=lambda n: (-JOB_COST.get(ids[n], DEFAULT_COST), n)):
        shard = min(range(count), key=lambda s: (loads[s], s))
        loads[shard] += JOB_COST.get(ids[n], DEFAULT_COST)
        numbers[n] = shard + 1
    return numbers


def file_digests(root: Path, patterns: tuple[str, ...] = ("**/*",)) -> dict[str, str]:
    paths = {p for pattern in patterns for p in root.glob(pattern) if p.is_file()}
    return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(paths)}


def tree_digest(files: dict[str, str]) -> str:
    """sha256 over the sorted lines "path NUL sha256(file)"."""
    return hashlib.sha256("".join(f"{path}\0{digest}\n" for path, digest in sorted(files.items())).encode()).hexdigest()


def checker_digest() -> str:
    """The check scripts and every lockfile and project file that decides their environment."""
    root = Path(__file__).resolve().parents[1]
    return tree_digest(file_digests(root, ("scripts/*.py", "scripts/*.lock", "pyproject.toml", "uv.lock")))


def outcome(exc: BaseException | None = None, errors: list[str] | None = None) -> dict:
    if exc is None:
        return {"errors": list(errors)}
    stderr = exc.stderr if isinstance(exc, subprocess.CalledProcessError) and exc.stderr else None
    return {"exception": {"type": type(exc).__name__, "message": str(exc), "caught": isinstance(exc, CAUGHT),
                          "stderr": None if stderr is None else str(stderr[-2000:])}}  # as main() prints it


def run_shard(run, work: list[tuple], indices: list[int], jobs: int) -> dict[int, dict]:
    """Every job in indices, without stages or gating: the errors it returned or the exception it raised."""
    pool, futures, finished, outcomes = None, {}, False, {}
    try:
        if jobs > 1:
            import multiprocessing
            from concurrent.futures import ProcessPoolExecutor

            pool = ProcessPoolExecutor(jobs, mp_context=multiprocessing.get_context("spawn"),
                                       initializer=inherit_browser_slot, initargs=(str(lock_path()),))
            futures = submit_jobs(pool, run, work, sorted(indices, key=lambda n: start_order(work[n][1])))
        for n in indices:
            try:
                outcomes[n] = outcome(errors=pool_result(futures.get(n), work[n]) if pool else run(*work[n]))
            except Exception as exc:  # noqa: BLE001 - every failure is a result the merge must see
                outcomes[n] = outcome(exc)
        finished = True
        return outcomes
    finally:
        if pool:
            stop_pool(pool, finished)


def write_shard(chrome: str, dist: Path, data: dict, shard: tuple[int, int], results: Path, jobs: int,
                quarantine: dict | None = None) -> int:
    """Run shard K of N, preserving skipped jobs so the merge cannot count them as passed."""
    number, count = shard
    work = browser_jobs(dist, data, None)
    ids = [job_id(*job) for job in work]
    mine = [n for n, s in enumerate(shard_numbers(ids, count)) if s == number]
    quarantine = load_quarantine() if quarantine is None else quarantine
    skipped = {n: {"not_run": quarantine[ids[n]]} for n in mine if ids[n] in quarantine}
    for o in skipped.values():
        skipped_job(o["not_run"])
    results.parent.mkdir(parents=True, exist_ok=True)
    try:  # a new file, so no line of an earlier run can join this one; without one, this shard runs untimed
        handle, name = tempfile.mkstemp(prefix=results.name + ".", suffix=".timings", dir=results.parent)
        os.close(handle)
        timings = Path(name)
    except OSError as exc:
        print(f"warning: browser jobs of shard {number}/{count} not timed: {exc}", file=sys.stderr)
        timings = None
    outcomes = skipped | run_shard(partial(timed_job, timings, time.monotonic(), partial(run_browser_job, chrome, dist)),
                                  work, [n for n in mine if n not in skipped], jobs)
    site = file_digests(dist)
    results.write_text(json.dumps({
        "format": SHARD_FORMAT, "shard": number, "count": count, "jobs": ids, "checker": checker_digest(),
        "site": tree_digest(site), "site_files": site, "outcomes": {str(n): o for n, o in outcomes.items()},
        "timings": read_timings(timings) if timings else [],
    }, indent=1), encoding="utf-8")
    if timings:
        drop_timings(timings)
    found = 0
    for n in mine:
        o = outcomes[n]
        if "not_run" in o:
            continue
        lines = o["errors"] if "errors" in o else [f"{o['exception']['type']}: {o['exception']['message']}"]
        for line in lines:
            print(f"shard {number}/{count} (raw, not the gate's report; the build job merges): {ids[n]}: {line}", file=sys.stderr)
        found += len(lines) if "errors" in o else 1
    print(f"shard {number}/{count}: {len(mine) - len(skipped)} browser jobs run, {len(skipped)} NOT RUN, {found} findings; results in {results}")
    return 1 if found else NOT_RUN if skipped else 0


def load_shards(directory: Path, dist: Path, ids: list[str], timings: list[dict] | None = None) -> dict[int, dict]:
    """Every job's recorded outcome from the shard files under directory, or ValueError naming what is wrong; with
    timings, also every shard's validated timing lines there, each with its shard as K/N, in shard order."""
    def refuse(problem: str):
        raise ValueError(f"shard results in {directory}: {problem}")

    def unique_keys(pairs):
        keys = [key for key, _ in pairs]
        if len(keys) != len(set(keys)):
            raise ValueError(f"duplicate keys {sorted(k for k in set(keys) if keys.count(k) > 1)}")
        return dict(pairs)

    def count_like(value) -> bool:
        return isinstance(value, int) and not isinstance(value, bool) and value >= 1

    files = sorted(directory.glob("**/shard-*.json"))
    if not files:
        refuse("none found")
    site, checker, shards, counts, outcomes, timed = file_digests(dist), checker_digest(), {}, set(), {}, {}
    for path in files:
        try:
            result = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=unique_keys)
        except (OSError, ValueError) as exc:  # JSONDecodeError is a ValueError, as is a duplicate key
            refuse(f"{path.name} is not readable JSON without duplicate keys ({exc})")
        keys = {"format", "shard", "count", "jobs", "checker", "site", "site_files", "outcomes", "timings"}
        if not isinstance(result, dict) or set(result) != keys or not count_like(result["format"]) or result["format"] != SHARD_FORMAT:
            refuse(f"{path.name} is not a format {SHARD_FORMAT} shard result")
        if not (count_like(result["shard"]) and count_like(result["count"]) and result["shard"] <= result["count"]):
            refuse(f"{path.name} has shard {result['shard']!r} of {result['count']!r}, not K of N with 1 <= K <= N")
        if not (isinstance(result["jobs"], list) and all(isinstance(j, str) for j in result["jobs"])
                and isinstance(result["checker"], str) and isinstance(result["site"], str)
                and isinstance(result["site_files"], dict)
                and all(isinstance(k, str) and isinstance(v, str) for k, v in result["site_files"].items())):
            refuse(f"{path.name} has malformed jobs, digests or site files")
        if result["shard"] in shards:
            refuse(f"shard {result['shard']} appears twice ({shards[result['shard']]} and {path.name})")
        shards[result["shard"]] = path.name
        counts.add(result["count"])
        if result["jobs"] != ids:
            refuse(f"{path.name} lists other browser jobs than this gate")
        if result["checker"] != checker:
            refuse(f"{path.name} was produced by other check scripts")
        if result["site"] != tree_digest(site):
            changed = sorted(k for k in set(site) | set(result["site_files"]) if site.get(k) != result["site_files"].get(k))
            refuse(f"{path.name} checked another build (differs in {', '.join(changed[:10]) or 'its digest'})")
        if not isinstance(result["outcomes"], dict):
            refuse(f"{path.name} has no outcomes")
        if not (isinstance(result["timings"], list) and all(timing_ok(t) and t["job"] in ids for t in result["timings"])):
            refuse(f"{path.name} has malformed timings")
        timed[result["shard"]] = result["timings"]
        for key, o in result["outcomes"].items():
            if not key.isdigit() or not 0 <= int(key) < len(ids):
                refuse(f"{path.name} has an outcome for job {key!r}, which does not exist")
            if int(key) in outcomes:
                refuse(f"job {ids[int(key)]} has outcomes in two shards")
            errors_ok = isinstance(o, dict) and set(o) == {"errors"} and isinstance(o["errors"], list) and all(isinstance(e, str) for e in o["errors"])
            exception = o.get("exception") if isinstance(o, dict) and set(o) == {"exception"} else None
            exception_ok = (isinstance(exception, dict) and set(exception) == {"type", "message", "caught", "stderr"}
                            and isinstance(exception["type"], str) and isinstance(exception["message"], str)
                            and isinstance(exception["caught"], bool) and isinstance(exception["stderr"], (str, type(None))))
            skipped = o.get("not_run") if isinstance(o, dict) and set(o) == {"not_run"} else None
            skipped_ok = (isinstance(skipped, dict) and {"job", "owner", "issue", "expires", "reason"} <= skipped.keys()
                          and skipped["job"] == ids[int(key)]
                          and all(isinstance(skipped[k], str) and skipped[k].strip() and not any(ord(c) < 32 for c in skipped[k])
                                  for k in ("job", "owner", "issue", "expires", "reason")))
            if not (errors_ok or exception_ok or skipped_ok):
                refuse(f"{path.name} has a malformed outcome for job {ids[int(key)]}")
            outcomes[int(key)] = o
    if len(counts) != 1 or set(shards) != set(range(1, next(iter(counts)) + 1)):
        refuse(f"expected shards 1 to N of one N, found {sorted(shards)} of {sorted(counts)}")
    if missing := [ids[n] for n in range(len(ids)) if n not in outcomes]:
        refuse(f"no outcome for {', '.join(missing)}")
    if timings is not None:
        timings += [{**t, "shard": f"{number}/{len(shards)}"} for number, lines in sorted(timed.items()) for t in lines]
    return outcomes


def timing_ok(t) -> bool:
    """A timed_job line: what the shard format allows in its timings."""
    number = lambda v: isinstance(v, (int, float)) and not isinstance(v, bool) and v >= 0  # noqa: E731
    steps = t.get("steps", {}) if isinstance(t, dict) else None
    return (isinstance(t, dict) and set(t) - {"steps"} == {"job", "started", "seconds", "errors", "exception"} and isinstance(t["job"], str)
            and isinstance(steps, dict) and all(isinstance(k, str) and number(v) for k, v in steps.items())
            and number(t["started"]) and number(t["seconds"]) and (t["errors"] is None) != (t["exception"] is None)
            and (t["errors"] is None or (isinstance(t["errors"], int) and not isinstance(t["errors"], bool) and t["errors"] >= 0))
            and (t["exception"] is None or isinstance(t["exception"], str)))


def recorded(outcomes: dict[int, dict], stage: str, n: int, args: tuple) -> list[str]:
    o = outcomes[n]
    if "errors" in o:
        return o["errors"]
    exc = o["exception"]
    if exc["caught"]:
        raise RecordedError(exc["message"], exc["stderr"])
    raise ShardJobError(f"{exc['type']}: {exc['message']}")


def check_merged(dist: Path, data: dict, directory: Path, timings: Path | None = None) -> list[str]:
    """check_browser's report from shard results instead of running the browser jobs; with timings, every shard's
    job lines there, each with its shard (started counts from that shard's start). Skipped jobs stay NOT RUN even
    after their quarantine expires: the shards contain no findings for them."""
    work = browser_jobs(dist, data, None)
    lines, ids = [], [job_id(*job) for job in work]
    outcomes = load_shards(directory, dist, ids, lines)
    if timings is not None:
        write_timings(timings, "".join(json.dumps(t) + "\n" for t in lines), "w")
    skipped = [o["not_run"] for o in outcomes.values() if "not_run" in o]
    for entry in skipped:
        skipped_job(entry)
    indexed = [(stage, n, ()) for n, (stage, _, _) in enumerate(work) if "not_run" not in outcomes[n]]
    errors = run_stages(partial(recorded, outcomes), indexed, browser_seeds(dist), stages=list(dict.fromkeys(stage for stage, _, _ in work)))
    if skipped and not errors:
        raise JobsNotRun
    return errors


def expected_budget_ratios(record, jurisdiction, data):
    """Independent oracle for the generated budget ratios (rule budget-ratios): raw Decimal arithmetic
    from the annual amount, the rates and the recorded denominators, six figures, a bound rounded so it stays true."""
    rounding = {"at_least": ROUND_FLOOR, "more_than": ROUND_FLOOR, "at_most": ROUND_CEILING, "less_than": ROUND_CEILING, "nearly": ROUND_CEILING}.get(record.get("amount_qualifier"), ROUND_HALF_UP)
    rates = {r["currency"]: Decimal(str(r["eur_per_unit"])) for r in data["dataset"]["exchange_rates"]} | {"EUR": Decimal(1)}

    def six(value):
        if value is None:
            return None
        value = value.quantize(Decimal(1).scaleb(value.adjusted() - 5), rounding=rounding) if value else value
        return float(value)

    with localcontext() as context:
        context.prec = 50
        eur = expected_annual_amount(record, data)
        population = (jurisdiction.get("population") or {}).get("value")
        gdp = jurisdiction.get("gdp") or {}
        per_person = eur / population if population else None
        share = None
        if gdp.get("value") and gdp["currency"] == record["currency"]:
            share = expected_annual_native(record) / gdp["value"]
        elif gdp.get("value") and gdp["currency"] in rates:
            share = eur / (gdp["value"] * rates[gdp["currency"]])
        label = None
        if per_person is not None:
            two = per_person.quantize(Decimal(1).scaleb(per_person.adjusted() - 1), rounding=rounding) if per_person else per_person
            if per_person and two.adjusted() != per_person.adjusted():
                two = two.quantize(Decimal(1).scaleb(two.adjusted() - 1))
            prefix = {"about": "about ", "at_most": "up to ", "at_least": "at least ", "nearly": "nearly ", "more_than": "more than ", "less_than": "less than "}.get(record.get("amount_qualifier"), "")
            mark = "" if record.get("amount_qualifier") == "about" or (record["currency"] == "EUR" and two == per_person) else "~"
            label = f"{prefix}{mark}EUR {two:,f} per person"
        return six(per_person), six(share), label


def check_budget_ratio_downloads(dist: Path, data: dict) -> list[str]:
    """Per person and share of GDP in aisi.json and funding.csv equal the oracle on a state's compared record, and appear nowhere else."""
    jurisdictions = {j["id"]: j for j in data["jurisdictions"]}
    rows = {(r["institute_id"], r["id"]): r for r in csv.DictReader(io.StringIO((dist / "data/funding.csv").read_text(encoding="utf-8")))}
    errors, compared = [], 0
    for inst in data["institutes"]:
        jurisdiction = jurisdictions[inst["jurisdiction_id"]]
        chosen = inst["display"].get("comparison_funding") if jurisdiction["kind"] == "state" else None
        for record in inst.get("funding", []):
            where = f"{inst['id']} funding {record['id']}"
            row = rows.get((inst["id"], record["id"]))
            if row is None:
                errors.append(f"{where}: missing from funding.csv")
                continue
            if record["id"] != chosen:
                if {"eur_per_person_est", "share_of_gdp_est", "ratio_denominators"} & record.keys() or "per_person" in record.get("display", {}) or row["eur_per_person_est"] or row["share_of_gdp_est"]:
                    errors.append(f"{where}: budget ratios on a record that is not a state's compared figure")
                continue
            compared += 1
            per_person, share, label = expected_budget_ratios(record, jurisdiction, data)
            if (record.get("eur_per_person_est"), record.get("share_of_gdp_est")) != (per_person, share):
                errors.append(f"{where}: aisi.json ratios {(record.get('eur_per_person_est'), record.get('share_of_gdp_est'))} differ from the expected {(per_person, share)}")
            csv_values = tuple(float(row[k]) if row[k] else None for k in ("eur_per_person_est", "share_of_gdp_est"))
            if csv_values != (per_person, share):
                errors.append(f"{where}: funding.csv ratios {csv_values} differ from the expected {(per_person, share)}")
            years = (row["population_year"], row["gdp_year"])
            expected_years = tuple(str(jurisdiction[k]["year"]) if v is not None else "" for k, v in (("population", per_person), ("gdp", share)))
            if years != expected_years:
                errors.append(f"{where}: funding.csv denominator years {years}, expected {expected_years}")
            if record.get("display", {}).get("per_person") != label:
                errors.append(f"{where}: per-person label {record.get('display', {}).get('per_person')!r}, expected {label!r}")
    if not compared:
        errors.append("no compared state budget found to check the budget ratios against")
    return errors


def check_state_downloads(dist: Path, data: dict) -> list[str]:
    """Every state's API file holds its record and its institute's (or null), with every excerpt pointer resolving inside the file to the value it supports in aisi.json."""

    def at(document, pointer):
        for part in pointer.split("/")[1:]:
            part = part.replace("~1", "/").replace("~0", "~")
            document = document[int(part)] if isinstance(document, list) else document[part]
        return document

    root = dist / f"api/v{data['dataset']['version'].split('.')[0]}/jurisdictions"
    institutes = {i["id"]: n for n, i in enumerate(data["institutes"])}
    errors = []
    for n, jurisdiction in enumerate(data["jurisdictions"]):
        path = root / f"{jurisdiction['id']}.json"
        if not path.is_file():
            errors.append(f"{jurisdiction['id']}: no state download at {path.relative_to(dist)}")
            continue
        served = json.loads(path.read_text(encoding="utf-8"))
        parts = [(f"/jurisdictions/{n}", "/record")]
        if served.get("record") != jurisdiction:
            errors.append(f"{jurisdiction['id']}: state download record differs from aisi.json")
        iid = jurisdiction.get("institute_id")
        embedded = served.get("institute", "missing")
        if iid is None and embedded is not None:
            errors.append(f"{jurisdiction['id']}: state download embeds an institute but the state has none")
        elif iid is not None:
            expected = f"/institutes/{institutes.get(iid)}"
            if not isinstance(embedded, dict) or embedded.get("aisi_json_pointer") != expected or embedded.get("record") != data["institutes"][institutes[iid]]:
                errors.append(f"{jurisdiction['id']}: state download does not embed institute {iid} as published")
            parts.append((expected, "/institute/record"))
        for source in served.get("sources", []):
            for excerpt in source.get("excerpts", []):
                for pointer in excerpt["supports"]:
                    match = next(((p, t) for p, t in sorted(parts, key=lambda x: -len(x[1])) if pointer == t or pointer.startswith(t + "/")), None)
                    try:
                        same = match is not None and at(served, pointer) == at(data, match[0] + pointer[len(match[1]):])
                    except (KeyError, IndexError, ValueError, TypeError):
                        same = False
                    if not same:
                        errors.append(f"{jurisdiction['id']}: excerpt pointer {pointer} in source {source['id']} does not resolve to its aisi.json value")
    return errors


# Favicon links per page. The error page is served at any nested missing address, so its link is
# root-relative (custom domain root); every other page links relatively, so it also works under a subpath.
FAVICON_HREFS = {
    "index.html": ({"favicon.svg"}, "expected relative SVG favicon"),
    "404.html": ({"/favicon.svg"}, "expected root-relative SVG favicon"),
    "method.html": ({"favicon.svg"}, "expected relative SVG favicon"),
}


def check_page_data(dist: Path, data: dict) -> list[str]:
    """data/page.json is what the build writes for aisi.json (display.page_data), minified; the page loads it by a URL carrying the
    dataset version and the file's hash; and the page script reads none of the fields the build leaves out
    (display.PAGE_DATA_DROPPED, the one list of them)."""
    import hashlib
    from display import PAGE_DATA_DROPPED, page_data

    errors = []
    path = dist / "data/page.json"
    if not path.is_file():
        return ["data/page.json: missing"]
    text = path.read_text(encoding="utf-8")
    want = json.loads(page_data(data))
    page = json.loads(text)
    mark = page.pop("build_mark", None)
    if list(json.loads(text))[-1:] != ["build_mark"]:
        errors.append("data/page.json: no build_mark as its last key (the page compares it with its own to catch a mixed release)")
    if page != want:
        errors.append("data/page.json differs from display.page_data(aisi.json)")
    if "\n" in text or ": " in text[:200]:
        errors.append("data/page.json is not minified")
    # The build mark is the version and the hash of the file without it; the data URL and the page carry the same.
    unmarked = json.dumps(page, ensure_ascii=False, separators=(",", ":"))
    want_mark = f"{data['dataset']['version']}-{hashlib.sha256(unmarked.encode('utf-8')).hexdigest()[:10]}"
    if mark != want_mark:
        errors.append(f"data/page.json: build_mark is {mark!r}, expected {want_mark!r} (the version and the hash of the file without it)")
    soup = BeautifulSoup((dist / "index.html").read_text(encoding="utf-8"), "html.parser")
    meta = soup.select('head meta[name="aisi-page-data"]')
    url = f"data/page.json?v={want_mark}"
    if len(meta) != 1 or meta[0].get("content") != url:
        errors.append(f"index.html: the page data URL is {[m.get('content') for m in meta]}, expected {url}")
    if len(meta) == 1 and meta[0].get("data-build") != want_mark:
        errors.append(f"index.html: the page's build mark (data-build) is {meta[0].get('data-build')!r}, expected {want_mark!r}")
    package = (dist / "data/datapackage.json").read_text(encoding="utf-8")
    if len(meta) == 1 and meta[0].get("data-package") != f"data/datapackage.json?v={data['dataset']['version']}-{hashlib.sha256(package.encode('utf-8')).hexdigest()[:10]}":
        errors.append(f"index.html: the data package URL {meta[0].get('data-package')!r} is not versioned by its content")
    script = "\n".join(node.get_text() for node in soup.select("body > script:not([src])"))
    kept = "|".join((*PAGE_DATA_DROPPED["check"], "functions"))
    dropped = [rf"\b{key}\b" for key in (*PAGE_DATA_DROPPED["sources"], *PAGE_DATA_DROPPED["records"])]
    for pattern in (*dropped, rf"\.check\.(?!(?:{kept})\b)\w+", r"\.check\[", r"\bcheck\)"):
        for found in re.finditer(pattern, script):
            errors.append(f"page script reads a field data/page.json leaves out: {script[max(0, found.start() - 40):found.end() + 20]!r}")
    return errors


def check_launch_assets(dist: Path) -> list[str]:
    """Recovery links must work at nested missing URLs on the custom domain."""
    errors = []
    for name in ("index.html", "404.html", "method.html"):
        path = dist / name
        if not path.is_file():
            errors.append(f"{name}: missing launch page")
            continue
        soup = BeautifulSoup(path.read_text(encoding="utf-8"), "html.parser")
        if name == "method.html":
            errors.extend(f"{name}: {error}" for error in check_footer_licence(soup))
            back = soup.select_one('footer a[href="index.html#method-sec"]')
            if not back or not back.get_text(strip=True):
                errors.append("method.html: missing footer back link")
        icons = soup.select('head link[rel="icon"]')
        hrefs, message = FAVICON_HREFS[name]
        if (
            len(icons) != 1
            or icons[0].get("href") not in hrefs
            or icons[0].get("type") != "image/svg+xml"
        ):
            errors.append(f"{name}: {message}")
        if name == "404.html":
            if not soup.select_one('meta[name="robots"][content="noindex"]'):
                errors.append("404.html: missing noindex")
            heading = soup.select_one("main h1")
            home = soup.select_one('main a[href="/"]')
            if heading is None:
                errors.append("404.html: missing h1")
            if not home or not home.get_text(strip=True):
                errors.append("404.html: missing root recovery link")
            if soup.select("script[src], link[rel=stylesheet], img, iframe"):  # an inline script (the stored theme) loads nothing
                errors.append("404.html: recovery page must be self-contained")
    try:
        icon = ElementTree.parse(dist / "favicon.svg").getroot()
        if (
            icon.tag != "{http://www.w3.org/2000/svg}svg"
            or icon.get("viewBox") != "0 0 32 32"
        ):
            errors.append("favicon.svg: expected a 32-unit SVG icon")
    except (OSError, ElementTree.ParseError):
        errors.append("favicon.svg: missing or invalid SVG")
    licence = dist / "LICENSE-DATA"
    if (
        not licence.is_file()
        or "Creative Commons Attribution 4.0 International Public License"
        not in licence.read_text(encoding="utf-8")
    ):
        errors.append("LICENSE-DATA: missing CC BY 4.0 legal code")
    code_licence = dist / "LICENSE"
    if (
        not code_licence.is_file()
        or "Permission is hereby granted, free of charge"
        not in code_licence.read_text(encoding="utf-8")
    ):
        errors.append("LICENSE: missing MIT licence text")
    return errors


def check_recovery_page(
    browser, base_url: str, dist: Path, screenshots: Path | None
) -> list[str]:
    """Serve the custom error body with status 404 at a nested missing address."""
    errors = []
    for width in (1440, 1000, 390):
        for theme in ("light", "dark"):
            page = themed_page(
                browser, theme, viewport={"width": width, "height": 1000}
            )
            page.route(
                "**/missing/nested/page",
                lambda route: route.fulfill(
                    status=404,
                    content_type="text/html",
                    body=(dist / "404.html").read_text(encoding="utf-8"),
                ),
            )
            response = page.goto(base_url + "missing/nested/page")
            label = f"404 {width}px {theme}"
            if page.evaluate("getComputedStyle(document.body).backgroundColor") != GROUNDS[theme]:
                errors.append(f"{label}: the error page does not follow the stored {theme} theme")
            if response.status != 404:
                errors.append(
                    f"{label}: missing address returned HTTP {response.status}, expected 404"
                )
            if not page.locator("main h1").is_visible():
                errors.append(f"{label}: error page is not visible")
            if page.evaluate("document.documentElement.scrollWidth > innerWidth"):
                errors.append(f"{label}: horizontal overflow")
            favicon_url = page.locator('link[rel="icon"]').evaluate("node => node.href")
            icon_response = page.request.get(favicon_url)
            if favicon_url != base_url + "favicon.svg" or icon_response.status != 200:
                errors.append(f"{label}: nested error page cannot load favicon")
            page.keyboard.press("Tab")
            if page.locator("a").evaluate(
                "node => node !== document.activeElement || getComputedStyle(node).outlineStyle === 'none'"
            ):
                errors.append(f"{label}: recovery link lacks keyboard focus")
            if screenshots:
                screenshots.mkdir(parents=True, exist_ok=True)
                page.screenshot(path=str(screenshots / f"404-{width}-{theme}.png"))
            page.locator('main a[href="/"]').click()
            if page.url != base_url:
                errors.append(f"{label}: recovery link did not return to tracker")
            page.close()
    return errors


@browser_slot("gate", wait=None)
def main(dist: Path = ROOT / "dist", screenshots: Path | None = None, capture: Path | None = None, jobs: int = 1,
         shard: tuple[int, int] | None = None, results: Path | None = None, merge: Path | None = None,
         timings: Path | None = None, only: list[str] | None = None, use_quarantine: bool = True, deadline: float | None = None,
         skip_static: bool = False) -> int:
    if timings is not None:  # no line from an earlier run survives this one, whatever happens next
        write_timings(timings, "", "w")
    guard_setup(deadline)
    try:  # the register is checked on every run, shards and merge included, before any browser work
        quarantine = load_quarantine()
        if not use_quarantine:  # still validated above; the detector sees every finding as it is
            quarantine = {}
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    chrome = find_chrome()
    if chrome is None:
        print(f"NOT RUN: {chrome_problem()}; rendered page checks were not run.", file=sys.stderr)
        return NOT_RUN
    # The browsers that start at once: no more than the selected jobs (--job) when there are fewer.
    if not (merge or capture) and (problem := jobs_problem(min(jobs, len(only)) if only else jobs, memory_gib())):
        print(f"NOT RUN: {problem}; rendered page checks were not run.", file=sys.stderr)
        return NOT_RUN
    try:
        if not (dist / "index.html").is_file():
            raise ValueError(
                f"{dist} has no index.html; build first: uv run --locked scripts/build.py --out {dist}"
            )
        if capture:
            capture_states(chrome, dist, capture)
            print(f"captured fixed page states in {capture}")
            return 0
        data = json.loads((dist / "data/aisi.json").read_text(encoding="utf-8"))
        if shard:
            return write_shard(chrome, dist, data, shard, results, jobs, quarantine)
        html, base_url = ("", None) if skip_static else render(chrome, dist)
        errors = [] if skip_static else check_method_pages(html, data, dist) + check_dom(html, data, dist, base_url) + check_agent_files(dist) + check_static_page(dist, data) + check_hydrated(html) + check_archive_links(html, data) + check_reread_lines(html, data) + check_launch_assets(dist) + check_page_data(dist, data) + check_budget_ratio_downloads(dist, data) + check_state_downloads(dist, data) + check_state_download_buttons(html, dist, data)
        if not errors:
            errors.extend(check_merged(dist, data, merge, timings) if merge else check_browser(chrome, dist, data, screenshots, jobs, timings, only, quarantine, job_limit(deadline)))
    except JobsNotRun:
        return NOT_RUN
    except DeadlinePassed as exc:
        print(exc, file=sys.stderr)
        return NOT_RUN
    except CAUGHT as exc:
        print(f"error: page check failed: {exc}", file=sys.stderr)
        if isinstance(exc, subprocess.CalledProcessError) and exc.stderr:
            print(exc.stderr[-2000:], file=sys.stderr)
        if isinstance(exc, RecordedError) and exc.stderr:
            print(exc.stderr, file=sys.stderr)  # recorded already as printed above
        return 1
    if errors:
        for error in errors:
            print(f"error: {error}", file=sys.stderr)
        return 1
    if only is not None:  # never mistaken for the gate
        print(f"ok: {'' if skip_static else 'static checks and '}{len(only)} selected browser job{'' if len(only) == 1 else 's'} passed ({', '.join(only)}); not the full gate"
              + ("; static checks skipped (--skip-static)" if skip_static else ""))
        return 0
    print(
        f"ok: rendered page and interaction checks passed ({len(data['jurisdictions'])} jurisdictions; {urlsplit(base_url).hostname})"
    )
    return 0


class DeadlinePassed(Exception):
    """--job-deadline ran out before the browser jobs started."""


def guard_setup(deadline: float | None) -> None:
    """With --job-deadline, end this process as NOT_RUN, with DEADLINE_PASSED, if it is still running at the
    deadline: start-up, the static checks and the hand-over to the job are bounded too, not only the job. It stays
    armed for the whole run: the job's own watchdog, set to the same deadline, normally stops a stuck job first and
    writes its JobTimeout line; whichever fires, the run ends by the deadline and never reports a pass after it."""
    if deadline is None:
        return

    def passed() -> None:
        print(DEADLINE_PASSED, file=sys.stderr, flush=True)
        os._exit(NOT_RUN)  # the Playwright driver closes its Chrome as the pipe closes, as timed_job's watchdog relies on

    timer = threading.Timer(max(deadline - (time.monotonic() - PROCESS_START), 0), passed)
    timer.daemon = True
    timer.start()


def job_limit(deadline: float | None) -> float:
    """The watchdog limit for browser jobs: JOB_LIMIT, or less when --job-deadline (seconds from this process's
    start) leaves less; it says so on stderr, so a stop at that limit is told apart from a job's own timeout."""
    if deadline is None:
        return JOB_LIMIT
    left = deadline - (time.monotonic() - PROCESS_START)
    if left < 1:
        raise DeadlinePassed(DEADLINE_PASSED)
    if left < JOB_LIMIT:
        print(f"{DEADLINE_LOWERED} {left:.0f} s", file=sys.stderr, flush=True)
        return left
    return JOB_LIMIT


def shard_spec(text: str) -> tuple[int, int]:
    number, _, count = text.partition("/")
    if not (number.isdigit() and count.isdigit() and 1 <= int(number) <= int(count)):
        raise argparse.ArgumentTypeError(f"expected K/N with 1 <= K <= N, not {text}")
    return int(number), int(count)


def job_count(text: str) -> int:
    if int(text) < 1:
        raise argparse.ArgumentTypeError(f"needs at least one job, not {text}")
    return int(text)


def default_jobs(environ=os.environ, memory: float | None = None) -> int:
    """Browser jobs at once when --jobs is not given (host_checks.default_jobs; memory read when not given). A
    setting the memory does not hold is refused when the browser jobs start (jobs_problem)."""
    try:
        return host_default_jobs(environ, memory_gib() if memory is None else memory)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dist",
        type=Path,
        default=ROOT / "dist",
        help="built site to check (default: dist/); it is never rebuilt here",
    )
    parser.add_argument(
        "--screenshots",
        type=Path,
        help="Save desktop and phone screenshots in both themes",
    )
    parser.add_argument(
        "--capture",
        type=Path,
        help="only capture fixed page states for a contact sheet into this directory; runs no checks",
    )
    parser.add_argument(
        "--jobs",
        type=job_count,
        default=default_jobs(),
        help="browser checks in this many parallel processes (default: AISI_GATE_JOBS, else the CPUs, at most 8 and at most what the memory holds at 4 GiB each); 1 runs them one after another",
    )
    parser.add_argument("--shard", type=shard_spec, help="CI: run only shard K/N of the browser jobs, no static checks, and write --results")
    parser.add_argument("--results", type=Path, help="with --shard: where to write the shard's results (JSON)")
    parser.add_argument("--merge", type=Path, help="CI: run the static checks, then report the browser jobs from the shard results under this directory")
    parser.add_argument("--job", type=job_ids, help="run only these browser jobs (comma-separated ids, see --list-jobs) after the static checks; "
                        "a debugging and repetition aid, never the gate")
    parser.add_argument("--list-jobs", action="store_true", help="only print every browser job's id, in report order")
    parser.add_argument("--job-deadline", type=float, help="with --job, one job and --jobs 1: the run must end within this many seconds "
                        "of this script's start, after its imports (static checks and the job count): its watchdog gets what is left, "
                        f"at most {JOB_LIMIT} s, and a timer ends the run NOT_RUN at the deadline. The flake detector keeps a batch "
                        "within its budget so, and also stops a whole attempt 30 s past its deadline")
    parser.add_argument("--skip-static", action="store_true", help="with --job and --no-quarantine: run only the selected jobs, without the "
                        "page render and static checks (the flake detector repeats jobs so; never the gate)")
    parser.add_argument("--no-quarantine", action="store_true", help="run quarantined jobs too (the flake detector measures their findings)")
    parser.add_argument("--timings", type=Path, help="write one JSON line per browser job (id, start, seconds, errors) to this file; with --merge, the shards' lines")
    parser.add_argument(
        "--find-chrome",
        action="store_true",
        help="only print the Chromium path this check would use; exit NOT_RUN if none",
    )
    parser.add_argument(
        "--install-chrome",
        action="store_true",
        help="only install the locked Playwright's Chromium, which build, gate and contact sheet use",
    )
    parser.add_argument("--with-deps", action="store_true", help="with --install-chrome on Linux: also install Chromium's system libraries (asks for sudo)")
    args = parser.parse_args()
    if args.skip_static and not (args.job and args.no_quarantine):
        parser.error("--skip-static is for the flake detector: it needs --job and --no-quarantine")
    if args.job_deadline is not None and (not args.job or len(args.job) != 1 or args.jobs != 1 or not 0 < args.job_deadline < math.inf):
        parser.error("--job-deadline needs --job with exactly one job, --jobs 1 (the job runs in this process, under its deadline) "
                     "and a value above 0")
    if sum(bool(x) for x in (args.shard, args.merge, args.capture)) > 1:
        parser.error("--shard, --merge and --capture exclude each other")
    if bool(args.shard) != bool(args.results):
        parser.error("--shard and --results go together")
    if args.with_deps and not args.install_chrome:
        parser.error("--with-deps goes with --install-chrome")
    if args.install_chrome:
        sys.exit(install_chrome(args.with_deps))
    if args.find_chrome:
        found = find_chrome()
        print(found or "")
        if not found:
            print(chrome_problem(), file=sys.stderr)
        sys.exit(0 if found else NOT_RUN)
    if args.list_jobs:
        print("\n".join(all_job_ids()))
        sys.exit(0)
    if args.job and (args.shard or args.merge or args.capture):
        parser.error("--job runs browser jobs here: it excludes --shard, --merge and --capture")
    if args.timings and (args.shard or args.capture):
        parser.error("--timings goes with a local gate or --merge (a shard's results carry its timings)")
    sys.exit(main(args.dist.resolve(), args.screenshots, args.capture, args.jobs, args.shard, args.results, args.merge and args.merge.resolve(),
                  args.timings and args.timings.resolve(), args.job, not args.no_quarantine,
                  args.job_deadline, args.skip_static))
