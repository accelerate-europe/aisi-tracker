# /// script
# requires-python = ">=3.11"
# dependencies = ["beautifulsoup4>=4.12", "playwright==1.63.0"]
# ///
"""Check a transformed preview under a project-site prefix, without building it.

Run: uv run --locked scripts/check_preview.py --dist DIR --shots DIR
The lock is identical to check_page.py's: the same dependencies and Chromium.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import tempfile
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

from browser_lock import browser_slot
from check_page import LOCALE, NOT_RUN, browser_env, chrome_problem, find_chrome
from preview import NOTICE, RELEASE_NOTE, REPLACED, replaced_selector, SOURCE_REPO, data_date, frozen_url, index_html, not_found_html, read_json, vienna

PREFIX = "/aisi-tracker-preview/"
PAGES = ("index.html", "method.html", "api/v1/index.html")
ROOT_REFERENCE = re.compile(r'''(?:href|src)\s*=\s*["']/|fetch\(\s*["']/|url\(\s*["']?/''', re.I)


def assumed_root(dist: Path) -> list[str]:
    return [f"{path.relative_to(dist)}:{text.count(chr(10), 0, match.start()) + 1}: {match.group()}"
            for path in sorted(dist.rglob("*")) if path.suffix in {".html", ".css", ".js"}
            for text in [path.read_text(encoding="utf-8")]
            for match in ROOT_REFERENCE.finditer(text)]


class Handler(SimpleHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def send_error(self, code, message=None, explain=None):
        if code != 404:
            return super().send_error(code, message, explain)
        body = (Path(self.directory) / PREFIX.strip("/") / "404.html").read_bytes()
        self.send_response(404)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


# The left edge of the header's content below the band: the site's header column, else the page's main column.
HEADER_LEFT = """() => {
  const box = document.querySelector('.masthead .measure') || document.querySelector('main');
  const rect = box.getBoundingClientRect();
  return rect.left + parseFloat(getComputedStyle(box).paddingLeft);
}"""


def check_band_layout(page, width: int) -> None:
    """Closed: one line at desktop width, label and actions on one line at phone width, height limits, aligned."""
    band = page.locator("body > .preview-banner")
    tops = {name: page.locator(f"body > .preview-banner .{name}").bounding_box()["y"] for name in ("pv-title", "pv-id", "pv-actions")}
    height = band.bounding_box()["height"]
    if width >= 1000:
        assert max(tops.values()) - min(tops.values()) <= 2, f"band is not one line at {width}px: {tops}"
        assert height <= 48, f"closed band is {height:.0f}px high at {width}px"
    else:
        assert abs(tops["pv-title"] - tops["pv-actions"]) <= 2, f"label and actions not on one line at {width}px: {tops}"
        assert tops["pv-id"] > max(tops["pv-title"], tops["pv-actions"]) + 5, f"identity is not on the second line at {width}px: {tops}"
        assert height <= 72, f"closed band is {height:.0f}px high at {width}px"
    # A long work-branch name keeps the same limits: it is cut, not wrapped.
    if not getattr(check_band_layout, "long_branch", False):
        branch = page.locator("body > .preview-banner .pv-row .pv-branch")
        original = branch.inner_text()
        branch.evaluate("e => { e.textContent = 'work/preview-banner-layout-and-accessibility-with-a-very-long-branch-name'; }")
        try:
            check_band_layout.long_branch = True
            check_band_layout(page, width)
        finally:
            check_band_layout.long_branch = False
            branch.evaluate("(e, text) => { e.textContent = text; }", original)
    left = page.locator("body > .preview-banner .pv-row").bounding_box()["x"]
    header = page.evaluate(HEADER_LEFT)
    assert abs(left - header) <= 1, f"band content starts at {left:.1f}px, the header at {header:.1f}px ({width}px)"


def check_details_keyboard(page) -> None:
    toggle = page.locator("body > .preview-banner .pv-toggle")
    details = page.locator("body > .preview-banner .pv-details")
    assert toggle.get_attribute("aria-expanded") == "false" and not details.is_visible(), "Details is open by default"
    toggle.focus()
    page.keyboard.press("Enter")
    assert toggle.get_attribute("aria-expanded") == "true" and details.is_visible(), "Enter does not open Details"
    assert details.locator("li").count() == 4
    page.keyboard.press("Space")
    assert toggle.get_attribute("aria-expanded") == "false" and not details.is_visible(), "Space does not close Details"


def check_marker(page, width: int) -> None:
    """Hidden while the band is in view; after scrolling, one short line in the corner with the band's commit, clear of
    fixed controls and of the focused element; a click brings the band back with Details open."""
    band = page.locator("body > .preview-banner")
    marker = band.locator(".pv-marker")
    page.evaluate("window.scrollTo(0, 0)")
    page.wait_for_function("document.querySelector('body > .preview-banner .pv-marker').hidden")
    assert not marker.is_visible(), "the marker shows while the band is in view"
    room = page.evaluate("document.documentElement.scrollHeight - innerHeight - document.querySelector('body > .preview-banner').getBoundingClientRect().bottom")
    if room <= 0:
        return  # a page shorter than the window never hides the band, so the marker never shows
    # Past the band, as far as the page allows (a short page may scroll only just beyond it).
    page.evaluate("window.scrollTo(0, Math.min(document.documentElement.scrollHeight - innerHeight, Math.max((document.documentElement.scrollHeight - innerHeight) / 2, document.querySelector('body > .preview-banner').getBoundingClientRect().bottom + scrollY + 1)))")
    page.wait_for_function("!document.querySelector('body > .preview-banner .pv-marker').hidden")
    text = " ".join(marker.inner_text().split())
    branch, commit = band.locator(".pv-row .pv-branch").inner_text(), band.locator(".pv-build").inner_text().split("·")[1].strip()
    assert text.startswith("Preview · ") and text.endswith(" · " + commit), f"marker reads {text!r}"
    shown = marker.locator(".pv-branch").inner_text()
    assert shown == branch, f"marker branch {shown!r}, band {branch!r}"
    box = marker.bounding_box()
    assert 22 <= box["height"] <= 30, f"marker is {box['height']:.0f}px high at {width}px"
    assert box["width"] <= min(0.6 * width, 320) + 1, f"marker is {box['width']:.0f}px wide at {width}px"
    assert box["x"] + box["width"] <= width and box["y"] + box["height"] <= page.viewport_size["height"], "marker leaves the window"
    others = page.evaluate("""() => [...document.querySelectorAll('body *')].filter(e => !e.closest('.pv-marker') && e.getClientRects().length
        && (getComputedStyle(e).position === 'fixed' || e.matches('.theme-toggle'))).map(e => { const r = e.getBoundingClientRect(); return [e.id || e.className, r.left, r.top, r.right, r.bottom]; })""")
    for name, left, top, right, bottom in others:
        assert right <= box["x"] or left >= box["x"] + box["width"] or bottom <= box["y"] or top >= box["y"] + box["height"], f"marker overlaps {name!r}"
    # A focused control under the marker: the marker steps aside, and comes back when focus leaves.
    page.evaluate("""m => { const r = m.getBoundingClientRect(), b = document.createElement('button');
        b.id = 'pv-probe'; b.textContent = 'probe'; Object.assign(b.style, {position: 'fixed', left: r.left + 'px', top: r.top + 'px', width: r.width + 'px', height: r.height + 'px'});
        document.body.append(b); b.focus(); }""", marker.element_handle())
    assert not marker.is_visible(), "the marker covers the focused element"
    page.evaluate("document.getElementById('pv-probe').remove(); document.activeElement.blur && document.activeElement.blur()")
    page.wait_for_function("!document.querySelector('body > .preview-banner .pv-marker').classList.contains('pv-away')")
    marker.click()
    page.wait_for_function("document.querySelector('body > .preview-banner').getBoundingClientRect().top >= -1")
    toggle = band.locator(".pv-toggle")
    assert toggle.get_attribute("aria-expanded") == "true" and band.locator(".pv-details").is_visible(), "the marker does not open Details"
    assert page.evaluate("document.activeElement === document.querySelector('body > .preview-banner .pv-toggle')"), "focus does not move to Details"
    page.wait_for_function("document.querySelector('body > .preview-banner .pv-marker').hidden")
    toggle.click()


def check_banner(page, meta: dict, logical: str, expected_frozen: str) -> None:
    banner = page.locator("body > .preview-banner")
    assert banner.count() == 1, "expected exactly one visible banner"
    assert banner.is_visible(), "banner is hidden"
    assert "preview-banner" in page.locator("body > :first-child").get_attribute("class").split()
    row = " ".join(banner.locator(".pv-row").inner_text().split())
    identity = f"{meta['branch']} · {meta['commit'][:10]} · {vienna(meta['built'])}"
    assert row == f"Preview, not published {identity} Feedback Details", f"band line reads {row!r}"
    details = [" ".join(t.split()) for t in banner.locator(".pv-details li").all_text_contents()]
    assert details == ["Data as of " + data_date(meta["as_of"]), "CI run for this build", "Link to this exact build", "Published site"], details
    assert re.search(r", \d\d:\d\d CES?T$", identity), f"build time without its zone: {identity!r}"
    assert banner.locator('a', has_text="CI run").get_attribute("href") == meta["run_url"]
    assert banner.locator(".pv-id a").count() == 0, "the commit is plain text"
    if page.locator("#updated").count():
        note = page.locator("#updated + .pv-chrome .pv-note")
        assert note.is_visible() and note.inner_text() == RELEASE_NOTE, "the release note is missing beside the version line"
    mail = banner.locator('a[href^="mailto:"]').get_attribute("href")
    from urllib.parse import parse_qs
    assert parse_qs(urlsplit(mail).query)["subject"] == [f"AISI preview {meta['branch']} {meta['commit'][:10]}"]
    # The capture listener updates href before this handler cancels navigation.
    exact = banner.locator("[data-preview-frozen]")
    exact.evaluate("link => { link.addEventListener('click', e => e.preventDefault(), {once: true}); link.click(); }")
    assert exact.get_attribute("href") == expected_frozen
    assert page.locator('meta[name="robots"]').get_attribute("content") == "noindex, nofollow"
    assert page.locator('link[rel="canonical"]').count() == 0
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), "horizontal overflow"
    assert banner.evaluate("e => getComputedStyle(e).position !== 'fixed'"), "banner covers content"
    if logical == "index.html":
        assert page.locator("#cite").inner_text() == NOTICE, "citation changed during hydration"


def check_links(context, urls: set[str], folder: str, requests: list[dict]) -> None:
    fetched = set()
    for url in sorted(urls):
        if urlsplit(url).scheme not in {"http", "https"} or urlsplit(url).netloc != urlsplit(folder).netloc:
            continue
        assert urlsplit(url).path.startswith(urlsplit(folder).path), f"link escapes build folder: {url}"
        # Different fragment links share one HTTP resource; check every resolution, fetch each file once.
        resource = urlsplit(url)._replace(fragment="").geturl()
        if resource in fetched:
            continue
        fetched.add(resource)
        requests.append({"page": folder, "url": resource, "kind": "link check"})
        response = context.request.get(resource)
        assert response.status == 200, f"link returned {response.status}: {url}"
        assert urlsplit(response.url).path.startswith(urlsplit(folder).path), f"redirect escapes folder: {url}"


def watch_requests(page, folder: str, requests: list[dict], errors: list[str], blocked: bool = False) -> None:
    def requested(request):
        requests.append({"page": page.url, "url": request.url, "blocked": blocked})
        if not request.url.startswith(folder):
            errors.append("request escapes build folder: " + request.url)

    def failed(request):
        if not (blocked and urlsplit(request.url).path.endswith("/data/page.json")):
            errors.append("failed request: " + request.url)

    def console(message):
        if message.type == "error" and not (blocked and ("net::ERR_FAILED" in message.text or message.text.startswith("Could not render institute tracker"))):
            errors.append(message.text)

    page.on("request", requested)
    page.on("requestfailed", failed)
    page.on("response", lambda response: errors.append(f"HTTP {response.status}: {response.url}") if response.status >= 400 else None)
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.on("console", console)


def check_page(browser, base: str, placement: str, logical: str, meta: dict, width: int, theme: str,
               shots: Path, requests: list[dict], blocked: bool = False) -> None:
    folder = base + placement + "/"
    url = folder + logical + ("#c-fr" if logical == "index.html" else "#preview-check")
    errors, held, links = [], [], set()
    with browser.new_context(viewport={"width": width, "height": 900}, color_scheme=theme, locale=LOCALE) as context:
        page = context.new_page()
        page.set_default_timeout(90_000)  # a shared host under load; a stuck page still fails

        watch_requests(page, folder, requests, errors, blocked)
        if logical == "index.html":
            page.route("**/data/page.json*", lambda route: route.abort() if blocked else held.append(route))
        page.goto(url, wait_until="domcontentloaded")
        if logical == "index.html" and not blocked:
            page.wait_for_function("document.documentElement.hasAttribute('data-prerendered')")
        expected = frozen_url(url, logical, meta["commit"])
        check_banner(page, meta, logical, expected)
        check_band_layout(page, width)
        label = f"{placement.replace('/', '-')}-{logical.replace('/', '-')}-{width}-{theme}"
        page.locator("body > .preview-banner").screenshot(path=str(shots / f"{label}-{'blocked' if blocked else 'before'}.png"))
        if logical == "index.html" and not blocked:
            assert held, "page data request was not observed"
            for route in held:
                route.continue_()
            page.wait_for_function("!document.documentElement.hasAttribute('data-prerendered')")
        page.wait_for_load_state("networkidle")
        check_banner(page, meta, logical, expected)
        check_band_layout(page, width)
        check_details_keyboard(page)
        check_marker(page, width)
        if logical == "index.html":
            card = page.locator("#c-fr")
            assert card.is_visible(), "#c-fr did not reveal its card"
            assert card.evaluate("e => { for (let n = e; n; n = n.parentElement) { if (n.tagName === 'DETAILS' && !n.open) return false; } return true; }"), "#c-fr remains in a closed disclosure"
            if blocked:
                assert page.locator("html[data-prerendered]").count() == 1
        page.locator("body > .preview-banner").screenshot(path=str(shots / f"{label}-{'blocked-after' if blocked else 'after'}.png"))
        for attrs in page.locator("[href], [src]").evaluate_all("nodes => nodes.filter(n => !n.closest('.preview-banner')).map(n => [n.getAttribute('href'), n.getAttribute('src')])"):
            for attr in attrs:
                if attr:
                    links.add(urljoin(url, attr))
        # Include template links, which are not part of document.querySelectorAll before hydration.
        soup = BeautifulSoup(page.content(), "html.parser")
        for node in soup.find_all(lambda tag: tag.has_attr("href") or tag.has_attr("src")):
            if not node.find_parent(class_="preview-banner"):
                for attr in ("href", "src"):
                    if node.get(attr):
                        links.add(urljoin(url, node[attr]))
        package_url = folder + "data/datapackage.json"
        requests.append({"page": url, "url": package_url, "kind": "package check"})
        response = context.request.get(package_url)
        assert response.status == 200
        for resource in response.json()["resources"]:
            paths = resource["path"] if isinstance(resource["path"], list) else [resource["path"]]
            links.update(urljoin(package_url, path) for path in paths)
        check_links(context, links, folder, requests)
        assert not errors, "\n".join(errors)


# Rendered text outside preview chrome, one entry per block element: what must equal the main build's page.
PAGE_TEXT = """replaced => {
  const BLOCK = 'p, h1, h2, h3, h4, h5, h6, li, dt, dd, td, th, summary, figcaption, blockquote, pre, div, section, header, footer, main, nav, aside, button, label';
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT, { acceptNode(node) {
    const el = node.parentElement;
    if (!el || el.closest('.pv-chrome, template, script, style, noscript' + replaced)) return NodeFilter.FILTER_REJECT;
    return el.getClientRects().length ? NodeFilter.FILTER_ACCEPT : NodeFilter.FILTER_REJECT;
  } });
  const blocks = []; let block = null;
  for (let node = walker.nextNode(); node; node = walker.nextNode()) {
    const owner = node.parentElement.closest(BLOCK);
    if (owner !== block || !blocks.length) { blocks.push(''); block = owner; }
    blocks[blocks.length - 1] += node.data;
  }
  const text = blocks.map(b => b.replace(/\\s+/g, ' ').trim()).filter(Boolean);
  // What a reader meets besides text: the page title, its description, link targets and accessible labels.
  const description = document.querySelector('meta[name="description"]');
  const attrs = ['@title ' + document.title, '@description ' + (description ? description.content : '')];
  // Each attribute with its element and that element's own text, in document order, so a link or label that moves to
  // other text is a difference too.
  for (const el of document.body.querySelectorAll('[href], [src], [title], [alt], [aria-label]')) {
    if (el.closest('.pv-chrome, template' + replaced)) continue;
    const own = (el.textContent || '').replace(/\\s+/g, ' ').trim().slice(0, 80);
    for (const name of ['href', 'src', 'title', 'alt', 'aria-label']) if (el.hasAttribute(name)) attrs.push('@' + el.tagName.toLowerCase() + ' ' + name + '=' + el.getAttribute(name) + ' | ' + own);
  }
  return text.concat(attrs);
}"""


# Every served file other than the three checked pages: identical to the build's, or listed here with the reason it
# must differ in a preview (verdict b in docs/preview.md). Anything else that differs fails.
CHANGED_FILES = {
    "data/aisi.json": "citation, the dataset DOI and the description marked as an unpublished preview",
    "data/page.json": "the same, read by the page; its cache mark follows",
    "data/datapackage.json": "the dataset DOI (id) removed, preview provenance added",
    "api/v1/index.json": "citation and dataset DOI replaced, preview provenance added",
    "api/v1/jurisdictions.json": "citation and dataset DOI replaced, preview provenance added",
    "api/v1/jurisdictions/*.json": "citation and dataset DOI replaced, preview provenance added",
    "api/v1/institutes/*.json": "citation and dataset DOI replaced, preview provenance added",
    "llms.txt": "citation replaced, live links to the frozen copy, the version sentence says preview",
    "feed.xml": "live links and ids to the frozen copy, title and citation marked",
    "robots.txt": "the live crawl rules replaced by Disallow",
    "sitemap.xml": "removed: it lists live addresses",
    "preview-build.json": "added: the preview's own provenance",
    "404.html": "never served inside a folder; its root links made relative",
}


def check_files(dist: Path, main: Path) -> None:
    from fnmatch import fnmatch

    names = {p.relative_to(root).as_posix() for root in (dist, main) for p in root.rglob("*") if p.is_file()}
    unexpected = []
    for name in sorted(names - set(PAGES)):
        a, b = dist / name, main / name
        if a.is_file() and b.is_file() and a.read_bytes() == b.read_bytes():
            continue
        if not any(fnmatch(name, pattern) for pattern in CHANGED_FILES):
            unexpected.append(name)
    assert not unexpected, "files differ from the build without a listed reason: " + ", ".join(unexpected[:20])


def page_text(browser, url: str, hydrate: bool, blocked: bool, replaced: str = "") -> tuple[list[str], str, str]:
    """The page's text outside preview chrome (and, on the build, outside the elements the preview replaces), its
    version line, and the selector of the elements the preview replaced (preview chrome with an id)."""
    with browser.new_context(viewport={"width": 1280, "height": 900}, locale=LOCALE) as context:
        page = context.new_page()
        page.set_default_timeout(90_000)
        if blocked:
            page.route("**/data/page.json*", lambda route: route.abort())
        page.goto(url, wait_until="networkidle")
        if hydrate and not blocked:
            page.wait_for_function("!document.documentElement.hasAttribute('data-prerendered')")
        line = " ".join(page.locator("#updated").inner_text().split()) if page.locator("#updated").count() else ""
        ids = page.evaluate("[...document.querySelectorAll('.pv-chrome[id]')].map(e => e.id)")
        selector = replaced_selector(ids)  # refuses an id the transform does not replace by design
        for name in ids:  # each replaced element shows exactly the preview's text for it
            shown = " ".join(page.locator(f"#{name}").inner_text().split())
            assert shown == REPLACED[name], f"#{name} shows {shown!r}, expected {REPLACED[name]!r}"
        return page.evaluate(PAGE_TEXT, replaced), line, selector


def self_test(browser) -> None:
    """The comparison's own counterexamples: a link moved to other text, and a label moved to another control."""
    page = browser.new_page()
    try:
        pairs = [('<p><a href="x">A</a><span>B</span></p>', '<p><span>A</span><a href="x">B</a></p>'),
                 ('<button aria-label="Open">Map</button><button>List</button>', '<button>Map</button><button aria-label="Open">List</button>')]
        for build, preview in pairs:
            page.set_content(build)
            want = page.evaluate(PAGE_TEXT, "")
            page.set_content(preview)
            assert page.evaluate(PAGE_TEXT, "") != want, f"the comparison misses a moved attribute: {preview}"
    finally:
        page.close()


