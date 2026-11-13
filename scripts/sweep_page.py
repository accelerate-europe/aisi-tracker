# /// script
# requires-python = ">=3.11"
# dependencies = ["playwright==1.63.0"]
# ///
"""Full page sweep of a built site for the release's visual pass (runbook step 3.3).

  uv run --locked scripts/sweep_page.py --dist DIR --out DIR [--views 390-light,390-dark]

Opens the page at 1440, 1000 and 390 px, light and dark, opens every fold (details and
aria-expanded buttons, except the download menu, the column picker and the theme toggle), draws
every off-screen section, makes sticky bars static, and saves the whole page as 1600 px tall slices
<width>-<theme>-NN.png. sweep.json records per view the controls opened, sticky bars made static,
page height, scroll and client width (a wider
scroll width is horizontal overflow), folds left closed, visible collapsed buttons and console
errors. Exit 1 when any view has errors, overflow or a collapsed button; 3 (NOT_RUN) without
Chromium. It takes the host's browser slot; on a shared host run it on CI (the sweep workflow).
"""

import argparse
import json
import os
import sys
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from browser_lock import browser_slot  # noqa: E402

NOT_RUN = 3
VIEWS = [f"{width}-{theme}" for width in (1440, 1000, 390) for theme in ("light", "dark")]
SLICE = 1600
OPEN_ALL = """() => {
  let n = 0;
  for (let round = 0; round < 6; round++) {
    let acted = 0;
    document.querySelectorAll('details:not([open])').forEach(d => {
      if (d.matches('.download-menu, .cols')) return;
      d.open = true; acted++;
    });
    document.querySelectorAll('button[aria-expanded="false"]').forEach(b => {
      if (b.id === 'theme-toggle' || b.hidden || b.closest('[hidden]') || b.offsetParent === null) return;
      b.click(); acted++;
    });
    n += acted;
    if (!acted) break;
  }
  return n;
}"""
INFO = """() => ({h: document.documentElement.scrollHeight,
  sw: document.documentElement.scrollWidth, cw: document.documentElement.clientWidth,
  closed: document.querySelectorAll('details:not([open])').length,
  collapsed: [...document.querySelectorAll('button[aria-expanded="false"]')].filter(b => b.offsetParent).map(b => b.id || b.className)})"""


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *args) -> None:
        pass


def sweep_view(browser, url: str, view: str, out: Path) -> dict:
    width, theme = view.split("-")
    context = browser.new_context(viewport={"width": int(width), "height": 900}, color_scheme=theme, locale="en-GB")
    page = context.new_page()
    errors: list[str] = []
    page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(url, wait_until="networkidle")
    page.wait_for_timeout(800)
    opened = page.evaluate(OPEN_ALL)
    page.wait_for_timeout(800)
    page.add_style_tag(content="*, *::before, *::after { content-visibility: visible !important; }")
    # Sticky bars keep their place in the layout, but a whole-page capture draws them at the end of their section,
    # over its last lines; made static, each shows once where it sits and covers nothing.
    unstuck = page.evaluate("""() => { let n = 0; for (const el of document.querySelectorAll('*'))
      if (getComputedStyle(el).position === 'sticky') { el.style.setProperty('position', 'static', 'important'); n++; } return n; }""")
    page.wait_for_timeout(1500)
    page.mouse.move(0, 0)
    info = page.evaluate(INFO)
    slices = 0
    for top in range(0, info["h"], SLICE):
        page.screenshot(path=str(out / f"{view}-{slices:02d}.png"), full_page=True,
                        clip={"x": 0, "y": top, "width": int(width), "height": min(SLICE, info["h"] - top)})
        slices += 1
    context.close()
    return {"view": view, "opened": opened, "unstuck": unstuck, **info, "errors": errors, "slices": slices}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dist", type=Path, required=True, help="built site (index.html at its root)")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--views", default=",".join(VIEWS), help="comma-separated <width>-<theme>, default all six")
    args = parser.parse_args()
    views = args.views.split(",")
    if unknown := [v for v in views if v not in VIEWS]:
        parser.error(f"unknown views {unknown}; choose from {VIEWS}")
    if not (args.dist / "index.html").is_file():
        parser.error(f"no index.html in {args.dist}")
    from playwright.sync_api import sync_playwright

    log = []
    handler = partial(QuietHandler, directory=str(args.dist.resolve()))
    with sync_playwright() as playwright:
        chrome = playwright.chromium.executable_path
        if not (os.path.isfile(chrome) and os.access(chrome, os.X_OK)):
            print("NOT RUN: Chromium missing; uv run --locked scripts/check_page.py --install-chrome", file=sys.stderr)
            return NOT_RUN
        args.out.mkdir(parents=True, exist_ok=True)
        with browser_slot("sweep_page.py"), ThreadingHTTPServer(("127.0.0.1", 0), handler) as server:
            threading.Thread(target=server.serve_forever, daemon=True).start()
            url = f"http://127.0.0.1:{server.server_address[1]}/"
            # Without a session bus, as check_page.browser_env: Chromium then stays inside a job's memory cap.
            browser = playwright.chromium.launch(executable_path=chrome, headless=True,
                                                 env={**os.environ, "DBUS_SESSION_BUS_ADDRESS": "disabled:"})
            for view in views:
                log.append(sweep_view(browser, url, view, args.out))
                print(json.dumps(log[-1]), flush=True)
                (args.out / "sweep.json").write_text(json.dumps(log, indent=2), encoding="utf-8")
            browser.close()
        server.shutdown()
    bad = [e["view"] for e in log if e["errors"] or e["sw"] > e["cw"] or e["collapsed"]]
    if bad:
        print(f"Errors, horizontal overflow or collapsed buttons in {', '.join(bad)}; see {args.out / 'sweep.json'}", file=sys.stderr)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
