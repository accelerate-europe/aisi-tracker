# /// script
# requires-python = ">=3.11"
# dependencies = ["beautifulsoup4>=4.12", "playwright==1.63.0"]
# ///
"""Save headless source responses; the parent owns the browser slot and process group.

uv run --locked scripts/monitor_browser.py --urls FILE --out DIR
Individual URL failures are metadata, while a tool failure exits non-zero.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

NOT_RUN = 3
USER_AGENT = "AISI-tracker-evidence/1.0 (headless Chromium)"


def fetch_page(page, url: str, timeout: float) -> tuple[bytes, dict]:
    """One navigation through Chromium's own network stack, so the site sees the browser it is checking for.

    A PDF response supplies its bytes directly. A file Chromium downloads instead of rendering is fetched once
    more through the page's request context, with the same User-Agent and cookies; only that request leaves Chromium's network stack.
    """
    from playwright.sync_api import Error, TimeoutError as PlaywrightTimeout

    response = None
    body = b""
    metadata = {
        "url": url, "final_url": url, "method": "headless",
        "http_status": None, "content_type": "", "status": "ok",
    }
    try:
        response = page.goto(url, wait_until="load", timeout=timeout * 1000)
        if response is not None and response.headers.get("content-type", "").split(";", 1)[0].strip().lower() == "application/pdf":
            body = response.body()
        else:
            try:
                page.wait_for_load_state("networkidle", timeout=2500)
            except PlaywrightTimeout:
                pass  # settling is capped; a busy page must not keep the run waiting
            body = page.content().encode("utf-8")
            soup = BeautifulSoup(body, "html.parser")
            for node in soup.select("script, style, template, noscript, [hidden]"):
                node.decompose()
            if soup.body is None or not soup.body.get_text(strip=True):
                metadata["status"] = "empty_render"
    except PlaywrightTimeout as exc:
        metadata.update(status="timeout", error=str(exc))
    except Error as exc:
        if page.is_closed() or not page.context.browser.is_connected():
            raise  # the helper failed, rather than one URL being unreachable
        if "Download is starting" not in str(exc):
            metadata.update(status="unreachable", error=str(exc))
        else:
            try:
                response = page.request.get(url, timeout=timeout * 1000)
                body = response.body()
            except PlaywrightTimeout as again:
                metadata.update(status="timeout", error=str(again))
            except Error as again:
                metadata.update(status="unreachable", error=str(again))
    if not body and metadata["status"] == "ok":
        metadata["status"] = "empty_render"
    if response is not None:
        metadata.update(
            final_url=response.url, http_status=response.status,
            content_type=response.headers.get("content-type", ""),
        )
        if response.status >= 400 and metadata["status"] in {"ok", "empty_render"}:
            metadata["status"] = "http_error"
    elif page.url != "about:blank":
        metadata["final_url"] = page.url
    metadata["retrieved"] = datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
    metadata["sha256"] = hashlib.sha256(body).hexdigest()
    return body, metadata


def write_result(out: Path, body: bytes, metadata: dict) -> None:
    """Publish the bytes first, then atomically publish their metadata."""
    folder = out / hashlib.sha256(metadata["url"].encode("utf-8")).hexdigest()
    folder.mkdir(parents=True, exist_ok=True)
    for name, content in (
        ("response.bin", body),
        ("metadata.json", (json.dumps(metadata, ensure_ascii=False, indent=2) + "\n").encode("utf-8")),
    ):
        temporary = folder / (name + ".tmp")
        with temporary.open("wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(folder / name)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--urls", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--delay", type=float, default=2.0)
    parser.add_argument("--timeout", type=float, default=30)
    parser.add_argument("--user-agent", default=USER_AGENT)
    args = parser.parse_args(argv)
    if not math.isfinite(args.delay) or args.delay < 0 or not 0 < args.timeout < math.inf:
        parser.error("--delay must be finite and non-negative; --timeout must be finite and above zero")
    from playwright.sync_api import Error, sync_playwright

    try:
        urls = [line.strip() for line in args.urls.read_text(encoding="utf-8").splitlines() if line.strip()]
        with sync_playwright() as playwright:
            chrome = playwright.chromium.executable_path
            if not os.path.isfile(chrome) or not os.access(chrome, os.X_OK):
                print("NOT RUN: Playwright's Chromium is not installed (uv run --locked scripts/check_page.py --install-chrome).", file=sys.stderr)
                return NOT_RUN
            browser = playwright.chromium.launch(
                executable_path=chrome, headless=True,
                env={**os.environ, "DBUS_SESSION_BUS_ADDRESS": "disabled:"},
            )
            try:
                last_host: dict[str, float] = {}
                for url in urls:
                    host = urlsplit(url).hostname or ""
                    if host in last_host:
                        time.sleep(max(0, args.delay - (time.monotonic() - last_host[host])))
                    last_host[host] = time.monotonic()
                    page = browser.new_page(user_agent=args.user_agent)
                    try:
                        body, metadata = fetch_page(page, url, args.timeout)
                        write_result(args.out, body, metadata)
                    finally:
                        page.close()
            finally:
                browser.close()
    except (OSError, ValueError, Error) as exc:
        print(f"error: browser helper failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
