# /// script
# requires-python = ">=3.11"
# dependencies = ["beautifulsoup4>=4.12", "playwright>=1.51"]
# ///
"""Pre-render a built site in place: open its index.html in headless Chrome and save the rendered page.

build.py runs this on its staged output: uv run scripts/prerender.py --dist DIR. The page script, opened
as ?prerender, renders as usual and then marks the page data-prerendered, keeping the untouched
page as <template id="page-template"> for hydration (see the comment at the top of the page script).
Chrome is a build requirement: without it this exits with NOT_RUN (3) and the build fails.
"""

import argparse
import sys
import tempfile
import threading
import time
from functools import partial
from http.server import ThreadingHTTPServer
from pathlib import Path

from check_page import NOT_RUN, QuietHandler, find_chrome

WIDTH, HEIGHT = 1440, 900  # desktop; at other widths the page regroups its card rows before the data arrives
TIMEOUT_MS = 60_000


def prerender(dist: Path, chrome: str) -> str:
    from playwright.sync_api import sync_playwright

    handler = partial(QuietHandler, directory=str(dist))
    with ThreadingHTTPServer(("127.0.0.1", 0), handler) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory(prefix="prerender-") as profile, sync_playwright() as playwright:
                browser = playwright.chromium.launch_persistent_context(
                    profile, executable_path=chrome, headless=True,
                    viewport={"width": WIDTH, "height": HEIGHT}, color_scheme="light")
                try:
                    page = browser.new_page()
                    errors = []
                    fatal_errors = []
                    render_errors = []

                    def page_error(error):
                        errors.append(str(error))
                        fatal_errors.append(str(error))

                    def console_message(message):
                        if message.type == "error":
                            errors.append(message.text)
                            # The page catches render exceptions and reports them here.
                            # Resource errors (for example a favicon 404) are diagnostic only.
                            if message.text.startswith("Could not render institute tracker"):
                                render_errors.append(message.text)

                    page.on("pageerror", page_error)
                    page.on("console", console_message)
                    page.goto(f"http://127.0.0.1:{server.server_port}/?prerender", wait_until="load")
                    try:
                        deadline = time.monotonic() + TIMEOUT_MS / 1000
                        while True:
                            rendered = page.locator("html[data-prerendered]").count()
                            if render_errors:
                                raise RuntimeError("rendering failed")
                            # Uncaught page errors are fatal only before completion;
                            # the page's own caught-render failure is always fatal.
                            if rendered:
                                break
                            if fatal_errors:
                                raise RuntimeError("rendering failed")
                            if time.monotonic() >= deadline:
                                raise TimeoutError(f"render timed out after {TIMEOUT_MS} ms")
                            # Playwright dispatches page events during this short wait.
                            page.wait_for_timeout(50)
                    except Exception as exc:  # noqa: BLE001 - report the page's own errors with the timeout
                        raise RuntimeError(f"the page did not finish rendering: {exc}; page errors: {errors}") from exc
                    return page.content()
                finally:
                    browser.close()
        finally:
            server.shutdown()
            thread.join(timeout=5)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dist", type=Path, required=True, help="built site whose index.html is replaced")
    args = parser.parse_args()
    chrome = find_chrome()
    if chrome is None:
        print("NOT RUN: Chrome/Chromium is required to pre-render the page and is not installed.", file=sys.stderr)
        return NOT_RUN
    page = args.dist / "index.html"
    html = prerender(args.dist, chrome)
    if "data-prerendered" not in html or '<template id="page-template">' not in html:
        print("error: the rendered page lacks its pre-rendering markers", file=sys.stderr)
        return 1
    temporary = page.with_name(page.name + ".tmp")
    temporary.write_text(html, encoding="utf-8")
    temporary.replace(page)
    return 0


if __name__ == "__main__":
    sys.exit(main())
