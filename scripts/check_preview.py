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
from preview import NOTICE, SOURCE_REPO, data_date, frozen_url, index_html, not_found_html, read_json, vienna

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


def check_banner(page, meta: dict, logical: str, expected_frozen: str) -> None:
    banner = page.locator("body > .preview-banner")
    assert banner.count() == 1, "expected exactly one visible banner"
    assert banner.is_visible(), "banner is hidden"
    assert page.locator("body > :first-child").get_attribute("class") == "preview-banner"
    text = banner.inner_text()
    for part in ("Preview, not published", meta["branch"], meta["commit"][:10], vienna(meta["built"]),
                 "Europe/Vienna", "Data as of " + data_date(meta["as_of"]), "CI run", "Link to this exact build", "Send feedback", "unless kept"):
        assert part in text, f"banner lacks {part!r}"
    assert banner.locator('a', has_text="CI run").get_attribute("href") == meta["run_url"]
    assert banner.locator('a', has_text=meta["commit"][:10]).get_attribute("href") == SOURCE_REPO + "/commit/" + meta["commit"]
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
        page.set_default_timeout(30_000)

        watch_requests(page, folder, requests, errors, blocked)
        if logical == "index.html":
            page.route("**/data/page.json*", lambda route: route.abort() if blocked else held.append(route))
        page.goto(url, wait_until="domcontentloaded")
        if logical == "index.html" and not blocked:
            page.wait_for_function("document.documentElement.hasAttribute('data-prerendered')")
        expected = frozen_url(url, logical, meta["commit"])
        check_banner(page, meta, logical, expected)
        label = f"{placement.replace('/', '-')}-{logical.replace('/', '-')}-{width}-{theme}"
        page.locator("body > .preview-banner").screenshot(path=str(shots / f"{label}-{'blocked' if blocked else 'before'}.png"))
        if logical == "index.html" and not blocked:
            assert held, "page data request was not observed"
            for route in held:
                route.continue_()
            page.wait_for_function("!document.documentElement.hasAttribute('data-prerendered')")
        page.wait_for_load_state("networkidle")
        check_banner(page, meta, logical, expected)
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


def check(dist: Path, shots: Path, chrome: str) -> None:
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
    args = parser.parse_args()
    chrome = find_chrome()
    if not chrome:
        print(f"NOT RUN: {chrome_problem()}")
        return NOT_RUN
    with browser_slot("check_preview.py") as acquired:
        if not acquired:
            return NOT_RUN
        check(args.dist.resolve(), args.shots.resolve(), chrome)
    print("preview: folder safety, hydration, banner and removed-build page passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