def check_same_text(browser, base: str, main: str) -> None:
    """The preview shows the page as the build renders it: outside preview chrome, every page's rendered text equals
    the main build's, after hydration and with the page data blocked, and the version line is the build's own."""
    for logical in PAGES:
        for blocked in ((False, True) if logical == "index.html" else (False,)):
            got, got_line, replaced = page_text(browser, base + "next/" + logical, logical == "index.html", blocked)
            want, want_line, _ = page_text(browser, main + logical, logical == "index.html", blocked, replaced)
            assert got_line == want_line, f"{logical}: the version line reads {got_line!r}, the build's {want_line!r}"
            if got != want:
                first = next((i for i, (a, b) in enumerate(zip(got, want)) if a != b), min(len(got), len(want)))
                raise AssertionError(f"{logical}{' (data blocked)' if blocked else ''}: text outside preview chrome differs from the build at block {first}: "
                                     f"preview {got[first:first + 1]!r}, build {want[first:first + 1]!r}")


def check(dist: Path, shots: Path, chrome: str, main: Path) -> None:
    meta = read_json(dist / "preview-build.json")
    short = meta["commit"][:10]
    roots = assumed_root(dist)
    shots.mkdir(parents=True, exist_ok=True)
    (shots / "assumed-root.json").write_text(json.dumps(roots, indent=2) + "\n", encoding="utf-8")
    assert not roots, "assumed-root references: " + "; ".join(roots)
    requests = []
    with tempfile.TemporaryDirectory(prefix="check-preview-") as tmp:
        root = Path(tmp)
        preview = root / PREFIX.strip("/")
        for placement in ("next", f"b/{short}"):
            shutil.copytree(dist, preview / placement)
        shutil.copytree(main, root / "main-build")
        (preview / "404.html").write_text(not_found_html(), encoding="utf-8")
        (preview / "index.html").write_text(index_html({"folders": {}, "frozen": {}}, set()), encoding="utf-8")
        with ThreadingHTTPServer(("127.0.0.1", 0), partial(Handler, directory=str(root))) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                base = f"http://127.0.0.1:{server.server_port}{PREFIX}"
                with sync_playwright() as playwright:
                    browser = playwright.chromium.launch(executable_path=chrome, env=browser_env())
                    try:
                        check_files(dist, main)
                        self_test(browser)
                        check_same_text(browser, base, f"http://127.0.0.1:{server.server_port}/main-build/")
                        for width in (1280, 390):
                            for theme in ("light", "dark"):
                                for placement in ("next", f"b/{short}"):
                                    for logical in PAGES:
                                        check_page(browser, base, placement, logical, meta, width, theme, shots, requests)
                                    check_page(browser, base, placement, "index.html", meta, width, theme, shots, requests, blocked=True)
                        # Directory URLs must use the same logical path as explicit index.html URLs.
                        with browser.new_context(locale=LOCALE) as context:
                            for placement in ("next", f"b/{short}"):
                                for logical in ("index.html", "api/v1/index.html"):
                                    page = context.new_page()
                                    errors = []
                                    watch_requests(page, base + placement + "/", requests, errors)
                                    url = base + placement + "/" + logical.removesuffix("index.html") + "#c-fr"
                                    page.goto(url, wait_until="networkidle")
                                    exact = page.locator("body > .preview-banner [data-preview-frozen]")
                                    exact.evaluate("link => { link.addEventListener('click', e => e.preventDefault(), {once:true}); link.click(); }")
                                    assert exact.get_attribute("href") == frozen_url(url, logical, meta["commit"])
                                    assert not errors, "\n".join(errors)
                                    page.close()
                            page = context.new_page()
                            page.on("request", lambda request: requests.append({"page": page.url, "url": request.url, "kind": "root check"}))
                            page.goto(base, wait_until="networkidle")
                            assert page.locator('meta[name="robots"]').get_attribute("content") == "noindex, nofollow"
                            missing = "0123456789" if short != "0123456789" else "9876543210"
                            response = page.goto(base + f"b/{missing}/x")
                            assert response.status == 404
                            assert page.locator("#missing").inner_text() == "This frozen build was removed"
                            assert page.locator("#commit a").inner_text() == missing
                            assert page.locator("#commit a").get_attribute("href") == SOURCE_REPO + "/commit/" + missing
                            assert page.locator('a', has_text="Preview index").get_attribute("href") == PREFIX
                            assert page.locator('a', has_text="next").get_attribute("href") == PREFIX + "next/"
                            assert page.locator('meta[name="robots"]').get_attribute("content") == "noindex, nofollow"
                    finally:
                        browser.close()
            finally:
                server.shutdown()
                thread.join()
                (shots / "requests.json").write_text(json.dumps(requests, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dist", type=Path, required=True)
    parser.add_argument("--shots", type=Path, required=True)
    parser.add_argument("--main", type=Path, required=True, help="the unmarked build the preview was made from")
    args = parser.parse_args()
    chrome = find_chrome()
    if not chrome:
        print(f"NOT RUN: {chrome_problem()}")
        return NOT_RUN
    with browser_slot("check_preview.py") as acquired:
        if not acquired:
            return NOT_RUN
        check(args.dist.resolve(), args.shots.resolve(), chrome, args.main.resolve())
    print("preview: folder safety, hydration, banner and removed-build page passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
