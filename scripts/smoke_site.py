"""Check the deployed site's page, assets, 404, downloads, API and Atom feed.

    uv run scripts/smoke_site.py [--url URL] [--version X.Y.Z]

Each check retries briefly for CDN propagation and prints one result line.
Exit 1 if any check fails. This uses HTTP only and never starts a browser.
"""

import argparse
import json
import sys
import time
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from http.client import HTTPException
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urldefrag, urljoin, urlsplit
from urllib.request import Request, urlopen

try:
    from .agent_files import PUBLIC_URL
except ImportError:
    from agent_files import PUBLIC_URL

ROOT = Path(__file__).resolve().parents[1]
TIMEOUT = 10
ATTEMPTS = 3
RETRY_DELAY = 1
NOT_FOUND_TITLE = "Page not found | AI Security Institutes tracker"
FAILED: list[str] = []  # every failed check, for the closing line


class Page(HTMLParser):
    """Asset references, title and the rendered version link, without executing scripts."""

    def __init__(self, body: bytes):
        super().__init__()
        self.assets = set()
        self.title = ""
        self.versions = []
        self._title = False
        self._version = None
        self.feed(body.decode("utf-8"))
        self.close()

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "title":
            self._title = True
        if (
            tag == "a"
            and attrs.get("href") == "#changes"
            and "meta-link" in (attrs.get("class") or "").split()
        ):
            self._version = ""
        if tag == "script" and attrs.get("src"):
            self.assets.add(attrs["src"])
        if tag == "link" and attrs.get("href"):
            rel = set((attrs.get("rel") or "").lower().split())
            if rel & {
                "stylesheet",
                "preload",
                "modulepreload",
                "icon",
                "apple-touch-icon",
                "mask-icon",
            }:
                self.assets.add(attrs["href"])

    def handle_data(self, data):
        if self._title:
            self.title += data
        if self._version is not None:
            self._version += data

    def handle_endtag(self, tag):
        if tag == "title":
            self._title = False
        if tag == "a" and self._version is not None:
            self.versions.append(self._version.strip())
            self._version = None


def origin(url):
    parts = urlsplit(url)
    return (
        parts.scheme,
        parts.hostname,
        parts.port or (443 if parts.scheme == "https" else 80),
    )


def check_url(label, url, validate=None, status=200):
    """Retry both HTTP and content failures; retain the last body for asset discovery."""
    body = b""
    for attempt in range(ATTEMPTS):
        try:
            try:
                response = urlopen(
                    Request(
                        url,
                        headers={
                            "User-Agent": "aisi-site-smoke",
                            "Cache-Control": "no-cache",
                        },
                    ),
                    timeout=TIMEOUT,
                )
            except HTTPError as exc:
                response = exc  # The nested missing path is expected to return 404.
            with response:
                body = response.read()
                if response.status != status:
                    raise ValueError(f"expected HTTP {status}, got {response.status}")
                if validate:
                    validate(response.headers.get_content_type(), body)
            print(f"PASS {label}: {url}")
            return True, body
        except (OSError, HTTPException, ValueError, ET.ParseError) as exc:
            error = str(exc)
        if attempt + 1 < ATTEMPTS:
            time.sleep(RETRY_DELAY)
    print(f"FAIL {label}: {url}: {error}")
    FAILED.append(f"{label} ({url})")
    return False, body


def html_page(content_type, body):
    if content_type != "text/html":
        raise ValueError(f"expected text/html, got {content_type}")
    return Page(body)


def json_body(content_type, body):
    if content_type != "application/json" and not content_type.endswith("+json"):
        raise ValueError(f"expected JSON content type, got {content_type}")
    return json.loads(body)


def smoke_site(url, version):
    url = url.rstrip("/") + "/"
    FAILED.clear()

    def home(content_type, body):
        page = html_page(content_type, body)
        if f"v{version}" not in page.versions:
            raise ValueError(f"expected rendered version link v{version}")

    def missing(content_type, body):
        if html_page(content_type, body).title.strip() != NOT_FOUND_TITLE:
            raise ValueError("missing the site's 404 page title")

    def download(content_type, body):
        data = json_body(content_type, body)
        dataset = data.get("dataset") if isinstance(data, dict) else None
        actual = dataset.get("version") if isinstance(dataset, dict) else None
        if actual != version:
            raise ValueError(f"expected dataset.version {version}, got {actual!r}")

    def feed(content_type, body):
        if ET.fromstring(body).tag != "{http://www.w3.org/2005/Atom}feed":
            raise ValueError("expected an Atom feed root")

    passed, body = check_url("home", url, home)
    try:
        references = Page(body).assets
    except ValueError:
        references = set()  # The failed home check already reports malformed HTML or text.
    for reference in sorted(references):
        try:  # each reference on its own: a malformed one is a named failure, and the rest are still checked
            asset = urldefrag(urljoin(url, reference))[0]
            same_site = origin(asset) == origin(url)
        except ValueError as exc:
            print(f"FAIL asset: {reference!r} on {url}: malformed address ({exc})")
            FAILED.append(f"asset ({reference!r}: malformed)")
            passed = False
            continue
        if same_site:
            ok, _ = check_url("asset", asset)
            passed &= ok
    for label, path, validate, status in (
        ("nested 404", "no/such/page/", missing, 404),
        ("download", "data/aisi.json", download, 200),
        ("API index", f"api/v{version.split('.')[0]}/index.json", json_body, 200),
        ("feed", "feed.xml", feed, 200),
    ):
        ok, _ = check_url(label, urljoin(url, path), validate, status)
        passed &= ok
    if not passed:
        print(f"smoke: the live site {url} fails {len(FAILED)} check{'' if len(FAILED) == 1 else 's'}: {'; '.join(FAILED)}", file=sys.stderr)
    return 0 if passed else 1


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument(
        "--url",
        default=PUBLIC_URL,
        help="site address (default: agent_files.PUBLIC_URL)",
    )
    ap.add_argument(
        "--version", help="expected version (default: data/aisi.json's dataset.version)"
    )
    args = ap.parse_args(argv)
    version = args.version
    if version is None:
        version = json.loads((ROOT / "data/aisi.json").read_text(encoding="utf-8"))[
            "dataset"
        ]["version"]
    return smoke_site(args.url, version)


if __name__ == "__main__":
    sys.exit(main())
