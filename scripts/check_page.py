# /// script
# requires-python = ">=3.11"
# dependencies = ["beautifulsoup4>=4.12", "playwright>=1.51"]
# ///
"""Check an already built site with Chrome; this script never builds or writes the checkout.

Run from any directory: uv run scripts/build.py --out DIR && uv run scripts/check_page.py --dist DIR
(--dist defaults to dist/). Exit 0 when every check passed, 1 on any failure, and NOT_RUN (3)
when Playwright's Chromium is missing, so callers can tell "not run" from "passed".
"""

import argparse
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
from html.parser import HTMLParser
import threading
import time
from collections import Counter, defaultdict
from contextlib import contextmanager, nullcontext
from datetime import date
from functools import partial, wraps
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP, Decimal, localcontext
from pathlib import Path
from xml.etree import ElementTree
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

sys.path.insert(0, str(Path(__file__).resolve().parent))
from browser_lock import browser_slot, lock_path  # noqa: E402
from display import GENERIC_FIELDS, record_sources as entry_sources  # noqa: E402
from method_copy import rule_label  # noqa: E402
from release_checks import today  # noqa: E402

PROCESS_START = time.monotonic()  # --job-deadline counts from here: after the imports above, before everything else
DEADLINE_LOWERED = "job deadline: watchdog lowered to"  # stderr marker the flake detector reads
DEADLINE_PASSED = "NOT RUN: job deadline passed"
ROOT = Path(__file__).resolve().parents[1]
QUARANTINE = ROOT / "scripts/quarantine.json"
NOT_RUN = 3
# A finding that starts with REPORT_MARK, or carries it after a caller's prefix (a width, theme or check name), is
# cosmetic drift (exact spacing, font weights, decorative alignment; test strategy D1) or a quarantined harness
# finding (D5). It travels with the
# errors, so stages, shards and the merge carry it unchanged, but it never fails the gate or stops a stage; the gate
# prints it as a report line (REPORT_LINE) and check.py lists it in its report for review. The mark is a control
# character, so no page text, data value or exception message carries it by accident; in-page checks write it as
# REPORT_JS's REPORT.
REPORT_MARK = "\x1f"
REPORT_LINE = "report line: "
REPORT_JS = f"const REPORT = {json.dumps(REPORT_MARK)};"


def report_line(text: str) -> str:
    return REPORT_MARK + text


def is_report_line(finding: str) -> bool:
    return REPORT_MARK in finding


def printed_report_line(finding: str) -> str:
    """How the gate prints a report line: REPORT_LINE first, the caller's prefix and the text after it, unmarked."""
    return REPORT_LINE + finding.replace(REPORT_MARK, "")


def check_reserved_mark(dist: Path, html: str) -> list[str]:
    """REPORT_MARK is reserved for report lines: no built file or rendered page may carry it, raw or JSON-escaped, so
    page text quoted in a blocking finding can never demote it. Blocking, and run before every other check."""
    text = {".html", ".json", ".js", ".svg", ".xml", ".txt", ".csv", ".cff", ".md"}
    files = [p for p in sorted(dist.rglob("*")) if p.is_file()]
    found = [p.relative_to(dist).as_posix().replace(REPORT_MARK, "\\x1f") for p in files if REPORT_MARK in p.relative_to(dist).as_posix()
             or (p.suffix in text and (REPORT_MARK.encode() in (b := p.read_bytes()) or b"\\u001f" in b.lower()))]
    found += ["the rendered page"] if REPORT_MARK in html else []
    return [f"reserved report-line mark U+001F found in {', '.join(found)}: remove it (control characters do not belong in the site)"] if found else []


def blocking(findings: list[str]) -> list[str]:
    """The findings that fail the gate: all but report lines."""
    return [f for f in findings if not is_report_line(f)]


def reports(check):
    """A check whose every finding is a report line: it pins cosmetic drift only (exact spacing, type roles,
    decorative alignment). It keeps its name, so it stays one browser job with the same id and pickles as before."""
    @wraps(check)
    def reported(*args, **kwargs) -> list[str]:
        return [report_line(finding) for finding in check(*args, **kwargs)]
    return reported
# One browser for build, gate and contact sheet on every host: the Chromium of the Playwright version this
# script's lock pins (prerender.py and monitor_browser.py pin the same, a test checks), in Playwright's default browser folder.
INSTALL_CHROME = "uv run --locked scripts/check_page.py --install-chrome"


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


def find_chrome() -> str | None:
    """The locked Playwright's Chromium, or None when it is not installed (INSTALL_CHROME). It runs as an ordinary
    child process, so a memory cap on the job holds it; a snap browser moved into its own scope."""
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
# off-screen sections, interaction times, the hydration flash) open theirs with skipping=True.
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


def install_chrome() -> int:
    """Download the locked Playwright's Chromium (no headless shell: every launch names this browser). On Linux
    its system libraries are a separate, root step: python -m playwright install-deps chromium."""
    code = subprocess.run([sys.executable, "-m", "playwright", "install", "--no-shell", "chromium"]).returncode
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

    source_urls = {source["url"] for source in data["sources"]}
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
    entries = soup.select(".dlist > li")
    links = soup.select(".dlist a")
    downloads = Counter(urljoin(base_url, str(a.get("href") or "")) for a in links)
    expected_downloads = Counter({urljoin(base_url, name): 1 for name in files})
    if not files or len(entries) != len(files) or downloads != expected_downloads:
        errors.append(
            f"downloads: expected one entry per CSV/JSON file ({len(files)}), got {len(entries)} entries and {len(links)} links"
        )
    return errors


DOWNLOAD_GROUPS = [
    ("CSV", ["jurisdictions.csv", "institutes.csv", "events.csv"]),
    ("CSV", ["staff.csv", "funding.csv", "people.csv", "outputs.csv", "agreements.csv", "national_partners.csv", "known_activity.csv"]),
    ("CSV", ["timeline.csv", "closest_bodies.csv", "related_bodies.csv", "network_milestones.csv"]),
    ("CSV", ["sources.csv", "source_excerpts.csv", "exchange_rates.csv"]),
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


INFO_LINES_JS = "() => {" + REPORT_JS + """
  const errors = [];
  for (const b of document.querySelectorAll('.info')) {
    const r = b.getBoundingClientRect();
    if (!r.width || !r.height || b.closest('[hidden], details:not([open]) > :not(summary)')) continue;
    // The nearest visible text before the (i) in document order: the last word of its heading or text.
    const walk = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
    let prev = null;
    for (let n = walk.nextNode(); n; n = walk.nextNode()) {
      if (b.compareDocumentPosition(n) & Node.DOCUMENT_POSITION_FOLLOWING) break;
      if (b.contains(n) || !n.textContent.trim() || n.parentElement.closest('.vh, [aria-hidden="true"], #tip')) continue;
      const range = document.createRange(); range.selectNodeContents(n);
      const rects = [...range.getClientRects()].filter(x => x.width > 0);
      if (rects.length) prev = rects[rects.length - 1];
    }
    const mid = (r.top + r.bottom) / 2;
    if (!prev || mid < prev.top || mid > prev.bottom || prev.right > r.left + 2)
      errors.push(REPORT + `(i) ${b.id || b.getAttribute('aria-label').slice(0, 40)} sits alone on a line, not beside the last word before it`);
  }
  return errors;
}"""


def check_info_lines(page, width: int) -> list[str]:
    """Supporting text: every visible (i) shares a line box with the last word of the heading or text before it."""
    return [f"{width}px: {e}" for e in page.evaluate(INFO_LINES_JS)]


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
    if text != f"{latest['version']} \u00b7 {day}":
        errors.append(f"version history row reads {text[:60]!r}, expected only version and date {latest['version']} \u00b7 {day}")
    if page.evaluate("() => { const s = document.querySelector('#changes > summary').getBoundingClientRect(), b = document.querySelector('#changes').getBoundingClientRect(); return s.right > b.right + 1 || s.width > innerWidth; }"):
        errors.append("version history row is wider than its box")
    if row.evaluate("n => n.getBoundingClientRect().height > 1.5 * parseFloat(getComputedStyle(n).lineHeight)"):
        errors.append(report_line("version history row is more than one line"))
    page.evaluate("document.querySelector('#changes').open = true")
    heads = [" ".join(t.split()) for t in page.locator("#changelog > li > .change-head").all_inner_texts()]
    shown = data["dataset"]["display"]["changelog"]
    want = [f"{c['version']} \u00b7 {date.fromisoformat(c['date']).strftime('%-d %b %Y')}" for c in shown]
    if heads != want:
        errors.append(f"version history list {heads} differs from the changelog {want}")
    if any(not t.strip() for t in page.locator("#changelog > li > p").all_inner_texts()):
        errors.append("a version history item has no notes")
    page.evaluate("document.querySelector('#changes').open = false")
    return errors


def check_download_layout(page) -> list[str]:
    """Names, file names and descriptions align across every group; the file name is muted; a group label is never
    weaker than its rows. Report lines, except that no download rows at all blocks (the downloads are missing)."""
    found = page.evaluate("""() => {
      const errors = [], rows = [...document.querySelectorAll('.dlist a')].filter(a => a.getClientRects().length);
      if (!rows.length) return null;
      const x = (a, sel) => Math.round(a.querySelector(sel).getBoundingClientRect().left);
      if (innerWidth > 640) for (const sel of ['.dl-title', '.dl-file', '.d']) if (new Set(rows.map(a => x(a, sel))).size > 1) errors.push(`download ${sel} does not align across rows`);
      const p = document.createElement('span'); p.style.color = 'var(--muted)'; document.body.appendChild(p); const muted = getComputedStyle(p).color; p.remove();
      for (const a of rows) {
        const file = getComputedStyle(a.querySelector('.dl-file'));
        const meta = (() => { const q = document.createElement('span'); q.style.font = 'var(--type-meta)'; document.body.appendChild(q); const v = parseFloat(getComputedStyle(q).fontSize); q.remove(); return v; })();
        if (file.color !== muted || parseFloat(file.fontSize) > meta + 0.5) errors.push('download file name is not muted meta text');
      }
      for (const h of document.querySelectorAll('.dlgroup > h4')) {
        const title = h.parentElement.querySelector('.dl-title');
        if (parseFloat(getComputedStyle(h).fontWeight) < parseFloat(getComputedStyle(title).fontWeight) || getComputedStyle(h).color !== getComputedStyle(title).color) errors.push(`download group label is weaker than its rows: ${h.textContent.trim()}`);
      }
      return errors;
    }""")
    return ["no download rows"] if found is None else [report_line(e) for e in found]


USING_THE_DATA = "Using the data"


PAGE_BYTES_BUDGET = 2_000_000  # search engines read only the first 2 MB of a page, so everything a reader should find must sit within it
# What a first visit downloads, in gzip bytes (level 6, as the host compresses text; woff2 counts as stored). Budgets,
# not timings, so the check is deterministic: a saved map drawing past its budget, or the data the page reads, or the
# whole first load, fails the gate and asks for a decision instead of slowing the first screen unnoticed. Set about a
# quarter (maps) or a tenth (the rest) above the sizes on 8 Nov 2026: map-wide 71 KB, map-narrow 44 KB, page.json
# 541 KB, first load 1.51 MB (all font subsets counted, the larger map once).
SAVED_MAP_GZIP_BUDGET = {"assets/map-wide.svg": 90_000, "assets/map-narrow.svg": 56_000}
PAGE_DATA_GZIP_BUDGET = 600_000
FIRST_LOAD_GZIP_BUDGET = 1_650_000
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
    """Search engines read only the first PAGE_BYTES_BUDGET bytes of a page: the state-by-state content (the world
    section) must end within them, or the check fails; a page that is longer in total only warns, because what
    follows is not content to find."""
    page = (dist / "index.html").read_bytes()
    end = world_section_end(page)
    if end is None:
        return ["index.html: cannot find where the world section (id=\"world\") ends"]
    if len(page) > PAGE_BYTES_BUDGET:
        print(f"warning: index.html is {len(page)} bytes, beyond the {PAGE_BYTES_BUDGET} that search engines read", file=sys.stderr)
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
        if mark is not None and (mark.get_text(strip=True) != "Not a member" or not mark.get("data-tip", "").strip() or mark.get("tabindex") != "0"):
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
    rows = soup.select("#world-rows > tr, #world-rest > tr")
    if len(rows) != sum(j["kind"] != "supranational" for j in data["jurisdictions"]):
        errors.append(f"static page: expected a table row for every state and territory, got {len(rows)}")
    if len(soup.select("#figures > .figure")) != 3:
        errors.append("static page: the figures row is missing")
    if uncited := uncited_values(soup):
        errors.append(f"static page: {len(uncited)} displayed values without a citation: {uncited[:10]}")
    files = sorted(p.relative_to(dist).as_posix() for p in (dist / "data").rglob("*") if p.suffix in (".csv", ".json") and p.name != PAGE_ONLY_DATA)
    errors.extend(check_download_groups(soup, set(files)))
    links = sorted(str(a.get("href")) for a in soup.select(".dlist a"))
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
    if page.locator("#record-name").text_content() != "Germany":
        errors.append("search does not open Germany's record")
    page.locator("#record .record-close").click()
    page.locator('#worldmap svg.map-wide .tgt[data-j="FR"]').dispatch_event("click")
    if not page.locator("#record").is_visible() or page.locator("#record-name").text_content() != "France":
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
                held.release()
                wait_hydrated(page)
                page.wait_for_timeout(300)
                if page.evaluate("getComputedStyle(document.body).backgroundColor") != GROUNDS[theme]:
                    errors.append(f"hydration {width}px {theme}: the page does not show the {theme} theme")
                after = page.screenshot(mask=[page.locator(".secnav, .group-nav")])
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
        if (name := page.locator("#record-name").text_content()) != "France":
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
        map_ready(page, 10_000)  # data-map reaches drawn once the late map is drawn
        page.locator('#regions .region-head[data-region="Europe"]').first.click()
        page.locator('#worldmap svg.map-wide .tgt[data-j="FR"]').dispatch_event("click")
        if not page.locator("#worldmap svg.map-wide .hl-sel").count() or page.locator("#record-name").text_content() != "France":
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
CARD_TRACKS = {"Institute": "2", "Status": "3", "Form": "4", "Head": "5", "Reports to": "6", "Partners": "7", "Mandate": "8", "Budget": "9", "Staff": "10", "Known activity": "11", "Related funding": "12"}
# Displayed values with no supporting quotation yet, by (table column or card label, jurisdiction id).
KNOWN_UNCITED = {}
CITED_COLUMNS = ("inst", "since", "op", "budget", "staff", "net")  # Region and status: reference data and derived status
NOTHING_FOUND = "None found yet"  # Kept contract uses: known-activity findings.


def uncited_values(soup) -> list[str]:
    """Every displayed value that a citation could support carries a citation mark: card rows, related funding
    and the worldwide table's value columns. Absence ("Not published", "None found yet") and an institute name
    linking to its own website (the link is the evidence) need none."""
    found, exempted = [], set()

    def bare(node) -> bool:
        text = " ".join(node.get_text(" ").split())
        return bool(text) and text not in ("\u2013", NOTHING_FOUND) and not node.select_one(".cites a.cn, .missing, .nonmember")

    def report(key: tuple[str, str], where: str) -> None:
        if key in KNOWN_UNCITED:
            exempted.add(key)
        else:
            found.append(where)

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
                report((name, card.get("data-j")), f"card {card.get('data-j')}: {name}")
    for item in soup.select(".related-funding-item"):
        if bare(item):
            found.append(f"related funding {item.get('data-funding-id')}")
    for tr in soup.select("#world-rows > tr, #world-rest > tr"):
        jid = tr.get("data-j") or ""
        for td in tr.select("td[data-col]"):
            column = td["data-col"]
            if column not in CITED_COLUMNS or (column == "inst" and td.select_one("a[href^='http']")):
                continue
            for part in td.select(".activity-kind") or [td]:
                if bare(part):
                    report((column, jid), f"table {jid}: {column}")
    # An exception whose value is now cited (or no longer shown) must leave the list.
    found += [f"KNOWN_UNCITED entry {key} is stale: remove it" for key in sorted(set(KNOWN_UNCITED) - exempted)]
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
    block = soup.select_one("p#using-data")
    api = f"api/v{json.loads((dist / 'data/aisi.json').read_text(encoding='utf-8'))['dataset']['version'].split('.')[0]}/index.json"
    if not block or [a.get("href") for a in block.select("a")] != ["llms.txt", api] or any(not a.get_text(strip=True) for a in block.select("a")) or not block.find_parent("section", attrs={"aria-labelledby": "data"}):
        errors.append(f"Data section: '{USING_THE_DATA}' is not one line in the Data section linking llms.txt and {api}")
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
    link = soup.select_one("#news ~ a#news-feed")  # the saved page; its template copy repeats it
    if not link or link.get("href") != "feed.xml":
        errors.append("News: no Atom feed link beside its heading")
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


# Hand-kept list: these crawlers must be explicitly allowed (see robots.txt generation in agent_files.py).
AI_CRAWLERS = ("GPTBot", "ClaudeBot", "PerplexityBot", "Google-Extended", "CCBot", "OAI-SearchBot", "Applebot-Extended")


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


def hydrated(page, url: str, map_drawn: bool = False) -> None:
    """Open the page and wait until its script has taken over the pre-rendered page, and with `map_drawn` also until
    the map is drawn and the layout has settled. A page from themed_page must show its theme's ground."""
    page.goto(url)
    wait_hydrated(page)
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


def check_citation_ends(soup) -> list[str]:
    """Every citation group ends its value; partner names carry their own citations in the fold."""
    errors: list[str] = []
    for group in soup.select(".cites"):
        host = group.find_parent(
            lambda n: (
                n.name in ("td", "p")  # a funding record's parts are separate paragraphs
                or ("pname" in n.get("class", []) and n.find_parent("details", class_="partner-how"))
                or (n.name == "li" and n.find_parent("details", class_="partner-how"))
                or n.has_attr("data-record")
                or bool(set(n.get("class", [])) & {"v", "fund-value", "fund-context", "activity-clause", "activity-since"})
            )
        )
        if host:
            # A collapsed fold-out after the value (the partners' "How they work together") is not part of the value's text.
            inside_partner_fold = host.find_parent("details", class_="partner-how") is not None
            text = [node for node in host.find_all(string=True) if inside_partner_fold or node.find_parent("details", class_="partner-how") is None]
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
    orders = {}
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
    for card in soup.select(".card:not(.mini)[data-j]"):
        jid = str(card["data-j"])
        ids = [li.get("data-source-id") for li in card.select(".srcs li")]
        orders[jid] = ids
        if len(ids) != len(set(ids)) or None in ids:
            errors.append(f"{jid}: source list has missing or duplicate IDs")
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
                card_ids = [a.get("data-source-id") for a in row.select(".cn")]
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

    # Documented display order: source-catalogue order, duplicates removed.
    catalogue = {sid: index for index, sid in enumerate(sources)}
    for jurisdiction in data["jurisdictions"]:
        if jurisdiction["id"] not in orders:
            orders[jurisdiction["id"]] = sorted(
                {sid for sid in entry_sources(jurisdiction) if sid in sources},
                key=catalogue.__getitem__,
            )
    for li in required("li[data-source-id]", cited_cards):
        sid = str(li["data-source-id"])
        link = li.select_one("a[href]")
        if sid not in sources or not link or link["href"] != sources[sid]["url"]:
            errors.append(f"source list {sid}: URL does not match data")
            continue
        en = li.select_one(":scope > .title-en")
        if en is not None and en.find_previous_sibling() is not link:
            errors.append(f"source list {sid}: the English title does not follow the original title's link")
        want_en = f" [{sources[sid]['title_en']}]" if sources[sid].get("title_en") else None  # Method: our English translation beside the original title
        if (en.get_text() if en else None) != want_en:
            errors.append(f"source list {sid}: English title {en.get_text() if en else None!r}, expected {want_en!r}")
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
        if sid not in order or cite.get_text(strip=True) != str(order.index(sid) + 1):
            errors.append(f"{jid}: citation {sid} differs from card source numbering")
        if sid not in sources or cite.get("href") != sources[sid]["url"]:
            errors.append(f"{jid}: citation {sid} URL does not match data")
    fund_items, fund_none = Keyed(soup, ".fund-item[data-j]"), Keyed(soup, ".fund-none li[data-j]")
    for institute in data["institutes"]:
        jid = institute["jurisdiction_id"]
        host = fund_items.one(jid)
        if not host and fund_none.one(jid):
            continue  # collapsed into the Not published line, which carries no source list
        if not host:
            errors.append(f"{jid}: missing funding entry")
            continue
        ids = [li.get("data-source-id") for li in host.select(".srcs li")]
        if ids != orders.get(jid):
            errors.append(f"{jid}: funding source list differs from card")
        for kind in ("funding", "staff"):
            records = host.select(f'[data-record="{kind}"]')
            expected = institute.get(kind, [])
            if len(records) != len(expected):
                errors.append(f"{jid}: missing {kind} records")
            by_id = {record["id"]: record for record in expected}
            for rendered in records:
                record = by_id.get(rendered.get("data-record-id"))
                if record is None:
                    errors.append(f"{jid}: {kind} record has unknown identity")
                    continue
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
                        if actual != in_numeral_order(list(dict.fromkeys(wanted)), orders.get(jid, [])):
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
                wanted = in_numeral_order(detail_citation_sources(record, kind), orders.get(jid, []))
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
        if label == "Not published" and not missing.get("data-tip", "").strip():
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
    ("NO", "no-1"): ["no-budget"],
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


def operating_lines(node) -> list[str]:
    """Rendered lines of a Known activity value: per kind, its value line, its since line and any interval line; else the whole value."""
    def text(n):
        return " ".join(n.get_text().split())
    kinds = node.select(".activity-kind")
    if not kinds:
        return [text(node)]
    return [
        text(part)
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
            # Any announcement or proposal wording counts, so a month-only or year-only date ("Announced on Mar 2025") cannot slip through.
            text += re.findall(r"(?:Announced|Proposed) on [^;·]+", " ".join(holder.get_text().split()))
        text = [t.strip() for t in text]
        for node in shown:
            if not node.find_parent(class_="qualifiers") or STATUS_INTERVAL_CAVEAT not in (node.get("data-exp") or ""):
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
            if {a.get("data-source-id") for a in status.select(".cn")} != wanted:
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
        if lines and INTERVAL_CAVEAT not in ((mark.get("data-tip") or mark.get("title") or "") if mark else ""):
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
    source_order = {source["id"]: n for n, source in enumerate(data["sources"])}
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
            for label in ("Budget", "Staff"):  # Funding and staff covers every institute (principle: Scope)
                if CARD_TRACKS[label] not in card_values:
                    errors.append(f"{j['id']}: card has no {label} row; Funding and staff covers every institute")
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
                cited = [sorted(e["source_ids"], key=source_order.__getitem__) for e in evidence]
                if any(clause_sources[id(n)] for n in node.select(".activity-since")):
                    errors.append(f"{j['id']}: {view} Known activity since line carries a citation")
                clauses = node.select(".activity-clause")
                if [c.get("data-kind") for c in clauses] != [e["kind"] for e in evidence] or [
                    clause_sources[id(c)] for c in clauses
                ] != cited:
                    errors.append(f"{j['id']}: {view} Known activity clause citations differ")
                if node.select(".sw, i"):
                    errors.append(report_line(f"{j['id']}: {view} Known activity carries a glyph; squares are for status only"))
                state = "confirmed" if evidence else "not_confirmed"
                if not node.select_one(f'.smark[data-state="{state}"]'):
                    errors.append(f"{j['id']}: {view} operational text mark missing")
                mark = node.select_one(".smark")
                # The tooltip carries the proxy caveat: no known work is not inactivity.
                tip = (mark.get("data-tip") or mark.get("title") or "") if mark else ""
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
    if not header or not (header.get("data-tip") or header.get("title") or "").strip():
        errors.append("Known activity header has no tooltip")
    for selector, wordings in (
        ("#rules", ("published evaluation", "joint testing", OPERATING_CAVEAT)),
    ):
        node = soup.select_one(selector)
        if not node or any(wording.lower() not in node.get_text().lower() for wording in wordings):
            errors.append(f"{selector}: operational definitions lack finding labels")
    return errors


def check_operating_edges(browser, base_url: str, data: dict) -> list[str]:
    fixture = copy.deepcopy(data)
    institutes = [i for i in fixture["institutes"] if i["status"] == "established"][:3]
    if len(institutes) != 3:
        return ["operational fixture needs three established institutes"]
    # Hand-worked exports exercise two kinds, ties, partial dates and exclusion.
    cases = [
        [("none", "2020", "policy"), ("published_work", "2025", "z-report"),
         ("joint_testing", "2025-03", "exercise"), ("published_work", "2025", "a-report")],
        [("joint_testing", "2025-03-07", "exercise")],
        [("none", "2020", "guidance")],
    ]
    winners = [[("published_work", "2025", "a-report"), ("joint_testing", "2025-03", "exercise")],
               [("joint_testing", "2025-03-07", "exercise")], []]
    credit_sources = {}
    for institute, outputs, selected in zip(institutes, cases, winners):
        source_ids = institute["source_ids"][:1]
        institute["outputs"] = [
            {"qualifies_as": kind, "date": day, "credit": {"institute_id": institute["id"], "date": day}, "lineage": lineage, "source_ids": source_ids,
             "type": "joint_testing" if kind == "joint_testing" else "report",
             "title": "Synthetic credited work", "attribution": "institutional_author",
             "qualification_note": "Synthetic evaluation, programme report or named exercise participation."}
            for kind, day, lineage in outputs
        ]
        credit_sources.update({(institute["id"], lineage): source_ids for _, _, lineage in outputs})
        institute["operating"] = [
            {"kind": kind, "date": day, "lineage": lineage, "source_ids": source_ids, "citations": {"date": source_ids}}
            for kind, day, lineage in selected
        ]
    page = browser.new_page(
        viewport={"width": 1000, "height": 1000}, accept_downloads=True
    )
    refresh_fixture_selections(fixture)
    page.route(PAGE_DATA, lambda route: route.fulfill(json=fixture))
    hydrated(page, base_url)
    page.wait_for_selector("#world-rows tr")
    page.locator("#world-status").select_option("")
    # Two kinds, a partial date, a finding of none and structural absence, sorted both ways.
    sort_errors = check_table_sorting(page, ("op",))
    page.locator("#world-download summary").click()
    with page.expect_download() as download:
        page.locator("#world-csv").click()
    rows = list(csv.DictReader(io.StringIO(Path(download.value.path()).read_text())))
    errors = sort_errors + check_operating_output(page.content(), fixture, rows, credit_sources=credit_sources) + check_interval_output(page.content(), fixture)
    page.close()
    return ["operational fixture: " + e for e in errors]


# Reads the rendered cells, never the page's sort keys: tier 0 a comparable value, 1 a value without a comparable figure,
# 2 a research gap ("Not published", "None found yet"), 3 structural absence (the dash).
SORT_ROWS_JS = r"""
(key) => {
  const ORDER = ['established', 'planned', 'proposed', 'exploring', 'none_identified'];
  const MON = 'Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec'.split(' ');
  const col = { name: 0, region: 1, status: 2, inst: 3, since: 4, op: 5, eur: 6, staff: 7, net: 8 }[key];
  const text = node => { const c = node.cloneNode(true); c.querySelectorAll('.cites, .vh').forEach(n => n.remove()); return c.textContent.replace(/\s+/g, ' ').trim(); };
  const date = t => { const p = t.split(' '), y = p[p.length - 1], m = p.length > 1 ? MON.indexOf(p[p.length - 2]) + 1 : 1, d = p.length > 2 ? p[0] : '';
    return /^\d{4}$/.test(y) && m ? [y, p.length > 1 ? String(m).padStart(2, '0') : '', d.padStart(d ? 2 : 0, '0')].filter(Boolean).join('-') : null; };
  const rows = [...document.querySelectorAll('#world-rows tr')].map(tr => {
    const td = tr.children[col], name = text(tr.children[0].querySelector('strong')), jid = tr.dataset.j;
    if (td.querySelector('.missing.na')) return { jid, name, tier: 3 };
    const mark = td.querySelector(':scope > .smark'), t = text(mark || td);
    if (t === 'Not published' || t === 'None found yet') return { jid, name, tier: 2 };
    let v = t;
    if (key === 'net') v = t === 'Member' ? 1 : null;  // Not a member: a known value without a comparable figure
    else if (key === 'status') v = ORDER.findIndex(s => td.querySelector('.sw.st-' + s));
    else if (key === 'since') v = date(t);
    else if (key === 'op') { const m = / since ((?:\d{1,2} )?(?:[A-Z][a-z]{2} )?\d{4})/.exec(t); v = m ? date(m[1]) : null; }  // kinds are in date order: the first since is the earliest
    else if (key === 'eur') { const m = /^(?:(?:up to|at least|about|nearly|more than|less than) )?~?EUR ([\d.,]+)(bn|m|k)?(?= )/.exec(t); v = m ? parseFloat(m[1].replace(/,/g, '')) * (m[2] === 'bn' ? 1e9 : m[2] === 'm' ? 1e6 : m[2] === 'k' ? 1e3 : 1) : null; }
    else if (key === 'staff') { const m = /^(?:up to )?~?(\d[\d,]*)\+?(?= \(| as of |,|$)/.exec(t); v = m ? parseInt(m[1].replace(/,/g, ''), 10) : null; }
    return v == null || v === -1 ? { jid, name, tier: 1, text: t } : { jid, name, tier: 0, v, text: t };
  });
  // Text compares in the page's locale collation, as ranks, so the gate compares numbers only.
  const collate = xs => [...new Set(xs)].sort((a, b) => a.localeCompare(b));
  const names = collate(rows.map(r => r.name));
  const texts = ['name', 'region', 'inst'].includes(key) ? collate(rows.filter(r => !r.tier).map(r => r.v)) : null;
  rows.forEach(r => { r.n = names.indexOf(r.name); if (texts && !r.tier) r.v = texts.indexOf(r.v); });
  return rows;
}
"""


def sort_errors_for(rows: list[dict], key: str, direction: int, data=None) -> list[str]:
    """Tiers never decrease, comparable values follow the direction, ties stay alphabetical."""
    errors = []
    label = f"sort {key} {'ascending' if direction > 0 else 'descending'}"
    if key in ("eur", "staff"):
        if data is None:
            raise ValueError("numeric sort oracle needs underlying data")
        jurisdictions = {j["id"]: j for j in data["jurisdictions"]}
        institutes = {i["jurisdiction_id"]: i for i in data["institutes"]}
        rows = copy.deepcopy(rows)
        seen = set()
        for row in rows:
            jid = row.get("jid")
            if jid not in jurisdictions or jid in seen:
                errors.append(
                    f"{label}: missing, unknown or duplicate jurisdiction {jid!r}"
                )
                continue
            seen.add(jid)
            institute = institutes.get(jid, {})
            display = institute.get("display", {})
            selection = display.get("budget" if key == "eur" else "staff")
            records = institute.get("funding" if key == "eur" else "staff", [])
            record = next((r for r in records if r["id"] == selection), {})
            # Published values are the page's sort keys. Raw amounts independently
            # establish the displayed label, never its order or equality.
            value = (
                record.get("eur_per_year_est" if key == "eur" else "total")
                if key != "eur" or selection == display.get("comparison_funding")
                else None
            )
            native_budget = (
                key == "eur"
                and value is None
                and record.get("amount") is not None
            )
            if native_budget:
                # A native EUR one-off looks numeric, but has no annual sort key.
                if row["tier"] > 1:
                    errors.append(
                        f"{label}: {jid} native budget label/data presence differs"
                    )
                else:
                    expected = expected_shown_funding_value(record, data)
                    if not re.match(re.escape(expected) + r"(?= |$)", row["text"]):
                        errors.append(
                            f"{label}: {jid} native budget label differs from primary value"
                        )
                row["tier"] = 1
            if (value is not None) != (row["tier"] == 0):
                errors.append(f"{label}: {jid} numeric label/data presence differs")
            if value is not None and row["tier"] == 0:
                expected = (
                    expected_annual_funding_value(record, data)
                    if key == "eur"
                    else expected_staff_total(record)
                )
                if not re.match(
                    re.escape(expected) + r"(?= \(| as of |,|$| a (?:financial )?year)",
                    row["text"],
                ):
                    errors.append(
                        f"{label}: {jid} numeric label differs from primary value"
                    )
                row["v"] = value
    for before, after in zip(rows, rows[1:]):
        if after["tier"] < before["tier"]:
            errors.append(f"{label}: {after['name']} (tier {after['tier']}) follows {before['name']} (tier {before['tier']}); gaps must stay last")
        elif after["tier"] == before["tier"]:
            if before["tier"] == 0 and before["v"] != after["v"]:
                if (before["v"] < after["v"]) != (direction > 0):
                    errors.append(f"{label}: {before['name']} and {after['name']} are out of order")
            elif before["n"] > after["n"]:
                errors.append(f"{label}: tie {before['name']}, {after['name']} not alphabetical")
    return errors


def check_table_sorting(page, keys=("name", "region", "status", "inst", "since", "op", "eur", "staff", "net"), *, data=None) -> list[str]:
    """Every world-table column, both directions: values by meaning, then gaps, then structural absence."""
    errors = []
    for key in keys:
        for direction in (1, -1):
            # Clicking a header sorts ascending, a second click descending; hidden columns' buttons still work.
            page.evaluate(
                "([k, dir]) => { const th = document.querySelector(`table.world th[data-k=\"${k}\"]`); const want = dir > 0 ? 'ascending' : 'descending';"
                " for (let n = 0; n < 3 && th.getAttribute('aria-sort') !== want; n++) th.querySelector('button').click(); }",
                [key, direction],
            )
            rows = page.evaluate(SORT_ROWS_JS, key)
            errors.extend(sort_errors_for(rows, key, direction, data))
    page.evaluate("() => { const th = document.querySelector('table.world th[data-k=\"name\"]'); for (let n = 0; n < 3 && th.getAttribute('aria-sort') !== 'ascending'; n++) th.querySelector('button').click(); }")
    return errors



def check_activity_tooltip(page) -> list[str]:
    """Keyboard reaches the definition of "since"; the tip opens below the whole value, clear of the kind lines; Escape dismisses."""
    errors = []
    page.mouse.move(0, 0)  # a pointer left over the page would hover whatever scrolls under it
    for where in ('.card .row[data-track="11"]', '#world-rows td[data-col="op"]'):
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


@reports
def check_activity_width(page) -> list[str]:
    """Known activity never breaks mid-phrase: each kind line and each since line stay on one line, table and cards."""
    return page.evaluate(
        """() => {
      const table = document.querySelector('table.world'), hidden = table.classList.contains('hide-op'), errors = [];
      table.classList.remove('hide-op');
      for (const n of document.querySelectorAll('[data-col="op"] .smark.value:not(.activity), [data-col="op"] .activity-clause, [data-col="op"] .activity-since, .card .row[data-track="11"] .smark.value:not(.activity), .card .row[data-track="11"] .activity-clause, .card .row[data-track="11"] .activity-since'))
        if (new Set([...n.getClientRects()].map(r => Math.round(r.top))).size !== 1)  // fragments (text, glued citation) on one line share a top
          errors.push('Known activity breaks mid-phrase: ' + n.textContent.trim());
      table.classList.toggle('hide-op', hidden);
      return [...new Set(errors)];
    }"""
    )


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
    if not note or not note.get("data-tip", "").strip():
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
    """The saved headline is the World sentence pair, worded from the data; Europe's 'European' carries the scope; no number ends a line."""
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
    if not scope or not scope.get_text(strip=True) or not scope.get("data-exp", "").strip():
        errors.append("headline: 'European' lacks its scope definition")
    if re.search(r"\d ", node.get_text()):
        errors.append(report_line("headline: a number may end a line (no non-breaking space after it)"))
    for version in soup.select("#headline .hl-version"):
        region = version.get("data-region")
        pool = [j for j in region_pool(data, region) if j["kind"] == "state"]
        n, k = sum(j["status"] == "established" for j in pool), sum(j["status"] in MOVING_STATUSES for j in pool)
        bold = [b.get_text().replace("\u00a0", " ") for b in version.select("b")]
        if bold != [f"{n} of {len(pool)}"] + ([f"{k} more" if n else str(k)] if k else []):
            errors.append(report_line(f"headline {region}: only the numbers are bold, got {bold}"))
        if version.get_text().replace("\u00a0", " ") != expected_headline(data, region):
            errors.append(f"headline {region}: {version.get_text()!r} differs from the data {expected_headline(data, region)!r}")
    if len(node.select(".hl-sentence")) != 2:
        errors.append(report_line("headline: the two sentences are not separate wrapping units"))
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


def expected_annual_funding_value(record, data):
    amount = expected_annual_amount(record, data)
    text = expected_native_funding_value(
        {**record, "amount": amount, "currency": "EUR"}
    )
    if (
        record["currency"] != "EUR"
        and record.get("amount_qualifier") != "about"
        and "~EUR" not in text
    ):
        text = text.replace("EUR", "~EUR")
    return text


TWO_FIGURE_SELECTORS = (".fund-budget > .fund-value strong", "#world-rows td[data-col=budget] .budget-amount", "#world-rows td[data-col=staff]", ".fund-staff > .fund-value strong", ".funding-native .budget-amount", ".fund-staff .fund-context")


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


def check_staff_figures(soup, data: dict, expected: dict | None = None) -> list[str]:
    """Each chart row's staff total is the hand-worked figure (tests/fixtures/staff_figures.json) at two figures, marked
    ~ exactly when rounding changed it. The expectation is read from the quotation that supports the latest staff
    record, never from display.total_staff; an institute with a staff total and no entry, or whose latest record no
    longer matches its entry, fails with its key until the fixture is reviewed."""
    if expected is None:
        expected = {e["institute_id"]: e for e in json.loads(STAFF_FIGURES.read_text(encoding="utf-8"))["staff"]}
    errors = []
    chart_rows = Keyed(soup, "#fund-rows article.fund-item[data-j]")
    known = {inst["id"] for inst in data["institutes"]}
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
        node = chart_rows.one(inst["jurisdiction_id"], ".fund-staff > .fund-value strong")
        if node:
            node = copy.copy(node)
            for cites in node.select(".cites"):
                cites.decompose()
        shown = node.get_text(strip=True) if node else None
        if shown != expected_staff_total(entry):
            errors.append(f"{inst['jurisdiction_id']}: staff total shown as {shown!r}, expected {expected_staff_total(entry)!r}")
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
                if not node.get("data-tip", "").strip() or node.get("tabindex") != "0":
                    errors.append(f"{place}: a Not published value lacks a non-empty focusable tooltip")
            for node in root.select('.smark[data-state="not_confirmed"]'):
                found["None found yet"] += 1
                if not node.get("data-tip", "").strip() or node.get("tabindex") != "0":
                    errors.append(f"{place}: an activity absence value lacks a non-empty focusable tooltip")
        for term, count in found.items():
            if not count:
                errors.append(f"{place}: no {term} value to check its tooltip")
    marks = soup.select('#entries .statusgroup[data-status="none_identified"] > h3 .smark')
    if not marks:
        errors.append("Europe cards: no None identified group heading to check its tooltip")
    for mark in marks:  # the table and the record beside the map: check_none_identified_tips, which shows them
        if not mark.get("data-tip", "").strip() or mark.get("tabindex") != "0":
            errors.append("Europe cards: the None identified heading lacks a non-empty focusable tooltip")
    for key in DASH_COLUMNS:
        button = soup.select_one(f'table.world thead th[data-k="{key}"] button')
        if not button or not button.get("data-tip", "").strip():
            errors.append(f"table column {key}: header lacks a non-empty tooltip")
    for node in soup.select("#entries .card .missing.na, #world-cards .card .missing.na"):
        if not node.get("data-tip", "").strip():
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
        if not mark.get("data-exp"):
            errors.append(f"marker {mark.get_text()!r} has no definition")
    # Current source-type grades still qualify every non-primary figure.
    words = list(FIGURE_LABELS)
    checked = expected_places = 0
    full_cards = Keyed(soup, ".card:not(.mini)[data-j]")
    table_rows = Keyed(soup, "#world-rows tr[data-j], #world-rest tr[data-j]")
    chart_rows = Keyed(soup, "#fund-rows > .fund-item[data-j]")
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
        chart = chart_rows.one(jid, ".fund-staff")
        total = shown.get("total")
        if total and not chart and any(r.select_one(f'[data-j="{jid}"]') for r in soup.select("#fund-rows")):
            errors.append(f"{jid}: funding Staff cell not found")
        if chart and total:
            context = " ".join(chart.select_one(".fund-context").get_text(" ").split()) if chart.select_one(".fund-context") else ""
            found = [w.replace("\\b", "").replace("(?! )", "") for w in words if re.search(w, context)]
            want = [figure_grade(total)] if figure_grade(total) else []
            if found != want:
                errors.append(f"{jid}: funding Staff shows grade {found}, expected {want}")
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
        errors.append("the definition's 'What counts' link does not go to method.html#qualifying-body")
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
    ("Compare", ("testing", "set-up", "funding")),
    ("News", ("news",)),
    ("States", ("europe", "world")),
    ("Data", ("data", "method")),
)
GROUP_ROWS = {"Compare": ("Testing", "Set-up", "Funding"), "States": ("Europe", "The world"), "Data": ("Downloads", "Method")}
SECTION_HEADINGS = ("map", "testing", "set-up", "funding", "news", "europe", "world", "data", "method")


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
        column = "groups-long" if key == "none" else "groups-short"
        if not block.find_parent(class_=column):
            errors.append(report_line(f"access group {key} is not in {column}"))
        heading = block.select_one("h3")
        if not heading or not heading.get_text(" ", strip=True):
            errors.append(f"access group {key} heading is missing or empty")
        if key == "privileged":
            term = heading.select_one("abbr.g") if heading else None
            if not term or not term.get_text(strip=True) or not term.get("data-exp", "").strip() or term.get("tabindex") != "0":
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
        tips = [(m.get("data-tip"), m.get("data-tip-sub"), m.get("title")) for m in block.select(".marks > i")]
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
            tips = [(t.get_text(), t.get("data-exp")) for t in note.select("abbr.g")]
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
        tip = tile.select_one("b > abbr.g[data-exp]")
        if bound == "at_most" and (not tip or tip.get("data-exp") != MEDIAN_CEILING or tip.get_text(strip=True) != figure):
            errors.append(f"{label} median {figure!r} is a ceiling but lacks the tooltip {MEDIAN_CEILING!r}")
        elif bound != "at_most" and tip:
            errors.append(f"{label} median {figure!r} is not a ceiling but carries a tooltip")
    # No budget: the established state institutes whose card shows the Budget row as Not published.
    missing = 0
    cards = Keyed(soup, ".card[data-j]")
    for jid in counted:
        row = cards.one(jid, '.row[data-track="9"]')
        value_node = row.select_one(".v") if row else None
        errors.extend(zero_match_errors("figures_row", f'.card[data-j="{jid}"] .row[data-track="9"] .v', int(value_node is not None)))
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
    if len(note) != 1 or note[0].get("data-tip") != want_note or len(soup.select("#figures-scope")) != 1:
        errors.append(f"figures row: the last figure does not end with one (i) reading {want_note!r}")
    return errors


# Institutional set-up (decisions: copy from Bahadir's feedback): every institute by form (principle: Scope), host as the note.
FORM_LABELS = {"unit_in_existing_body": "Unit in an existing body", "federation_of_existing_bodies": "Network of existing bodies", "university_centre": "University centre", "new_body": "New body"}
FORM_UNKNOWN = "Form not published"
PER_PERSON_TIP = "Annual budget divided by population. For scale only, since budgets cover different work and costs differ between states."


def check_setup_groups(soup, data: dict) -> list[str]:
    """Institutional set-up: heading, groups by form (larger first, Form not published last), members, marks, links, host notes and citations, from aisi.json."""
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
      const bg = sel => { const n = document.querySelector(sel); return n ? getComputedStyle(n).backgroundImage + '|' + getComputedStyle(n).backgroundColor : null; };
      const clear = key.every(k => { const sw = k.querySelector('.key-swatch').getBoundingClientRect(), r = document.createRange(); r.selectNodeContents(k); r.setStartAfter(k.querySelector('.key-swatch')); const t = r.getBoundingClientRect(); return sw.width > 0 && sw.right <= t.left + .5; });
      return { key: key.map(k => k.textContent.trim()), clear,
        solid: [bg('.fund-key .key-swatch:not(.pending)'), bg('#fund-rows .bar-value:not(.pending)')],
        hatch: [bg('.fund-key .key-swatch.pending'), bg('#fund-rows .bar-value.pending')],
        tips: [...document.querySelectorAll('#fund-rows .fund-bar')].map(b => b.dataset.tip) };
    }""")
    errors = []
    if got["key"] != ["adopted", "announced"]:
        errors.append(f"funding key is not two swatches labelled adopted and announced: {got['key']}")
    if not got["clear"]:
        errors.append("funding key: a swatch is hidden or overlapped by its label")
    # Stacked rows (phones): one left edge for labels, values and qualifier lines (principle: Values and qualifiers).
    ragged = page.evaluate("""() => [...document.querySelectorAll('#fund-rows > .fund-item')].filter(row => getComputedStyle(row).display === 'flex').filter(row => {
      const left = row.getBoundingClientRect().left;
      return [...row.querySelectorAll('.fund-budget-label, .fund-budget > .fund-value, .budget-qualifiers, .fund-staff > .label, .fund-staff > .fund-value, .fund-staff > .fund-context')].filter(n => n.getClientRects().length).some(n => Math.abs(n.getBoundingClientRect().left - left) > 1);
    }).map(row => row.dataset.j)""")
    if ragged:
        errors.append(report_line(f"stacked funding rows lack one left edge: {ragged}"))
    for kind in ("solid", "hatch"):
        if not got[kind][0] or got[kind][0] != got[kind][1]:
            errors.append(report_line(f"funding key {kind} swatch is not drawn like its bars: {got[kind]}"))
    if not got["tips"] or any(not (t or "").strip() for t in got["tips"]):
        errors.append("funding bars lack a non-empty tooltip")
    return errors


def check_per_person(soup, data: dict) -> list[str]:
    """Funding rows: a charted state budget carries its per-person figure (independent oracle) as a qualifier, its caveat
    in the figure's own tooltip."""
    errors, jurisdictions = [], {j["id"]: j for j in data["jurisdictions"]}
    for node in soup.select(".per-person"):
        if node.name != "abbr" or "g" not in node.get("class", []) or node.get("data-exp") != PER_PERSON_TIP or node.get("tabindex") != "0":
            errors.append(f"per-person figure {node.get_text()!r} lacks the focusable caveat tooltip {PER_PERSON_TIP!r}")
    shown = 0
    fund_items = Keyed(soup, ".fund-item[data-j]")
    for inst in data["institutes"]:
        jurisdiction = jurisdictions[inst["jurisdiction_id"]]
        record = inst["display"]["funding_records"].get(inst["display"].get("comparison_funding") or "")
        row = fund_items.one(jurisdiction["id"], ".fund-value")
        node = row.select_one(".qualifiers .per-person") if row else None
        want = expected_budget_ratios(record, jurisdiction, data)[2] if record and jurisdiction["kind"] == "state" and record.get("eur_per_year_est") is not None else None
        if (node.get_text() if node else None) != want:
            errors.append(f"{jurisdiction['id']}: per person {node.get_text() if node else None!r}, expected {want!r}")
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
        if not link or link.get("href") != path or link.get("download") != f"{jid}.json" or [n for n in link.find_all(string=True) if n.strip() and not n.find_parent(class_="vh")] or not link.get("data-tip", "").strip() or not link.get_text(strip=True) or "i-dl" not in link.get("class", []):
            errors.append(f"card {jid}: download button missing or not the decided icon, path and label")
            continue
        file = dist / path
        if not file.is_file() or json.loads(file.read_text(encoding="utf-8")).get("record", {}).get("id") != jid:
            errors.append(f"card {jid}: download {path} missing or holds another state")
    return errors


