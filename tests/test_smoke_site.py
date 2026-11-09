"""Smoke checks against a small local HTTP site, including broken deploys."""

import json
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest

from scripts import smoke_site

VERSION = "2.3.4"
PAGE = """<!doctype html><html><head>
<title>Tracker</title>
<script src="assets/app.js?v=1&amp;build=2"></script>
<link rel="stylesheet" href="assets/style.css">
<link rel="preload" href="assets/font.woff2" as="font">
<link rel="modulepreload" href="assets/module.js">
<link rel="shortcut icon" href="favicon.svg">
<script src="https://example.invalid/external.js"></script>
<link rel="stylesheet" href="https://example.invalid/external.css">
<link rel="alternate" href="not-an-asset.xml">
</head><body><p id="updated">Updated today ·
<a class="meta-link" href="#changes">v2.3.4</a></p></body></html>"""
NOT_FOUND = """<!doctype html><html><head>
<title>Page not found | AI Security Institutes tracker</title>
</head><body><h1>Page not found</h1></body></html>"""


@pytest.fixture
def fake_site(tmp_path, monkeypatch):
    files = {
        "index.html": PAGE,
        "404.html": NOT_FOUND,
        "assets/app.js": "// app",
        "assets/style.css": "body {}",
        "assets/font.woff2": "font",
        "assets/module.js": "// module",
        "favicon.svg": "<svg/>",
        "data/aisi.json": json.dumps({"dataset": {"version": VERSION}}),
        "api/v2/index.json": json.dumps({"dataset": {"version": VERSION}}),
        "feed.xml": '<feed xmlns="http://www.w3.org/2005/Atom"/>',
    }
    for name, content in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    requests, transient = [], {}

    class Handler(SimpleHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            requests.append(self.path)
            if self.path in transient:
                status, content_type, body = transient.pop(self.path)
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.end_headers()
                self.wfile.write(body)
                return
            super().do_GET()

        def send_error(self, code, message=None, explain=None):
            page = tmp_path / "404.html"
            if code == 404 and page.is_file():
                self.send_response(404)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(page.read_bytes())
            else:
                super().send_error(code, message, explain)

    server = ThreadingHTTPServer(
        ("127.0.0.1", 0), partial(Handler, directory=str(tmp_path))
    )
    url = f"http://127.0.0.1:{server.server_port}/"
    urlopen = smoke_site.urlopen

    def local_only(request, **kwargs):
        assert urlsplit(request.full_url).netloc == urlsplit(url).netloc
        assert kwargs["timeout"] > 0
        return urlopen(request, **kwargs)

    monkeypatch.setattr(smoke_site, "urlopen", local_only)
    monkeypatch.setattr(smoke_site, "RETRY_DELAY", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield SimpleNamespace(
            root=tmp_path, url=url, requests=requests, transient=transient
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def run_site(site):
    return smoke_site.main(["--url", site.url, "--version", VERSION])


def test_smoke_site_passes_and_checks_every_local_asset(fake_site, capsys):
    assert run_site(fake_site) == 0
    assert set(fake_site.requests) == {
        "/",
        "/assets/app.js?v=1&build=2",
        "/assets/style.css",
        "/assets/font.woff2",
        "/assets/module.js",
        "/favicon.svg",
        "/no/such/page/",
        "/data/aisi.json",
        "/api/v2/index.json",
        "/feed.xml",
    }
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == len(fake_site.requests)
    assert all(line.startswith("PASS ") for line in lines)


@pytest.mark.parametrize(
    "path, message",
    [
        ("index.html", "expected rendered version link v2.3.4"),
        ("data/aisi.json", "expected dataset.version 2.3.4"),
    ],
)
def test_smoke_site_rejects_wrong_version(fake_site, capsys, path, message):
    file = fake_site.root / path
    content = file.read_text().replace(VERSION, "2.3.40")
    if path == "index.html":
        content += '<script>var old = "v2.3.4";</script><p>Historical v2.3.4</p>'
    file.write_text(content)
    assert run_site(fake_site) == 1
    assert message in capsys.readouterr().out
    assert (
        fake_site.requests.count("/" if path == "index.html" else "/" + path)
        == smoke_site.ATTEMPTS
    )
    assert "/feed.xml" in fake_site.requests


@pytest.mark.parametrize(
    "path",
    [
        "assets/app.js",
        "assets/style.css",
        "assets/font.woff2",
        "assets/module.js",
        "favicon.svg",
    ],
)
def test_smoke_site_rejects_missing_asset(fake_site, capsys, path):
    (fake_site.root / path).unlink()
    assert run_site(fake_site) == 1
    captured = capsys.readouterr()
    output = captured.out
    assert f"FAIL asset: {fake_site.url}{path}" in output
    assert "expected HTTP 200, got 404" in output
    assert f"smoke: the live site {fake_site.url} fails 1 check: asset ({fake_site.url}{path}" in captured.err  # names the check and URL


def test_smoke_site_rejects_missing_custom_404(fake_site, capsys):
    (fake_site.root / "404.html").unlink()
    assert run_site(fake_site) == 1
    output = capsys.readouterr().out
    assert "FAIL nested 404:" in output
    assert "missing the site's 404 page title" in output


def test_smoke_site_rejects_a_soft_404(fake_site, capsys):
    path = fake_site.root / "no/such/page/index.html"
    path.parent.mkdir(parents=True)
    path.write_text(NOT_FOUND)
    assert run_site(fake_site) == 1
    assert "expected HTTP 404, got 200" in capsys.readouterr().out


@pytest.mark.parametrize("body", ["<feed", "<rss/>", "<feed/>"])
def test_smoke_site_rejects_bad_feed(fake_site, capsys, body):
    (fake_site.root / "feed.xml").write_text(body)
    assert run_site(fake_site) == 1
    assert "FAIL feed:" in capsys.readouterr().out


@pytest.mark.parametrize("path", ["data/aisi.json", "api/v2/index.json"])
def test_smoke_site_rejects_invalid_json(fake_site, capsys, path):
    (fake_site.root / path).write_text("<html>not JSON</html>")
    assert run_site(fake_site) == 1
    assert f"{fake_site.url}{path}" in capsys.readouterr().out


@pytest.mark.parametrize(
    "status, body",
    [
        (503, "Try again"),
        (200, PAGE.replace(VERSION, "2.3.3")),
    ],
)
def test_smoke_site_retries_http_and_stale_content(fake_site, capsys, status, body):
    fake_site.transient["/"] = (status, "text/html", body.encode())
    assert run_site(fake_site) == 0
    assert fake_site.requests.count("/") == 2
    assert "FAIL" not in capsys.readouterr().out


def test_smoke_site_defaults_to_public_url_and_local_version(fake_site, monkeypatch):
    monkeypatch.setattr(smoke_site, "PUBLIC_URL", fake_site.url)
    monkeypatch.setattr(smoke_site, "ROOT", fake_site.root)
    assert smoke_site.main([]) == 0


@pytest.mark.parametrize("reference", ["http://[", "http://example.org:notaport/x.js"])
def test_a_malformed_asset_address_is_a_named_failure_and_the_rest_still_run(fake_site, capsys, reference):
    page = fake_site.root / "index.html"
    page.write_text(page.read_text().replace("</head>", f'<script src="{reference}"></script></head>'))
    assert run_site(fake_site) == 1
    captured = capsys.readouterr()
    assert "malformed address" in captured.out and "PASS feed:" in captured.out  # later checks still ran
    assert "asset (" in captured.err and "malformed" in captured.err
