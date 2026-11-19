# /// script
# requires-python = ">=3.11"
# dependencies = ["playwright==1.63.0"]
# ///
"""Full page sweep of a built site for the release's visual pass (runbook step 3.3).

  uv run --locked scripts/sweep_page.py --dist DIR --out DIR [--views 390-light,390-dark]

Opens the page at 1440, 1000 and 390 px, light and dark, opens every fold (details and
aria-expanded buttons, except the download menu, the column picker, the theme toggle and single-open groups), shows
every table row (the table's "Show all"), draws
every off-screen section, makes sticky bars static, and saves the whole page as 1600 px tall slices
<width>-<theme>-NN.png. sweep.json records per view the controls opened, sticky bars made static,
page height, scroll and client width (a wider
scroll width is horizontal overflow), folds left closed, visible collapsed buttons, table rows shown and
console errors. Single-open Functions rows are opened in turn, including their claims and quotes,
and saved as <width>-<theme>-fn-<institute-id>-NN.png; the grid view is saved as
<width>-<theme>-fn-grid-NN.png. The report counts rows found, opened and captured, row and grid
slices, and capture failures. Exit 1 when any view has errors, overflow, a collapsed button outside
a single-open group, a filtered table or a failed extra capture; 3 (NOT_RUN) without
Chromium. It takes the host's browser slot; on a shared host run it on CI (the sweep workflow).
"""

import argparse
import json
import math
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
# Each form owns one detail panel: selecting another row/cell removes the previous panel.
# Function labels within a grouped row are independent toggles, so must NOT be excluded.
SINGLE_OPEN = {"rows": "button.fn-phone-row", "cells": "button.fn-cell"}
OPEN_ALL = """(root, singleOpen) => {
  let n = 0;
  for (let round = 0; round < 6; round++) {
    let acted = 0;
    root.querySelectorAll('details:not([open])').forEach(d => {
      if (d.matches('.download-menu, .cols')) return;
      d.open = true; acted++;
    });
    root.querySelectorAll('button[aria-expanded="false"]').forEach(b => {
      if (b.matches(singleOpen) || b.getAttribute('aria-expanded') !== 'false') return;
      if (b.id === 'theme-toggle' || b.hidden || b.closest('[hidden]') || b.offsetParent === null) return;
      b.click(); acted++;
    });
    n += acted;
    if (!acted) break;
  }
  return n;
}"""
INFO = """singleOpen => ({h: document.documentElement.scrollHeight,
  sw: document.documentElement.scrollWidth, cw: document.documentElement.clientWidth,
  closed: document.querySelectorAll('details:not([open])').length,
  rows: document.querySelectorAll('#world-rows tr').length,
  filtered: [...document.querySelectorAll('#world-count button')].some(x => x.textContent.trim() === 'Show all'),
  collapsed: [...document.querySelectorAll('button[aria-expanded="false"]')]
    .filter(b => b.offsetParent && !b.matches(singleOpen)).map(b => b.id || b.className)})"""
UNSTICK = """root => { let n = 0; for (const el of [root, ...root.querySelectorAll('*')])
  if (getComputedStyle(el).position === 'sticky') { el.style.setProperty('position', 'static', 'important'); n++; } return n; }"""


def slice_clips(box: dict) -> list[dict]:
    """Cover document coordinates without gaps, rounding outwards to whole pixels."""
    if box["width"] <= 0 or box["height"] <= 0:
        raise ValueError("empty capture region")
    left, top = math.floor(box["x"]), math.floor(box["y"])
    right, bottom = math.ceil(box["x"] + box["width"]), math.ceil(box["y"] + box["height"])
    return [{"x": left, "y": y, "width": right - left, "height": min(SLICE, bottom - y)}
            for y in range(top, bottom, SLICE)]


def capture_region(page, region, out: Path, prefix: str) -> int:
    if not region.count() or not region.is_visible() or not region.inner_text().strip():
        raise ValueError("missing or empty capture region")
    box = region.evaluate("""el => { const r = el.getBoundingClientRect();
      return {x: r.x + scrollX, y: r.y + scrollY, width: r.width, height: r.height}; }""")
    clips = slice_clips(box)
    for n, clip in enumerate(clips):
        page.screenshot(path=str(out / f"{prefix}-{n:02d}.png"), full_page=True, clip=clip)
    return len(clips)


def view_failed(info: dict) -> bool:
    return bool(info["errors"] or info["sw"] > info["cw"] or info["collapsed"] or info["filtered"] or info["fn_errors"])


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *args) -> None:
        pass