ACCESS_LAYOUT_JS = """(sel) => {
  const box = document.querySelector(sel), short = box.querySelector('.groups-short'), long = box.querySelector('.groups-long');
  const xs = [...long.querySelectorAll('.group-list > li')].map(li => Math.round(li.getBoundingClientRect().left));
  const columns = [...new Set(xs)].sort((a, b) => a - b), per = columns.map(x => xs.filter(v => v === x).length);
  const shortGroups = [...short.querySelectorAll('.group-block')].map(g => g.getBoundingClientRect());
  const sr = short.getBoundingClientRect(), lr = long.getBoundingClientRect();
  const groups = [...box.querySelectorAll('.group-block')].map(g => { const r = g.getBoundingClientRect(), lefts = new Set([...g.querySelectorAll('.group-list > li')].map(li => Math.round(li.getBoundingClientRect().left))); return { left: r.left, top: r.top, bottom: r.bottom, listColumns: lefts.size }; });
  // Lines (rule 15): a thin rule opens each group; no hairline after a group's last member. Phones (rule 6): every note starts at one left edge.
  const px = v => parseFloat(v) || 0;
  const rules = [...box.querySelectorAll('.group-block')].map(g => ({ top: px(getComputedStyle(g).borderTopWidth), last: px(getComputedStyle(g.querySelector('.group-list > li:last-child')).borderBottomWidth) }));
  const noteLefts = new Set([...box.querySelectorAll('.group-list > li')].map(li => { const a = li.querySelector(':scope > a'), n = li.querySelector('.group-note'); return n ? Math.round(n.getBoundingClientRect().left - a.getBoundingClientRect().left) : 0; }));
  return { rules, noteOffsets: [...noteLefts], columns: columns.length, per, groups, longLeft: lr.left, shortRight: sr.right, shortLeft: sr.left, shortBottom: sr.bottom, longTop: lr.top,
    stacked: shortGroups.every((r, k) => !k || r.top >= shortGroups[k - 1].bottom - 1), longWidth: long.getBoundingClientRect().width, shortWidth: short.getBoundingClientRect().width };
}"""


def check_access_tips(page) -> list[str]:
    """Every tooltip term in Testing and access opens its definition on focus, hover and tap, and Escape closes it."""
    errors = []
    terms = page.locator("#access .group-block abbr.g:visible")
    for n in range(terms.count()):
        term = terms.nth(n)
        want, label = term.get_attribute("data-exp"), term.inner_text()
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
    """Phones: the short groups and the long group share one left edge, the long group below them, its list in one
    column. Overlap blocks; the edge and the column count are report lines."""
    errors = []
    if m["columns"] != 1:
        errors.append(report_line(f"{width}px: long {name} list keeps {m['columns']} columns"))
    if abs(m["shortLeft"] - m["longLeft"]) > 1:
        errors.append(report_line(f"{width}px: {name} groups do not share one left edge (short left {m['shortLeft']}, long left {m['longLeft']})"))
    if m["longTop"] < m["shortBottom"] - 1:  # overlapping groups cannot be read: blocking
        errors.append(f"{width}px: the long {name} group overlaps the short ones (short bottom {m['shortBottom']}, long top {m['longTop']})")
    groups = m["groups"]  # every group, in reading order: one left edge, each below the one before, each list in one column
    if any(abs(g["left"] - groups[0]["left"]) > 1 for g in groups):
        errors.append(report_line(f"{width}px: {name} groups are not stacked on one left edge: {[(round(g['left']), round(g['top'])) for g in groups]}"))
    if any(g["top"] < p["bottom"] - 1 for p, g in zip(groups, groups[1:])):
        errors.append(f"{width}px: a {name} group starts above the end of the one before (overlap): {[(round(g['top']), round(g['bottom'])) for g in groups]}")
    if any(g["listColumns"] > 1 for g in groups):
        errors.append(report_line(f"{width}px: a {name} group list keeps several columns: {[g['listColumns'] for g in groups]}"))
    return errors


def check_access_layout(page, width: int) -> list[str]:
    """Testing and access and Institutional set-up share one layout (principle: Groups): short groups stack on the left; the long group spans two columns with its list in halves; phones keep one column."""
    errors = []
    for name, sel in (("access", "#access"), ("set-up", "#setup")):
        m = page.evaluate(ACCESS_LAYOUT_JS, sel)
        if not m["stacked"]:
            errors.append(report_line(f"short {name} groups are not stacked"))
        if any(abs(r["top"] - 1) > 0.01 or r["last"] for r in m["rules"]):
            errors.append(report_line(f"{name} groups: expected a thin top rule and no hairline after the last member, got {m['rules']}"))
        if width <= 640 and m["noteOffsets"] != [0]:
            errors.append(report_line(f"{name} groups on a phone: notes do not start at their state's left edge (offsets {m['noteOffsets']})"))
        if width > 640:
            if m["longLeft"] < m["shortRight"] or m["longWidth"] < 1.8 * m["shortWidth"]:
                errors.append(report_line(f"long {name} group is not the right column spanning two: {m}"))
            if m["columns"] != 2 or max(m["per"]) - min(m["per"]) > 1:
                errors.append(report_line(f"long {name} list is not in halves: {m['per']}"))
        else:
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
    """A development row's " · " parts: each first-output part reads the approved line, wherever it falls in a merged row."""
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
        if not name or not (name.get("data-exp") or "").strip():
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
    expected_date = date.fromisoformat(data["changelog"][-1]["date"]).strftime(
        "%-d %b %Y"
    )
    if (
        not updated
        or updated.get_text()
        != f"Updated {expected_date} · v{dataset['version']}"
    ):
        errors.append("hero metadata is not just update date and version")
    if (
        not version
        or version.get_text() != "v" + dataset["version"]
        or version.get("data-tip")
        or version.get("title")
    ):
        errors.append("hero version is not 'v<version>' linking the changelog without a tooltip")
    changes = soup.select_one("#changes")
    section = changes.find_parent("section") if changes else None
    if not section or section.get("aria-labelledby") != "data":  # the Data section's accessible name: blocking
        errors.append("the changelog is not in a section named by its Data heading (aria-labelledby)")
    elif not section.select_one("#cite"):
        errors.append(report_line("changelog is not next to Cite as in Data"))
    errors += check_cite(soup, dataset)

    errors += first_output_data_errors(data)
    kinds = {
        "commitment",
        "proposed",
        "designated",
        "launched",
        "first_output",
        "exploration_started",
        "withdrawn",
        "closed",
    }
    groups: dict[tuple[str, str, str | None], list[dict]] = {}
    jurisdictions = {j["id"]: j for j in data["jurisdictions"]}
    for j in data["jurisdictions"]:
        for event in sorted(j.get("timeline", []), key=lambda event: event["id"]):
            if event["event"] not in kinds:
                continue
            key = (j["id"], event["date"], None if len(event["date"]) == 10 else event["id"])
            groups.setdefault(key, []).append(event)
    # Stable two-pass sort: newest date, jurisdiction ID, stable event identity.
    ordered = sorted(groups.items(), key=lambda item: (item[0][0], item[0][2] or ""))
    ordered.sort(key=lambda item: item[0][1], reverse=True)
    ordered = ordered[:10]
    rows = soup.select(".development-row")
    if len(soup.select("#development-list > li:not(.earlier)")) != min(3, len(ordered)) or len(
        soup.select("#development-list > li.earlier")
    ) != max(0, len(ordered) - 3):
        errors.append("developments do not use the three plus seven split")
    if len(rows) != len(ordered):
        errors.append("developments row count differs from merged data")
    for row, ((jid, day, _), events) in zip(rows, ordered):
        clock = row.select_one(".development-date")
        name = row.select_one(".development-name")
        text = row.select_one(".development-text")
        if (
            row.get("data-j") != jid
            or row.get("data-events", "").split() != [e["id"] for e in events]
            or not clock
            or clock.get("data-date") != day
            or not name
            or name.get_text() != jurisdictions[jid]["name"]
        ):
            errors.append(
                "development ordering, grouping, date or jurisdiction differs from data"
            )
        ids = list(
            dict.fromkeys(sid for event in events for sid in expected_event_sources(data, jurisdictions[jid], event))
        )
        source_order = {source["id"]: index for index, source in enumerate(data["sources"])}
        ids.sort(key=lambda sid: source_order[sid])
        if [a.get("data-source-id") for a in row.select(".cn")] != ids:
            errors.append("development citations differ from grouped event sources")
        if text:
            for cites in text.select(".cites"):
                cites.decompose()
            parts = text.get_text().split(" · ")
            errors += first_output_part_errors(parts, events)
            if len(parts) != len(events) or any(
                (event.get("short") or "").strip() != part
                if (event.get("short") or "").strip()
                else not part or not event["description"].strip().startswith(part)
                for part, event in zip(parts, events)
            ):
                errors.append(
                    "development summary differs from short text or description"
                )
        else:
            errors.append("development text missing")
    link = soup.select_one('#events-download[download="events.csv"]')
    if not link or link.get("href") != "data/events.csv":
        errors.append("developments lack the events download")
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


def check_hit_targets(page) -> list[str]:
    """Measure visible controls, including labelled inputs and expanded content.

    Only inline anchors within running paragraph/list text are exempt. Focusable
    glossary terms, citation marks and standalone links are controls too.
    """
    return page.evaluate(
        "() => {"
        + BROWSER_GEOMETRY
        + """
      const errors = [];
      const selector = 'a[href], button, input:not([type="hidden"]), select, textarea, summary, [tabindex]:not([tabindex="-1"]), [role="button"], [contenteditable="true"]';
      document.querySelectorAll(selector).forEach(control => {
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
        if (height < 27.5) errors.push(`${control.tagName.toLowerCase()}#${control.id}.${control.getAttribute("class")}: ${(control.textContent.trim() || control.getAttribute("aria-label") || "").slice(0,60)} (${height.toFixed(1)}px hit height)`);
      });
      return errors;
    }"""
    )


@reports
def check_typography(page) -> list[str]:
    """Resolve responsive font tokens, then inspect actual visible text parents."""
    return page.evaluate(
        "() => {"
        + BROWSER_GEOMETRY
        + """
      const errors = [], roles = ['title', 'label', 'body', 'meta', 'figure'];
      const probe = document.createElement('span');
      probe.style.cssText = 'position:fixed;visibility:hidden;pointer-events:none';
      document.body.appendChild(probe);
      const pairs = roles.map(role => {
        probe.style.font = 'var(--type-' + role + ')';
        const style = getComputedStyle(probe);
        return [style.fontSize, style.fontWeight, style.fontFamily];
      });
      probe.remove();
      if (new Set(pairs.map(p => p.join('/'))).size !== roles.length) errors.push('typography roles do not resolve to five distinct size/weight/family roles');
      const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
      const checked = new Set();
      while (walker.nextNode()) {
        const text = walker.currentNode, parent = text.parentElement;
        if (!text.textContent.trim() || checked.has(parent) || parent.closest('script, style, template, svg, option') || !visible(parent)) continue;
        const range = document.createRange(); range.selectNodeContents(text);
        if (![...range.getClientRects()].some(r => r.width && r.height)) continue;
        checked.add(parent);
        const s = getComputedStyle(parent);
        const emphasis = parent.closest('strong, b');
        const base = emphasis && getComputedStyle(emphasis.parentElement);
        const inheritedEmphasis = base && s.fontSize === base.fontSize && s.fontFamily === base.fontFamily && Number(s.fontWeight) <= 500;
        // Citations are sized relative to the text they annotate (.75em), not a fixed role.
        const cites = parent.closest('.cites'), host = cites && cites.parentElement && getComputedStyle(cites.parentElement.closest(':not(.cite-glue)') || cites.parentElement);
        if (cites && Math.abs(parseFloat(s.fontSize) - .75 * parseFloat(host.fontSize)) < .6) continue;
        // The headline is the figure role with only its numbers at figure weight; its words take body weight (owner's mock).
        const [figSize, , figFamily] = pairs[roles.indexOf('figure')], bodyWeight = pairs[roles.indexOf('body')][1];
        if (parent.closest('#headline') && s.fontSize === figSize && s.fontFamily === figFamily && s.fontWeight === bodyWeight) continue;
        if (!inheritedEmphasis && !pairs.some(([size, weight, family]) => size === s.fontSize && weight === s.fontWeight && family === s.fontFamily)) {
          errors.push(`unassigned typography ${s.fontSize}/${s.fontWeight}/${s.fontFamily}: ${parent.tagName.toLowerCase()}.${parent.className} ${text.textContent.trim().slice(0,60)}`);
        }
      }
      return errors;
    }"""
    )


BROWSER_BASELINE = """
      const baseline = node => {
        if (!node) return null;
        const walker = document.createTreeWalker(node, NodeFilter.SHOW_TEXT);
        while (walker.nextNode()) {
          const text = walker.currentNode;
          if (!text.textContent.trim() || !visible(text.parentElement)) continue;
          const probe = document.createElement('span');
          probe.style.cssText = 'display:inline-block;width:0;height:0;padding:0;margin:0;border:0;vertical-align:baseline';
          // A raw text node is an anonymous flex/grid item. Keep the probe in
          // that text run, rather than creating an independently aligned item.
          let wrapper = null;
          if (/(flex|grid)/.test(getComputedStyle(text.parentElement).display)) {
            wrapper = document.createElement('span'); wrapper.style.display = 'inline';
            text.before(wrapper); wrapper.appendChild(text);
          }
          text.before(probe);
          const y = probe.getBoundingClientRect().bottom;
          probe.remove();
          if (wrapper) wrapper.replaceWith(text);
          return y;
        }
        return null;
      };
"""


@reports
def check_baselines_and_rhythm(page) -> list[str]:
    """Measure first text baselines, including text nested inside links or marks."""
    return page.evaluate(
        "() => {"
        + BROWSER_GEOMETRY
        + BROWSER_BASELINE
        + """
      const errors = [];
      // Each descriptor is one visual row, not a stack of independent rows.
      const rowTypes = [
        ['.card .row', '.k, .v'], ['.card .top', ':scope > span:not(.vh), :scope > .name'],
        ['.card.mini > summary', ':scope > span, :scope > .name'], ['.meta', ':scope > *'],
        ...(document.querySelector('.fund-staff') && getComputedStyle(document.querySelector('.fund-staff')).display === 'grid' ? [['.fund-staff', ':scope > .label, :scope > .fund-value']] : []),  // stacked rows put the label above
        ['.dlist a', innerWidth <= 640 ? ':scope > .dl-title, :scope > .dl-file' : ':scope > .dl-title, :scope > .dl-file, :scope > .d'],
        ['table.world tr', ':scope > th, :scope > td'],
        ['.secnav .measure', ':scope > a'], ['.region-chips', ':scope > button'],
        ['.legend-entry', ':scope > .legend-copy'],
        ['.development-row', innerWidth <= 640 ? ':scope > .development-date, :scope > .development-name' : ':scope > .development-date, :scope > .development-name, :scope > .development-text'],
      ];
      if (innerWidth > 640) rowTypes.push(['.fund-item', ':scope > div']);  // the chart key alone sits over the bars, no axis labels
      for (const [selector, items] of rowTypes) {
        document.querySelectorAll(selector).forEach(row => {
          if (!visible(row)) return;
          const nodes = [...row.querySelectorAll(items)].filter(visible);
          if (nodes.length < (['.legend-entry', '.meta'].includes(selector) || (selector === '.card .top' && row.closest('.statusgroup, .eu-level')) || (selector === '.card.mini > summary' && row.parentElement.open && row.querySelector('.find-whole')) ? 1 : 2)) errors.push(`missing baseline items: ${selector}`);
          const lines = [];
          nodes.forEach(node => {
            const rect = node.getBoundingClientRect();
            // Flex items on separate wrapped lines are separate baseline groups.
            const wraps = getComputedStyle(row).flexWrap === 'wrap';
            let line = wraps ? lines.find(group => group.some(other => {
              const r = other.getBoundingClientRect(); return Math.min(rect.bottom, r.bottom) - Math.max(rect.top, r.top) > .5;
            })) : lines[0];
            if (!line) { line = []; lines.push(line); }
            line.push(node);
          });
          const ys = nodes.map(baseline);
          for (const line of lines) {
            const values = line.map(node => ys[nodes.indexOf(node)]);
            if (values.some(y => y === null) || (values.length > 1 && Math.max(...values) - Math.min(...values) > 1)) errors.push(`${selector} first baselines differ: ${values.join('/')} (${row.textContent.trim().slice(0,60)})`);
          }
          // Empty swatches and pseudo icons synthesise a baseline at their bottom edge.
          const icon = row.querySelector(':scope > .sw');
          const pseudo = row.matches('.dlist a') && getComputedStyle(row, '::before');
          if (icon && ys.length && Math.abs(icon.getBoundingClientRect().bottom - ys[0]) > 1) errors.push(`${selector} swatch misses first baseline`);
          if (pseudo && pseudo.alignSelf !== 'baseline' && pseudo.alignSelf !== 'first baseline') errors.push('download icon does not use the first baseline');
        });
      }
      document.querySelectorAll('.smark:not([data-state])').forEach(mark => {
        if (!visible(mark)) return;
        const sw = mark.querySelector('.sw'), y = baseline(mark);
        if (!sw || y === null || Math.abs(sw.getBoundingClientRect().bottom - y) > 1) errors.push(`swatch misses first baseline: ${mark.className} ${mark.textContent.trim().slice(0,60)}`);
      });
      const measure = (selector, property) => {
        const node = document.querySelector(selector);
        return node ? parseFloat(getComputedStyle(node)[property]) : NaN;
      };
      const line = Math.max(...[...document.querySelectorAll('.card .k, .card .v, .card .v *')].filter(visible).map(n => {
        const s = getComputedStyle(n); return parseFloat(s.lineHeight) - parseFloat(s.fontSize);
      }).filter(Number.isFinite));
      const row = measure('.card .row', 'marginBottom');
      const header = measure('.card .top', 'marginBottom');
      const card = measure('.cards:not(.minis)', 'rowGap');
      const section = measure('main .measure', 'rowGap');
      const probe = document.createElement('span'); document.body.appendChild(probe);
      const roleLine = Math.max(...['body', 'label', 'meta'].map(role => {
        probe.style.font = 'var(--type-' + role + ')'; const s = getComputedStyle(probe);
        return parseFloat(s.lineHeight) - parseFloat(s.fontSize);
      })); probe.remove();
      const values = [line, row, header, card, section], expected = [roleLine, 16, 24, 40, 64];
      if (values.some((v, i) => !Number.isFinite(v) || Math.abs(v - expected[i]) > 1) || values.some((v, i) => i && v <= values[i-1])) {
        errors.push(`line/row/header/card/section rhythm: ${values.join('/')}px, expected approximately ${expected.join('/')}px`);
      }
      return errors;
    }"""
    )


