"""Local page-event fixtures for caught and uncaught render failures."""

import sys
from types import SimpleNamespace
from pathlib import Path

import pytest

with pytest.MonkeyPatch.context() as imports:
    imports.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts"))
    from scripts import prerender


class Page:
    def __init__(self, events, rendered=False):
        self.events = events
        self.rendered = rendered
        self.handlers = {}
        self.waits = 0

    def on(self, name, callback):
        self.handlers[name] = callback

    def goto(self, url, **kwargs):
        for name, message in self.events:
            if name in self.handlers:
                self.handlers[name](message)

    # Keep the original selector API for the before-fix mutation replay.
    def wait_for_selector(self, *args, **kwargs):
        self.waits += 1
        if not self.rendered:
            raise TimeoutError("full render timeout")

    def locator(self, selector):
        assert selector == "html[data-prerendered]"
        return SimpleNamespace(count=lambda: int(self.rendered))

    def wait_for_timeout(self, milliseconds):
        self.waits += 1

    def content(self):
        return '<html data-prerendered><template id="page-template"></template></html>'


@pytest.fixture
def browser_fixture(monkeypatch):
    class Context:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    server = Context()
    server.server_port = 12345
    server.serve_forever = server.shutdown = lambda: None
    monkeypatch.setattr(prerender, "ThreadingHTTPServer", lambda *a, **kw: server)
    monkeypatch.setattr(
        prerender.threading,
        "Thread",
        lambda **kw: SimpleNamespace(start=lambda: None, join=lambda **kw: None),
    )

    def install(page):
        closed = []
        browser = SimpleNamespace(
            new_page=lambda: page, close=lambda: closed.append(True)
        )
        driver = Context()
        driver.chromium = SimpleNamespace(
            launch_persistent_context=lambda *a, **kw: browser
        )
        monkeypatch.setitem(
            sys.modules,
            "playwright.sync_api",
            SimpleNamespace(sync_playwright=lambda: driver),
        )
        return closed

    return install


@pytest.mark.parametrize("event", ["console", "pageerror"])
def test_render_failures_report_real_error_without_timeout(
    tmp_path, browser_fixture, event
):
    message = "Could not render institute tracker TypeError: missing display"
    value = (
        SimpleNamespace(type="error", text=message) if event == "console" else message
    )
    page = Page([(event, value)])
    closed = browser_fixture(page)
    with pytest.raises(RuntimeError, match="missing display") as caught:
        prerender.prerender(tmp_path, "fixture-chrome")
    assert str(caught.value).count("missing display") == 1
    assert page.waits == 0, "render failure must not wait for the full render timeout"
    assert closed == [True]


def test_unrelated_console_error_does_not_reject_success(tmp_path, browser_fixture):
    page = Page(
        [
            (
                "console",
                SimpleNamespace(type="error", text="Failed to load favicon: 404"),
            )
        ],
        rendered=True,
    )
    closed = browser_fixture(page)
    assert "data-prerendered" in prerender.prerender(tmp_path, "fixture-chrome")
    assert closed == [True]


def test_stalled_render_reports_console_diagnostics(
    tmp_path, browser_fixture, monkeypatch
):
    page = Page(
        [("console", SimpleNamespace(type="error", text="Data request failed: 503"))]
    )
    closed = browser_fixture(page)
    monkeypatch.setattr(prerender, "TIMEOUT_MS", 0)
    with pytest.raises(RuntimeError, match="Data request failed: 503"):
        prerender.prerender(tmp_path, "fixture-chrome")
    assert closed == [True]


@pytest.mark.parametrize("event", ["console", "pageerror"])
def test_completed_render_only_tolerates_unrelated_page_errors(
    tmp_path, browser_fixture, event
):
    message = "Could not render institute tracker: render failed"
    value = (
        SimpleNamespace(type="error", text=message)
        if event == "console"
        else "Unrelated timer failed"
    )
    page = Page([(event, value)], rendered=True)
    closed = browser_fixture(page)
    if event == "console":
        with pytest.raises(RuntimeError, match="render failed"):
            prerender.prerender(tmp_path, "fixture-chrome")
    else:
        assert "data-prerendered" in prerender.prerender(tmp_path, "fixture-chrome")
    assert closed == [True]