def sweep_view(browser, url: str, view: str, out: Path) -> dict:
    from playwright.sync_api import Error

    width, theme = view.split("-")
    context = browser.new_context(viewport={"width": int(width), "height": 900}, color_scheme=theme, locale="en-GB")
    page = context.new_page()
    errors: list[str] = []
    page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(url, wait_until="networkidle")
    page.wait_for_timeout(800)
    page.add_style_tag(content="*, *::before, *::after { content-visibility: visible !important; }")
    single_open = ", ".join(SINGLE_OPEN.values())
    body = page.locator("body")
    fn = {"fn_rows": 0, "fn_opened": 0, "fn_captured": 0, "fn_slices": 0, "fn_grid_slices": 0, "fn_errors": []}
    row_ids = page.locator(SINGLE_OPEN["rows"]).evaluate_all("bs => bs.map(b => b.closest('li[data-institute]').dataset.institute)")
    fn["fn_rows"] = len(row_ids)
    if page.locator("#functions-table").count() and not row_ids:
        fn["fn_errors"].append("Functions has no grouped rows")
    opened = 0

    def open_row(institute_id):
        nonlocal opened
        button = page.locator(f'.fn-phone li[data-institute={json.dumps(institute_id)}]').locator(SINGLE_OPEN["rows"])
        opened += button.evaluate("b => { if (b.getAttribute('aria-expanded') !== 'false') return 0; b.click(); return 1; }")
        if button.get_attribute("aria-expanded") != "true":
            raise ValueError("row did not open")
        return page.locator(f'[id={json.dumps(button.get_attribute("aria-controls"))}]')

    # Use the first row in the page's enforced FN_ORDER for the main slices.
    if row_ids:
        try:
            open_row(row_ids[0])
        except (Error, ValueError) as exc:
            fn["fn_errors"].append(f"main row {row_ids[0]}: {exc}")
    opened += body.evaluate(OPEN_ALL, single_open)
    # The table opens filtered to states that have or consider an institute; its "Show all" lists every state.
    page.evaluate("""() => { const b = [...document.querySelectorAll('#world-count button')]
      .find(x => x.textContent.trim() === 'Show all'); if (b) b.click(); }""")
    page.wait_for_timeout(800)
    # Sticky bars keep their place in the layout, but a whole-page capture draws them at the end of their section,
    # over its last lines; made static, each shows once where it sits and covers nothing.
    unstuck = body.evaluate(UNSTICK)
    page.wait_for_timeout(1500)
    page.mouse.move(0, 0)
    info = page.evaluate(INFO, single_open)
    clips = slice_clips({"x": 0, "y": 0, "width": int(width), "height": info["h"]})
    for n, clip in enumerate(clips):
        page.screenshot(path=str(out / f"{view}-{n:02d}.png"), full_page=True, clip=clip)

    def capture_extra(region, prefix):
        nonlocal unstuck
        unstuck += region.evaluate(UNSTICK)
        count = capture_region(page, region, out, prefix)
        # Keep the main page's height/row counts; retain failures from every extra state.
        extra = page.evaluate(INFO, single_open)
        info["sw"] = max(info["sw"], extra["sw"])
        info["collapsed"] = sorted(set(info["collapsed"]) | set(extra["collapsed"]))
        info["filtered"] |= extra["filtered"]
        return count

    for institute_id in row_ids:
        try:
            region = open_row(institute_id)
            fn["fn_opened"] += 1
            opened += region.evaluate(OPEN_ALL, single_open)  # Labels first, then their newly inserted quote folds.
            # Quote bodies are lazy: the page fills them on the asynchronous details toggle event.
            page.wait_for_function("""id => { const region = document.getElementById(id);
              return region && [...region.querySelectorAll('details.quote[open]')].every(d => d.querySelector('.quote-body')); }""",
                                   arg=region.get_attribute("id"), timeout=5000)
            fn["fn_slices"] += capture_extra(region, f"{view}-fn-{institute_id}")
            fn["fn_captured"] += 1
        except (Error, ValueError) as exc:
            fn["fn_errors"].append(f"{institute_id}: {exc}")

    grid = page.locator('.fn-view-switch button[data-view="grid"]')
    if grid.count():
        try:
            grid.evaluate("b => { if (b.getAttribute('aria-pressed') !== 'true') b.click(); }")
            if grid.get_attribute("aria-pressed") != "true" or not page.locator(".fn-scroll").is_visible():
                raise ValueError("grid did not open")
            fn["fn_grid_slices"] = capture_extra(page.locator("#functions-table"), f"{view}-fn-grid")
        except (Error, ValueError) as exc:
            fn["fn_errors"].append(f"grid: {exc}")
    context.close()
    return {"view": view, "opened": opened, "unstuck": unstuck, **info, "errors": errors, "slices": len(clips), **fn}


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
    bad = [e["view"] for e in log if view_failed(e)]
    if bad:
        print(f"Errors, horizontal overflow, collapsed buttons, a filtered table or failed extra captures in {', '.join(bad)}; see {args.out / 'sweep.json'}", file=sys.stderr)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