def check_citations(page) -> list[str]:
    """Check tight superscripts and their vertical-only effective hit regions."""
    return page.evaluate(
        "() => {"
        + BROWSER_GEOMETRY
        + REPORT_JS
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
        const cr = range.getBoundingClientRect(), s = getComputedStyle(cites);
        if (wr.bottom < cr.top || wr.top > cr.bottom) errors.push('citation is on a separate line');
        const gap = cr.left - wr.right;
        // The gap is .12em of the marker (site/index.html .cites), so it grows with the annotated text.
        if (gap < 0 || gap > Math.max(2, .13 * parseFloat(s.fontSize))) errors.push(REPORT + `citation gap to final word is ${gap.toFixed(1)}px`);
        if (s.whiteSpace !== 'nowrap' || parseFloat(s.lineHeight) !== 0 || Math.abs(parseFloat(s.top) + .4 * parseFloat(s.fontSize)) > .5) errors.push(REPORT + 'citation group is not a tight raised superscript');
        // One rule everywhere: three quarters of the annotated text's size, raised from that text, never above meta size (beside a big figure).
        const annotated = parseFloat(getComputedStyle(word.parentElement).fontSize), ratio = parseFloat(s.fontSize) / annotated;
        const probe = document.body.appendChild(Object.assign(document.createElement('span'), { style: 'font: var(--type-meta); position: absolute' }));
        const meta = parseFloat(getComputedStyle(probe).fontSize); probe.remove();
        const want = Math.min(.75 * annotated, meta);
        if (Math.abs(parseFloat(s.fontSize) - want) > .02 * annotated) errors.push(REPORT + `citation ${cites.textContent} is sized from its container, not its text: ${ratio.toFixed(2)} of "${word.textContent.trim().slice(-20)}"`);
        const numbers = [...cites.querySelectorAll('a')].map(a => Number(a.textContent));
        if (numbers.some((n, k) => k && n <= numbers[k - 1])) errors.push(`citation numerals not ascending: ${cites.textContent}`);
        const commas = document.createTreeWalker(cites, NodeFilter.SHOW_TEXT);
        while (commas.nextNode()) {
          const text = commas.currentNode;
          if (text.parentElement.closest('a')) continue;
          range.selectNodeContents(text);
          if (text.textContent !== ',' || range.getBoundingClientRect().width > 4) errors.push(REPORT + 'citation separator is not a tight comma');
        }
        cites.querySelectorAll('a').forEach(a => {
          const r = a.getBoundingClientRect(), p = pseudoBox(a), style = getComputedStyle(a);
          const preceding = document.createTreeWalker(glue, NodeFilter.SHOW_TEXT), boxes = [];
          while (preceding.nextNode()) {
            const text = preceding.currentNode;
            if (a.contains(text)) break;
            const range = document.createRange(); range.selectNodeContents(text);
            boxes.push(...[...range.getClientRects()].filter(box => box.width && box.height));
          }
          if (boxes.some(box => box.left < r.right && box.right > r.left + .1 && box.top < (p || r).bottom && box.bottom > (p || r).top)) errors.push(`citation ${a.textContent} overlaps preceding text`);
          if (parseFloat(style.marginLeft) < 0 || parseFloat(style.marginRight) < 0) errors.push(REPORT + 'negative horizontal citation margin');
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


@reports
def check_style_source(html: str) -> list[str]:
    """Static token contracts that can run without a browser."""
    errors = []
    for role in ("title", "label", "body", "meta", "figure"):
        if not re.search(rf"--type-{role}:\s*[^;]*/var\(--leading-{role}\)", html):
            errors.append(f"{role} line height does not use its role token")
    anchor_rule = re.search(r"(?:^|\n)a\s*\{([^}]+)\}", html)
    for prop, token in (
        ("text-decoration-thickness", "--underline-width"),
        ("text-underline-offset", "--underline-offset"),
        ("text-decoration-color", "--underline-colour"),
    ):
        if not anchor_rule or not re.search(
            rf"{prop}:\s*var\({token}\)", anchor_rule[1]
        ):
            errors.append(f"inline links do not use {token}")
    return errors


def check_design_roles(page) -> list[str]:
    """Inspect contextual links, inline emphasis, leading, folds and bar fills."""
    return page.evaluate(
        "() => {"
        + BROWSER_GEOMETRY
        + REPORT_JS
        + """
      const errors = [], root = getComputedStyle(document.documentElement);
      const probe = document.createElement('span'); document.body.appendChild(probe);
      probe.style.font = 'var(--type-meta)';
      const metaLeading = getComputedStyle(probe).lineHeight;
      probe.style.marginTop = 'var(--gap-row)';
      const rowGap = getComputedStyle(probe).marginTop;
      probe.style.width = 'var(--bar-min)'; probe.style.display = 'inline-block';
      const barMin = parseFloat(getComputedStyle(probe).width);
      probe.style.color = 'var(--ink)'; const ink = getComputedStyle(probe).color;
      probe.style.color = 'var(--accent)'; const accent = getComputedStyle(probe).color;
      probe.remove();
      if (!(Number(root.getPropertyValue('--leading-meta')) < Number(root.getPropertyValue('--leading-body')))) errors.push(REPORT + 'meta leading is not tighter than body');
      document.querySelectorAll('.dlist .d, .fund-context, .updated, .legend-def, .legend-definition').forEach(n => {
        if (visible(n) && getComputedStyle(n).lineHeight !== metaLeading) errors.push(REPORT + `meta line height differs from token: ${n.className}`);
      });
      document.querySelectorAll('details.more').forEach(n => {
        const row = n.closest('.fund-item'), stacked = row && getComputedStyle(row).display === 'flex';
        const previous = stacked && n.matches('.fund-detail') ? row.querySelector('.fund-staff') : n.previousElementSibling;  // stacked funding rows: Sources follows Staff (principle: Funding rows)
        if (!visible(n) || !previous || !visible(previous)) return;
        // The content's own extent: an inline line's box leaves out an item box that wraps below its first line.
        const bottom = Math.max(previous.getBoundingClientRect().bottom, ...[...previous.querySelectorAll('.meta-item')].filter(visible).map(i => i.getBoundingClientRect().bottom));
        const gap = n.getBoundingClientRect().top - bottom;
        if (Math.abs(gap - parseFloat(rowGap)) > 1) errors.push(REPORT + `fold does not follow content at row gap: ${n.id || n.className} (${gap}px)`);
      });
      document.querySelectorAll('p strong, p b, p em, .v .smark, td .smark').forEach(n => {
        if (n.closest('#headline')) return; // the headline's numbers take figure weight by design (Type principle)
        const s = getComputedStyle(n), p = getComputedStyle(n.parentElement);
        if (s.fontSize !== p.fontSize || s.fontFamily !== p.fontFamily || Number(s.fontWeight) > 500) errors.push(REPORT + `inline emphasis changes type role: ${n.textContent}`);
      });
      document.querySelectorAll('a').forEach(n => {
        if (!visible(n) || n.matches(':hover, :focus-visible')) return;
        const s = getComputedStyle(n);
        const standalone = n.matches('.cn, .standalone, .wordmark, .tile, .correction, .secnav a, .group-nav a, .popover a, .dlist a');
        if (s.textDecorationLine !== (standalone ? 'none' : 'underline')) errors.push(`wrong resting link decoration: ${n.className} ${n.textContent.slice(0,40)}`);
      });
      document.querySelectorAll('.secnav a').forEach(n => {
        if (getComputedStyle(n).textDecorationLine !== 'none') errors.push(REPORT + 'nav has an underline');
        if (!n.classList.contains('active')) return;
        const s = getComputedStyle(n), line = getComputedStyle(n, '::after');
        // The active link must show a mark (state, not decoration): blocking. Its colour and exact size are cosmetic.
        const behind = getComputedStyle(n.closest('nav') || document.body).backgroundColor, paint = line.backgroundColor;
        if (line.content === 'none' || parseFloat(line.height) < 1 || parseFloat(line.width) < 1 || /^(transparent|rgba\\(.*,\\s*0\\))$/.test(paint) || paint === behind) errors.push('active nav shows no indicator');
        else if (s.color !== ink || parseFloat(line.width) < 8 || parseFloat(line.width) > n.getBoundingClientRect().width + .5 || line.backgroundColor !== accent) errors.push(REPORT + 'active nav lacks its short accent indicator'); // the label's width (principle: Navigation); computed widths round, so a label-wide line can read a hair either side
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
        if (r.width < Math.min(barMin, track.width) - .5 || r.width > track.width + .5) errors.push('funding bar violates its minimum/maximum width');
        if (parseFloat(s.borderTopWidth) || s.boxShadow !== 'none') errors.push(REPORT + 'funding bar uses an outline');
        if (n.classList.contains('pending')) {
          if (!s.backgroundImage.includes('repeating-linear-gradient')) errors.push('non-adopted funding bar is not hatched');
          // A quiet hatch (owner: the dense black one shimmered): stripes thinner than gaps, a period of at least 7px, and stripes lighter than full ink.
          const rgb = c => { const m = c.match(/^color\\(srgb ([\\d.e-]+) ([\\d.e-]+) ([\\d.e-]+)/); return m ? m.slice(1, 4).map(v => parseFloat(v) * 255) : (c.match(/[\\d.]+/g) || []).slice(0, 3).map(Number); };
          const dist = (a, b) => Math.hypot(...a.map((v, k) => v - b[k]));
          const stops = [...s.backgroundImage.matchAll(/(rgba?\\([^)]*\\)|color\\([^)]*\\))\\s+([\\d.]+)px/g)].map(m => [rgb(m[1]), parseFloat(m[2])]);
          const [p0, p1, p2, p3] = stops.length === 4 ? stops.map(t => t[1]) : [0, 0, 0, 0], period = p3 - p0, stripe = p1 - p0, gap = p3 - p2;
          const inkRgb = rgb(ink), track = rgb(getComputedStyle(n.parentElement).backgroundColor), axis = track.map((v, k) => v - inkRgb[k]), span2 = axis.reduce((a, v) => a + v * v, 0);
          const c = stops.length === 4 ? stops[0][0] : inkRgb, frac = axis.reduce((a, v, k) => a + v * (c[k] - inkRgb[k]), 0) / span2, off = dist(c, inkRgb.map((v, k) => v + frac * axis[k]));
          const quiet = stops.length === 4 && dist(stops[0][0], stops[1][0]) < 1 && dist(stops[2][0], stops[3][0]) < 1 && dist(stops[2][0], track) < 2
            && Math.abs(p1 - p2) < .01 && off < 3 && frac > .3 && frac < .8; // stripes an interior mix of ink and track, gaps the track colour
          // No visible hatch (no stripe, no gap, or stripes the gaps' colour) leaves announced funding looking like spent
          // funding: blocking. Its quietness is cosmetic.
          const unseen = stops.length !== 4 || stripe <= 0 || gap <= 0 || dist(stops[0][0], stops[2][0]) < 2;
          if (unseen || !quiet || period < 7 || stripe >= gap) errors.push((unseen ? '' : REPORT) + `announced funding bar hatch is not a quiet ink hatch on the track (stops: ${JSON.stringify(stops)}, ink ${ink}, track ${JSON.stringify(track)})`);
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
        if (link.target !== '_blank' || !link.relList.contains('noopener') || !/new (tab|window)/i.test(link.querySelector('.vh')?.textContent || '')) fail('external link lost accessible new-tab text');  // its meaning, in any wording
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
    """Compare real first baselines, not card heights, in row reading order."""
    return page.evaluate(
        "() => {"
        + BROWSER_GEOMETRY
        + BROWSER_BASELINE
        + REPORT_JS
        + """
      // Three card shapes; structurally absent fields are omitted. Each field owns a fixed track so shapes stay aligned.
      const SHAPES = {
        established: ['2', '3', '4', '8', '9', '10', '11'],  // Head, Reports to and Partners are optional here: check_leadership_rows holds them to the data
        record: ['2', '3', '4', '8', '9', '10'],
        none: ['3', '8'],
      };
      const OPTIONAL = ['12', '5', '6', '7'];
      const errors = [];
      document.querySelectorAll('.cards:not(.minis)').forEach(grid => {
        const cards = [...grid.querySelectorAll('.card')], rows = [...grid.children];
        const columns = Number(getComputedStyle(grid).getPropertyValue('--columns'));
        if (columns !== (innerWidth > 1100 ? 3 : innerWidth > 640 ? 2 : 1)) errors.push(REPORT + 'card column count differs from breakpoint contract');
        if (!cards.length || rows.some(r => !r.matches('.card-row'))) errors.push(REPORT + 'card row wrappers missing');
        if (columns === 3 && cards.length === 1) {  // a lone card spans two of the three tracks (principle: Card grids)
          const gap = parseFloat(getComputedStyle(grid).columnGap), track = (grid.getBoundingClientRect().width - 2 * gap) / 3;
          if (Math.abs(cards[0].getBoundingClientRect().width - (2 * track + gap)) > 1) errors.push(REPORT + `lone card does not span two tracks: ${cards[0].id}`);
        }
        const names = cards.map(c => c.querySelector('.name').textContent);
        if (names.some((n, i) => i && n.localeCompare(names[i-1], 'en') < 0)) errors.push('cards are not alphabetical in row reading order');
        rows.forEach((row, i) => {
          const siblings = [...row.children];
          if (siblings.length !== Math.min(columns, cards.length - i * columns)) errors.push(REPORT + 'incorrect card row length');
          siblings.forEach((card, j) => {
            const r = card.getBoundingClientRect();
            if (j && (r.left <= siblings[j-1].getBoundingClientRect().left || Math.abs(r.top - siblings[0].getBoundingClientRect().top) > 1)) errors.push('cards do not flow left to right');
            if (i && r.top < rows[i-1].getBoundingClientRect().bottom) errors.push('comparison rows overlap');
            const fieldRows = [...card.querySelectorAll(':scope > .row')];
            const tracks = fieldRows.map(n => n.dataset.track);
            if (fieldRows.some(n => !n.querySelector('.k')?.textContent.trim())) errors.push(`card field label missing or empty: ${card.id}`);
            const shape = tracks[0] !== '2' ? 'none' : (card.querySelector('.top .sw.st-established') || card.closest('.statusgroup[data-status="established"]') || (card.closest('.eu-level') && card.querySelector(':scope > .top > .vh')?.textContent === ', Established')) ? 'established' : 'record';  // the EU entry says its status in its accessible name only
            if (JSON.stringify(tracks.filter(track => !OPTIONAL.includes(track))) !== JSON.stringify(SHAPES[shape])) errors.push(`missing or misplaced card fields: ${card.id} (${tracks.join(', ')})`);
            card.querySelectorAll(':scope > .row > .k').forEach(k => { if (visible(k) && k.getBoundingClientRect().height > parseFloat(getComputedStyle(k).lineHeight) * 1.5) errors.push(REPORT + `card label wraps: ${k.textContent}`); });
            if (shape === 'none') {
              const finding = fieldRows[1]?.querySelector('.v');
              if (!finding || !finding.textContent.trim()) errors.push('record without an institute lost its finding');
            }
            if (innerWidth <= 640 && getComputedStyle(card).display !== 'flex') errors.push(REPORT + 'phone card lost its flex stack');
            if (innerWidth > 640) {
              if (!getComputedStyle(card).gridTemplateRows.startsWith('subgrid')) errors.push(REPORT + 'card does not use row subgrid');
              const placed = [[card.querySelector(':scope > .top'), 1, 'top'], [card.querySelector(':scope > .foot'), 13, 'footer']]
                .concat(fieldRows.map((n, k) => [n, tracks[k], tracks[k]]));
              placed.forEach(([n, track, label]) => {
                if (!n || getComputedStyle(n).gridRowStart !== String(track)) errors.push(REPORT + `${label} is not in its fixed track ${track}: ${card.id}`);
              });
            }
          });
          if (innerWidth <= 640 || siblings.length < 2) return;
          // Compare per track among the cards that fill it; Mandate and Finding share track 8.
          for (const track of [2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 'footer']) {
            for (const side of track === 'footer' ? ['.source-footer-line'] : ['.k', '.v']) {
              const ys = siblings.map(card => {
                const row = track === 'footer' ? card.querySelector('.foot') : card.querySelector(`:scope > .row[data-track="${track}"]`);
                return row ? baseline(row.querySelector(side)) : undefined;
              }).filter(y => y !== undefined);
              if (ys.length < 2) continue;
              if (ys.some(y => y == null) || Math.max(...ys) - Math.min(...ys) > 1) errors.push(REPORT + `track ${track} ${side} baselines differ across cards: ${ys.join('/')}`);
            }
          }
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
            tracks != ["3", "8"]
            or [bool(text.strip()) for text in finding.locator(".row > .k").all_inner_texts()] != [True, True]  # one label per row
            or finding.locator(".row")
            .nth(1)
            .locator(".v")
            .evaluate(
                "e => { const c = e.cloneNode(true); c.querySelectorAll('.cites').forEach(n => n.remove()); return c.textContent.trim(); }"
            )
            != without_institute["summary"]
        ):
            errors.append(
                f"{width}px: record without an institute lost its finding or fields"
            )
        page.close()
    return errors


def check_source_footers(page) -> list[str]:
    """The source summary holds only the count; the check date and correction follow the list and open with it."""
    return page.evaluate(
        "() => {"
        + BROWSER_GEOMETRY
        + REPORT_JS
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
      folds.forEach(fold => {
        const summary = fold.querySelector(':scope > summary'), foot = fold.closest('.foot');
        const count = summary?.querySelector('.source-count'), list = fold.querySelector(':scope > ol');
        if (!foot || !summary || !list || !count || count.textContent !== `${list.children.length} ${list.children.length === 1 ? 'source' : 'sources'}`) {
          errors.push('source footer lost its count or list'); return;
        }
        if (getComputedStyle(foot).borderTopWidth !== '0px') errors.push(REPORT + 'source footer has a top rule');
        if (!isMeta(summary)) errors.push(REPORT + 'source summary is not muted meta');
        if (!visible(summary)) return; // A mini card may itself be closed.
        const r = summary.getBoundingClientRect();
        if (summary.scrollWidth > summary.clientWidth + 1 || r.right > foot.getBoundingClientRect().right + 1) errors.push('source footer overflows');
        if (r.height < 27.5) errors.push('source summary lacks its inline hit height');
        if (summary.querySelector('a, .meta-dot') || summary.textContent !== count.textContent) errors.push('source summary holds more than the count');
        if (visible(list) !== fold.open) errors.push('source list visibility differs from disclosure state');
        const after = fold.querySelector(':scope > .source-after');
        if (foot.querySelectorAll('.correction').length !== (after ? after.querySelectorAll('.correction').length : 0)) errors.push('correction is outside the expanded sources');
        if (after && (after.previousElementSibling !== list || visible(after) !== fold.open)) errors.push('source metadata does not follow the list inside the fold');
      });
      return errors;
    }"""
    )


@reports
def check_meta_separators(page, force_wrap=False) -> list[str]:
    """A dot's preceding character must share its line, including wrapped footers."""
    return page.evaluate(
        "forceWrap => {"
        + BROWSER_GEOMETRY
        + """
      const errors = [];
      let fixture = null;
      if (forceWrap) {
        fixture = document.createElement('div'); fixture.style.cssText = 'width:180px;position:fixed;left:0;top:0';
        const original = document.querySelector('.source-after:has(.meta-dot)');
        if (!original) return ['missing footer for forced-wrap fixture'];
        fixture.appendChild(original.cloneNode(true)); document.body.appendChild(fixture);
        const line = fixture.firstChild;
        if (line.getBoundingClientRect().height < parseFloat(getComputedStyle(line).lineHeight) * 1.5) errors.push('separator fixture did not force a wrapped footer');
      }
      const selectors = '.source-footer-line, .source-after, .finding, .updated, .development-text, .fund-value, .fund-context, #world-count, #foot, #changes > summary, .fund-axis, .card .v';
      document.querySelectorAll(selectors).forEach(line => {
        if (!visible(line)) return;
        const walker = document.createTreeWalker(line, NodeFilter.SHOW_TEXT);
        while (walker.nextNode()) {
          const node = walker.currentNode;
          if (node.textContent.includes('·') && !node.parentElement.closest('.meta-dot')) errors.push('unbound meta separator: ' + line.textContent.slice(0, 70));
        }
      });
      for (const selector of ['.source-after', '.updated']) {
        if (!document.querySelector(selector + ' .meta-dot')) errors.push('missing separator coverage: ' + selector);
      }
      document.querySelectorAll('.meta-dot').forEach(dot => {
        if (!visible(dot)) return;
        const tail = dot.parentElement;
        if (!tail.classList.contains('meta-tail') || getComputedStyle(tail).whiteSpace !== 'nowrap') { errors.push('separator is not bound to its preceding item'); return; }
        // The whole item is one box, so the line breaks only between items.
        const item = tail.closest('.meta-item');
        // The chart key (.fund-axis) is one sentence of two clauses, not a list of items.
        if (!tail.closest('.fund-axis') && (!item || getComputedStyle(item).display !== 'inline-block')) errors.push('separated item is not one unbreakable box: ' + tail.textContent);
        const walker = document.createTreeWalker(tail, NodeFilter.SHOW_TEXT);
        let last = null;
        while (walker.nextNode()) {
          const node = walker.currentNode;
          if (dot.contains(node)) break;
          if (node.textContent.trim() && !node.parentElement.closest('.vh')) last = node;
        }
        if (!last) { errors.push('separator has no preceding text'); return; }
        const end = last.textContent.trimEnd().length, range = document.createRange();
        range.setStart(last, end - 1); range.setEnd(last, end);
        const before = range.getBoundingClientRect(), mark = dot.getBoundingClientRect();
        if (mark.left < before.right - 1 || mark.bottom <= before.top || mark.top >= before.bottom) errors.push('separator starts a line: ' + tail.textContent);
      });
      // An item wraps inside only when it alone is wider than its container; it is never split into two boxes.
      // Citation glue goes inside a cited item (the funding qualifiers carry markers after their last item).
      if (![...document.querySelectorAll('.meta-item .cite-glue')].some(visible)) errors.push('no cited separated item to check the glue against');
      document.querySelectorAll('.meta-item').forEach(item => {
        if (!visible(item)) return;
        if (item.parentElement.closest('.meta-item, .cite-glue') || item.querySelector('.meta-item')) errors.push('separated item split or nested: ' + item.textContent.slice(0, 60));
        const lines = item.getBoundingClientRect().height / parseFloat(getComputedStyle(item).lineHeight);
        if (!(lines >= 1.5)) return;
        let box = item.parentElement; while (box && /^inline/.test(getComputedStyle(box).display)) box = box.parentElement;
        if (box && item.getBoundingClientRect().width < box.clientWidth - parseFloat(getComputedStyle(box).paddingLeft) - parseFloat(getComputedStyle(box).paddingRight) - 2)
          errors.push('separated item wraps inside though it fits its container: ' + item.textContent.slice(0, 60));
      });
      if (fixture) fixture.remove();
      return errors;
    }""",
        force_wrap,
    )


@reports
def check_line_end_separators(page) -> list[str]:
    """No visible separator "·" starts or ends a rendered line, and none is hidden mid-line.

    Measured per character, independent of the page's own marking. A "·" inside a word
    (Korean or Catalan source text) is not a separator and is skipped.
    """
    return page.evaluate(
        "() => {"
        + BROWSER_GEOMETRY
        + """
      const errors = [], range = document.createRange();
      // A flex item (a group row's separator) shares its line with its siblings, so it is measured in its flex container.
      const blockOf = node => { let e = node.parentElement; while (e && (/^inline/.test(getComputedStyle(e).display) || (e.parentElement && /flex$/.test(getComputedStyle(e.parentElement).display)))) e = e.parentElement; return e || document.body; };
      const glyph = (node, i) => { range.setStart(node, i); range.setEnd(node, i + 1); return range.getBoundingClientRect(); };
      const neighbour = (block, node, i, step) => {
        const walker = document.createTreeWalker(block, NodeFilter.SHOW_TEXT);
        walker.currentNode = node;
        let t = node, k = i + step;
        for (;;) {
          for (; k >= 0 && k < t.data.length; k += step) if (/\\S/.test(t.data[k])) return glyph(t, k);
          do { t = step > 0 ? walker.nextNode() : walker.previousNode(); } while (t && (!visible(t.parentElement) || t.parentElement.closest('.vh')));
          if (!t) return null;
          k = step > 0 ? 0 : t.data.length - 1;
        }
      };
      const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
      while (walker.nextNode()) {
        const node = walker.currentNode, parent = node.parentElement;
        if (!node.data.includes('·') || parent.closest('script, style, template') || !parent.getClientRects().length) continue;
        const hidden = !visible(parent);
        if (hidden && !parent.closest('.meta-dot.line-end')) continue;
        const block = blockOf(node);
        for (let i = 0; i < node.data.length; i++) {
          if (node.data[i] !== '·') continue;
          if (!parent.closest('.meta-dot') && /\\S/.test(node.data[i - 1] || ' ') && /\\S/.test(node.data[i + 1] || ' ')) continue;
          const dot = glyph(node, i);
          if (!dot.height) continue;
          const next = neighbour(block, node, i, 1), prev = neighbour(block, node, i, -1);
          const ends = !next || next.top >= dot.bottom - 2, starts = !prev || prev.bottom <= dot.top + 2;
          const where = (block.closest('[id]')?.id || block.tagName.toLowerCase()) + ': ' + block.textContent.replace(/\\s+/g, ' ').trim().slice(0, 60);
          if (hidden && !ends) errors.push('separator hidden mid-line: ' + where);
          if (!hidden && ends) errors.push('separator ends a line: ' + where);
          if (!hidden && starts) errors.push('separator starts a line: ' + where);
        }
      }
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
    """Rules, control borders and bar tracks must actually paint (guards token collisions)."""
    return page.evaluate(
        "() => {" + REPORT_JS + """
      const errors = [], clear = c => !c || c === 'transparent' || /rgba\\(.*,\\s*0\\)$/.test(c);
      const noBorder = (s, side) => clear(s['border' + side + 'Color']) || s['border' + side + 'Style'] === 'none' || parseFloat(s['border' + side + 'Width']) === 0;
      const nav = document.querySelector('.secnav'), th = document.querySelector('table.world th');
      const search = document.querySelector('#world-search, input[type="search"]'), bar = document.querySelector('.fund-bar');
      if (nav && noBorder(getComputedStyle(nav), 'Bottom')) errors.push(REPORT + 'nav rule does not paint');
      // Principle Controls: a field is a soft fill without a drawn edge, so its fill (not a border) must paint against the page.
      if (search && (clear(getComputedStyle(search).backgroundColor) || getComputedStyle(search).backgroundColor === getComputedStyle(document.body).backgroundColor)) errors.push(REPORT + 'control fill does not paint');
      if (bar && clear(getComputedStyle(bar).backgroundColor) && getComputedStyle(bar).backgroundImage === 'none') errors.push(REPORT + 'funding bar track does not paint');
      if (th && noBorder(getComputedStyle(th), 'Bottom') && noBorder(getComputedStyle(th.closest('thead') || th), 'Bottom')) errors.push(REPORT + 'table header rule does not paint');
      return errors;
    }"""
    )


WIDE_WIDTHS = (1100, 1101, 1120, 1200, 1440)


TEXT_MEASURE_JS = """() => {
  const lines = el => { const out = [], w = document.createTreeWalker(el, NodeFilter.SHOW_TEXT); let n;
    while ((n = w.nextNode())) { if (!n.data.trim()) continue; const p = n.parentElement; if (!p || !p.getClientRects().length) continue;
      if (getComputedStyle(p).visibility === 'hidden' || p.closest('.vh, [hidden], .cites')) continue;
      const r = document.createRange(); r.selectNodeContents(n); for (const b of r.getClientRects()) if (b.width > 0) out.push(b); }
    return out; };
  // A text block holds running text only: no table, map, control, heading or multi-column grid inside it.
  const flowOnly = el => !el.querySelector('table, svg, img, input, select, textarea, button, h1, h2, h3, h4') && [el, ...el.querySelectorAll('*')].every(n => { const d = getComputedStyle(n); return !(/grid/.test(d.display) && d.gridTemplateColumns.split(' ').length > 1); });
  const isText = el => parseFloat(getComputedStyle(el).fontSize) <= 16.5 && !/^H[1-6]$/.test(el.tagName) && flowOnly(el);
  const name = e => e.tagName.toLowerCase() + (e.id ? '#' + e.id : '') + (e.classList.length ? '.' + [...e.classList].join('.') : '');
  const errors = [], seen = new Set(); let wrapped = 0;
  for (const c of document.querySelectorAll('main *, footer *')) {
    const cs = getComputedStyle(c);
    if (!c.childNodes.length || !c.getClientRects().length || cs.display.startsWith('inline') || c.closest('table, svg, .suggest, #tip, .popover')) continue;
    const blocks = [];
    for (const child of c.children) {
      const s = getComputedStyle(child);
      if (!child.getClientRects().length || s.display.startsWith('inline') || /absolute|fixed/.test(s.position) || s.whiteSpace === 'nowrap' || !isText(child)) continue;
      const box = child.getBoundingClientRect(), ls = lines(child);
      if (ls.length) blocks.push({ el: child, left: box.left, right: box.right - parseFloat(s.paddingRight) - parseFloat(s.borderRightWidth), ls });
    }
    // Bare text beside block children is one anonymous block with the container's measure.
    const bare = [...c.childNodes].filter(n => n.nodeType === 3 ? n.data.trim() : n.nodeType === 1 && getComputedStyle(n).display.startsWith('inline'));
    if (bare.length && blocks.length && isText(c)) {
      const ls = bare.flatMap(n => { const r = document.createRange(); r.selectNode(n); return [...r.getClientRects()].filter(b => b.width > 0); });
      if (ls.length) blocks.push({ el: c, bare: true, left: Math.min(...ls.map(b => b.left)), right: c.getBoundingClientRect().right - parseFloat(cs.paddingRight), ls });
    }
    if (blocks.length < 2) continue;
    for (const b of blocks) {
      if (new Set(b.ls.map(r => Math.round(r.top / 4))).size < 2 || Math.max(...b.ls.map(r => r.right)) < b.right - 160) continue; // one line, or its lines stop well short of its measure
      wrapped++;
      for (const o of blocks) {
        if (o === b || Math.abs(o.left - b.left) > 40) continue; // side by side, not stacked (a list indent is stacked)
        const further = Math.max(...o.ls.map(r => r.right)) - b.right, key = name(b.el) + '|' + name(o.el);
        if (further > 4 && !seen.has(key)) { seen.add(key); errors.push(`${name(b.el)}${b.bare ? ' (bare text)' : ''} "${b.el.textContent.trim().slice(0, 40)}" wraps at ${Math.round(b.right - b.left)}px while its stacked sibling ${name(o.el)} "${o.el.textContent.trim().slice(0, 30)}" runs ${Math.round(further)}px further`); }
      }
    }
  }
  return [errors, wrapped];
}"""


def check_none_identified_tips(browser, base_url: str, data: dict) -> list[str]:
    """None identified explains itself where the default view hides it: in the table with every status shown,
    and in the record beside the map."""
    errors = []
    page = browser.new_page(viewport={"width": 1280, "height": 900})
    hydrated(page, base_url)
    page.locator("#world-status").select_option("")
    marks = page.evaluate("""() => [...document.querySelectorAll('#world-rows td .smark')].filter(m => m.querySelector('.st-none_identified')).map(m => [m.dataset.tip, m.tabIndex])""")
    if not marks or any(not (tip or "").strip() or tabindex != 0 for tip, tabindex in marks):
        errors.append(f"table: None identified status marks lack the focusable tooltip ({len(marks)} marks, first {marks[:1]})")
    name = sorted(j["name"] for j in data["jurisdictions"] if j.get("europe_scope") and j["status"] == "none_identified")[0]
    page.locator("#map-search").fill(name)
    page.locator("#map-search").press("Enter")
    got = page.evaluate("""() => { const m = document.querySelector('#record .record-status .smark'); return m ? [m.dataset.tip, m.tabIndex] : null; }""")
    if not got or not (got[0] or "").strip() or got[1] != 0:
        errors.append(f"record beside the map: {name}'s None identified mark lacks the focusable tooltip ({got})")
    # The record qualifies the status date as the card does, no more: advisory recommendation, then any status confidence other than high.
    dated_states = [j for j in data["jurisdictions"] if j["status"] != "none_identified" and j.get("status_since") and j.get("kind") != "supranational"]
    qualified_states = [j for j in dated_states if j["display"].get("advisory") or j.get("status_confidence") not in (None, "high")]
    plain = next((j for j in sorted(dated_states, key=lambda j: j["name"]) if j not in qualified_states), None)
    if not qualified_states:
        errors.append("record beside the map: no state with a qualified status date to check")
    for j in qualified_states + ([plain] if plain else []):
        want = (["advisory recommendation"] if j["display"].get("advisory") else []) + ([j["status_confidence"] + " confidence"] if j.get("status_confidence") not in (None, "high") else [])
        page.locator("#map-search").fill(j["name"])
        page.locator("#map-search").press("Enter")
        shown = page.evaluate("""j => { const r = document.getElementById('record'); if (r.dataset.j !== j) return null; const q = r.querySelector('.record-status .qualifiers'); if (!q) return []; return [...q.querySelectorAll(':scope > .meta-item')].map(n => { const c = n.cloneNode(true); c.querySelectorAll('.cites, .cn').forEach(x => x.remove()); return c.textContent.replace(/\\u00b7/g, '').trim(); }); }""", j["id"])
        if shown is None:
            errors.append(f"record beside the map: searching {j['name']} does not open its record")
        elif shown != want:
            errors.append(f"record beside the map: {j['name']}'s status qualifiers {shown}, expected those on its card {want}")
    page.close()
    return errors


def check_text_measure(browser, base_url: str) -> list[str]:
    """Principle Measure: stacked sibling text blocks in one container wrap at one width, so no paragraph,
    list or note breaks early beside a sibling whose lines run further (bare text included). Measured from
    the rendered lines with every disclosure open, at 1280 and 390 px. Report lines (cosmetic, D1), except a missing
    headline or first screen, which blocks."""
    errors = []
    for width in (1600, 1280, 390):
        page = browser.new_page(viewport={"width": width, "height": 900})
        hydrated(page, base_url)
        page.locator("details").evaluate_all("nodes => nodes.forEach(n => n.open = true)")
        page.evaluate("async () => { document.body.offsetHeight; await document.fonts.ready; await new Promise(requestAnimationFrame); }")  # opened lists reveal italic text in its own font
        found, wrapped = page.evaluate(TEXT_MEASURE_JS)
        errors.extend(report_line(f"{width}px: {e}") for e in found)
        # The headline runs full width (principle: Headline) but never past the first screen's own column (the headline is not a paragraph, so TEXT_MEASURE_JS skips it).
        over = page.evaluate("""() => { const h = document.querySelector('#headline'), f = document.querySelector('section.first'); if (!h || !f) return 'missing';
          const r = document.createRange(); r.selectNodeContents(h); const right = Math.max(...[...r.getClientRects()].map(x => x.right));
          return right > f.getBoundingClientRect().right + 1 ? Math.round(right) : null; }""")
        if over == "missing":  # the headline or the first screen is not on the page: blocking
            errors.append(f"{width}px: the headline or the first screen's column is missing")
        elif over:
            errors.append(report_line(f"{width}px: the headline runs past the first screen's column ({over})"))
        if wrapped < 20:
            errors.append(report_line(f"{width}px: only {wrapped} wrapped text blocks measured; the measure check is not seeing the page"))
        page.close()
    return errors


def check_wide_layout(browser, base_url: str) -> list[str]:
    """At 1100px and wider, with default settings, nothing needs sideways scroll and the worldwide
    table header sticks below the section navigation and the stuck States row; a phone scrolls the table without a sticky header."""
    errors = []
    for width in WIDE_WIDTHS + (390,):
        for theme in ("light", "dark"):
            page = themed_page(browser, theme, viewport={"width": width, "height": 900})
            try:
                hydrated(page, base_url)
                page.wait_for_selector("#world-rows tr")
                page.evaluate("document.fonts.ready")
                result = page.evaluate(
                    """() => new Promise(done => requestAnimationFrame(() => requestAnimationFrame(() => {
                  const scrolling = [...document.querySelectorAll('body *')].filter(n => n.getClientRects().length && /auto|scroll/.test(getComputedStyle(n).overflowX) && n.scrollWidth > n.clientWidth + 1)
                    .map(n => n.id || n.getAttribute('class') || n.tagName.toLowerCase());
                  const page = document.documentElement.scrollWidth > innerWidth + 1;
                  const table = document.querySelector('#world-rows').getBoundingClientRect();
                  scrollTo(0, scrollY + table.top + 300);
                  requestAnimationFrame(() => requestAnimationFrame(() => {
                    const th = document.querySelector('table.world thead th'), nav = document.querySelector('.secnav').getBoundingClientRect(), row = th.closest('.group').querySelector('.group-nav').getBoundingClientRect();
                    done({ scrolling, page, sticky: getComputedStyle(th).position === 'sticky', top: th.getBoundingClientRect().top, nav: row.top <= nav.bottom + 1 ? row.bottom : nav.bottom });
                  }));
                }))) """
                )
            finally:
                page.close()
            label = f"{width}px {theme}"
            if width < 640:
                if result["sticky"]:
                    errors.append(f"{label}: phone table header is sticky")
                if "world-scroll" not in result["scrolling"]:
                    errors.append(f"{label}: phone table no longer scrolls sideways (gate expectation outdated?)")
                continue
            if result["page"] or result["scrolling"]:
                errors.append(f"{label}: needs sideways scroll with default settings: {', '.join(result['scrolling']) or 'page'}")
            if not result["sticky"] or abs(result["top"] - result["nav"]) > 1:
                errors.append(report_line(f"{label}: table header does not stick below the section navigation and group row (top {result['top']:.0f}, rows {result['nav']:.0f})"))
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
  return { token: token('--highlight-fill'), paper: token('--paper'), ground: token('--ground'), step: ratio(token('--highlight-fill'), token('--paper')),
    texts: [...document.querySelectorAll('#map-search-list li[aria-selected="true"], #map-search-list li[aria-selected="true"] *')].filter(n => [...n.childNodes].some(c => c.nodeType === 3 && c.textContent.trim())).map(n => ratio(getComputedStyle(n).color, token('--highlight-fill'))),
    selected: getComputedStyle(document.querySelector('#map-search-list li[aria-selected="true"]') || document.body).backgroundColor, list: getComputedStyle(document.getElementById('map-search-list')).backgroundColor };
}"""


def check_highlight_fill(browser, base_url: str) -> list[str]:
    """Principle Highlight: the current item in a list or menu stands out from the paper by HIGHLIGHT_STEP in both themes, its text at least 4.5:1,
    and the active suggestion, a hovered download menu item, table row and download all take that one token."""
    errors, steps = [], {}
    for theme in ("light", "dark"):
        page = themed_page(browser, theme, viewport={"width": 1280, "height": 900})
        try:
            hydrated(page, base_url)
            page.locator("#map-search").fill("den")
            page.locator("#map-search").press("ArrowDown")
            page.wait_for_timeout(50)
            got = page.evaluate(HIGHLIGHT_JS)
            steps[theme] = got["step"]
            if got["step"] < HIGHLIGHT_STEP:
                errors.append(f"{theme}: --highlight-fill steps {got['step']:.2f}:1 from the paper, below {HIGHLIGHT_STEP}:1")
            if not got["texts"]:
                errors.append(f"{theme}: no active suggestion with text to measure")
            elif min(got["texts"]) < 4.5:
                errors.append(f"{theme}: text on the active suggestion is {min(got['texts']):.2f}:1, below 4.5:1")
            if got["selected"] == got["list"]:  # the active suggestion cannot be seen: blocking
                errors.append(f"{theme}: the active suggestion has the list's own fill ({got['list']})")
            elif got["selected"] != got["token"] or got["list"] != got["paper"]:
                errors.append(report_line(f"{theme}: the active suggestion ({got['selected']}) is not --highlight-fill ({got['token']}) on the paper list ({got['list']})"))
            page.locator("#map-search").fill("")
            page.locator("#map-search").press("Escape")
            def hovered(label, node):
                node.scroll_into_view_if_needed()
                node.hover()
                if node.evaluate("n => getComputedStyle(n).backgroundColor") != got["token"]:
                    errors.append(report_line(f"{theme}: a hovered {label} does not take --highlight-fill"))

            hovered("table row", page.locator("#world-rows tr").first)
            page.locator("#world-download > summary").click()
            hovered("download menu item", page.locator("#world-download .popover > *").first)
            page.locator("#world-download > summary").click()
            hovered("download", page.locator(".dlist a").first)
        finally:
            page.close()
    if len(steps) == 2 and abs(steps["light"] - steps["dark"]) > THEME_PARITY_TOLERANCE:
        errors.append(report_line(f"--highlight-fill steps {steps['light']:.2f}:1 from the paper in light but {steps['dark']:.2f}:1 in dark"))
    return [f"highlight: {e}" for e in errors]


PALETTE_SECTIONS = ("map", "testing", "set-up", "funding", "news", "europe", "world", "data", "method", "definition")
PALETTE_GROUPS = ["palette-g-sections", "palette-g-regions", "palette-g-actions", "palette-g-states"]
PALETTE_JS = """() => {
  const canvas = document.createElement('canvas').getContext('2d', { willReadFrequently: true });
  const rgb = c => { canvas.clearRect(0, 0, 1, 1); canvas.fillStyle = c; canvas.fillRect(0, 0, 1, 1); return [...canvas.getImageData(0, 0, 1, 1).data].slice(0, 3); };
  const lum = c => { const f = v => { v /= 255; return v <= .03928 ? v / 12.92 : ((v + .055) / 1.055) ** 2.4; }; const [r, g, b] = rgb(c).map(f); return .2126 * r + .7152 * g + .0722 * b; };
  const ratio = (a, b) => { const x = lum(a), y = lum(b); return (Math.max(x, y) + .05) / (Math.min(x, y) + .05); };
  const token = name => { const p = document.createElement('span'); p.style.color = `var(${name})`; document.body.appendChild(p); const c = getComputedStyle(p).color; p.remove(); return c; };
  const box = document.getElementById('palette'), q = document.getElementById('palette-q'), list = document.getElementById('palette-list');
  const active = q && document.getElementById(q.getAttribute('aria-activedescendant') || '-');
  const fill = active ? getComputedStyle(active).backgroundColor : '';
  return { open: !!box && box.open, focus: document.activeElement && document.activeElement.id, value: q ? q.value : null,
    roles: box && [box.getAttribute('role'), box.getAttribute('aria-modal'), q.getAttribute('role'), q.getAttribute('aria-controls'), q.getAttribute('aria-expanded'), q.getAttribute('aria-label'), list.getAttribute('role')],
    active: active ? [active.getAttribute('role'), active.getAttribute('aria-selected'), list.contains(active), active.querySelector('.sname').firstChild.textContent] : null,
    selected: [...list.querySelectorAll('[aria-selected="true"]')].length,
    groups: [...list.querySelectorAll('[role="group"]')].map(g => [g.querySelector('.palette-group')?.textContent, [...g.querySelectorAll('[role="option"]')].map(o => o.querySelector('.sname').firstChild.textContent), [...g.querySelectorAll('[role="option"]')].map(o => o.textContent), g.getAttribute('aria-labelledby')]),
    options: [...list.querySelectorAll('[role="option"]')].map(o => o.id),
    fill, token: token('--highlight-fill'),
    texts: active ? [active, ...active.querySelectorAll('*')].filter(n => [...n.childNodes].some(c => c.nodeType === 3 && c.textContent.trim())).map(n => ratio(getComputedStyle(n).color, fill)) : [] };
}"""


def check_command_palette(browser, base_url: str) -> list[str]:
    """Principle Command palette: Cmd/Ctrl+K and "/" open a modal dialog with focus in its labelled combobox; an empty field
    lists Sections and Regions first; a state name and Enter open its record as the map search does; institute and former
    names find their state; a section row lands on its anchor and a region row presses its tab; Esc returns focus; the
    current row takes --highlight-fill with text at least 4.5:1 in both themes; "/" in a text field types a slash; the
    hint names the platform's key and hides on phones; the saved page holds the dialog closed."""
    errors = []

    def palette(page):
        return page.evaluate(PALETTE_JS)

    def open_with(page, key, where):
        page.keyboard.press(key)
        got = palette(page)
        if not got["open"] or got["focus"] != "palette-q" or got["value"] != "":
            errors.append(f"{where}: {key} does not open the palette with focus in its empty field ({got['open']}, {got['focus']}, {got['value']!r})")
        return got

    def typed(page, text):
        page.locator("#palette-q").fill(text)
        return palette(page)

    for theme in ("light", "dark"):
        page = themed_page(browser, theme, viewport={"width": 1280, "height": 900})
        try:
            hydrated(page, base_url)
            raw = page.request.get(base_url).text()
            if not re.search(r'<dialog class="palette" id="palette"(?![^>]*\sopen)[^>]*>', raw):
                errors.append(f"{theme}: the saved page does not hold the palette as a closed dialog")
            mac = page.evaluate("document.documentElement.hasAttribute('data-mac')")
            hint = page.locator("#palette-open").inner_text().strip()
            # The platform's shortcut, in any typography: the Command key on a Mac, Ctrl elsewhere, then K.
            modifier = r"(⌘|cmd|command)" if mac else r"ctrl"
            if not re.search(modifier + r"\s*\+?\s*k\b", hint, re.I) or (not mac and re.search(r"⌘|cmd|command", hint, re.I)):
                errors.append(f"{theme}: the shortcut hint reads {hint!r}, not the {'Command' if mac else 'Ctrl'} K shortcut of this platform")
            tab = page.locator('#region-chips button[data-region="Asia"]')
            tab.focus()
            got = open_with(page, "ControlOrMeta+k", theme)
            if got["roles"] is None or got["roles"][:5] + got["roles"][6:] != ["dialog", "true", "combobox", "palette-list", "true", "listbox"] or not (got["roles"][5] or "").strip():
                errors.append(f"{theme}: the dialog's roles are {got['roles']}")
            if not got["active"] or got["active"][:3] != ["option", "true", True] or got["selected"] != 1:
                errors.append(f"{theme}: aria-activedescendant does not name the one selected option ({got['active']}, {got['selected']} selected)")
            if [g[3] for g in got["groups"]] != PALETTE_GROUPS or any(not (g[0] or "").strip() or any(not name.strip() for name in g[1]) for g in got["groups"]):
                errors.append(f"{theme}: the empty palette's groups are {[g[0] for g in got['groups']]}, not {PALETTE_GROUPS}")
            # One row per section, each named once (the rows carry no section key to compare; a duplicate cannot stand in for a missing one).
            elif len(got["groups"][0][1]) != len(PALETTE_SECTIONS) or len(set(got["groups"][0][1])) != len(PALETTE_SECTIONS) or got["groups"][1][1] != ["World", "Europe", "Americas", "Asia", "Oceania", "Africa"]:
                errors.append(f"{theme}: the Sections and Regions rows are {got['groups'][0][1]} and {got['groups'][1][1]}")
            if got["fill"] != got["token"]:
                errors.append(f"{theme}: the current row is {got['fill']}, not --highlight-fill {got['token']}")  # the keyboard's place: blocking
            if not got["texts"] or min(got["texts"]) < 4.5:
                errors.append(f"{theme}: text on the current row is {min(got['texts'] or [0]):.2f}:1, below 4.5:1")
            moved = []
            for key in ("End", "Home", "ArrowDown", "ArrowUp"):
                page.keyboard.press(key)
                moved.append(page.locator("#palette-q").get_attribute("aria-activedescendant"))
            ids = got["options"]
            if not ids or moved != [ids[-1], ids[0], ids[1], ids[0]]:
                errors.append(f"keyboard: End, Home, ArrowDown and ArrowUp move the current row to {moved}, not the last, first, second and first")
            # Modified keys and input-method composition stay with the text field.
            page.locator("#palette-q").fill("ma")
            before = page.locator("#palette-q").get_attribute("aria-activedescendant")
            page.keyboard.press("ArrowDown")
            current = page.locator("#palette-q").get_attribute("aria-activedescendant")
            page.keyboard.press("Shift+Home")
            if page.locator("#palette-q").get_attribute("aria-activedescendant") != current or current == before:
                errors.append("keyboard: Shift+Home moves the current row instead of selecting text")
            page.locator("#palette-q").dispatch_event("keydown", {"key": "Enter", "isComposing": True, "keyCode": 229, "bubbles": True})
            if not palette(page)["open"] or page.locator("#palette-q").input_value() != "ma":
                errors.append("keyboard: Enter during input-method composition acts on the current row")
            page.locator("#palette-q").fill("")
            page.keyboard.press("Tab")
            if palette(page)["focus"] != "palette-q":
                errors.append("focus: Tab leaves the palette's field")
            page.keyboard.press("Escape")
            got = palette(page)
            if got["open"] or page.evaluate("document.activeElement && document.activeElement.dataset.region") != "Asia":
                errors.append(f"{theme}: Esc does not close the palette and return focus to the Asia tab")
            if theme == "dark":
                continue
            # "/" outside a text field opens it; inside one it types a slash.
            page.evaluate("document.activeElement.blur()")
            open_with(page, "/", "slash")
            # A state's name, its institute's short form and a former name find it; accents are ignored.
            for text, want in (("france", ["France"]), ("INESIA", ["France"]), ("CAISI", ["Canada", "United States"]), ("sao tome", ["São Tomé and Príncipe"])):
                got = typed(page, text)
                first = got["groups"][0] if got["groups"] else ["", [], [], ""]
                if first[3] != "palette-g-states" or not (first[0] or "").strip() or first[1][:len(want)] != want:
                    errors.append(f"matching: {text!r} lists {first[1][:3]} under {first[0]!r} first, not {want}")
                if text == "CAISI" and not any("formerly" in row for row in first[2][1:2]):
                    errors.append(f"matching: the United States row found by a former name does not say formerly ({first[2]})")
            typed(page, "france")
            page.keyboard.press("Enter")
            if palette(page)["open"] or page.locator("#record-name").text_content() != "France":
                errors.append("state: typing France and Enter does not close the palette and open France's record")
            page.locator("#record .record-close").click()
            # A section row lands on its anchor, below the section bar.
            open_with(page, "ControlOrMeta+k", "section")
            typed(page, "funding")
            page.keyboard.press("Enter")
            page.wait_for_timeout(200)
            landed = page.evaluate("() => { const r = document.getElementById('funding').getBoundingClientRect(); return [location.hash, r.top > 0 && r.top < innerHeight / 2, document.activeElement && document.activeElement.id]; }")
            if landed != ["#funding", True, "funding"]:
                errors.append(f"section: the Funding row lands at {landed}, not on #funding in view with its heading focused")
            # A region row switches the map as its tab does.
            open_with(page, "ControlOrMeta+k", "region")
            typed(page, "oceania")
            page.keyboard.press("Enter")
            pressed = page.evaluate("[...document.querySelectorAll('#region-chips button[aria-pressed=\"true\"]')].map(b => b.dataset.region)")
            if pressed != ["Oceania"] or page.locator("#headline").get_attribute("data-region") != "Oceania":
                errors.append(f"region: the Oceania row leaves the pressed tabs at {pressed}")
            page.locator("#world-filter").focus()
            page.keyboard.press("/")
            if palette(page)["open"] or page.locator("#world-filter").input_value() != "/":
                errors.append("slash: '/' in a text field opens the palette instead of typing a slash")
            page.locator("#world-filter").fill("")
            # The theme row moves the toggle's cycle and the palette stays open on it.
            page.locator("#palette-open").click()
            typed(page, "theme")
            page.keyboard.press("Enter")
            if page.evaluate("document.documentElement.getAttribute('data-theme')") != "dark" or not palette(page)["open"]:
                errors.append("theme: the theme row does not move light to dark with the palette left open")
        finally:
            page.close()
    page = browser.new_page(viewport={"width": 390, "height": 800})
    try:
        hydrated(page, base_url)
        if page.locator("#palette-open").is_visible():
            errors.append(report_line("390px: the shortcut hint shows on a phone"))
    finally:
        page.close()
    return [f"command palette: {e}" for e in errors]


# Principle Off-screen sections: a section with more elements than this skips style and paint while off-screen, and the
# page outside such sections keeps under the budget, so a theme switch restyles and repaints only that much. A
# structural bound rather than a switch time, which varies with the machine's load (timings warn, never block).
OFFSCREEN_SECTION_ELEMENTS, ALWAYS_DRAWN_ELEMENTS = 1500, 4000


def check_offscreen_sections(browser, base_url: str) -> list[str]:
    """Principle Off-screen sections: once the page has settled, every large section has content-visibility: auto and
    remembers its rendered size, the elements always drawn stay within budget, and skipping moves nothing: a later
    section's place and the scroll position are the same as with every section drawn, also after a width change."""
    from playwright.sync_api import TimeoutError as PlaywrightTimeout

    errors = []
    shape = """([limit, budget]) => { const skips = s => getComputedStyle(s).contentVisibility === 'auto';
      const sections = [...document.querySelectorAll('main section')], skipping = sections.filter(skips);
      return { large: sections.filter(s => s.getElementsByTagName('*').length > limit && !(skips(s) && getComputedStyle(s).containIntrinsicBlockSize.startsWith('auto'))).map(s => s.getAttribute('aria-labelledby')),
        skipping: skipping.length, drawn: document.getElementsByTagName('*').length - skipping.reduce((n, s) => n + s.getElementsByTagName('*').length, 0) }; }"""
    # The last heading's place in the document and the scroll position, with the sections above it skipped, then drawn.
    place = "() => [document.getElementById('method').getBoundingClientRect().top + scrollY, scrollY]"
    settled = "document.documentElement.hasAttribute('data-settled')"
    page = browser.new_page(viewport={"width": 1440, "height": 900}, skipping=True)
    try:
        hydrated(page, base_url)
        for width in (1440, 390):
            if width != 1440:
                page.set_viewport_size({"width": width, "height": 900})
                page.wait_for_timeout(1000)  # past the page's 250 ms wait for the width change to end, and its redraw
            try:
                page.wait_for_function(settled, timeout=5_000)
            except PlaywrightTimeout:
                errors.append(f"{width}px: the page never marks itself settled (data-settled), so no section skips while off-screen")
                break
            got = page.evaluate(shape, [OFFSCREEN_SECTION_ELEMENTS, ALWAYS_DRAWN_ELEMENTS])
            if got["large"] or not got["skipping"]:
                errors.append(f"{width}px: sections with more than {OFFSCREEN_SECTION_ELEMENTS} elements without content-visibility: auto and a remembered size (contain-intrinsic-size: auto): {got['large'] or 'no section skips'}")
            if got["drawn"] > ALWAYS_DRAWN_ELEMENTS:
                errors.append(f"{width}px: {got['drawn']} elements outside the off-screen sections, beyond the budget of {ALWAYS_DRAWN_ELEMENTS}; a theme switch restyles every one")
            page.evaluate("scrollTo(0, document.documentElement.scrollHeight)")
            page.wait_for_timeout(200)
            skipped = page.evaluate(place)
            page.evaluate("document.documentElement.removeAttribute('data-settled')")
            drawn = page.evaluate(place)
            if any(abs(a - b) > 1 for a, b in zip(skipped, drawn)):
                errors.append(f"{width}px: skipping the off-screen sections moves the page (Method heading and scroll position {skipped} skipped, {drawn} drawn)")
            page.evaluate("document.documentElement.setAttribute('data-settled', ''); scrollTo(0, 0)")
    finally:
        page.close()
    return errors


# Interaction times (they warn, never block): at CPU_SLOWDOWN, standing in for a phone, the page
# answers within INTERACTION_MS after load and on a theme switch, opening the command palette and opening a disclosure.
CPU_SLOWDOWN, INTERACTION_MS = 4, 100


def check_interaction_times(browser, base_url: str) -> list[str]:
    """Warn (stderr, no error) where an interaction takes longer than INTERACTION_MS at CPU_SLOWDOWN: the longest task
    after load, and the median of several theme switches, palette openings and disclosure openings, each timed from
    the action to the next frame."""
    # Each sample starts from the closed state, after a pause in which the page's idle work (the palette list) can run.
    timed = """async ([action, n]) => { const frame = () => new Promise(r => requestAnimationFrame(() => setTimeout(r, 0)));
      const out = []; for (let i = 0; i < n; i++) { const p = document.getElementById('palette'); if (p.open) p.close(); await new Promise(r => setTimeout(r, 300));
        await frame(); await frame(); const t0 = performance.now(); await (0, eval)(action)(i); await frame(); out.push(performance.now() - t0); }
      return out.sort((a, b) => a - b)[Math.floor(out.length / 2)]; }"""
    actions = {
        "theme switch": "i => document.documentElement.setAttribute('data-theme', i % 2 ? 'light' : 'dark')",
        "opening the command palette": "i => document.dispatchEvent(new KeyboardEvent('keydown', { key: '/', bubbles: true }))",
        "opening a disclosure": "i => { const d = document.getElementById('method-details'); if (d.open) d.open = false; d.open = true; }",
    }
    from playwright.sync_api import TimeoutError as PlaywrightTimeout

    for width in (1440, 390):
        page = browser.new_page(viewport={"width": width, "height": 900}, skipping=True)
        try:
            page.context.new_cdp_session(page).send("Emulation.setCPUThrottlingRate", {"rate": CPU_SLOWDOWN})
            page.add_init_script("window.__long = 0; new PerformanceObserver(l => l.getEntries().forEach(e => { window.__long = Math.max(window.__long, e.duration); })).observe({ type: 'longtask', buffered: true });")
            hydrated(page, base_url)
            page.wait_for_function("document.documentElement.hasAttribute('data-settled')", timeout=60_000)
            page.wait_for_timeout(1000)
            got = {"longest task after load": page.evaluate("window.__long")}
            for name, action in actions.items():
                got[name] = page.evaluate(timed, [action, 5])
                page.evaluate("() => { const p = document.getElementById('palette'); if (p.open) p.close(); document.documentElement.removeAttribute('data-theme'); }")
            for name, ms in got.items():
                if ms > INTERACTION_MS:
                    print(f"warning: {width}px at {CPU_SLOWDOWN}x slower CPU: {name} takes {ms:.0f} ms, beyond {INTERACTION_MS} ms", file=sys.stderr)
        except PlaywrightTimeout as exc:  # a timing diagnostic: readiness itself is checked, unslowed, by the blocking checks
            print(f"warning: {width}px at {CPU_SLOWDOWN}x slower CPU: interaction times not measured ({exc})", file=sys.stderr)
        finally:
            page.close()
    return []


def check_toggle_place(browser, base_url: str) -> list[str]:
    """Principles Theme and Phone: one theme toggle, at the right end of the sticky section bar above 430px (still there
    when the page is scrolled), in the header on phones; the bar row keeps fitting without sideways scroll."""
    errors = []
    for width, scale in ((1440, "100%"), (1100, "110%"), (1000, "100%"), (680, "110%"), (650, "110%"), (470, "110%"), (431, "110%"), (390, "100%"), (360, "110%")):
        page = browser.new_page(viewport={"width": width, "height": 800})
        try:
            hydrated(page, base_url)
            page.evaluate(f"document.documentElement.style.fontSize = '{scale}'")
            page.evaluate("scrollTo(0, 1500)")
            page.wait_for_timeout(100)
            got = page.evaluate("""() => { const t = document.getElementById('theme-toggle'), bar = document.querySelector('.secnav .measure'), r = t.getBoundingClientRect(), b = bar.getBoundingClientRect(), pad = parseFloat(getComputedStyle(bar).paddingRight);
              return { count: document.querySelectorAll('.theme-toggle').length, inBar: !!t.closest('.secnav'), inHeader: !!t.closest('.topbar'), last: bar.lastElementChild === t,
                right: b.right - pad - r.right, shown: r.width > 0 && r.top >= 0 && r.bottom <= document.querySelector('.secnav').getBoundingClientRect().bottom + 1,
                overflow: bar.scrollWidth - bar.clientWidth }; }""")
            if got["count"] != 1:
                errors.append(f"{width}px: {got['count']} theme toggles in the page, expected one")
            if width > 430 and not got["shown"]:  # the toggle cannot be seen or reached: blocking
                errors.append(f"{width}px: the theme toggle is not shown in the sticky section bar while scrolled ({got})")
            elif width > 430 and not (got["inBar"] and got["last"] and abs(got["right"]) <= 2):
                errors.append(report_line(f"{width}px: the theme toggle is not at the right end of the sticky section bar while scrolled ({got})"))
            if width <= 430:
                page.evaluate("scrollTo(0, 0)")
                shown, top = page.evaluate("() => { const r = document.getElementById('theme-toggle').getBoundingClientRect(); return [r.width > 0 && r.height > 0 && r.top >= 0 && r.bottom <= innerHeight && r.left >= 0 && r.right <= innerWidth, r.bottom <= document.querySelector('.secnav').getBoundingClientRect().top + 1]; }")
                if not shown:  # at the top of the page it must be on screen: blocking
                    errors.append(f"{width}px: on phones the theme toggle is not shown")
                elif not got["inHeader"] or not top:
                    errors.append(report_line(f"{width}px: on phones the theme toggle is not in the header above the section bar"))
            if got["overflow"] > 1:
                errors.append(f"{width}px at {scale} text: the section bar row scrolls sideways by {got['overflow']}px with the theme toggle")
        finally:
            page.close()
    # The toggle keeps keyboard focus when a width change moves it between the bar and the header.
    page = browser.new_page(viewport={"width": 1000, "height": 800})
    hydrated(page, base_url)
    page.locator("#theme-toggle").focus()
    for width, home in ((390, ".topbar"), (1000, ".secnav")):
        page.set_viewport_size({"width": width, "height": 800})
        page.wait_for_timeout(200)
        got = page.evaluate("home => [document.activeElement && document.activeElement.id, !!document.querySelector(home + ' #theme-toggle')]", home)
        if got != ["theme-toggle", True]:
            errors.append(f"theme toggle loses focus or place when the width changes to {width}px: {got}")
    page.close()
    return errors


def check_group_rows(browser, base_url: str) -> list[str]:
    """Phones and sticky group rows (owner report from an Android phone): at 360, 390 and 412px, also with the root
    text 10% larger, the bar is one row of the five items with no sideways scroll and no visible scrollbar, and each
    group row is one line; inside a group its row sticks directly under the bar with the current section's link
    marked, and in News none sticks; after a jump to a section, from a group row or a hash link, its heading
    lands below both rows."""
    errors: list[str] = []
    bar = """() => { const n = document.querySelector('.secnav'), m = n.querySelector('.measure'), items = [...m.children].filter(a => a.getBoundingClientRect().width);
      return [m.scrollWidth - m.clientWidth, new Set(items.map(a => Math.round(a.getBoundingClientRect().top))).size, items.map(a => a.innerText.trim()), m.offsetHeight - m.clientHeight,
        Math.max(...[...document.querySelectorAll('.group-nav')].map(r => new Set([...r.querySelectorAll('a')].map(a => Math.round(a.getBoundingClientRect().top))).size))]; }"""
    for width in (360, 390, 412):
        for scale in ("100%", "110%"):
            page = browser.new_page(viewport={"width": width, "height": 780})
            hydrated(page, base_url)
            page.evaluate(f"document.documentElement.style.fontSize = '{scale}'")
            page.evaluate("document.fonts.ready")
            page.evaluate("scrollTo(0, document.querySelector('#testing').closest('section').offsetTop)")
            page.wait_for_timeout(150)
            got = page.evaluate(bar)
            # An item cut off or missing blocks; a wrapped row or a visible scrollbar is a report line.
            if got[0] != 0 or len(got[2]) != len(SECTION_BAR) or not all(got[2]):
                errors.append(f"{width}px at {scale} text: the bar cuts off or lacks one of the five items (overflow, items: {got[0]}, {got[2]})")
            if got[1] != 1 or got[3] != 0 or got[4] != 1:
                errors.append(report_line(f"{width}px at {scale} text: the bar wraps or shows a scrollbar, or a group row wraps (bar rows, scrollbar, most group row lines: {got[1]}, {got[3]}, {got[4]})"))
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
        # A keyboard focus under the rows comes out from under them: a funding disclosure reached by Tab.
        got = page.evaluate("""() => { const s = document.querySelector('#fund-rows details > summary'), all = [...document.querySelectorAll('a[href], button, summary, input, select, [tabindex="0"]')].filter(n => n.getClientRects().length);
          const prev = all[all.indexOf(s) - 1], nav = document.querySelector('.secnav').getBoundingClientRect().bottom;
          scrollTo(0, s.getBoundingClientRect().top + scrollY - nav - 4); prev.focus({ preventScroll: true }); return !!prev; }""")
        page.wait_for_timeout(100)
        page.keyboard.press("Tab")
        page.wait_for_timeout(200)
        got = page.evaluate("""() => { const a = document.activeElement, s = document.querySelector('#fund-rows details > summary'), row = document.querySelector('.group-nav:has(a[href="#testing"])').getBoundingClientRect();
          return [a === s, Math.round(a.getBoundingClientRect().top), Math.round(row.bottom)]; }""")
        if not got[0] or got[1] < got[2]:
            errors.append(f"{width}px: a funding disclosure focused by Tab under the group row stays under it (focused, top, row bottom: {got})")
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
        # Deep inside a group the row stays; in News no group row shows.
        page.evaluate("scrollTo(0, document.getElementById('world-scroll').getBoundingClientRect().top + scrollY - 200)")
        page.wait_for_timeout(200)
        got = page.evaluate(rows)
        if not got or not (got[0][0] or "").strip() or got[0][1] != 0 or "#world" not in got[0][3]:
            errors.append(report_line(f"{width}px: deep in The world the States row is not stuck under the bar ({got})"))
        page.evaluate("scrollTo(0, document.getElementById('news').closest('section').getBoundingClientRect().top + scrollY - 96)")
        page.wait_for_timeout(200)
        got = page.evaluate(rows)
        if got:
            errors.append(report_line(f"{width}px: in News a group row sticks under the bar ({got})"))
        page.close()
    # Without scripting, with larger text, the saved page's row still sticks flush under the bar (rem defaults, no stale pixels).
    page = browser.new_page(viewport={"width": 360, "height": 780}, java_script_enabled=False)
    page.route(base_url, lambda route: route.fulfill(response=(r := route.fetch()), body=r.text().replace("</head>", "<style>html { font-size: 110% !important; }</style></head>", 1)))
    page.goto(base_url)
    page.wait_for_timeout(200)
    box = page.locator(".group-nav").first.bounding_box()
    page.mouse.move(180, 400)
    page.mouse.wheel(0, box["y"] + 600)
    page.wait_for_timeout(400)
    got = [page.locator(".secnav").bounding_box(), page.locator(".group-nav").first.bounding_box()]
    if abs(got[1]["y"] - (got[0]["y"] + got[0]["height"])) > 1:
        errors.append(report_line(f"no script, 110% text: the Compare row does not stick flush under the bar (bar bottom {got[0]['y'] + got[0]['height']:.0f}, row top {got[1]['y']:.0f})"))
    page.close()
    return errors


def check_touch_tips(browser, base_url: str) -> list[str]:
    """Touch (owner report from an Android phone): a tooltip opened by a tap closes when the reader jumps to a record
    (hash link), scrolls, or taps elsewhere; it never stays floating over other content."""
    errors: list[str] = []
    context = browser.new_context(viewport={"width": 360, "height": 780}, has_touch=True, is_mobile=True)
    page = context.new_page()
    hydrated(page, base_url)
    page.evaluate("document.fonts.ready")
    shown = "() => { const t = document.getElementById('tip'); return !!t && !t.hidden; }"
    trigger = page.locator("#world-rows abbr.g").first

    def tap() -> bool:
        trigger.scroll_into_view_if_needed()
        page.wait_for_timeout(150)
        trigger.tap()
        page.wait_for_timeout(150)
        return page.evaluate(shown)

    for action, run in (
        ("a hash jump to a record", lambda: page.evaluate("location.hash = '#c-au'")),
        ("a hash change that neither scrolls nor moves focus", lambda: page.evaluate("location.hash = '#no-such-target'")),
        ("history back to an earlier in-page entry", lambda: page.evaluate("history.pushState(null, '', '#tip-a'); history.pushState(null, '', '#tip-b'); history.back()")),
        ("a scroll", lambda: page.evaluate("scrollBy(0, 200)")),
        ("a sideways scroll of the table", lambda: page.evaluate("document.getElementById('world-scroll').scrollLeft += 60")),
        ("a tap elsewhere", lambda: page.tap("#world-count")),
    ):
        if not tap():
            errors.append(f"tapping a table tooltip term shows no tooltip (before {action})")
            continue
        run()
        page.wait_for_timeout(300)
        if page.evaluate(shown):
            errors.append(f"a tapped tooltip stays open after {action}")
    if tap():  # a few pixels of jitter keep it
        page.evaluate("scrollBy(0, 4)")
        page.wait_for_timeout(200)
        if not page.evaluate(shown):
            errors.append("a tapped tooltip closes on a 4px scroll")
    # Principle Supporting text: on touch, a tap on every visible (i) opens its note, as hover and focus do elsewhere.
    page.evaluate("scrollTo(0, 0)")
    for info_id in page.evaluate("() => [...document.querySelectorAll('button.info[id]')].filter(b => b.getClientRects().length).map(b => b.id)"):
        page.tap("#world-count")
        page.wait_for_timeout(150)
        button = page.locator(f"#{info_id}")
        button.scroll_into_view_if_needed()
        page.wait_for_timeout(150)
        button.tap()
        page.wait_for_timeout(200)
        if not page.evaluate("id => { const t = document.getElementById('tip'); return !!t && !t.hidden && t.textContent.startsWith(document.getElementById(id).dataset.tip); }", info_id):
            errors.append(f"tapping the (i) #{info_id} on touch shows no note")
    context.close()
    for viewport, scale in (({"width": 412, "height": 924}, 2.625), ({"width": 360, "height": 780}, 3)):
        errors.extend(f"{viewport['width']}px: {e}" for e in touch_control_tips(browser, base_url, viewport, scale))
    # Keyboard: a trigger reached by Tab keeps its tip while the page scrolls, and the tip follows it.
    page = browser.new_page(viewport={"width": 1280, "height": 800})
    hydrated(page, base_url)
    page.evaluate("document.fonts.ready")
    page.evaluate("""() => { const t = document.querySelector('#world-rows abbr.g'), all = [...document.querySelectorAll('a[href], button, summary, input, select, [tabindex="0"]')].filter(n => n.getClientRects().length);
      t.scrollIntoView({ block: 'center' }); all[all.indexOf(t) - 1].focus({ preventScroll: true }); }""")
    page.keyboard.press("Tab")
    page.wait_for_timeout(200)
    page.evaluate("scrollBy(0, 120)")
    page.wait_for_timeout(300)
    got = page.evaluate("""() => { const a = document.activeElement, t = document.getElementById('tip'), r = a.getBoundingClientRect(), b = t ? t.getBoundingClientRect() : null;
      return [a.matches('#world-rows abbr.g'), !!t && !t.hidden, !!b && (Math.abs(b.top - r.bottom) < 12 || Math.abs(b.bottom - r.top) < 12)]; }""")
    if got != [True, True, True]:
        errors.append(f"a tooltip opened by keyboard does not stay with its trigger on scroll (focused, shown, beside trigger: {got})")
    page.close()
    return errors


def touch_control_tips(browser, base_url: str, viewport: dict, scale: float) -> list[str]:
    """Touch (owner report from a Pixel phone): a tap's emulated mouseover and the focus it leaves (which comes back with
    the tab after a link opened another one) never open a tip. A tap on a control (link, button) does its job with no
    tip; a tap on a citation opens its tip instead of the source, the tip's host link opens the source in a new tab, and
    a tap on the tip's text closes it.

    A tab a tap should open is awaited as an event armed before the tap, never sampled after a pause, and the tip-text
    scenario runs in a context of its own, so a tab that arrives late cannot be missed or counted by a later tap."""
    from playwright.sync_api import Error as PlaywrightError, TimeoutError as PlaywrightTimeout

    errors: list[str] = []
    local = base_url.split("/")[2]
    shown = "() => { const t = document.getElementById('tip'); return !!t && !t.hidden; }"

    @contextmanager
    def touch_page():
        context = browser.new_context(viewport=viewport, device_scale_factor=scale, has_touch=True, is_mobile=True)
        try:
            context.route(lambda url: url.split("/")[2] != local, lambda route: route.fulfill(status=200, content_type="text/html", body=""))
            page = context.new_page()
            page.opened = []  # every tab opened beside the page, recorded until the context closes
            context.on("page", lambda new: page.opened.append(new))
            hydrated(page, base_url, map_drawn=True)  # a map dot is tapped below
            yield page
        finally:
            context.close()  # closes any tab left open, too

    def point_of(page, selector: str):
        """Where to tap the first rendered match (a citation is a zero-height box whose hit area reaches above the
        baseline), and whether it is a link that opens a new tab; None, with an error, when nothing is tappable."""
        found = page.evaluate("""s => { const n = [...document.querySelectorAll(s)].find(n => n.getClientRects().length); if (!n) return null;
          n.scrollIntoView({ block: 'center', inline: 'center' }); const r = n.getBoundingClientRect(), x = r.left + r.width / 2, y = r.height ? r.top + r.height / 2 : r.top - 4;
          const h = document.elementFromPoint(x, y), a = n.closest('a'); return h && n.contains(h) ? [x, y, !!a && a.target === '_blank'] : null; }""", selector)
        if not found:
            errors.append(f"no tappable {selector}")
        return found

    def tap(page, point, tab: bool, tip: bool | None = None) -> tuple[int, bool]:
        """Tap; return where this tap's tabs start in page.opened (read that slice at the assertion, so a tab is
        counted however late it comes) and whether a tip shows, sampled before any new tab takes focus. With `tip`
        True or False the tip should open or close, which is awaited (bounded); with None no tip should open, which
        only a window can show: 400 ms, in which a tip from the tap's emulated mouseover or focus would appear (a
        slow run can only miss one, never fail a correct page). With `tab`, the new tab is awaited (bounded) from
        before the tap."""
        mark, tipped = len(page.opened), False
        page.wait_for_timeout(150)  # apart from the last tap, so the two are never one double tap
        try:
            with page.context.expect_page(timeout=15_000) if tab else nullcontext():
                page.touchscreen.tap(*point[:2])
                if tip is None:
                    page.wait_for_timeout(400)
                else:
                    settle(page, shown if tip else f"() => !({shown})()")
                tipped = page.evaluate(shown)
        except PlaywrightTimeout:
            pass
        return mark, tipped

    def come_back(page, tabs: list, url: str | None = None) -> list[str]:
        """Close the tabs a tap opened and return to the page, as a reader does (focus comes back to the tapped link);
        return their addresses, once each has reached `url` (a new tab starts at about:blank) or loaded. A tab that
        closes by itself on the way is still listed."""
        opened = []
        for p in tabs:
            try:
                if not p.is_closed():
                    if url:
                        p.wait_for_url(lambda u: u == url, timeout=15_000)
                    else:
                        p.wait_for_load_state()
            except PlaywrightError:  # timed out or closed
                pass
            opened.append(p.url)
            try:
                if not p.is_closed():
                    p.bring_to_front()
                    page.wait_for_timeout(150)
                    p.close()
            except PlaywrightError:  # closed meanwhile
                pass
        page.bring_to_front()
        page.wait_for_timeout(400)  # a window: the focus that comes back with the tab must not open a tip
        return opened

    def open_france(page) -> None:
        """The owner's case: the France record panel, opened from the map search."""
        page.locator("#map-search").fill("fra")
        page.locator("#map-search-0").tap()
        france = "() => { const r = document.getElementById('record'); return !r.hidden && r.dataset.j === 'FR'; }"
        settle(page, france)
        if not page.evaluate(france):
            errors.append("a tap on the map search's France suggestion does not open the France record")

    cite = "#record a.cn"
    with touch_page() as page:
        for selector in ("#world-rows td:first-child a[data-tip]", "#downloads a.ico", "#theme-toggle", "#world-scroll button[data-tip]", "#updated a.meta-link"):
            point = point_of(page, selector)
            if not point:
                continue
            mark, tipped = tap(page, point, tab=point[2])
            if tipped:
                errors.append(f"a tap on the control {selector} shows a tooltip")
            come_back(page, page.opened[mark:])
            if page.evaluate(shown):
                errors.append(f"after a tap on the control {selector} and a return to the tab, a tooltip shows")
            hydrated(page, base_url, map_drawn=True)  # a reload: the map dot below needs the drawn map again
        # A map dot is a control: its tap opens the record, which names the state, with no tip over the map.
        page.evaluate("document.querySelector('#worldmap').scrollIntoView({ block: 'center' })")
        layout_settled(page)
        dot = page.evaluate("""() => { const t = [...document.querySelectorAll('#worldmap svg')].find(n => n.getClientRects().length).querySelector('.tgt[data-j="FR"]'), r = t.getBoundingClientRect(),
          x = r.left + r.width / 2, y = r.top + r.height / 2, h = document.elementFromPoint(x, y); return h && h.closest('#worldmap') ? [x, y] : null; }""")
        if not dot:
            errors.append("no tappable map dot for France")
        else:
            page.touchscreen.tap(*dot)
            # The record opens on the tap's click, after its emulated mouseover: a tip from that would show by now.
            settle(page, "() => !document.getElementById('record').hidden")
            hidden, tipped = page.evaluate("document.getElementById('record').hidden"), page.evaluate(shown)
            if hidden or tipped:
                errors.append(f"a tap on a map dot does not open the record without a tooltip (record hidden: {hidden}, tooltip shown: {tipped})")
            page.locator("#record .record-close").tap()
            settle(page, "() => document.getElementById('record').hidden")
            if not page.evaluate("document.getElementById('record').hidden"):
                errors.append("a tap on the record's close button does not close it")
        open_france(page)
        point = point_of(page, cite)
        if point:
            href = page.evaluate("s => [...document.querySelectorAll(s)].find(n => n.getClientRects().length).href", cite)
            cited, tipped = tap(page, point, tab=False, tip=True)
            if page.opened[cited:] or not tipped:
                errors.append(f"a tap on a citation does not open its tooltip in place (tooltip shown: {tipped}, tabs opened: {len(page.opened[cited:])})")
            come_back(page, page.opened[cited:])
            if not page.evaluate(shown):
                errors.append("a citation's tooltip closes on a return to the tab")
            link = page.evaluate("""h => { const a = [...document.querySelectorAll('#tip a')].find(a => a.href === h); if (!a || document.getElementById('tip').hidden) return null;
              const r = a.getBoundingClientRect(), x = r.left + r.width / 2, y = r.top + r.height / 2; return a.contains(document.elementFromPoint(x, y)) ? [x, y] : null; }""", href)
            if not link:
                errors.append("a citation's tooltip has no tappable link to its source")
            else:
                mark, _ = tap(page, link, tab=True)
                opened = come_back(page, page.opened[mark:], href)
                # One tab from the citation's tap on: none late from the citation itself, none more while coming back.
                if opened != [href] or len(page.opened) - cited != 1:
                    errors.append(f"a tap on the source link in a citation's tooltip does not open the source in one new tab (opened: {opened}, tabs since the citation's tap: {len(page.opened) - cited})")
                if page.evaluate(shown):
                    errors.append("a citation's tooltip stays (or comes back) after its source link was followed")
    # Its own context: no tab from the taps above can reach these counts.
    with touch_page() as page:
        open_france(page)
        point = point_of(page, cite)
        if point:
            mark, _ = tap(page, point, tab=False, tip=True)
            corner = page.evaluate("() => { const t = document.getElementById('tip'), r = t.getBoundingClientRect(); return t.hidden ? null : [r.left + 6, r.top + 6]; }")
            if corner:
                tap(page, corner, tab=False, tip=False)
            if not corner or page.evaluate(shown) or page.opened[mark:]:
                errors.append(f"a tap on a citation tooltip's text does not close it (tooltip was shown: {bool(corner)}, tabs opened: {len(page.opened[mark:])})")
            come_back(page, page.opened[mark:])
            page.evaluate("scrollBy(0, 200)")
            page.wait_for_timeout(400)  # a window: a closed tip must not come back on the scroll
            if page.evaluate(shown):
                errors.append("a closed citation tooltip comes back after a return to the tab or a scroll")
        # A mouse on a touch screen: after a tap, hovering with the mouse shows tips again.
        point = point_of(page, "#world-rows abbr.g")
        if point:
            tap(page, point, tab=False)
            page.touchscreen.tap(3, viewport["height"] // 2)
            box = page.locator("#world-rows abbr.g").first.bounding_box()
            page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2, steps=3)
            page.wait_for_timeout(300)
            if not page.evaluate(shown):
                errors.append("after a tap, hovering a term with a mouse shows no tooltip")
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
              return [n.classList.contains('scrolled'), m.scrollWidth - m.clientWidth, new Set(items.map(a => Math.round(a.getBoundingClientRect().top))).size, items.map(a => a.innerText.trim())]; }""")
            # A cut-off or missing item, or a bar not in its scrolled state, blocks; a wrap is a report line.
            if got[0] is not True or got[1] != 0 or len(got[3]) != len(SECTION_BAR) or not all(got[3]):
                errors.append(f"390px: the section bar is not scrolled, or cuts off or lacks one of the five items (scrolled, overflow, items: {got[0]}, {got[1]}, {got[3]})")
            if got[2] != 1:
                errors.append(report_line(f"390px: the section bar wraps onto {got[2]} rows"))
            page.evaluate("scrollTo(0, 0)")
        box = page.locator("#map-search")
        box.fill("ger")
        page.locator("#map-search-0").dispatch_event("mousedown")
        if not page.evaluate("document.getElementById('record').hidden"):
            errors.append(f"{width}px: pressing a search suggestion opens it before the click")
            box.fill("ger")
        page.locator("#map-search-0").click()
        got = page.evaluate("() => { const a = document.activeElement, r = a.getBoundingClientRect(), nav = document.querySelector('.secnav').getBoundingClientRect(); return [a.id, a.textContent, r.top >= nav.bottom - 1 && r.bottom <= innerHeight]; }")
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
    # Tooltips are short now (rule 12), so the probe carries a long text: this tests the scrolling mechanism, not the copy.
    phrase.evaluate("n => { const long = Array(12).fill(n.dataset.tip || n.title).join(' '); n.dataset.tip = long; if (n.title) n.title = long; n.scrollIntoView({ block: 'center', behavior: 'instant' }); }")
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


def check_arrival(browser, base_url: str) -> list[str]:
    """Principle Arrival: a card reached from the record beside the map, a repeated click or a shared link keeps the
    --highlight-fill tint with no outline while it is the target, and takes focus; a card reached from the record has one "Back to map" link, which returns to the first
    screen with the same region and record open, focus on the record's link.

    Records are opened from the page: the first Europe list row whose card is a full card (list click) and the first
    Europe dot whose card is a none-identified fold (map click). Every width and theme; light runs with motion, dark
    with reduced motion.
    """
    errors: list[str] = []
    state = r"""id => { const c = document.getElementById(id), s = getComputedStyle(c), nav = document.querySelector('.secnav').getBoundingClientRect(), r = c.getBoundingClientRect();
        const probe = document.createElement('span'); probe.style.backgroundColor = 'var(--highlight-fill)'; document.body.appendChild(probe); const fill = getComputedStyle(probe).backgroundColor; probe.remove();
        const alpha = (s.backgroundColor.match(/[\d.]+/g) || []).map(Number), seen = alpha.length === 4 ? alpha[3] > 0 : alpha.length === 3;
        return { hash: location.hash, marked: seen && s.outlineStyle === 'none', tinted: seen, tint: s.backgroundColor === fill, ring: s.outlineStyle !== 'none', arrived: c.classList.contains('arrived'),
          focused: document.activeElement === c, visible: r.top >= nav.bottom - 1 && r.top < innerHeight, open: c.tagName !== 'DETAILS' || c.open,
          named: !!c.getAttribute('aria-labelledby') && c.getAttribute('aria-labelledby').split(' ').every(n => document.getElementById(n)), animation: s.animationName }; }"""
    for width in (1440, 1000, 390):
        for theme in ("light", "dark"):
            motion = "no-preference" if theme == "light" else "reduce"
            label = f"arrival {width}px {theme} motion={motion}"
            page = themed_page(browser, theme, viewport={"width": width, "height": 667 if width == 390 else 1000}, reduced_motion=motion)  # a short phone: the record under the map is below the fold
            try:
                hydrated(page, base_url, map_drawn=True)
                page.locator('#region-chips button[data-region="Europe"]').click()  # Europe's map has none-identified dots
                map_ready(page)
                page.evaluate("scrollTo(0, 0)")
                rows = page.locator("#region-rows-europe .pick").evaluate_all(
                    "bs => bs.map(b => [b.dataset.pick, document.getElementById('c-' + b.dataset.pick.toLowerCase())?.tagName])"
                )
                dots = page.evaluate(
                    "() => [...[...document.querySelectorAll('#worldmap svg')].find(n => n.getClientRects().length).querySelectorAll('.tgt')].map(t => [t.dataset.j, document.getElementById('c-' + t.dataset.j.toLowerCase())?.tagName])"
                )
                full_id = next((j for j, tag in rows if tag == "ARTICLE"), None)
                mini_id = next((j for j, tag in dots if tag == "DETAILS"), None)
                if not full_id or not mini_id:
                    errors.append(f"{label}: no list row leads to a full card or no dot to a none-identified fold")
                    page.close()
                    continue
                full, mini = f"c-{full_id.lower()}", f"c-{mini_id.lower()}"

                def open_record(jid, by):
                    page.evaluate("scrollTo(0, 0)")
                    if by == "list":
                        if page.locator("#record").is_visible():  # on wide screens an open record covers the list
                            page.locator("#record .record-close").click()
                        page.locator(f'#region-rows-europe .pick[data-pick="{jid}"]').click()
                    else:
                        if page.locator("#headline").get_attribute("data-region") != "Europe":  # after a reload the page opens on World, without none-identified dots
                            if page.locator("#record").is_visible():
                                page.locator("#record .record-close").click()
                            page.locator('#region-chips button[data-region="Europe"]').click()
                        page.evaluate("j => [...document.querySelectorAll('#worldmap svg')].find(n => n.getClientRects().length).querySelector('.tgt[data-j=\"' + j + '\"]').dispatchEvent(new MouseEvent('click', { bubbles: true }))", jid)
                    if page.locator("#record").get_attribute("data-j") != jid or not page.locator("#record").is_visible():
                        errors.append(f"{label}: a {by} click on {jid} does not open its record beside the map")
                    page.locator("#record .record-more").click()

                def expect(card_id, step, *, motion=motion):
                    page.wait_for_timeout(150)
                    got = page.evaluate(state, card_id)
                    if got["ring"]:
                        errors.append(report_line(f"{label}: {step}: #{card_id} arrives with an outline; arrival is the tint alone"))
                    for key, message in (("marked", "is not visibly tinted"), ("arrived", "is not marked as arrived"), ("focused", "does not take focus"),
                                         ("visible", "is not in view below the nav"), ("open", "fold is not open"), ("named", "has no accessible name")):
                        if not got[key]:
                            errors.append(f"{label}: {step}: #{card_id} {message}")
                    if got["hash"] != "#" + card_id:
                        errors.append(f"{label}: {step}: hash is {got['hash']!r}, not #{card_id}")
                    if motion == "reduce" and got["animation"] != "none":
                        errors.append(f"{label}: {step}: #{card_id} animates with reduced motion")

                open_record(full_id, "list")
                expect(full, "record link")
                if page.locator(f"#{full} .back-map").count() != 1 or page.locator(".back-map").count() != 1:
                    errors.append(f"{label}: #{full} reached from the record lacks its one Back to map link")
                page.keyboard.press("Escape")  # reading the card: Escape must not close the record waiting above
                if page.locator("#record").get_attribute("data-j") != full_id or page.evaluate("document.getElementById('record').hidden"):
                    errors.append(f"{label}: Escape on the card closes the record that Back to map returns to")
                    continue  # the rest of this run needs the record
                if motion == "no-preference":
                    page.wait_for_timeout(1700)  # past the 1.5 s fade: the outline lasts
                    if not page.evaluate(state, full)["tint"]:
                        errors.append(report_line(f"{label}: #{full} loses its arrival tint when the flash ends"))
                page.evaluate("() => { document.activeElement.blur(); scrollTo(0, 0); }")
                page.wait_for_timeout(100)
                page.locator("#record .record-more").click()
                expect(full, "same record link again")
                if page.locator(f"#{full} .back-map").count():
                    page.locator(f"#{full} .back-map").click()
                    page.wait_for_timeout(150)
                    back = page.evaluate("""id => { const s = document.querySelector('section.first').getBoundingClientRect(), nav = document.querySelector('.secnav').getBoundingClientRect(), record = document.querySelector('#record'), more = record.querySelector('.record-more');
                      const m = more.getBoundingClientRect();
                      return [location.hash, s.bottom > nav.bottom && s.top < innerHeight && m.top >= nav.bottom - 1 && m.bottom <= innerHeight + 1, !record.hidden && record.dataset.j, document.activeElement === more, document.getElementById(id).classList.contains('arrived'), document.querySelectorAll('.back-map').length]; }""", full)
                    if back != ["#map", True, full_id, True, False, 0]:
                        errors.append(f"{label}: Back to map does not return to the first screen with the record open and focused, arrival ended ({back})")
                    # The browser's Back returns to the card as it was reached: from the record, so with its link.
                    page.go_back()
                    expect(full, "browser Back to the card")
                    if page.locator(f"#{full} .back-map").count() != 1:
                        errors.append(f"{label}: browser Back to #{full} after Back to map loses its Back to map link")
                    # After a reload the history entry still knows the origin: Back to map opens that record again.
                    page.reload()
                    wait_hydrated(page)
                    page.evaluate("document.fonts.ready")
                    page.wait_for_timeout(150)
                    if page.locator(f"#{full} .back-map").count() != 1:
                        errors.append(f"{label}: #{full} loses its Back to map link on reload")
                    else:
                        page.locator(f"#{full} .back-map").click()
                        page.wait_for_timeout(150)
                        got = page.evaluate("() => { const r = document.getElementById('record'); return [location.hash, !r.hidden && r.dataset.j, document.activeElement === r.querySelector('.record-more')]; }")
                        if got != ["#map", full_id, True]:
                            errors.append(f"{label}: Back to map after a reload does not reopen the record with focus on its link ({got})")
                open_record(mini_id, "map")
                expect(mini, "none-identified record link")
                if page.evaluate("id => document.getElementById(id).matches('.arrived, :target')", full) or page.evaluate(state, full)["tinted"] or page.evaluate(state, full)["ring"]:
                    errors.append(report_line(f"{label}: #{full} keeps its arrival tint after another card is the target"))
                # Section navigation changes the address without a hashchange; the arrival ends there too.
                page.locator('.secnav a[href="#europe"]').click()
                page.wait_for_timeout(100)
                if page.evaluate(state, mini)["tinted"] or page.evaluate(state, mini)["ring"]:
                    errors.append(report_line(f"{label}: #{mini} keeps its arrival tint after section navigation"))
                # A shared link: the saved page is swapped for a fresh copy; the outline must survive the swap.
                hydrated(page, base_url + "#" + full, map_drawn=True)  # Europe's dots are clicked after the reload
                page.wait_for_timeout(150)
                got = page.evaluate(state, full)
                if not (got["marked"] and got["arrived"]):
                    errors.append(report_line(f"{label}: shared link #{full} has no arrival tint after the page script takes over"))
                if not got["visible"]:
                    errors.append(f"{label}: shared link #{full} does not arrive below the nav")
                # The same link percent-encoded names the same card.
                encoded = "c-" + "".join(f"%{ord(ch):02X}" for ch in full_id.lower()[:1]) + full_id.lower()[1:]
                hydrated(page, base_url + "#" + encoded, map_drawn=True)  # Europe's dots are clicked after the reload
                page.wait_for_timeout(150)
                got = page.evaluate(state, full)
                if not (got["marked"] and got["arrived"] and got["focused"] and got["open"]):
                    errors.append(f"{label}: shared link #{encoded} does not arrive at #{full} ({got})")
                if width == 1440:
                    # Fonts that load after the reader followed the record's link: the card keeps its Back to map link.
                    late = themed_page(browser, theme, viewport={"width": width, "height": 1000}, reduced_motion=motion)
                    try:
                        held = Held(late, "**/assets/fonts/**")
                        late.goto(base_url, wait_until="commit")  # held fonts hold the load event
                        wait_hydrated(late)
                        late.locator(f'#region-rows-europe .pick[data-pick="{full_id}"]').click()
                        late.locator("#record .record-more").click()
                        late.wait_for_timeout(150)
                        pending = late.evaluate("document.fonts.status")
                        held.release()
                        late.evaluate("document.fonts.ready")
                        late.wait_for_timeout(150)
                        if pending != "loading" or late.locator(f"#{full} .back-map").count() != 1 or not late.evaluate("id => document.getElementById(id).classList.contains('arrived')", full):
                            errors.append(f"{label}: #{full} reached from the record before the fonts loaded (fonts {pending}) loses its arrival or Back to map link when they load")
                    finally:
                        late.close()
                if width == 1440:
                    # Slow data: a control the reader focused on the saved page keeps focus after the swap, also one without an id.
                    slow = themed_page(browser, theme, viewport={"width": width, "height": 1000}, reduced_motion=motion)
                    try:
                        data = Held(slow, PAGE_DATA)
                        slow.goto(base_url + "#" + full)
                        data.wait()
                        slow.evaluate("document.fonts.ready")
                        slow.focus("#changes > summary")
                        if not slow.evaluate("document.documentElement.hasAttribute('data-prerendered')"):
                            errors.append(f"{label}: the page script took over while its data was held")
                        data.release()
                        wait_hydrated(slow)
                        slow.wait_for_timeout(200)
                        if slow.evaluate("getComputedStyle(document.body).backgroundColor") != GROUNDS[theme]:
                            errors.append(f"{label}: the slow-data page does not show the {theme} theme")
                        if not slow.evaluate("document.activeElement === document.querySelector('#changes > summary')"):
                            errors.append(f"{label}: shared link #{full} takes focus from a control the reader focused before the page script took over")
                    finally:
                        slow.close()
            finally:
                page.close()
    return errors


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
    screen): edge against the ground (a dot's stroke at its drawn width), text against what is behind it, and every
    fill colour, including gradient stops and the hatch."""
    return page.evaluate(
        "statuses => {" + CONTRAST_JS + """
      const ground = token('--ground'), out = [];
      const behind = node => { for (let n = node; n; n = n.parentElement) { const s = getComputedStyle(n); if (opaque(s.backgroundColor)) return { colour: s.backgroundColor, pattern: false }; if (s.backgroundImage !== 'none') return { colour: null, pattern: true }; } return { colour: ground, pattern: false }; };
      const fills = n => { const s = getComputedStyle(n); return [s.backgroundColor, ...(s.backgroundImage.match(/rgba?\\([^)]*\\)/g) || [])].filter(opaque); };
      const svg = [...document.querySelectorAll('#worldmap svg')].find(n => n.getClientRects().length);
      const scale = svg ? svg.getBoundingClientRect().width / svg.viewBox.baseVal.width : 1;
      for (const status of statuses) for (const [kind, node] of [['swatch', document.querySelector('#legend .sw.st-' + status)], ['dot', svg && svg.querySelector(':scope > g > .dot.st-' + status)]]) {
        if (!node) { out.push({ status, kind, missing: true }); continue; }
        if (kind === 'dot') {
          const s = getComputedStyle(node), hatch = s.fill.startsWith('url') && svg.querySelector('.hatch-line'), centre = node.nextElementSibling && node.nextElementSibling.matches('.dot-centre') ? node.nextElementSibling : null;
          const fillsOf = [hatch ? getComputedStyle(hatch).fill : s.fill, centre && getComputedStyle(centre).fill].filter(c => c && opaque(c));
          out.push({ status, kind, edge: ratio(s.stroke, ground), edgeWidth: parseFloat(s.strokeWidth) * scale, image: hatch ? 'hatch' : 'none', text: [],
            fills: fillsOf.filter(c => rgb(c).join() !== rgb(ground).join()).map(c => [c, ratio(c, '#000')]), centre: centre ? ratio(getComputedStyle(centre).fill, ground) : 0, centreRound: !!centre && centre.tagName.toLowerCase() === 'circle' });
          continue;
        }
        const s = getComputedStyle(node), shadow = s.boxShadow.match(/^(rgba?\\([^)]*\\)) 0px 0px 0px ([\\d.]+)px inset$/);
        const m = { status, kind, edge: ratio(s.borderTopColor, ground), edgeWidth: parseFloat(s.borderTopWidth), image: s.backgroundImage, ring: !!shadow && rgb(shadow[1]).join() === rgb(ground).join() && parseFloat(shadow[2]) >= 1,
          fills: [...fills(node), ...[...node.children].flatMap(fills)].filter(c => rgb(c).join() !== rgb(ground).join()).map(c => [c, ratio(c, '#000')]), text: [] };
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


VALUE_TIP_CLASSES = ("conf", "interval", "per-person", "developer", "provision")  # values with a tooltip, held here independently of the page


def check_tooltip_cues(page) -> list[str]:
    """Principle Tooltip cues (owner, 5 Nov 2026): the dotted underline marks abbreviations and terms only; a value with a
    tooltip keeps its tooltip and focus without it. Developer names, provisions and a median ceiling in the figures row are values too."""
    got = page.evaluate("""cls => [...document.querySelectorAll('abbr.g')].filter(n => n.getClientRects().length).map(n => {
        const value = cls.some(c => n.classList.contains(c)) || !!n.closest('#figures .figure > b');
        const c = getComputedStyle(n);
        return [n.textContent.trim().slice(0, 40), value, c.textDecorationLine, c.textDecorationStyle, n.tabIndex === 0 && !!n.dataset.exp]; })""", list(VALUE_TIP_CLASSES))
    errors = []
    for text, value, line, style, tip in got:
        if not tip:
            errors.append(f"tooltip cues: {text!r} has no focusable tooltip")
        if value and line != "none":
            errors.append(report_line(f"tooltip cues: the value {text!r} is underlined; values keep their tooltip without the dotted mark"))
        if not value and (line != "underline" or style != "dotted"):
            errors.append(report_line(f"tooltip cues: the term {text!r} lacks its dotted underline"))
    if not any(value for _, value, *_ in got):
        errors.append(report_line("tooltip cues: no value with a tooltip to check"))
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
    included. Measured on the rendered colours, so a token that drifts fails even where other checks only compare an
    element with the token. Skipped: text where a background image or gradient shows through (no single background;
    imageShows), screen-reader-only text clipped to nothing, svg text, and genuinely disabled controls and their labels
    (WCAG exempts inactive components). In both themes: the colours are the theme's."""
    found = page.evaluate("limits => {" + CONTRAST_JS + PAINT_JS + """
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
        for (let n = node; n; n = n.parentElement) { const t = getComputedStyle(n); layers.push([rgba(t.backgroundColor), parseFloat(t.opacity), t.backgroundImage !== 'none']); }
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
        for colour, black in m["fills"]:
            if black < 2:
                errors.append(report_line(f"status contrast: {where} has a near-black fill {colour} ({black:.2f}:1 against black)"))
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
            # The record's heading takes focus (as for list and search picks); the dot says it controls the open record; a tooltip, if still shown, names the state.
            aside = page.evaluate("j => { const a = document.activeElement, t = document.getElementById('tip'), d = [...document.querySelectorAll('#worldmap svg')].find(n => n.getClientRects().length).querySelector('.tgt[data-j=\"' + j + '\"]'); return [a && a.id === 'record-name' ? null : (a && (a.dataset.j || a.id || a.tagName)), t && !t.hidden ? t.firstChild.textContent : null, d.getAttribute('aria-controls') === 'record' && d.getAttribute('aria-expanded') === 'true']; }", jid)
            name = next(j["name"] for j in data["jurisdictions"] if j["id"] == jid)
            # Opening the record can scroll another tooltip trigger under the resting pointer (a region list short name); its own tip is not the dot's.
            under = page.evaluate("([x, y]) => { const n = document.elementFromPoint(x, y), o = n && n.closest('abbr.g, [data-tip]'); return o && !o.closest('#worldmap') ? (o.dataset.exp || o.dataset.tip) : null; }", [x, y])
            if under is not None and aside[1] == under:
                aside[1] = None
            if got != jid:
                wrong.append(f"{jid}->{got}")
            elif aside[0] or aside[1] not in (name, None) or not aside[2]:
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
        if got[:2] != [fr["id"], "record-name"] or got[2] not in (fr["name"], None):
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
    if page.locator("#record-name").text_content() != eu["name"] or page.evaluate("document.activeElement && document.activeElement.id") != "record-name":
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
    if page.locator("#record-name").text_content() != "Germany" or page.locator("#map-search-list").is_visible() or box.get_attribute("aria-expanded") != "false":
        errors.append("search: Enter does not open the active option's record and close the list")
    if page.locator("#record-name").text_content() != "Germany":
        # The record steps below assume Germany's record; with another one open they would fail on something else
        # (a citation that is not there) and hide the search errors above, so the search errors are the result.
        return errors + [f"search: 'ger' then Enter opened {page.locator('#record-name').text_content()!r}, not Germany; later first-screen steps skipped"]
    de = next(j for j in data["jurisdictions"] if j["name"] == "Germany")
    link = page.locator("#record .record-more")
    if not (link.text_content() or "").strip() or link.get_attribute("href") != f"#c-{de['id'].lower()}":
        errors.append(f"record: Germany's link down reads {link.text_content()!r} to {link.get_attribute('href')!r}")
    # A citation's tooltip in the record takes the first Escape; the second closes the record.
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



def theme_state_matches(state: list, scheme: str, active: str, mode: str) -> bool:
    """The applied scheme and paint are the active theme's, and the toggle's name says the chosen mode (system, light
    or dark) in any wording: the state, not the words, is checked."""
    return state[:2] == [scheme, GROUNDS[active]] and re.search(rf"\b{mode}\b", state[2] or "", re.I) is not None


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

    def bare_toggle(page, theme):
        """The toggle is a bare icon button: no visible border, fill or pressed accent line, in either state."""
        look = page.evaluate("""() => { const s = getComputedStyle(document.getElementById('theme-toggle'));
          return [s.borderTopColor, s.backgroundColor, s.boxShadow]; }""")
        if look[0] != "rgba(0, 0, 0, 0)" or look[1] != "rgba(0, 0, 0, 0)" or look[2] != "none":
            errors.append(report_line(f"theme: in {theme} the toggle is not a bare icon button (border, fill, shadow: {look})"))

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
        bare_toggle(page, "system dark")
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
            bare_toggle(page, name)
        if len(set(names)) != 3 or any(sum(bool(re.search(rf"\b{m}\b", n or "", re.I)) for m in ("light", "dark", "system")) != 1 for n in names):
            errors.append(f"theme: the toggle's names do not each say one mode ({names})")
        # Dark, then light (against the dark system), are each kept after a reload.
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
        if not re.search(r"\blight\b", page.evaluate(label) or "", re.I):  # the mode as a whole word, as theme_state_matches
            errors.append("theme: the saved page's toggle does not show the stored light theme")
        page.locator("#theme-toggle").click(timeout=5_000)
        held.release()
        wait_hydrated(page)
        if not theme_state_matches(state(page), "dark", "dark", "dark") or page.evaluate(stored) != "dark":
            errors.append(f"theme: a toggle before the script takes over is lost ({state(page)})")
        # The other pages follow the choice.
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


def check_menu_touch(browser, base_url: str) -> list[str]:
    """Touch taps reach popover labels: the menu stays open and the option toggles."""
    errors = []
    context = browser.new_context(
        viewport={"width": 390, "height": 900}, has_touch=True, is_mobile=True
    )
    page = context.new_page()
    hydrated(page, base_url)
    page.wait_for_selector("#col-opts label", state="attached")
    page.locator("details.cols summary").tap()
    label = page.locator("#col-opts label").nth(1)
    before = label.locator("input").is_checked()
    label.tap()
    page.wait_for_timeout(200)
    if not page.evaluate("document.querySelector('details.cols').open"):
        errors.append("touch: tapping a Columns label closes the menu")
    if label.locator("input").is_checked() == before:
        errors.append("touch: tapping a Columns label does not toggle it")
    context.close()
    return errors


def check_link_states(page) -> list[str]:
    """Exercise hover and focus instead of trusting the rest-state declarations."""
    errors = []
    for selector, decoration in (
        ('.secnav a[href="#map"]', "none"),
        (".cn", "underline"),
        ("a.standalone", "underline"),
        ("#regions .pick .pname", "underline"),
        ("#corr a", "underline"),
    ):
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
        for state in ("hover", "focus"):
            if state == "hover" and selector == ".cn":
                box = link.bounding_box()
                page.mouse.move(
                    box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
                )
            elif state == "hover":
                link.hover()
            else:
                page.mouse.move(0, 0)
                page.keyboard.press("Shift")
                (link.locator("xpath=..") if selector.endswith(".pname") else link).focus()
            want = "underline" if state == "focus" and selector.startswith(".secnav") else decoration  # Focus: keyboard focus underlines nav links too (tint plus underline)
            if (
                link.evaluate("n => getComputedStyle(n).textDecorationLine")
                != want
            ):
                errors.append(f"{selector} has wrong {state} decoration" if state == "focus" else report_line(f"{selector} has wrong {state} decoration"))  # focus must show: blocking
            if selector.startswith(".secnav") and not link.evaluate(
                "n => { const p=document.createElement('span'); p.style.color='var(--ink)'; document.body.appendChild(p); const ok=getComputedStyle(n).color===getComputedStyle(p).color; p.remove(); return ok; }"
            ):
                errors.append(f"nav does not darken on {state}" if state == "focus" else report_line(f"nav does not darken on {state}"))
            if selector == "#corr a" and not link.evaluate(
                "n => getComputedStyle(n).textDecorationColor === getComputedStyle(n).color"
            ):
                errors.append(f"inline link underline does not strengthen on {state}" if state == "focus" else report_line(f"inline link underline does not strengthen on {state}"))
            if selector == ".cn":
                errors.extend(check_citations(page))
        link.evaluate("n => n.blur()")
    page.mouse.move(0, 0)
    page.evaluate("scrollTo(0, 0)")
    return errors


def check_wrapped_rows(page) -> list[str]:
    """Force multi-line text in existing row types, then restore the DOM."""
    page.evaluate("""() => {
      window.rowFixtures = [];
      const targets = '.meta > span:first-child, .card .top .name, .fund-identity h3, .dlist .dl-title, .dlist .d, .card.mini > summary .find, .card:not(.mini) .row .v, .legend-definition, .fund-staff .fund-value, #world-rows td[data-col="inst"], .development-text';
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
    """One rule in cards, table and funding: the value leads on its own line; qualifiers sit on one small
    muted line beneath; no qualifier (confidence, as-of date, scope) is written inline after the value."""
    return page.evaluate("() => {" + REPORT_JS + r"""
      // Folded records (funding disclosures) count too: open them, measure, restore.
      const folds = [...document.querySelectorAll('details:not([open])')]; folds.forEach(d => d.open = true);
      const errors = [], probe = document.createElement('span');
      let tracked = 0;  // card and table fields that carry qualifiers: none found means the check saw nothing
      probe.style.cssText = 'font: var(--type-meta); color: var(--muted)'; document.body.appendChild(probe);
      const meta = getComputedStyle(probe), want = { fontSize: meta.fontSize, color: meta.color }; probe.remove();
      const fields = [...document.querySelectorAll('.card .row .v, #world-rows td, .fund-value, .funding-native, .fund-context, p[data-record]')].filter(n => n.getClientRects().length);
      const name = n => (n.closest('[data-j]') || {}).dataset?.j + ' ' + (n.closest('.row')?.querySelector('.k')?.textContent || n.closest('td')?.dataset.col || n.className);
      for (const field of fields) {
        for (const q of field.querySelectorAll('.qualifiers')) {
          if (!q.getClientRects().length) continue;
          const s = getComputedStyle(q);
          if (s.display !== 'block' || s.fontSize !== want.fontSize || s.color !== want.color) errors.push(REPORT + `qualifiers are not a small muted block: ${name(field)}`);
          // The qualifier line starts below the value's first line.
          const walker = document.createTreeWalker(field, NodeFilter.SHOW_TEXT);
          let text = null; while (walker.nextNode()) if (walker.currentNode.nodeValue.trim() && !walker.currentNode.parentElement.closest('.qualifiers, .vh')) { text = walker.currentNode; break; }
          const range = document.createRange(); if (text) range.selectNodeContents(text);
          const first = text && [...range.getClientRects()].find(r => r.width > 0);
          if (!first || q.getBoundingClientRect().top < first.bottom - 1) errors.push(REPORT + `qualifiers share the value's line: ${name(field)}`);
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
      return errors.filter(e => !e.startsWith(REPORT)).concat(errors.filter(e => e.startsWith(REPORT)).slice(0, 10));  // every blocking finding, at most 10 report lines
    }""")


@reports
def check_value_column(page) -> list[str]:
    """Within a card, every field's value starts at the same x (first glyph or swatch of each value)."""
    return page.evaluate("""() => {
      const errors = [];
      const start = v => {
        const walker = document.createTreeWalker(v, NodeFilter.SHOW_TEXT | NodeFilter.SHOW_ELEMENT);
        while (walker.nextNode()) {
          const n = walker.currentNode;
          if (n.nodeType === 1 && n.matches('.sw') && n.getClientRects().length) return n.getBoundingClientRect().left;
          if (n.nodeType === 3 && n.nodeValue.trim() && !n.parentElement.closest('.vh')) {
            const range = document.createRange(); range.selectNodeContents(n);
            const rect = [...range.getClientRects()].find(r => r.width > 0);
            if (rect) return rect.left;
          }
        }
        return null;
      };
      document.querySelectorAll('.card:not(.mini)').forEach(card => {
        const xs = [...card.querySelectorAll(':scope > .row > .v')].map(v => [v.parentElement.querySelector('.k').textContent, start(v)]).filter(([, x]) => x != null);
        if (xs.length < 2) return;
        const min = Math.min(...xs.map(([, x]) => x));
        const off = xs.filter(([, x]) => x - min > .5);
        if (off.length) errors.push(`card ${card.dataset.j}: values do not start at one x (${off.map(([k, x]) => k + ' +' + (x - min).toFixed(1) + 'px').join(', ')})`);
      });
      return errors;
    }""")


# Repeated components: every instance shares its spacing, whichever section it sits in. Inline components
# (marks, chip strips) compare only their inner spacing; their outer margin belongs to the context that places them.
INNER_SPACING = ("paddingTop", "paddingRight", "paddingBottom", "paddingLeft", "rowGap", "columnGap")
SPACING_PROPERTIES = ("marginTop", "marginRight", "marginBottom", "marginLeft") + INNER_SPACING
REPEATED_COMPONENTS = tuple(
    (selector, SPACING_PROPERTIES)
    for selector in (
        ".statusgroup", ".cards:not(.minis)", ".card-row", ".card:not(.mini) > .top", ".card:not(.mini) > .row", ".card .foot",
        ".card.mini", ".card.mini > summary", ".more:not(.quiet) > summary", ".more.quiet > summary", ".list-more",
        "summary.source-footer-line", ".source-count", ".fund-item", ".development-row", ".dlist a", ".legend-entry",
        ".region-chips button", ".qualified", ".qualifiers", ".activity-kinds",
    )
) + ((".smark", ("rowGap", "columnGap")), (".region-chips", ("rowGap", "columnGap")))


@reports
def check_repeated_spacing(page) -> list[str]:
    """Repeated components keep the same spacing across sections: identical spacing properties (including a
    disclosure chevron's offset) for every instance, and the same measured gaps between a status group's heading
    and its cards and between card rows. Hidden instances (earlier developments, closed folds) are not compared."""
    return page.evaluate(
        """components => {
      const errors = [], section = n => n.closest('section[aria-labelledby], header, footer')?.getAttribute('aria-labelledby') || n.closest('header, footer')?.tagName.toLowerCase() || 'page';
      for (const [selector, props] of components) {
        const seen = new Map();
        for (const n of document.querySelectorAll(selector)) {
          if (!n.getClientRects().length) continue;
          const s = getComputedStyle(n), after = getComputedStyle(n, '::after');
          const key = props.map(p => s[p]).join(' ') + (after.content !== 'none' ? ' | after ' + after.marginLeft + ' ' + after.marginRight : '');
          if (!seen.has(key)) seen.set(key, section(n) + ' ' + (n.id || n.closest('[data-j]')?.dataset.j || ''));
        }
        if (seen.size > 1) errors.push(`${selector}: spacing differs between instances (${[...seen.values()].join(' vs ')})`);
      }
      const gaps = (label, pairs) => {
        const values = pairs.map(([a, b, where]) => [Math.round((b.getBoundingClientRect().top - a.getBoundingClientRect().bottom) * 10) / 10, where]);
        if (values.length && Math.max(...values.map(([v]) => v)) - Math.min(...values.map(([v]) => v)) > .5) errors.push(`${label} differs across sections: ${values.map(([v, w]) => w + ' ' + v + 'px').join(', ')}`);
      };
      const groups = [...document.querySelectorAll('.statusgroup')].filter(g => g.getClientRects().length);
      gaps('status group heading-to-cards gap', groups.map(g => [g.querySelector(':scope > h3'), g.querySelector(':scope > .cards'), section(g) + '/' + g.dataset.status]));
      const rows = [...document.querySelectorAll('.cards:not(.minis)')].flatMap(grid => [...grid.children].slice(1).map(r => [r.previousElementSibling, r, section(grid) + '/' + grid.closest('.statusgroup').dataset.status]));
      gaps('card row gap', rows);
      return errors;
    }""",
        [[selector, list(props)] for selector, props in REPEATED_COMPONENTS],
    )


# Kept apart from method_checks.GENERIC_SHORT_FORMS on purpose: the page gate is an independent oracle.
GENERIC_ACRONYMS = {"AISI", "AISIS", "ASI"}


def expected_display_names(data: dict) -> dict[str, str]:
    """Hand-worked display name per institute: the name without a status suffix, plus the country where two share it."""
    base = {i["id"]: re.sub(r" \((planned|proposed)\)$", "", i["name"]) for i in data["institutes"]}
    uses = Counter(name.casefold() for name in base.values())
    country = {j["id"]: j["name"] for j in data["jurisdictions"]}
    return {
        i["id"]: base[i["id"]] + (f" ({country[i['jurisdiction_id']]})" if uses[base[i["id"]].casefold()] > 1 else "")
        for i in data["institutes"]
    }


def check_institute_names(html, data: dict) -> list[str]:
    """One display name per body, one recorded short form for tight places, never a bare generic acronym.

    Same-named bodies carry their country, tight places (table cells) show the recorded short form, the short
    form names the full display name on hover, and cards and funding rows show the full display name.
    """
    soup = BeautifulSoup(html, "html.parser")
    errors = []
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
        elif mark is not None and mark.get("data-exp") != hover:
            errors.append(f"{jid} short form {shorts[iid]!r} does not give {hover!r} on hover")
        # The region list is a tight place too: its short form, cut off where long, gives the same name on hover or focus.
        pick = soup.select_one(f'#regions .region-rows .pick[data-pick="{jid}"]')
        short = pick.parent.select_one(":scope > .small") if pick else None
        if pick is None or short is None:
            errors.append(f"{jid} region list row or its short form {shorts[iid]!r} is missing")
        else:
            mark = short.select_one("abbr.g")
            if short.get_text(strip=True) != shorts[iid] or (mark is None) != (hover is None) or (mark is not None and (mark.get("data-exp") != hover or mark.get("tabindex") != "0")):
                errors.append(f"{jid} region list short form {shorts[iid]!r} does not give {hover!r} on hover and focus")
    for node in soup.select("td[data-col=inst] abbr"):
        if re.sub(r"[^A-Za-z]", "", node.get_text()).upper() in GENERIC_ACRONYMS:
            # dataset.method: none requires an acronym ban.
            errors.append(report_line(f"bare generic acronym {' '.join(node.get_text().split())!r} used as an institute name"))
    # Funding rows are not a tight place: the institute shows its full display name.
    fund_rows = soup.select("#fund-rows .fund-item")
    if not fund_rows:
        errors.append("no funding rows found to check institute names in")
    for row in fund_rows:
        jid = str(row.get("data-j"))
        ids = by_state.get(jid, [])
        name = row.select_one(".fund-identity > span:not(.smark)")
        if len(ids) != 1:
            errors.append(f"{jid} funding row: the institute name rule needs a decision")
        elif name is None or " ".join("".join(t for t in name.find_all(string=True) if not t.find_parent(class_="vh")).split()) != names[ids[0]]:
            shown = " ".join(name.get_text().split()) if name is not None else None
            errors.append(f"{jid} funding row names its institute {shown!r}, expected {names[ids[0]]!r}")
    return errors


LEADERSHIP_ROWS = ("Head", "Reports to", "Partners")  # card labels, in card order, directly after Form
PARTNER_RELATIONS = ("member", "works_with", "planned")  # card order after the cyber agency
CARD_PARTNERS = 6  # the card shows at most this many; the oracle's own copy of the cap
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
                # Cyber agency first, then member, works with, planned; authored order breaks ties (the sort is stable).
                entries = sorted(
                    enumerate(partners["entries"]),
                    key=lambda pair: (not pair[1].get("cyber_agency"), PARTNER_RELATIONS.index(pair[1]["relation"])),
                )
                shown = entries[:CARD_PARTNERS]
                texts = [
                    f"{e.get('acronym') or e.get('name_en') or e['name']} ({e['what']}, {vocabularies['partner_relation'][e['relation']]['label']})"
                    for _, e in shown
                ]
                hows = [
                    (e.get("acronym") or e.get("name_en") or e["name"], e["how"],
                     row_sources(data, n, [f"national_partners/entries/{k}/{field}" for field in ("name", "how")]))
                    for k, e in entries
                ]
                cites = row_sources(data, n, [f"national_partners/entries/{k}/{field}" for k, _ in shown for field in ("name", "relation", "how", "cyber_agency", "what")])
                if not any(e.get("cyber_agency") for e in partners["entries"]):
                    texts.append(vocabularies["partner_cyber_none_published"])  # the tail is worded in the vocabulary only
                rows["Partners"] = ("", texts, cites, hows)
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
                if amount.get("data-tip") != NO_RATE_TIP or amount.get("tabindex") != "0":
                    errors.append(f"{identity}: {currency} amount must say on hover and focus why there is no euro figure")
    return errors


def check_euro_amounts(html, data: dict) -> list[str]:
    """Every shown amount in a currency with an ECB rate is also in euros, "~EUR 52m (JPY 8.8bn)", and its value cites
    the rate's source beside the amount's own (rule currency-rounding); a compared figure keeps its annual label."""
    soup = BeautifulSoup(html, "html.parser")
    errors = []
    for institute in data["institutes"]:
        compared = institute.get("display", {}).get("comparison_funding")
        for record in institute.get("funding", []) or []:
            identity, wanted_sources = record["id"], rate_sources(record, data)
            if not wanted_sources:
                continue
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
                # Related funding whose source wording states the native amount: the euro figure alone, without its bracket (rule 8).
                want = re.sub(r" \([^()]*\)$", "", expected) if "related-funding-eur" in figure.get("class", []) and isinstance(after, str) and after.startswith(": ") and native.search(after) else expected
                if figure.get_text(strip=True) != want:
                    errors.append(f"{identity}: amount shown as {figure.get_text(strip=True)!r}, expected {want!r}")
                value = figure.find_parent(lambda n: n.name == "td" or bool(set(n.get("class", [])) & {"v", "fund-value", "funding-native", "related-funding-item"}))
                cited = {a.get("data-source-id") for a in value.select(".cn")} if value else set()
                if not set(wanted_sources) <= cited:
                    errors.append(f"{identity}: euro figure {expected!r} does not cite the amount and exchange-rate sources")
    return errors


def check_leadership_rows(html, data: dict) -> list[str]:
    """Head, Reports to and Partners: present with the data, in order after Form, with the stated words, order and citations.

    The rows are absent for an institute without the fields (the page does not change before the data lands) and present on
    every card once any institute has them. Texts, partner order, labels and the muted tail are worked out here from the data
    and its vocabularies, not read from the page's own selection.
    """
    soup = BeautifulSoup(html, "html.parser")
    expected = expected_leadership(data)
    errors = []
    any_rows = any(rows for rows in expected.values())
    by_id = Keyed(soup, "[id]", "id")
    for institute in data["institutes"]:
        jid = institute["jurisdiction_id"]
        card = by_id.one(f"c-{jid.lower()}")
        if card is None:
            continue  # an institute without a card is the card checks' concern
        rows = expected[institute["id"]]
        if any_rows and set(rows) != set(LEADERSHIP_ROWS):
            errors.append(f"{institute['id']}: data lacks {sorted(set(LEADERSHIP_ROWS) - set(rows))}; every institute needs all three rows once any has them")
        tracks = [r.get("data-track") for r in card.select(":scope > .row")]
        shown = [track for track in tracks if track in {CARD_TRACKS[label] for label in LEADERSHIP_ROWS}]
        want = [label for label in LEADERSHIP_ROWS if label in rows]
        wanted_tracks = [CARD_TRACKS[label] for label in want]
        if shown != wanted_tracks:
            errors.append(f"{jid}: card rows {shown} differ from the data's {wanted_tracks}")
            continue
        if want and ("4" not in tracks or tracks[tracks.index("4") + 1 : tracks.index("4") + 1 + len(want)] != wanted_tracks):
            errors.append(report_line(f"{jid}: Head, Reports to and Partners must follow Form directly, in that order: {tracks}"))
        for label in want:
            text, items, cites, hows = rows[label]
            row = card.select_one(f':scope > .row[data-track="{CARD_TRACKS[label]}"]')
            heading = row.select_one(".k")
            if not heading or not heading.get_text(strip=True):
                errors.append(f"{jid}: {label} row label is missing or empty")
            value = row.select_one(".v")
            if label == "Partners" and not text:
                # The page keeps a citation with its last word by cloning the enclosing spans, so compare the line's text, not its elements.
                clone = copy.copy(value)
                for mark in clone.select(".cites, .meta-dot, details.partner-how"):
                    mark.decompose()
                got = " ".join(clone.get_text().split())  # the line only: a how sentence on it would differ from the expected texts
                if got != " ".join(items):
                    errors.append(f"{jid} Partners: shows {got!r}, expected {' '.join(items)!r}")
                tail = " ".join("".join(n.get_text() for n in value.select(".partners-none")).split())
                has_cyber = any(e.get("cyber_agency") for e in partners_entries(data, institute))
                want_tail = "" if has_cyber else items[-1]
                if tail != want_tail:
                    errors.append(f"{jid} Partners: the muted tail reads {tail!r}, expected {want_tail or 'none'!r}")
                ids = sorted({a.get("data-source-id") for a in value.select(".cn") if a.find_parent("details", class_="partner-how") is None})
                if ids != cites:
                    errors.append(f"{jid} Partners: citations {ids}, expected {cites}")
                # Groups and names follow first occurrence in priority order; every name keeps its own sources.
                folded = []
                fold_cites = []
                for li in value.select("details.partner-how li"):
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
                            errors.append(f"{jid} Partners: shared explanation needs explicit plural wording: {how!r}")
                    else:
                        sentence = how
                    want_fold.append(([name for name, _ in members], sentence[:1].lower() + sentence[1:]))
                    want_cites.extend(sorted(set(ids)) for _, ids in members)
                if folded != want_fold:
                    errors.append(f"{jid} Partners: fold-out shows {folded}, expected {want_fold}")
                if fold_cites != want_cites:
                    errors.append(f"{jid} Partners: fold-out citations {fold_cites}, expected {want_cites}")
                fold = value.select_one("details.partner-how")
                if fold is not None:
                    if any(a.find_parent("li") is None for a in fold.select(".cn")):
                        errors.append(f"{jid} Partners: fold-out citation outside a partner item")
                    if fold.has_attr("open"):
                        errors.append(f"{jid} Partners: the fold-out must start closed")
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


ROLE_PHRASES = {"single_point_of_contact": "single point of contact", "market_surveillance": "market surveillance", "notifying": "notifying"}
NON_LATIN_SCRIPT = re.compile(r"[\u0370-\u03ff\u0400-\u052f\u0530-\u058f\u0590-\u06ff\u0900-\u0dff\u0e00-\u0eff\u10a0-\u10ff\u1f00-\u1fff\u3040-\u30ff\u3400-\u9fff\uac00-\ud7af]")  # same scripts as method_checks.NON_LATIN, kept apart as the page gate's own oracle


def expected_group_label(roles) -> str:
    """Hand-worked heading of an AI Act authority group: its functions in the fixed order, joined with 'and'."""
    ordered = [ROLE_PHRASES[r] for r in ("single_point_of_contact", "market_surveillance", "notifying") if r in (roles or [])]
    if not ordered:
        return "Other AI Act role"
    return "AI Act " + (ordered[0] if len(ordered) == 1 else ", ".join(ordered[:-1]) + " and " + ordered[-1])


def check_nonidentified_folds(html, data: dict) -> list[str]:
    """None-identified fold-outs list bodies by role, English name first.

    Every body appears once under a group heading for its role (closest candidate, AI Act authorities by
    function, other bodies); a name in another language shows its English name with the original beneath, and
    a non-Latin script never stands alone. Each body with sources cites them inline.
    """
    soup = BeautifulSoup(html, "html.parser")
    errors = []
    folds = Keyed(soup, "details.card.mini[id]", "id")
    for j in data["jurisdictions"]:
        card = folds.one(f"c-{j['id'].lower()}")
        if card is None:
            if j.get("status") == "none_identified" and j.get("europe_scope"):
                errors.append(f"{j['id']} none-identified entry is missing")
            continue
        closest = j.get("closest_body")
        bodies = [
            b for b in j.get("related_bodies", [])
            if not closest or (b["name"] != closest["name"] and not b["name"].startswith(closest["name"] + " ("))
        ]
        expected = {}
        for b in bodies:
            label = expected_group_label(b.get("authority_roles")) if b["kind"] == "ai_act_authority" else "Other bodies"
            expected.setdefault(label, []).append(b)
        if closest:
            expected["Closest candidate"] = [closest]
        groups = {}
        unmatched = []
        for group in card.select(".body-group"):
            heading = group.select_one(".gl")
            label = " ".join(heading.get_text().split()) if heading else ""
            names = [" ".join("".join(t for t in n.find_all(string=True) if not t.find_parent(class_="cites")).split()) for n in group.select("li .body-name")]
            matches = [key for key, members in expected.items() if names == [b.get("name_en") or b["name"] for b in members]]
            if len(matches) != 1 or matches[0] in groups:
                unmatched.append(names)
                continue
            key = matches[0]
            if not label or (key not in ("Other bodies", "Closest candidate") and label != key):
                errors.append(f"{j['id']} fold-out groups: {key!r} heading is empty or differs from its authority roles")
            groups[key] = group
        if unmatched or set(groups) != set(expected):
            errors.append(f"{j['id']} fold-out groups {sorted(groups)}, expected {sorted(expected)}")
            continue
        for label, members in expected.items():
            items = groups[label].select("li")
            if len(items) != len(members):
                errors.append(f"{j['id']} group {label!r} lists {len(items)} bodies, expected {len(members)}")
                continue
            for li, b in zip(items, members):
                cited = bool(li.select(".cn"))
                for marks in li.select_one(".body-name").select(".cites"):
                    marks.extract()  # the numerals are not part of the name
                shown = " ".join(li.select_one(".body-name").get_text().split())
                original = li.select_one(".orig")
                want = b.get("name_en") or b["name"]
                if shown != want:
                    errors.append(f"{j['id']} body shows {shown!r}, expected {want!r}")
                if NON_LATIN_SCRIPT.search(shown):
                    errors.append(f"{j['id']} body {shown!r} stands in a non-Latin script without an English name")
                if bool(b.get("name_en")) != (original is not None) or (original is not None and " ".join(original.get_text().split()) != b["name"]):
                    errors.append(f"{j['id']} body {want!r}: the original name must sit beneath exactly when there is an English name")
                state = {"designated": "Designated by", "proposed": "Proposed, not yet designated"}.get(b.get("designation_state"), "Designation not yet identified")
                if b.get("kind") == "ai_act_authority" and state not in " ".join(groups[label].get_text().split()) and label != "Closest candidate":
                    errors.append(f"{j['id']} body {want!r}: its designation status {state!r} is not shown")
                role = " ".join(b.get("role", "").split())
                for marks in li.select(".cites"):
                    marks.extract()
                dets = [" ".join(d.get_text().split()) for d in li.select(".det")]
                basis = groups[label].select_one(".basis")
                basis_text = " ".join(basis.get_text().split()).lower() if basis else ""
                # Each role text is recoverable: on the entry, as the group's shared sentence, or as the entry's part plus the shared closing words.
                shown_role = any(d.lower() == role.lower() or (role.lower().startswith(d.lower().rstrip(".")) and d and role[len(d.rstrip(".")):].strip().lower() in basis_text) for d in dets)
                if role and label != "Closest candidate" and not shown_role and role.lower() not in basis_text:
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
    summaries = data["dataset"]["method_summaries"]
    if len(soup.select("#rules > li")) != len(summaries):
        errors.append("Method: reader summary count differs from the dataset")
    for summary in summaries:
        sid = summary["id"]
        item = soup.find(id="method-summary-" + sid)
        text = item.select_one(".method-copy") if item else None
        want = summary["summary"]
        shorthand = want == data["dataset"].get("definition")
        if text is None or not text.get_text(strip=True) or (not shorthand and " ".join(text.get_text().split()) != " ".join(want.split())):
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


@reports
def check_name_roles(page) -> list[str]:
    """One style per role for state names in lists: names in linked lists (Testing and access, Institutional set-up,
    Budget not published) take the accent link style; News and None identified names take the card title's type.
    On phones a None identified row stacks its name above a full-width summary."""
    return page.evaluate("""() => {
      const errors = [], look = n => { const c = getComputedStyle(n); return [c.fontFamily, c.fontWeight, c.fontSize, c.color].join('|'); };
      const title = document.querySelector('.card .top .name'), link = document.querySelector('#access .group-list a.standalone');
      if (!title || !link) return ['name roles: no card title or linked list name to compare'];
      const linkLook = look(link), titleType = look(title).split('|').slice(0, 3).join('|');
      document.querySelectorAll('#setup .group-list a.standalone, #fund-none li > a').forEach(a => { if (look(a) !== linkLook) errors.push('linked list name not in the accent link style: ' + a.textContent); });
      document.querySelectorAll('.development-name, .card.mini > summary .name').forEach(n => { if (look(n).split('|').slice(0, 3).join('|') !== titleType) errors.push('list name not in the card title type: ' + n.textContent); });
      if (innerWidth <= 640) document.querySelectorAll('.card.mini > summary').forEach(s => {
        const name = s.querySelector('.name').getBoundingClientRect(), find = s.querySelector('.find');
        if (!find || !find.getClientRects().length) return;
        const f = find.getBoundingClientRect();
        if (f.top < name.bottom - 1 || Math.abs(f.left - name.left) > 1) errors.push('phone None identified row keeps its name beside the summary: ' + s.textContent.slice(0, 30));
        // Full width: the summary runs to the chevron (the summary's content edge less the chevron, its margin and the gap).
        const sc = getComputedStyle(s), chev = getComputedStyle(s, '::after'), right = s.getBoundingClientRect().right - parseFloat(sc.paddingRight) - parseFloat(chev.width) - parseFloat(chev.marginLeft) - parseFloat(sc.columnGap);
        if (right - f.right > 1) errors.push('phone None identified summary is not full width: ' + s.textContent.slice(0, 30));
      });
      return [...new Set(errors)].slice(0, 10);
    }""")


@reports
def check_nonidentified_style(page) -> list[str]:
    """The How we checked block is muted and smaller than the bodies above it; group headings are never weaker than their items."""
    return page.evaluate("""() => {
      const folds = [...document.querySelectorAll('details:not([open])')]; folds.forEach(d => d.open = true);
      const errors = [], px = e => parseFloat(getComputedStyle(e).fontSize), weight = e => Number(getComputedStyle(e).fontWeight);
      for (const card of document.querySelectorAll('.card.mini')) {
        const id = card.dataset.j, how = card.querySelector('.how p:not(.gl)');
        if (how && getComputedStyle(how).color === getComputedStyle(card.querySelector('summary .name')).color) errors.push(id + ': How we checked is not muted');
        if (how && px(how) >= px(card.querySelector('summary .name'))) errors.push(id + ': How we checked is not smaller than the entry name');
        for (const g of card.querySelectorAll('.body-group')) {
          const gl = g.querySelector('.gl'), name = g.querySelector('.body-name');
          if (name && (weight(gl) < weight(name) || px(gl) < px(name))) errors.push(id + ': group heading ' + gl.textContent + ' is weaker than its items');
          const orig = g.querySelector('.orig');
          if (orig && getComputedStyle(orig).color === getComputedStyle(name).color) errors.push(id + ': original name is not muted');
        }
      }
      folds.forEach(d => d.open = false);
      return [...new Set(errors)];
    }""")


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
        if sources[sid]["url"] not in links:
            errors.append(f"Method does not name generic source {sid}")
    text = box.get_text().replace(" (opens in a new tab)", "") if box else ""
    if not (text.startswith(GENERIC_LEAD) and text.endswith(GENERIC_TAIL)):
        errors.append("Method generic sources line differs from its copy")
    return errors


GENERIC_LEAD = "Population and region sources apply to every entry: "
GENERIC_TAIL = ". Entry source lists leave them out; the downloads include them."


def check_archive_links(html, data: dict) -> list[str]:
    """A rendered source with an archive_url has "(archived copy)" right after its link; one without has none.

    The oracle is the data: each source list item names its source id, and the Method
    inline links follow display.generic_sources, then each other overview's source_links.
    Superscript citations never carry an archive link.
    """
    soup = BeautifulSoup(html, "html.parser")
    sources = {s["id"]: s for s in data["sources"]}
    errors, expected = [], 0

    def follows(link, sid: str, where: str) -> None:
        nonlocal expected
        archive = sources[sid].get("archive_url") or ""
        after = link.find_next_sibling()
        if after is not None and "title-en" in (after.get("class") or []):
            after = after.find_next_sibling()  # a source list puts our English title between the link and its archived copy
        has = after is not None and after.select_one("a.archive") is not None
        if not archive:
            if has:
                errors.append(f"{where}: source {sid} has no archive_url but shows an archived copy link")
            return
        expected += 1
        if not has:
            errors.append(f"{where}: source {sid} lacks its archived copy link")
            return
        copy = after.select_one("a.archive")
        if copy.get("href") != archive or not "".join(t for t in copy.find_all(string=True) if not t.find_parent(class_="vh")).strip():
            errors.append(f"{where}: source {sid} archived copy link differs from data")
        text = "".join(t for t in after.find_all(string=True) if not t.find_parent(class_="vh"))
        label = "".join(t for t in copy.find_all(string=True) if not t.find_parent(class_="vh"))
        if "archive-copy" not in (after.get("class") or []) or text != f"({label})" or after.previous_sibling != " ":
            errors.append(f"{where}: source {sid} archived copy is not one bracketed phrase after its link")

    items = soup.select("li[data-source-id]")
    if not items:
        errors.append("no source lists found to check archive links")
    for li in items:
        sid, scope = str(li["data-source-id"]), li.find_parent(attrs={"data-source-scope": True})
        host = scope or li.find_parent(attrs={"data-j": True})
        where = f"{(host.get('data-source-scope') or host.get('data-j')) if host else 'page'} source list"
        link = li.select_one("a[href]")
        if sid in sources and link is not None:
            follows(link, sid, where)
    display = data["dataset"]["display"]
    lists = [display["generic_sources"]] + [o["display"]["source_links"] for o in data["dataset"].get("other_overviews") or []]
    spans = soup.select(".inline-links")
    if len(spans) != len(lists):
        errors.append(f"Method: {len(spans)} inline source groups, data has {len(lists)}")
    for span, ids in zip(spans, lists):
        links = [a for a in span.select("a") if "archive" not in (a.get("class") or [])]
        if [a.get("href") for a in links] != [sources[sid]["url"] for sid in ids]:
            errors.append(f"Method: inline sources differ from data ({', '.join(ids)})")
            continue
        for link, sid in zip(links, ids):
            follows(link, sid, "Method")
    shown = soup.select("a.archive")
    if len(shown) != expected:
        errors.append(f"{len(shown)} archived copy links on the page, {expected} expected from source lists and Method")
    if any(a.find_parent(class_="cn") or "cn" in (a.get("class") or []) for a in shown):
        errors.append("a superscript citation carries an archived copy link")
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
    """Consecutive blocks in an open fold (none-identified entries, funding records) never touch: an overlap
    blocks, a gap under 4px is a report line.

    Opens every disclosure to measure, then restores the page.
    """
    return page.evaluate("() => {" + REPORT_JS + """
      const folds = [...document.querySelectorAll('details:not([open])')];
      folds.forEach(d => d.open = true);
      const errors = [];
      for (const box of document.querySelectorAll('.card.mini, .fund-detail-body > div:not(.srcs), .funding-record')) {
        const kids = [...box.children].filter(k => k.tagName !== 'SUMMARY' && k.getClientRects().length && getComputedStyle(k).position !== 'absolute');
        for (let i = 1; i < kids.length; i++) {
          const gap = kids[i].getBoundingClientRect().top - kids[i - 1].getBoundingClientRect().bottom;
          if (gap < 4) errors.push((gap < -0.5 ? 'blocks overlap' : REPORT + 'blocks touch') + ` in ${box.closest('[data-j]')?.dataset.j || box.closest('.fund-item')?.querySelector('h3')?.textContent.trim() || box.className}: ${kids[i].tagName.toLowerCase()}.${kids[i].className} ${gap.toFixed(1)}px`);
        }
      }
      folds.forEach(d => d.open = false);
      return [...new Set(errors)];
    }""")


@reports
def check_unbroken_dates(page) -> list[str]:
    """A formatted date (day Mon year or Mon year) never breaks across lines, folds included.

    Opens every disclosure and earlier development to measure them, then restores the page.
    """
    return page.evaluate(r"""() => {
      const folds = [...document.querySelectorAll('details:not([open])')], earlier = [...document.querySelectorAll('#development-list > [hidden]')];
      folds.forEach(d => d.open = true); earlier.forEach(r => r.hidden = false);
      const errors = [], seen = new Set(), date = /\b(?:\d{1,2} )?(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec) \d{4}\b/g;
      // Join each block's text nodes, so a date split across elements (e.g. by a citation glue) is still found.
      const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
      const nodes = []; while (walker.nextNode()) if (walker.currentNode.parentElement.getClientRects().length && !walker.currentNode.parentElement.closest('script, style, #tip, .vh')) nodes.push(walker.currentNode);
      let text = '', map = [];
      // Text from different blocks is separated, so a trailing digit never joins the next block's date.
      const block = n => { let e = n.parentElement; while (e && getComputedStyle(e).display.startsWith('inline')) e = e.parentElement; return e; };
      let previous = null;
      for (const n of nodes) { const b = block(n); if (previous && b !== previous) text += '\n'; previous = b; map.push([text.length, n]); text += n.nodeValue; }
      const locate = i => { let lo = 0, hi = map.length - 1; while (lo < hi) { const mid = (lo + hi + 1) >> 1; if (map[mid][0] <= i) lo = mid; else hi = mid - 1; } return [map[lo][1], i - map[lo][0]]; };
      let m;
      while ((m = date.exec(text))) {
        const [sn, so] = locate(m.index), [en, eo] = locate(m.index + m[0].length - 1);
        const range = document.createRange(); range.setStart(sn, so); range.setEnd(en, eo + 1);
        const lines = new Set([...range.getClientRects()].filter(r => r.width > 0 && r.height > 0).map(r => Math.round(r.bottom)));
        const where = sn.parentElement.closest('[data-j]')?.dataset.j || sn.parentElement.closest('[id]')?.id || sn.parentElement.className || sn.parentElement.tagName;
        const key = m[0] + ' in ' + where;
        if (lines.size > 1 && !seen.has(key)) { seen.add(key); errors.push(`date breaks across lines: ${key}`); }
      }
      folds.forEach(d => d.open = false); earlier.forEach(r => r.hidden = true);
      return errors.slice(0, 8);
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
    'fund-none': 'funding, Not published line', 'fund-chart-h': 'funding chart heading', foot: 'page footer', content: 'main content',
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
      const name = text(n.querySelector('h3'));
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
    """Measure the shared control system and responsive toolbar in the browser."""
    return page.evaluate(
        "() => {"
        + BROWSER_GEOMETRY
        + REPORT_JS
        + """
      const errors = [], probe = document.createElement('span');
      probe.style.cssText = 'position:fixed;visibility:hidden;height:var(--control);border:var(--control-border);border-radius:var(--r-sm);background:var(--field-fill)';
      document.body.appendChild(probe);
      const p = getComputedStyle(probe);
      const expected = {height:parseFloat(p.height), radius:p.borderTopLeftRadius, border:p.borderTop, fill:p.backgroundColor};
      probe.style.background = 'var(--selected-fill)';
      expected.selected = getComputedStyle(probe).backgroundColor; probe.remove();
      if (Math.abs(expected.height - 40) > .5) errors.push(REPORT + 'control token is not 40px');
      document.querySelectorAll('.field, .btn').forEach(node => {
        if (!visible(node) || node.id === 'theme-toggle') return; // a bare icon button by design; check_theme_default checks it
        const s = getComputedStyle(node), r = node.getBoundingClientRect();
        const selected = node.matches('[aria-pressed="true"], .btn:hover') || !!node.closest('.selectwrap.is-filtered');
        if (Math.abs(r.height-expected.height) > 1 || s.borderTopLeftRadius !== expected.radius || s.borderTop !== expected.border) errors.push(REPORT + `inconsistent control dimensions/border: ${node.id || node.className || node.textContent}`);
        if (s.backgroundColor !== (selected ? expected.selected : expected.fill)) errors.push(REPORT + `inconsistent control fill: ${node.id || node.className || node.textContent}`);
      });
      document.querySelectorAll('input[type="checkbox"]').forEach(node => {
        if (!visible(node)) return;
        const r = node.getBoundingClientRect(), p = pseudoBox(node);
        if (Math.abs(r.width-20) > 1 || Math.abs(r.height-20) > 1 || !p || p.height < 39 || p.width < 39) errors.push('checkbox lacks 20px visual / 40px effective target');
      });
      document.querySelectorAll('#col-opts label, .more > summary').forEach(node => {
        if (visible(node) && node.getBoundingClientRect().height < 39) errors.push(`control/disclosure label is below 40px: ${node.textContent.trim().slice(0,60)}`);
      });
      const toolbar = document.querySelector('.tbar'), search = document.querySelector('#world-filter');
      const tr = toolbar.getBoundingClientRect(), sr = search.getBoundingClientRect();
      if (Math.abs(sr.width-tr.width) > 1 || Math.abs(sr.left-tr.left) > 1) errors.push(REPORT + 'search does not fill toolbar width');
      const actions = [...document.querySelector('.toolbar-actions').children].filter(n => innerWidth > 640 || n.matches('.cols, .download-menu')).map(n => n.getBoundingClientRect());
      if (!actions.length || actions.some(r => Math.abs(r.top-actions[0].top) > 1 || r.top < sr.bottom)) errors.push(REPORT + 'toolbar actions do not share one row below search');
      const region = document.querySelector('#world-region');
      if (!(region.getAttribute('aria-label') || '').trim()) errors.push('region filter lacks an accessible name');
      if (innerWidth <= 640) {
        const s = getComputedStyle(region), r = region.getBoundingClientRect();
        const canvas = document.createElement('canvas'), ctx = canvas.getContext('2d'); ctx.font = [s.fontWeight, s.fontSize, s.fontFamily].join(' ');
        if (s.color === 'rgba(0, 0, 0, 0)' || r.width-parseFloat(s.paddingLeft)-parseFloat(s.paddingRight)-2 < ctx.measureText(region.selectedOptions[0]?.textContent || '').width) errors.push('phone region filter has no room for a visible label');
        const statusBox = document.querySelector('#world-status').getBoundingClientRect();
        if (statusBox.left < tr.left-1 || statusBox.right > tr.right+1 || statusBox.height < 40) errors.push('phone status select does not fit the toolbar or is too short to tap');
        if (!(document.querySelector('#world-download > summary').getAttribute('aria-label') || '').trim() || document.querySelector('#world-download .control-label').getBoundingClientRect().width > 1) errors.push('phone Download is not icon-only with its accessible name');
      } else if (!document.querySelector('#world-download > summary.ico.i-dl') || !visible(document.querySelector('#world-download .control-label'))) {
        errors.push('Download lacks its download icon and visible label');
      }
      if (innerWidth <= 640) {
        if ([...document.querySelector('.toolbar-actions').children].some(n => { const r = n.getBoundingClientRect(); return r.left < tr.left-1 || r.right > tr.right+1; })) errors.push('phone toolbar actions overflow');
      }
      const inline = document.querySelector('#world-count button');
      if (inline) {
        const s = getComputedStyle(inline), parent = getComputedStyle(inline.parentElement), hit = pseudoBox(inline);
        if (['fontSize', 'fontWeight', 'fontFamily', 'lineHeight'].some(k => s[k] !== parent[k]) || parseFloat(s.borderTopWidth) || parseFloat(s.paddingLeft) || parseFloat(s.paddingRight) || inline.getBoundingClientRect().height > parseFloat(parent.lineHeight)+1) errors.push(REPORT + 'Show all does not share the count line typography');
        if (!hit || hit.height < 28) errors.push('Show all lacks its invisible hit area');
      }
      const body = getComputedStyle(document.body);
      const mutedProbe = document.createElement('span'); mutedProbe.style.color = 'var(--muted)'; document.body.appendChild(mutedProbe);
      const muted = getComputedStyle(mutedProbe).color; mutedProbe.remove();
      document.querySelectorAll('.missing').forEach(node => {
        if (!visible(node)) return;
        const s = getComputedStyle(node), structural = node.matches('.na');
        if (['fontSize', 'fontWeight', 'fontFamily', 'lineHeight'].some(k => s[k] !== body[k])) errors.push(REPORT + 'missing value does not use the body role');
        // "Not published" is a research finding in body ink; a structural dash is muted by design.
        if (s.color !== (structural ? muted : body.color)) errors.push(structural ? 'structural absence is not muted' : 'missing value does not use the body ink');
      });
      if (document.querySelector('#world-all')) errors.push('duplicate full-dataset toolbar action remains');
      // Principle Region tabs: the tab text starts on the map column's edge and the search spans the list column (beside the map) or the list's width (under it).
      const tabs = document.querySelector('#region-chips'), first = tabs && tabs.querySelector('button'), map = [...document.querySelectorAll('#worldmap svg')].find(visible), list = document.querySelector('#regions'), find = document.querySelector('#map-search');
      if (tabs && visible(tabs) && first && map && list && find) {
        const range = document.createRange(); range.selectNodeContents(first);
        const tx = range.getBoundingClientRect().left, mr = map.closest('#worldmap').getBoundingClientRect(), lr = list.getBoundingClientRect(), fr = find.getBoundingClientRect();
        if (Math.abs(tx - mr.left) > 1) errors.push(REPORT + `region tab text starts ${Math.round(tx - mr.left)}px off the map column edge`);
        const beside = lr.left > mr.right; // the list beside the map, or stacked under it
        if (beside && (Math.abs(fr.left - lr.left) > 1 || Math.abs(fr.right - lr.right) > 1)) errors.push(REPORT + `search box [${Math.round(fr.left)}, ${Math.round(fr.right)}] does not span the region list [${Math.round(lr.left)}, ${Math.round(lr.right)}]`);
        if (!beside && Math.abs(fr.right - mr.right) > 1) errors.push(REPORT + `search box ends at ${Math.round(fr.right)}, not on the map's right edge ${Math.round(mr.right)}`);
        const hr = tabs.getBoundingClientRect();
        if (!beside && hr.right > fr.left + 1 && hr.bottom > fr.top + 1) errors.push('region tabs and the search overlap');
        const ts = getComputedStyle(first);
        if (parseFloat(ts.borderTopWidth) || ts.backgroundColor !== 'rgba(0, 0, 0, 0)') errors.push(REPORT + 'region tabs are boxed');
      }
      return errors;
    }"""
    )


IN_CONTENT_CONTROLS = "th button, .list-more, .more > summary, .linkbtn, summary.source-footer-line"


def check_in_content_controls(page) -> list[str]:
    """Controls inside content are never boxed: no border, fill or shadow, the context's type and colour;
    the table header shows an underline on hover. Standalone controls are boxed (check_controls)."""
    errors = page.evaluate(
        "selector => {" + REPORT_JS + """
      const errors = [], clear = c => c === 'transparent' || /rgba\\(.*,\\s*0\\)$/.test(c);
      document.querySelectorAll(selector).forEach(node => {
        if (!node.getClientRects().length) return;
        const s = getComputedStyle(node), name = (node.className || node.tagName.toLowerCase()) + ': ' + node.textContent.trim().slice(0, 30);
        const border = ['Top', 'Right', 'Bottom', 'Left'].some(side => parseFloat(s['border' + side + 'Width']) > 0 && s['border' + side + 'Style'] !== 'none' && !clear(s['border' + side + 'Color']));
        if (border || !clear(s.backgroundColor) || s.backgroundImage !== 'none' || s.boxShadow !== 'none') errors.push(REPORT + `in-content control is boxed: ${name}`);
        if (node.matches('th button')) {
          const th = getComputedStyle(node.closest('th'));
          if (['fontSize', 'fontWeight', 'fontFamily', 'color'].some(k => s[k] !== th[k])) errors.push(REPORT + `table header control does not take the header's type and colour: ${name}`);
          const td = document.querySelector('#world-rows td');
          if (td && Math.abs(parseFloat(s.paddingLeft) - parseFloat(getComputedStyle(td).paddingLeft)) > .5) errors.push(REPORT + 'table header text is not aligned with the cell padding');
        }
      });
      const head = document.querySelector('table.world thead th');
      if (head && (parseFloat(getComputedStyle(head).borderBottomWidth) < 2 || getComputedStyle(head).borderBottomColor !== getComputedStyle(document.body).color)) errors.push(REPORT + 'table header lacks one strong rule');
      const arrows = [...document.querySelectorAll('table.world th')].filter(th => th.getClientRects().length).map(th => [th.hasAttribute('aria-sort'), getComputedStyle(th.querySelector('button'), '::after').color]);
      const ink = getComputedStyle(document.body).color;
      if (arrows.some(([active, colour]) => active !== (colour === ink))) errors.push(REPORT + 'sort arrows: only the active one is full ink');
      return errors;
    }""",
        IN_CONTENT_CONTROLS,
    )
    # The first column's header is always in view horizontally, so the table's sideways scroll is untouched.
    scroll_y = page.evaluate("scrollY")
    button = page.locator('table.world th[data-k="name"] button')
    button.scroll_into_view_if_needed()
    feedback = """n => { const s = getComputedStyle(n), label = n.querySelector('.sort-label'), arrow = getComputedStyle(n, '::after');
      const clear = c => c === 'transparent' || /rgba\\(.*,\\s*0\\)$/.test(c);
      return { underline: !!label && getComputedStyle(label).textDecorationLine.includes('underline'),
               arrowUnderlined: s.textDecorationLine.includes('underline') || arrow.textDecorationLine.includes('underline'),
               boxed: !clear(s.backgroundColor) || s.boxShadow !== 'none' || parseFloat(s.borderTopWidth) > 0 }; }"""
    button.hover()
    hovered = button.evaluate(feedback)
    page.mouse.move(0, 0)
    button.focus()
    page.keyboard.press("Shift")  # keyboard modality, so :focus-visible applies
    focused = button.evaluate(feedback)
    button.evaluate("n => n.blur()")
    for state, result in (("hover", hovered), ("focus", focused)):
        if not result["underline"]:
            errors.append(f"table header control lacks {state} underline on its label")
        if result["arrowUnderlined"]:
            errors.append(report_line(f"table header sort arrow is underlined on {state}"))
        if result["boxed"]:
            errors.append(report_line(f"table header control gains a box on {state}"))
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
        return " ".join(text.split()).rstrip(" ·")

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

    def prose(node):
        return " ".join(node.get_text().split()) if node else ""

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
        funding = soup.select_one(f'.fund-item[data-j="{jid}"], .fund-none li[data-j="{jid}"]')
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
            and prose(funding.select_one(".fund-budget > .fund-value strong"))
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
            for p in funding.select(".fund-staff .fund-context")
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
    "de-aisi": None, "es-isia": None, "no-ki-norge": "funding-0475119a71cc5e2099e293d3a48356b0",
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
        wanted = expected[identity]  # every institute: Funding and staff covers all of them (principle: Scope)
        card = cards.one(jid)
        budget = card.select_one('.row[data-track="9"] .v') if card else None
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
            if not card.select_one('.row[data-track="12"]'):
                errors.append(f"{jid}: wider funding must use a separate Related funding line")
    return errors


def funding_groups(data: dict) -> tuple[set, set, set]:
    """Hand-worked funding groups by jurisdiction: bar rows, rows without a bar, the Not published line."""
    institutes = {i["id"]: i["jurisdiction_id"] for i in data["institutes"]}
    staffed = {
        i["jurisdiction_id"]
        for i in data["institutes"]
        if any(s.get("total") is not None or s.get("technical") is not None for s in i.get("staff", []))
    }
    own = {institutes[iid] for iid, fid in EXPECTED_BUDGET_IDS.items() if fid is not None and iid in institutes}
    everyone = set(institutes.values())
    bars = set(EXPECTED_FUNDING_IDS) & everyone
    nobar = {jid for jid in everyone - bars if jid in own or jid in staffed}
    return bars, nobar, everyone - bars - nobar


def check_funding_outside(html, data: dict) -> list[str]:
    """One funding list: bar rows, then rows without a bar, then one Not published line.

    Expected groups come from the hand-worked bar and Budget identities and the staff
    records, not the producer. Each institute appears once; rows without a bar keep an
    empty track and a reason, their Budget in a lighter cell, staff at full weight; only
    institutes with neither a funding nor a staff figure are on the Not published line.
    """
    soup = BeautifulSoup(html, "html.parser")
    errors = []
    year = data["dataset"]["as_of"][:4]
    heading = soup.select_one("#fund-chart-h")
    if not heading or year not in heading.get_text():
        errors.append(f"funding chart heading must name the reference year {year}")
    if soup.select_one("#fund-outside, #fund-unavailable, .fund-outside-item, .fund-missing"):
        errors.append(report_line("funding keeps the old separate lists; rows without a bar belong in the one list"))
    by_id = {i["id"]: i for i in data["institutes"]}
    institutes = {iid: i["jurisdiction_id"] for iid, i in by_id.items()}
    bars, nobar, none = funding_groups(data)
    has_total = {
        i["jurisdiction_id"] for i in data["institutes"] if any(s.get("total") is not None for s in i.get("staff", []))
    }
    described_only = {}
    for i in data["institutes"]:
        shown_staff = i["display"]["staff_records"].get(i["display"].get("staff") or "")
        if shown_staff and shown_staff.get("total") is None and shown_staff.get("technical") is None and shown_staff.get("description"):
            described_only[i["jurisdiction_id"]] = shown_staff
    own_ids = {institutes[iid]: fid for iid, fid in EXPECTED_BUDGET_IDS.items() if fid is not None and iid in institutes}
    got_bars = [str(n["data-j"]) for n in soup.select("#fund-rows > .fund-item:not(.fund-nobar)[data-j]")]
    rows = soup.select("#fund-rows > .fund-item[data-j]")
    got_nobar = [str(n["data-j"]) for n in soup.select("#fund-rows > .fund-nobar[data-j]")]
    got_none = [str(li["data-j"]) for li in soup.select("#fund-none li[data-j]")]
    for label, got, want in (("bar rows", got_bars, bars), ("rows without a bar", got_nobar, nobar), ("Not published line", got_none, none)):
        if sorted(got) != sorted(want):
            errors.append(f"funding {label} list {sorted(got)}, expected {sorted(want)}")
    everyone = Counter(got_bars + got_nobar + got_none)
    if everyone != Counter(institutes.values()):
        errors.append(f"funding must list each institute once: {sorted(j for j, n in everyone.items() if n != 1)}")
    kinds = [("fund-nobar" in (r.get("class") or [])) for r in rows]
    if kinds != sorted(kinds):
        errors.append("rows without a bar must follow every bar row")
    line = soup.select_one("#fund-none")
    if line is None or line.has_attr("hidden") != (not none):
        errors.append("Not published line: visibility differs from its entries")
    ids = {str(n["id"]) for n in soup.select("[id]")}
    for li in soup.select("#fund-none li[data-j]"):
        link = li.select_one("a[href]")
        if not link or str(link["href"]).lstrip("#") not in ids:
            errors.append(f"{li['data-j']}: Not published line does not link to the institute's entry")
    for row in soup.select("#fund-rows > .fund-nobar[data-j]"):
        jid = str(row["data-j"])
        if row.select_one(".fund-bar .bar-value"):  # a bar where the figure is not charted changes what the chart says
            errors.append(f"{jid}: a row without a bar draws a bar")
        elif not row.select_one(".fund-bar"):
            errors.append(report_line(f"{jid}: a row without a bar must keep an empty track"))
        value = row.select_one(".fund-budget .budget-value")
        if jid in own_ids:
            if not value or value.get("data-funding-id") != own_ids[jid] or not value.select_one(".budget-amount") or not value.select_one(".budget-qualifiers"):
                errors.append(f"{jid}: row without a bar shows the wrong own Budget record")
            if not row.select(".fund-budget .fund-value .cn"):
                errors.append(f"{jid}: own funding without a bar has no citation")
            # Why it has no bar is the last qualifier on the value's own line, never a sentence of its own (principle: Values and qualifiers).
            shown = row.select_one(".fund-budget .budget-qualifiers")
            # The qualifier words without the citation numerals that follow them (an amount in euros cites the rate too).
            words = " ".join(" ".join(t for t in shown.find_all(string=True) if not t.find_parent(class_=["cites", "cn"])).split()) if shown else ""
            if row.select(".fund-reason") or not words.endswith("· not charted"):
                errors.append(f"{jid}: row without a bar should end its qualifier line with 'not charted', got {words!r}")
        elif not row.select_one(".fund-budget .fund-value > .missing"):
            errors.append(f"{jid}: row without a bar or own funding must say Not published")
        staff_cell = row.select_one(".fund-staff .fund-value")
        described = described_only.get(jid)
        if described is not None:
            # A description-only staff record (a published range) shows its text, as the card does, with citations and its as-of line.
            shown = " ".join(" ".join(t for t in staff_cell.find_all(string=True) if not t.find_parent(class_=["cites", "cn"])).split()) if staff_cell else ""
            context = row.select_one(".fund-staff .fund-context")
            if shown != described["description"] or not staff_cell.select(".cites a, .cn") or (described.get("as_of") and not (context and "as of" in context.get_text())):
                errors.append(f"{jid}: funding Staff should show {described['description']!r} with citations and its date, got {shown!r}")
        if (jid in has_total) == (staff_cell is None or staff_cell.select_one(".missing") is not None or staff_cell.select_one("strong") is None):
            errors.append(f"{jid}: Staff cell differs from the staff records")
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
    return sorted(selectors)


def label_ladder_match_errors(data: dict, counts: dict[str, int]) -> list[str]:
    return [error for selector in label_ladder_selectors(data)
            for error in zero_match_errors("label_ladder", selector, counts.get(selector, 0))]


def check_label_ladder(page) -> list[str]:
    """Group headings are not weaker than items; cards retain accessible status; source marks match Method."""
    data = checked_page_data(page)
    result = page.evaluate("selectors => {" + REPORT_JS + """
      const errors = [], visible = n => n.getClientRects().length > 0;
      const css = (n, p) => getComputedStyle(n)[p];
      const counts = Object.fromEntries(selectors.map(s => [s, [...document.querySelectorAll(s.replace(/:visible$/, ''))].filter(n => !s.endsWith(':visible') || visible(n)).length]));
      for (const h of document.querySelectorAll('.statusgroup > h3, .eu-level .eu-head > h3')) {
        if (!visible(h)) continue;
        const name = h.closest('.statusgroup, .eu-level').querySelector('.card .top .name');
        if (!name) continue; // mini-only groups have no .top; full-card names are required above
        if (css(h, 'color') !== css(name, 'color') || parseFloat(css(h, 'fontWeight')) < parseFloat(css(name, 'fontWeight')) || parseFloat(css(h, 'fontSize')) < parseFloat(css(name, 'fontSize'))) errors.push(REPORT + `group heading is weaker than its items: ${h.textContent.trim()}`);
      }
      // A card under a status group heading does not repeat the status in its title row; its accessible name still carries it.
      for (const card of document.querySelectorAll('.statusgroup .card:not(.mini)')) {
        if (card.querySelector(':scope > .top > .smark')) errors.push(REPORT + `card in a status group repeats its status in the title row: ${card.id}`);
        const said = card.querySelector(':scope > .top > .vh'), ids = (card.getAttribute('aria-labelledby') || '').split(' ');
        const want = ', ' + card.closest('.statusgroup').querySelector(':scope > h3 .smark > span').textContent.replace(/ \\(\\d+\\)$/, '');
        if (!said || said.textContent !== want || !ids.includes(said.id)) errors.push(`card in a status group lacks its status in the accessible name: ${card.id}`);
      }
      // The EU entry's dated Status row says its status, so its title row shows none either; the accessible name keeps it (owner, 5 Nov 2026).
      for (const card of document.querySelectorAll('.eu-level .card:not(.mini)')) {
        if (card.querySelector(':scope > .top > .smark')) errors.push(REPORT + 'the EU entry repeats its status in the title row; its Status row says it');
        const said = card.querySelector(':scope > .top > .vh'), ids = (card.getAttribute('aria-labelledby') || '').split(' ');
        const row = card.querySelector('.row[data-track="3"]'), shown = row ? row.querySelector('.v').textContent.trim().split(' since ')[0] : null;
        if (!said || !shown || said.textContent !== ', ' + shown || !ids.includes(said.id)) errors.push(`the EU entry's accessible status ${said && said.textContent} does not match its Status row ${shown}`);
      }
      return {errors, counts};
    }""", label_ladder_selectors(data))
    return label_ladder_match_errors(data, result["counts"]) + result["errors"]


@reports
def check_funding_weights(page) -> list[str]:
    """Weight is per cell: a Budget figure without a bar is lighter than a charted one; staff figures keep full weight."""
    return page.evaluate("""() => {
      const errors = [], size = n => parseFloat(getComputedStyle(n).fontSize), weight = n => parseFloat(getComputedStyle(n).fontWeight);
      let compared = 0;
      const figure = document.querySelector('#fund-rows > .fund-item:not(.fund-nobar) .fund-budget .fund-value > strong');
      if (!figure) return ['funding: no charted figure to compare weights with'];
      for (const row of document.querySelectorAll('#fund-rows > .fund-nobar')) {
        const cell = row.querySelector('.fund-budget .fund-value > .budget-value');
        if (cell) compared++;
        if (cell && (size(cell) >= size(figure) || getComputedStyle(cell).color === getComputedStyle(figure).color)) errors.push(`${row.dataset.j}: Budget without a bar is not lighter than a charted figure`);
        const staff = row.querySelector('.fund-staff .fund-value > strong'), charted = document.querySelector('#fund-rows > .fund-item:not(.fund-nobar) .fund-staff .fund-value > strong');
        if (staff && charted && (size(staff) !== size(charted) || weight(staff) !== weight(charted))) errors.push(`${row.dataset.j}: Staff figure is not at full weight`);
      }
      if (!compared) errors.push('funding: no row without a bar to compare');
      return errors;
    }""")


def check_funding_bars(page, data: dict, expected_ids=None) -> list[str]:
    errors = []
    expected_ids = EXPECTED_FUNDING_IDS if expected_ids is None else expected_ids
    for institute in data["institutes"]:
        jid = institute["jurisdiction_id"]
        row = page.locator(f'.fund-item:not(.fund-nobar)[data-j="{jid}"]')
        if jid not in expected_ids:
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
        figure = row.locator(".fund-budget > .fund-value strong").inner_text()
        stated, currency = funding.get("annual_amount"), funding.get("currency")
        bracket = ""
        if currency and currency != "EUR" and stated is not None:
            kept = half_up_two(stated, funding.get("amount_qualifier"))
            bracket = f" ({'~' if kept != Decimal(str(stated)) else ''}{currency} {short_amount(kept)})"
        if (bracket and not figure.endswith(bracket)) or (not bracket and "(" in figure):
            errors.append(f"{jid}: original-currency figure differs from the data: expected {bracket!r} at the end of {figure!r}")
        match = re.fullmatch(r"(?:up to |at least |about |nearly |more than |less than )?~?EUR ([0-9.,]+)(k|m|bn)?(?: \(~?[A-Z]{3} [0-9.,]+(?:k|m|bn)?\))?", figure)
        amount = Decimal(match[1].replace(",", "")) * {None: 1, "k": 10**3, "m": 10**6, "bn": 10**9}[match[2]] if match else None
        raw = expected_annual_amount(funding, data)
        rounded = half_up_two(raw, funding.get("amount_qualifier"))
        if amount != rounded:
            errors.append(f"{jid}: comparison figure differs from selected record")
        marked = (currency != "EUR" or rounded != raw) and funding.get("amount_qualifier") != "about"
        if ("~EUR " in figure) != marked:
            errors.append(f"{jid}: comparison approximation marker differs from selected record")
        pending = row.locator(".bar-value.pending").count() == 1
        if pending != (funding["commitment"] != "adopted"):
            errors.append(f"{jid}: bar fill differs from commitment")
        value_text = " ".join(row.locator(".fund-budget > .fund-value").inner_text().split())
        if ("average over period" in value_text) != (funding.get("annual_basis_code") == "period_mean"):
            errors.append(f"{jid}: period mean marker differs from data")
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


def check_funding_large_text(browser, base_url: str) -> list[str]:
    """Funding rows stack where their tracks no longer fit, also with text 25% larger just above the phone width (principle: Funding rows)."""
    errors = []
    for width in (641, 700, 760):
        page = browser.new_page(viewport={"width": width, "height": 900})
        page.route(base_url, lambda route: route.fulfill(response=(r := route.fetch()), body=r.text().replace("</head>", "<style>html { font-size: 125% !important; }</style></head>", 1)))
        hydrated(page, base_url)
        got = page.evaluate("""() => {
          const rem = parseFloat(getComputedStyle(document.documentElement).fontSize), list = document.getElementById('fund-rows'), edge = list.getBoundingClientRect().right;
          const fits = list.getBoundingClientRect().width >= 36.5 * rem;  // the three tracks: 10rem + 12.5rem + 11rem and two 1.5rem gaps
          const rows = [...document.querySelectorAll('#fund-rows > .fund-item')], bad = [];
          rows.forEach(row => {
            const j = row.dataset.j, grid = getComputedStyle(row).display === 'grid';
            if ([...row.querySelectorAll('*')].some(n => n.getClientRects().length && n.getBoundingClientRect().right > edge + 1)) bad.push(j + ': overflows its section');
            if (grid !== fits) bad.push(j + (fits ? ': stacked although its tracks fit' : ': keeps its tracks although they do not fit'));
            if (grid) {
              const b = row.querySelector('.fund-budget').getBoundingClientRect(), s = row.querySelector('.fund-staff').getBoundingClientRect(), i = row.querySelector('.fund-identity').getBoundingClientRect();
              if (i.right > b.left + 1 || b.right > s.left + 1) bad.push(j + ': budget and staff tracks overlap');
            } else {
              const left = row.getBoundingClientRect().left;
              if ([...row.querySelectorAll('.fund-budget-label, .fund-budget > .fund-value, .budget-qualifiers, .fund-staff > .label, .fund-staff > .fund-value, .fund-staff > .fund-context')].filter(n => n.getClientRects().length).some(n => Math.abs(n.getBoundingClientRect().left - left) > 1)) bad.push(j + ': stacked row lacks one left edge');
            }
          });
          return { rem, count: rows.length, bad };
        }""")
        if got["rem"] != 20:
            errors.append(f"{width}px: the large-text check ran at a {got['rem']}px root font, not 125% (20px)")
        if not got["count"]:
            errors.append(f"{width}px, text 125%: no funding rows to check")
        errors += [f"{width}px, text 125%: funding row {e}" for e in got["bad"]]
        page.close()
    return errors


def check_funding_edges(browser, base_url: str, data: dict) -> list[str]:
    """Serve an in-memory subset of real records; never alter the source dataset."""
    fixture = copy.deepcopy(data)
    fixture["institutes"] = [
        institute
        for institute in fixture["institutes"]
        if any(
            f.get("eur_per_year_est") is not None
            and f["kind"] in ("annual", "multi_year")
            for f in institute.get("funding", [])
        )
    ]
    chosen = {}
    for index, institute in enumerate(fixture["institutes"]):
        funding = next(
            f
            for f in institute["funding"]
            if f.get("eur_per_year_est") is not None
            and f["kind"] in ("annual", "multi_year")
        )
        funding["commitment"] = ("adopted", "announced")[index % 2]  # proposals never make a bar
        funding["coverage"] = "whole_institute"
        funding["annual_period"] = {"start": "2026-01-01", "end": "2026-12-31"}
        funding.pop("decision_date", None)
        institute["funding"] = [funding]
        chosen[institute["jurisdiction_id"]] = funding["id"]
        if index:
            funding["eur_per_year_est"] = 1  # Must remain a bar, not a checkbox.
            funding["annual_basis_code"] = "period_mean"
        for staff in institute.get("staff", []):
            # Exercise the owner's missing-date case even though the schema requires dates.
            staff.pop("as_of", None)
            staff.pop("description", None)
    expected = {
        field: sum(
            any(staff.get(field) is not None for staff in i.get("staff", []))
            for i in fixture["institutes"]
        )
        for field in ("total", "technical")
    }
    expected["both"] = sum(all(any(staff.get(field) is not None for staff in i.get("staff", [])) for field in ("total", "technical")) for i in fixture["institutes"])
    if not all(expected.values()):
        return [
            "funding fixture needs comparable institutes with total and technical staff records"
        ]
    errors = []
    page_errors: list[Exception] = []
    page = browser.new_page(viewport={"width": 390, "height": 1000})
    page.on("pageerror", lambda e, errs=page_errors: errs.append(e))
    refresh_fixture_selections(fixture)
    page.route(PAGE_DATA, lambda route: route.fulfill(json=fixture))
    hydrated(page, base_url)
    page.wait_for_selector("#fund-rows > .fund-item")
    if page.locator("#fund-none").is_visible():
        errors.append("funding fixture: empty Not published line is visible")
    latest = page.locator(".fund-staff .fund-context").filter(has_text="Latest total")
    if latest.count() != expected["both"]:  # the label only beside a technical figure (principle: Values and qualifiers)
        errors.append(f"funding fixture: {latest.count()} 'Latest total' labels, expected {expected['both']} (one per row with a technical figure beside the total)")
    grade = r"\s*·\s*(?:indirect or secondary|secondary sources|press report|our estimate)"
    for text in latest.all_text_contents():
        rest = re.sub(grade, "", " ".join(text.split())).strip()
        if not rest or "·" in rest:
            errors.append(f"funding fixture: undated latest total label is empty or has dangling punctuation ({text!r})")
    technical = page.locator(".fund-staff .fund-context").filter(has_text="technical")
    if technical.count() != expected["technical"]:
        errors.append("funding fixture: technical staff records are missing")
    if any("·" in re.sub(grade, "", text) for text in technical.all_text_contents()):
        errors.append(
            "funding fixture: undated technical count has dangling punctuation"
        )
    records = page.locator("[data-record]").all_text_contents()
    if not records or any(
        re.search(r"\b(?:undefined|NaN)\b|^\s*,", text) for text in records
    ):
        errors.append(
            "funding fixture: missing descriptions or dates produce invalid text"
        )
    errors.extend(check_design_roles(page))
    errors.extend(check_funding_bars(page, fixture, chosen))
    errors.extend(f"funding fixture JavaScript error: {error}" for error in page_errors)
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


def check_development_edges(browser, base_url: str, data: dict) -> list[str]:
    """Exercise short text and closure events without a current institute card."""
    fixture = copy.deepcopy(data)
    jurisdiction = next(
        j
        for j in fixture["jurisdictions"]
        if j["status"] == "none_identified" and not j.get("institute_id")
    )
    common = {
        "date": fixture["dataset"]["as_of"],
        "source_ids": jurisdiction["source_ids"][:1],
        "citations": {field: jurisdiction["source_ids"][:1] for field in ("date", "event", "description", "short")},
    }
    jurisdiction["timeline"] = [
        {
            **common,
            "id": "fixture-closed",
            "event": "closed",
            "short": "Fixture short summary.",
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
        page = browser.new_page(viewport={"width": width, "height": 1000})
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
        errors.extend(check_source_alignment(page.content(), fixture))
        errors.extend(check_wrapped_rows(page))
        errors.extend(page_errors)
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
    # Mypy cannot infer the overloaded callback with a bound default.
    page.on("pageerror", lambda e, errs=page_errors: errs.append(e))  # type: ignore[call-overload]

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
    page.wait_for_timeout(100)
    require(
        page.locator("#figures").is_visible(),
        "figures row is hidden",
    )
    lap("load")
    for error in check_hit_targets(page):
        require(False, error)
    for check in (
        check_typography,
        check_baselines_and_rhythm,
        check_citations,
        check_design_roles,
        check_source_footers,
        check_meta_separators,
        check_line_end_separators,
        check_card_grid,
        check_external_markers,
        check_status_contrast,
        check_status_states,
        check_tooltip_cues,
    ):
        for error in check(page):
            require(False, error)
    lap("layout and style")
    for error in check_source_keyboard(page):
        require(False, error)
    for error in check_wrapped_rows(page):
        require(False, "wrapped rows: " + error)
    for error in check_unbroken_dates(page):
        require(False, error)
    for error in check_fold_block_gaps(page):
        require(False, error)
    for error in check_value_qualifiers(page):
        require(False, error)
    for check in (check_value_column, check_repeated_spacing):
        for error in check(page):
            require(False, error)
    if width in (390, 1000):
        for error in check_meta_separators(page, force_wrap=True):
            errors.append(f"{width}px forced wrap: {error}")
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
    for error in check_archive_wrap(page) + check_nonidentified_style(page) + check_name_roles(page):
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
    for name_sel in ("#world-rows td:first-child a[data-tip]", "#world-rows td:first-child strong[data-tip]"):
        name = page.locator(name_sel).first
        if not name.count():
            require(False, f"no state name tooltip for {name_sel}")
            continue
        page.mouse.move(0, 0)  # a pointer left over the page would hover whatever scrolls under it
        name.focus()
        require(
            page.evaluate("s => { const n = document.querySelector(s), t = document.getElementById('tip'); return n === document.activeElement && t && !t.hidden && t.textContent === n.dataset.tip && !!n.getAttribute('aria-description'); }", name_sel),
            f"state summary tooltip not shown on keyboard focus: {name_sel}",
        )
        page.keyboard.press("Escape")
        require(page.evaluate("() => document.getElementById('tip').hidden"), "Escape does not dismiss the state summary tooltip")
        page.evaluate("document.activeElement.blur()")
    page.locator("#world-status").select_option("pos")
    page.evaluate("([x, y]) => scrollTo(x, y)", scroll)
    page.wait_for_timeout(300)
    page.evaluate("scrollTo(0, 0)")  # the nav strip follows the section in view; at the top it starts at its first item
    page.wait_for_timeout(300)
    require(
        page.evaluate(
            "() => !document.body.textContent.includes('\\u2197') && ![...document.querySelectorAll('[data-tip], [data-tip-sub], [data-exp]')].some(n => Object.values(n.dataset).some(v => v.includes('\\u2197')))"
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
    for error in check_activity_width(page) + check_activity_tooltip(page):
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
    page.wait_for_timeout(150)
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
        page.wait_for_timeout(400)
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
    if page.locator("#development-list > li.earlier").count():
        more = page.locator("#developments-more")
        n = page.locator("#development-list > li.earlier").count()
        # A list extension stays after the list's last visible item and extends it in place.
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
    with page.expect_download() as event_download:
        page.locator("#events-download").click()
    require(
        Path(event_download.value.path()).read_bytes()
        == (dist / "data" / "events.csv").read_bytes(),
        "events download differs from the built events.csv",
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
        page.wait_for_timeout(100)
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
    page.wait_for_timeout(100)
    require(
        page.locator('.secnav a[data-sections~="method"]').get_attribute("aria-current")
        == "location",
        "last section is not active at page bottom",
    )
    page.locator('.secnav a[href="#europe"]').evaluate(
        "e => e.click()"
    )
    page.wait_for_timeout(100)
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
    require(
        page.locator(".fund-detail").evaluate_all(
            "folds => folds.every(f => { const row = f.closest('.fund-item'), before = getComputedStyle(row).display === 'flex' ? row.querySelector('.fund-staff') : f.previousElementSibling; return f.getBoundingClientRect().top - before.getBoundingClientRect().bottom < 32; })"
        ),
        "funding disclosure is separated from the content before it (the budget, or Staff where the row stacks)",
    )

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
        for error in check_operating_output(page.content(), data):
            require(False, error)
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
    # The hint "Scroll sideways" shows exactly when the table overflows.
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
            page.locator("#fund-rows > .fund-item").count()
            + page.locator("#fund-none li[data-j]").count()
            == len(data["institutes"]),
            "funding section omits an institute",
        )

        for record_type in ("funding", "staff"):
            # Institutes on the Not published line keep their records on their cards, not in a fold.
            expected = sum(
                len(i.get(record_type, []))
                for i in data["institutes"]
                if i["jurisdiction_id"] not in funding_groups(data)[2]
            )
            require(
                page.locator(f"[data-record={record_type}]").count()
                == expected,
                f"funding folds lost {record_type} records",
            )
    lap("filters, menus, CSV, glossary")
    # Check all controls revealed by disclosures.
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
        *((check_typography, check_baselines_and_rhythm, check_citations) if theme == "light" else ()),
        check_design_roles,
        check_source_footers,  # compares each footer's colour with the theme's --muted, so in both themes
        *((check_meta_separators, check_card_grid) if theme == "light" else ()),
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
                check_typography,
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
        page.locator("th:visible").evaluate_all(
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
    page.on("pageerror", lambda e, errs=page_errors: errs.append(e))  # type: ignore[call-overload]
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
        page.locator('.secnav a[href="#testing"]').focus(timeout=5_000)
        page.keyboard.press("Enter")
        settle(page, "document.activeElement.id === 'testing'")
        # The tint is read at the start of its fade, paused there: sampled later, a slow run would see it faded.
        got = page.evaluate("() => {" + CONTRAST_JS + PIN_FOCUS_FADE + " const n = document.activeElement, faded = pinFade(n), c = getComputedStyle(n); return [n.id, n.tagName, c.outlineStyle, rgb(c.backgroundColor).join() === rgb(token('--highlight-fill')).join(), faded]; }")
        if got != ["testing", "H2", "none", True, True]:
            errors.append(f"{scheme}: after a jump to Testing the focused heading is {got} (expected the #testing h2 with the tint, no outline and its fade)")
        # The palette's Definition destination is a paragraph, not a control: the same tint, no ring (rule 14).
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


def table_sorting(browser, base_url: str, data: dict) -> list[str]:
    sort_page = browser.new_page(viewport={"width": 1440, "height": 1000})
    hydrated(sort_page, base_url)
    sort_page.wait_for_selector("#world-rows tr")
    sort_page.locator("#world-status").select_option("")
    errors = check_table_sorting(sort_page, data=data)
    sort_page.close()
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


def browser_jobs(dist: Path, data: dict, screenshots: Path | None) -> list[tuple]:
    """Every browser check as (stage, check, arguments after browser and base URL), in the order its errors are
    reported. No job depends on another: each opens its own pages."""
    passes = [(interaction_pass, (dist, data, screenshots, width, theme)) for width in (1440, 1000, 390) for theme in ("light", "dark")]
    return [
        ("hydration", hydration_paint, ()),
        ("hydration", hydration_edges, (dist,)),
        ("origins", check_origins, (dist,)),
        ("origins", check_first_load, ()),
        ("interactions", check_recovery_page, (dist, screenshots)),
        *(("interactions", check, args) for check, args in passes),
        ("interactions", shared_links, ()),
        ("interactions", check_development_edges, (data,)),
        ("interactions", check_access_edges, (data,)),
        ("interactions", check_funding_edges, (data,)),
        ("interactions", check_funding_large_text, ()),
        ("interactions", check_published_amount_fallback, (data,)),
        ("interactions", check_menu_touch, ()),
        ("interactions", check_arrival, ()),
        ("interactions", partial(prefixed, "access: ", check_access_fixes), ()),
        ("interactions", partial(prefixed, "group rows: ", check_group_rows), ()),
        ("interactions", partial(prefixed, "touch tips: ", check_touch_tips), ()),
        ("interactions", check_theme_default, (dist,)),
        ("interactions", check_wide_layout, ()),
        ("interactions", partial(prefixed, "measure: ", check_text_measure), ()),
        ("interactions", check_none_identified_tips, (data,)),
        ("interactions", check_theme_parity, ()),
        ("interactions", check_highlight_fill, ()),
        ("interactions", check_toggle_place, ()),
        ("interactions", check_offscreen_sections, ()),
        ("interactions", check_interaction_times, ()),
        ("interactions", check_command_palette, ()),
        ("interactions", check_anchor_aliases, ()),
        ("interactions", check_format_edges, (data,)),
        ("interactions", check_operating_edges, (data,)),
        ("interactions", table_sorting, (data,)),
        ("interactions", check_card_grid_edges, (data,)),
    ]


# Jobs that start before the others: hydration, which every later stage waits on, then the longest (about 15 to
# 35 s each on an M3 Max), longest first, so the parallel run ends soon after the slowest one.
FIRST = (hydration_paint, hydration_edges, check_touch_tips, check_arrival, interaction_pass, check_group_rows, check_wide_layout)


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


def quarantine_findings(entry: dict | None, findings: list[str]) -> list[str]:
    """A quarantined job's findings as report lines naming the entry; any other job's unchanged."""
    if entry is None:
        return findings
    prefix = f"quarantined until {entry['expires']} (owner {entry['owner']}, {entry['issue']}): "
    return [report_line(prefix + finding.replace(REPORT_MARK, "")) for finding in findings]


def quarantined_job(run, quarantine: dict[str, dict], stage: str, check, args: tuple) -> list[str]:
    # Exceptions still raise and block, including for quarantined jobs: only returned findings become report lines.
    return quarantine_findings(quarantine.get(job_id(stage, check, args)), run(stage, check, args))


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
        ended.update(errors=len(blocking(errors)), exception=None)  # report lines are not errors
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
    for line in text.splitlines():
        try:
            lines.append(json.loads(line))
        except ValueError:
            pass  # a line cut short when its worker died
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


def run_stages(run, work: list[tuple], seeds: dict, jobs: int = 1) -> list[str]:
    """Report (stage, check, args) jobs, each run as run(stage, check, args), as the sequential gate does: stages in
    the order they first appear, each only when every earlier one passed; within a stage its seed's errors (a
    function of no arguments), then each job's in list order; the first exception in that order ends the run. Report
    lines are reported in the same order but never stop a stage.

    With jobs > 1 every job starts at once in that many spawned processes (FIRST first) and results are read back
    in list order, so the report and the exception are the same; the results of a stage that is not reached are
    discarded. run and every check must pickle: module-level functions or partials of them.
    """
    stages = list(dict.fromkeys(stage for stage, _, _ in work))
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
            if blocking(errors):
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
    # Timings count raw findings, as shards record them; the quarantine applies only to what is reported.
    run = partial(quarantined_job, partial(timed_job, timings, time.monotonic(), partial(run_browser_job, chrome, dist), limit=limit), quarantine)
    return run_stages(run, work, browser_seeds(dist), jobs)


def all_job_ids() -> list[str]:
    """Every browser job's id, in report order; ids do not depend on the site or the data."""
    return [job_id(*job) for job in browser_jobs(Path("site"), {}, None)]


def job_ids(text: str) -> list[str]:
    """--job: comma-separated job ids, each one that exists."""
    wanted, known = list(dict.fromkeys(i.strip() for i in text.split(",") if i.strip())), all_job_ids()  # each id once
    if not wanted or (unknown := [i for i in wanted if i not in known]):
        raise argparse.ArgumentTypeError(f"unknown browser job {', '.join(unknown) if wanted else '(none given)'}; --list-jobs prints them")
    return wanted


def browser_seeds(dist: Path) -> dict:
    return {"interactions": lambda: check_style_source((dist / "index.html").read_text())}


# Sharding: CI splits the browser jobs over several machines (--shard K/N) and one job reads the results back
# (--merge) through the same run_stages, after the static checks. Findings, their order and exit codes are the local
# gate's; an exception from a browser job prints the same text when main() catches its type, and any other failure
# (a dead worker, a missing or foreign shard result) still fails, with its own message.

SHARD_FORMAT = 2  # 2: every shard result carries its jobs' timings
CAUGHT = (OSError, subprocess.SubprocessError, ValueError, RuntimeError)  # what main() reports as "page check failed"

# Seconds per job on GitHub's ubuntu-latest, one job at a time (bench run 40025105295, 24 Oct 2026); shards are
# balanced by them, so they affect only how long each shard takes. A job not listed counts DEFAULT_COST.
JOB_COST = {
    "hydration/hydration_paint": 27.8, "hydration/hydration_edges": 23.8, "origins/check_origins": 2.8, "origins/check_first_load": 6.0,
    "interactions/check_recovery_page": 3.2,
    "interactions/interaction_pass/1440/light": 65.6, "interactions/interaction_pass/1440/dark": 65.6,
    "interactions/interaction_pass/1000/light": 59.4, "interactions/interaction_pass/1000/dark": 59.6,
    "interactions/interaction_pass/390/light": 59.1, "interactions/interaction_pass/390/dark": 57.8,
    "interactions/shared_links": 6.5, "interactions/check_development_edges": 22.4, "interactions/check_access_edges": 8.8,
    "interactions/check_funding_edges": 2.9, "interactions/check_funding_large_text": 4.0, "interactions/check_published_amount_fallback": 2.8,
    "interactions/check_menu_touch": 2.9, "interactions/check_arrival": 45.8, "interactions/check_access_fixes": 14.4,
    "interactions/check_group_rows": 25.1, "interactions/check_touch_tips": 46.2, "interactions/check_theme_default": 10.0,
    "interactions/check_wide_layout": 22.5, "interactions/check_text_measure": 7.7,
    "interactions/check_none_identified_tips": 2.9, "interactions/check_theme_parity": 4.0, "interactions/check_highlight_fill": 4.0, "interactions/check_toggle_place": 4.0, "interactions/check_anchor_aliases": 6.0,
    "interactions/check_format_edges": 9.3, "interactions/check_operating_edges": 6.0, "interactions/table_sorting": 3.9,
    "interactions/check_card_grid_edges": 7.8,
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


def write_shard(chrome: str, dist: Path, data: dict, shard: tuple[int, int], results: Path, jobs: int) -> int:
    """Run shard K of N and write its results; print its findings as raw diagnostics. Exit 1 when it found any.
    Shards record raw findings: the merge applies the quarantine on its own date, so no date ties shards to it."""
    number, count = shard
    work = browser_jobs(dist, data, None)
    ids = [job_id(*job) for job in work]
    mine = [n for n, s in enumerate(shard_numbers(ids, count)) if s == number]
    results.parent.mkdir(parents=True, exist_ok=True)
    try:  # a new file, so no line of an earlier run can join this one; without one, this shard runs untimed
        handle, name = tempfile.mkstemp(prefix=results.name + ".", suffix=".timings", dir=results.parent)
        os.close(handle)
        timings = Path(name)
    except OSError as exc:
        print(f"warning: browser jobs of shard {number}/{count} not timed: {exc}", file=sys.stderr)
        timings = None
    outcomes = run_shard(partial(timed_job, timings, time.monotonic(), partial(run_browser_job, chrome, dist)), work, mine, jobs)
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
        lines = o["errors"] if "errors" in o else [f"{o['exception']['type']}: {o['exception']['message']}"]
        for line in lines:
            shown = printed_report_line(line) if "errors" in o and is_report_line(line) else line
            print(f"shard {number}/{count} (raw, not the gate's report; the build job merges): {ids[n]}: {shown}", file=sys.stderr)
        found += len(blocking(lines)) if "errors" in o else 1  # an exception always fails, whatever its message says
    print(f"shard {number}/{count}: {len(mine)} browser jobs, {found} findings; results in {results}")
    return 1 if found else 0


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
            if not (errors_ok or exception_ok):
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


def check_merged(dist: Path, data: dict, directory: Path, timings: Path | None = None, quarantine: dict | None = None) -> list[str]:
    """check_browser's report from shard results instead of running the browser jobs; with timings, every shard's
    job lines there, each with its shard (started counts from that shard's start). The quarantine is applied here,
    on the merge's own run date, to the shards' raw findings."""
    work = browser_jobs(dist, data, None)
    lines, ids = [], [job_id(*job) for job in work]
    outcomes = load_shards(directory, dist, ids, lines)
    if timings is not None:
        write_timings(timings, "".join(json.dumps(t) + "\n" for t in lines), "w")
    indexed = [(stage, n, ()) for n, (stage, _, _) in enumerate(work)]
    quarantine = load_quarantine() if quarantine is None else quarantine
    return run_stages(partial(quarantined_record, outcomes, ids, quarantine), indexed, browser_seeds(dist))


def quarantined_record(outcomes: dict[int, dict], ids: list[str], quarantine: dict[str, dict], stage: str, n: int, args: tuple) -> list[str]:
    return quarantine_findings(quarantine.get(ids[n]), recorded(outcomes, stage, n, args))


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
    """data/page.json is aisi.json without a state's research log (check, except last_checked and method) and a
    source's private-copy metadata (snapshot, access_note), minified; the page loads it by a URL carrying the
    dataset version and the file's hash; and the page script reads none of the fields left out."""
    import hashlib

    errors = []
    path = dist / "data/page.json"
    if not path.is_file():
        return ["data/page.json: missing"]
    text = path.read_text(encoding="utf-8")
    want = copy.deepcopy(data)
    for j in want["jurisdictions"]:
        if "check" in j:
            j["check"] = {k: j["check"][k] for k in ("last_checked", "method") if k in j["check"]}
    for source in want["sources"]:
        source.pop("snapshot", None)
        source.pop("access_note", None)
    if json.loads(text) != want:
        errors.append("data/page.json differs from aisi.json without check (except last_checked and method), snapshot and access_note")
    if "\n" in text or ": " in text[:200]:
        errors.append("data/page.json is not minified")
    soup = BeautifulSoup((dist / "index.html").read_text(encoding="utf-8"), "html.parser")
    meta = soup.select('head meta[name="aisi-page-data"]')
    url = f"data/page.json?v={data['dataset']['version']}-{hashlib.sha256(text.encode('utf-8')).hexdigest()[:10]}"
    if len(meta) != 1 or meta[0].get("content") != url:
        errors.append(f"index.html: the page data URL is {[m.get('content') for m in meta]}, expected {url}")
    package = (dist / "data/datapackage.json").read_text(encoding="utf-8")
    if len(meta) == 1 and meta[0].get("data-package") != f"data/datapackage.json?v={data['dataset']['version']}-{hashlib.sha256(package.encode('utf-8')).hexdigest()[:10]}":
        errors.append(f"index.html: the data package URL {meta[0].get('data-package')!r} is not versioned by its content")
    script = "\n".join(node.get_text() for node in soup.select("body > script:not([src])"))
    for pattern in (r"\.snapshot\b", r"\baccess_note\b", r"\.check\.(?!last_checked\b|method\b)\w+", r"\.check\[", r"\bcheck\)"):
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
        print(
            f"NOT RUN: Playwright's Chromium is not installed ({INSTALL_CHROME}); rendered page checks were not run.",
            file=sys.stderr,
        )
        return NOT_RUN
    try:
        if not (dist / "index.html").is_file():
            raise ValueError(
                f"{dist} has no index.html; build first: uv run scripts/build.py --out {dist}"
            )
        if capture:
            capture_states(chrome, dist, capture)
            print(f"captured fixed page states in {capture}")
            return 0
        data = json.loads((dist / "data/aisi.json").read_text(encoding="utf-8"))
        if shard:
            return write_shard(chrome, dist, data, shard, results, jobs)
        # --skip-static (the flake detector's runs only): no render and no static checks, just the selected job; the
        # reserved-mark scan of the built files still runs, as it must before any finding is read.
        html, base_url = ("", None) if skip_static else render(chrome, dist)
        if reserved := check_reserved_mark(dist, html):  # fails outright: a finding quoting such text could pass for a report line
            print(f"error: {reserved[0]}", file=sys.stderr)
            return 1
        errors = [] if skip_static else check_method_pages(html, data, dist) + check_dom(html, data, dist, base_url) + check_agent_files(dist) + check_static_page(dist, data) + check_hydrated(html) + check_archive_links(html, data) + check_launch_assets(dist) + check_page_data(dist, data) + check_budget_ratio_downloads(dist, data) + check_state_downloads(dist, data) + check_state_download_buttons(html, dist, data)
        if not blocking(errors):
            errors.extend(check_merged(dist, data, merge, timings, quarantine) if merge else check_browser(chrome, dist, data, screenshots, jobs, timings, only, quarantine, job_limit(deadline)))
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
    for line in errors:
        if is_report_line(line):
            print(printed_report_line(line))  # stdout, so check.py's report lists it
    if blocking(errors):
        for error in blocking(errors):
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


def default_jobs(environ=os.environ) -> int:
    """Browser jobs at once when --jobs is not given: AISI_GATE_JOBS where a host sets it (1 where its memory
    holds one browser at a time), else the CPUs, at most 8."""
    if text := environ.get("AISI_GATE_JOBS", "").strip():
        try:
            return job_count(text)
        except (ValueError, argparse.ArgumentTypeError) as exc:
            raise SystemExit(f"AISI_GATE_JOBS={text!r}: {exc}") from exc
    return min(os.cpu_count() or 1, 8)


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
        help="browser checks in this many parallel processes (default: AISI_GATE_JOBS, else the CPUs, at most 8); 1 runs them one after another",
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
    parser.add_argument("--no-quarantine", action="store_true", help="report quarantined jobs' findings as errors (the flake detector measures them so)")
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
    if args.install_chrome:
        sys.exit(install_chrome())
    if args.find_chrome:
        found = find_chrome()
        print(found or "")
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
