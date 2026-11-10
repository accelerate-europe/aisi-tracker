"""Browser ownership and response metadata, using local processes and fake pages only."""

import hashlib
import json
import os
import signal
import subprocess
import sys
import threading
import time
from contextlib import contextmanager, nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import browser_lock, monitor_browser


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("AISI_BROWSER_LOCK", str(tmp_path / "browser.lock"))
    monkeypatch.delenv("AISI_BROWSER_LOCK_HELD", raising=False)

    def forbidden(*args, **kwargs):
        raise AssertionError("Unexpected network request")

    monkeypatch.setattr("urllib.request.urlopen", forbidden)


def test_lock_path_precedence(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "runtime"))
    assert browser_lock.lock_path() == tmp_path / "browser.lock"
    monkeypatch.delenv("AISI_BROWSER_LOCK")
    assert browser_lock.lock_path() == tmp_path / "runtime/aisi-browser.lock"
    monkeypatch.delenv("XDG_RUNTIME_DIR")
    path = browser_lock.lock_path()
    assert path == tmp_path / "home/.cache/aisi-tracker/browser.lock"
    assert path.parent.is_dir()


@contextmanager
def owner():
    code = """
import sys
from scripts.browser_lock import browser_slot
with browser_slot('first') as held:
    print(held, flush=True)
    sys.stdin.read()
"""
    process = subprocess.Popen([sys.executable, "-c", code], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        assert process.stdout.readline().strip() == "True"
        yield process
    finally:
        process.communicate(timeout=5)


def test_second_process_times_out_and_run_does_not_start(tmp_path):
    marker = tmp_path / "must-not-run"
    with owner() as process:
        assert f"first (PID {process.pid})" in browser_lock.lock_path().read_text()
        started = time.monotonic()
        with browser_lock.browser_slot("second", wait=0.15) as held:
            assert held is False
        assert time.monotonic() - started >= 0.15
        assert browser_lock.run_in_slot(
            [sys.executable, "-c", f"open({str(marker)!r}, 'w').close()"], "second", wait=0,
        ) == (False, None)
    assert not marker.exists()


def test_nested_marker_is_for_the_same_resolved_path_only(monkeypatch, tmp_path):
    with owner():
        monkeypatch.setenv("AISI_BROWSER_LOCK_HELD", str(browser_lock.lock_path()))
        with browser_lock.browser_slot("nested", wait=0) as held:
            assert held is True
        monkeypatch.setenv("AISI_BROWSER_LOCK_HELD", str(tmp_path / "different.lock"))
        with browser_lock.browser_slot("sibling", wait=0) as held:
            assert held is False


def test_wait_forever_names_owner_once():
    with owner() as first:
        second = subprocess.Popen(
            [sys.executable, "-c", "from scripts.browser_lock import browser_slot\nwith browser_slot('second') as held: print(held)"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        try:
            assert second.stderr.readline().strip() == f"Waiting for browser slot held by first (PID {first.pid})"
            first.communicate(timeout=5)
            out, err = second.communicate(timeout=5)
            assert out.strip() == "True" and err == ""
        finally:
            if second.poll() is None:
                second.kill()
                second.wait()


def test_sigkill_releases_flock():
    with owner() as process:
        process.kill()
        process.wait(timeout=5)
        with browser_lock.browser_slot("next", wait=1) as held:
            assert held is True


def test_run_sets_only_child_environment(tmp_path):
    output = tmp_path / "env.json"
    given = {**os.environ, "EXTRA": "kept"}
    before = dict(os.environ)
    code = f"import json, os; open({str(output)!r}, 'w').write(json.dumps(dict(os.environ)))"
    assert browser_lock.run_in_slot([sys.executable, "-c", code], "monitor", wait=0, env=given) == (True, 0)
    child_env = json.loads(output.read_text())
    assert child_env["AISI_BROWSER_LOCK_HELD"] == str(browser_lock.lock_path())
    assert child_env["EXTRA"] == "kept"
    assert dict(os.environ) == before and "AISI_BROWSER_LOCK_HELD" not in given


def worker_slot(stage, check, args):
    with browser_lock.browser_slot("worker", wait=0) as held:
        assert held is True
    return [os.environ["AISI_BROWSER_LOCK_HELD"]]


@pytest.mark.parametrize("shard", [False, True])
def test_gate_workers_receive_lock_marker(shard):
    from scripts import check_page

    work = [("stage", worker_slot, ())]
    before = dict(os.environ)
    with browser_lock.browser_slot("gate", wait=0):
        if shard:
            result = check_page.run_shard(worker_slot, work, [0], jobs=2)
            assert result == {0: {"errors": [str(browser_lock.lock_path())]}}
        else:
            result = check_page.run_stages(worker_slot, work, {}, jobs=2)
            assert result == [str(browser_lock.lock_path())]
    assert dict(os.environ) == before


def test_gate_entry_holds_slot(monkeypatch):
    from scripts import check_page

    def missing():
        with browser_lock.browser_slot("contender", wait=0) as held:
            assert held is False
        return None

    monkeypatch.setattr(check_page, "find_chrome", missing)
    assert check_page.main() == check_page.NOT_RUN


def test_prerender_entry_holds_slot(monkeypatch, tmp_path):
    from scripts import prerender

    def render(dist, chrome):
        with browser_lock.browser_slot("contender", wait=0) as held:
            assert held is False
        return '<html data-prerendered><template id="page-template"></template></html>'

    monkeypatch.setattr(sys, "argv", ["prerender.py", "--dist", str(tmp_path)])
    monkeypatch.setattr(prerender, "find_chrome", lambda: "fixture-chrome")
    monkeypatch.setattr(prerender, "prerender", render)
    monkeypatch.setattr(prerender, "split_map", lambda html, dist: html)
    assert prerender.main() == 0


def wait_file(path):
    until = time.monotonic() + 10
    while not path.exists():
        if time.monotonic() >= until:
            pytest.fail(f"child did not write {path}")
        time.sleep(0.02)


@pytest.mark.parametrize("ending", ["normal", "deadline", "exception", "interrupt", "sigterm", "cleanup_sigterm", pytest.param(
    "spawn_interrupt", marks=pytest.mark.skipif(sys.platform != "linux", reason="Linux discovers children when Popen did not return"),
)])
@pytest.mark.parametrize("detached", [False, pytest.param(True, marks=pytest.mark.skipif(sys.platform != "linux", reason="Linux subreaper containment"))])
def test_group_is_gone_before_next_holder(tmp_path, ending, detached):
    """A grandchild ignoring SIGTERM, even in its own session, is gone before the next holder."""
    ready = tmp_path / "child.json"
    grandchild = f"""
import json, os, signal, time
from pathlib import Path
signal.signal(signal.SIGTERM, signal.SIG_IGN)
path = Path({str(ready)!r})
temporary = path.with_suffix('.tmp')
temporary.write_text(json.dumps([os.getpid(), os.getpgrp(), os.getppid()]))
temporary.replace(path)
time.sleep(60)
"""
    child = f"""
import subprocess, sys, time
from pathlib import Path
subprocess.Popen([sys.executable, '-c', {grandchild!r}], start_new_session={detached!r})
while not Path({str(ready)!r}).exists():
    time.sleep(0.01)
{'pass' if ending == 'normal' else 'time.sleep(60)'}
"""
    command = [sys.executable, "-c", child]
    monitor = f"""
import json, signal, subprocess, time
from pathlib import Path
from scripts.browser_lock import run_in_slot
ending = {ending!r}
original_wait = subprocess.Popen.wait
def ready():
    until = time.monotonic() + 10
    while not Path({str(ready)!r}).exists():
        if time.monotonic() > until:
            raise RuntimeError('child did not start')
        time.sleep(0.02)
if ending in ('exception', 'interrupt'):
    def interrupted(self, *args, **kwargs):
        subprocess.Popen.wait = original_wait
        ready()
        raise RuntimeError('test exception') if ending == 'exception' else KeyboardInterrupt()
    subprocess.Popen.wait = interrupted
if ending == 'cleanup_sigterm':
    import os, threading
    def signalled(self, *args, **kwargs):
        subprocess.Popen.wait = original_wait
        ready()
        def sibling():
            # An unmasked sibling thread receives SIGTERM while the main thread cleans up.
            for _ in range(10):
                os.kill(os.getpid(), signal.SIGTERM)
                time.sleep(0.2)
        threading.Thread(target=sibling, daemon=True).start()
        raise RuntimeError('cleanup under signals')
    subprocess.Popen.wait = signalled
if ending == 'spawn_interrupt':
    class InterruptedSpawn(subprocess.Popen):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            ready()
            raise KeyboardInterrupt()
    subprocess.Popen = InterruptedSpawn
try:
    result = run_in_slot({command!r}, 'monitor', 0, deadline=time.monotonic() + 3 if ending == 'deadline' else None)
    print(json.dumps(result))
except (RuntimeError, KeyboardInterrupt, SystemExit) as exc:
    assert signal.getsignal(signal.SIGTERM) == signal.SIG_DFL
    print(type(exc).__name__)
"""
    process = subprocess.Popen([sys.executable, "-c", monitor], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    group, helper = None, None
    try:
        wait_file(ready)
        pid, group, helper = json.loads(ready.read_text())
        if ending == "sigterm":
            process.terminate()  # an actual SIGTERM while the supervisor waits for its child
        with browser_lock.browser_slot("next", wait=25) as held:
            assert held is True
            with pytest.raises(ProcessLookupError):
                os.killpg(group, 0)
            with pytest.raises(ProcessLookupError):
                os.kill(pid, 0)
            with pytest.raises(ProcessLookupError):
                os.kill(helper, 0)
        out, err = process.communicate(timeout=5)
        expected = {"cleanup_sigterm": "SystemExit", "exception": "RuntimeError", "interrupt": "KeyboardInterrupt", "sigterm": "SystemExit", "spawn_interrupt": "KeyboardInterrupt"}.get(ending, "[true, null]")
        assert process.returncode == 0 and out.strip() == expected, (out, err)
    finally:
        for remaining in {group, helper} - {None}:
            try:
                os.killpg(remaining, signal.SIGKILL)
            except ProcessLookupError:
                pass
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=5)


def test_existing_children_are_neither_killed_nor_reaped():
    alive = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    exiting = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(0.1); raise SystemExit(7)"])
    try:
        assert browser_lock.run_in_slot(
            [sys.executable, "-c", "import time; time.sleep(0.3)"], "monitor", wait=0,
        ) == (True, 0)
        assert alive.poll() is None
        assert exiting.wait(timeout=5) == 7  # reaping another Popen's child would lose this exit code
    finally:
        alive.kill()
        alive.wait()
        exiting.wait(timeout=5)


@pytest.mark.parametrize("custom", [False, True])
def test_sigterm_handler_is_restored_and_custom_handler_kept(custom):
    """A SIGTERM during the run is deferred, then reaches the caller's handler (or the default exit) after cleanup."""
    def handler(signum, frame):
        raise KeyboardInterrupt()

    original_handler = signal.getsignal(signal.SIGTERM)
    chosen = handler if custom else signal.SIG_DFL
    signal.signal(signal.SIGTERM, chosen)
    try:
        assert browser_lock.run_in_slot([sys.executable, "-c", "pass"], "monitor", 0) == (True, 0)
        assert signal.getsignal(signal.SIGTERM) == chosen
        threading.Timer(0.3, os.kill, (os.getpid(), signal.SIGTERM)).start()
        with pytest.raises(KeyboardInterrupt if custom else SystemExit):
            browser_lock.run_in_slot([sys.executable, "-c", "import time; time.sleep(30)"], "monitor", 0)
        assert signal.getsignal(signal.SIGTERM) == chosen
    finally:
        signal.signal(signal.SIGTERM, original_handler)


def test_signal_while_installing_deferral_starts_no_child(monkeypatch, tmp_path):
    """SIGTERM arriving between the two handler installations acts at once, before any child exists."""
    marker = tmp_path / "started"
    original = signal.signal
    calls = []

    def installing(sig, handler):
        previous = original(sig, handler)
        calls.append(sig)
        if len(calls) == 2 and sig == signal.SIGINT:  # after _termination_handler, between the two recordings
            os.kill(os.getpid(), signal.SIGTERM)
        return previous

    handlers = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
    monkeypatch.setattr(browser_lock.signal, "signal", installing)
    try:
        with pytest.raises(SystemExit):
            browser_lock.run_in_slot([sys.executable, "-c", f"open({str(marker)!r}, 'w')"], "monitor", 0)
        assert {sig: signal.getsignal(sig) for sig in handlers} == handlers
    finally:
        for sig, handler in handlers.items():
            original(sig, handler)
    assert not marker.exists()
    with browser_lock.browser_slot("next", wait=0) as held:
        assert held is True



class FakeError(Exception):
    pass


class FakeTimeout(FakeError):
    pass


class FakePage:
    def __init__(self, html="<html><body>Evidence</body></html>", status=200, content_type="text/html", failure=None, download_pdf=True):
        self.html, self.failure = html, failure
        self.main_frame = object()
        self.url = "about:blank"
        self.response = SimpleNamespace(
            url="https://example.test/final", status=status,
            headers={"content-type": content_type}, body=lambda: b"%PDF-1.7 fixture",
        )
        self.context = SimpleNamespace(browser=SimpleNamespace(is_connected=lambda: True))
        self.settled = False
        self.request = SimpleNamespace(get=self.get)
        self.downloaded = False
        self.download_pdf = download_pdf

    def goto(self, url, **kwargs):
        assert kwargs == {"wait_until": "load", "timeout": 30000}
        if self.failure:
            raise self.failure
        if "pdf" in self.response.headers["content-type"].lower() and self.download_pdf:
            raise FakeError("net::ERR_ABORTED; Download is starting")
        self.url = self.response.url
        return self.response

    def get(self, url, **kwargs):
        assert kwargs == {"timeout": 30000}
        self.downloaded = True
        return self.response

    def wait_for_load_state(self, state, timeout):
        assert (state, timeout) == ("networkidle", 2500)
        self.settled = True
        raise FakeTimeout("busy page")

    def content(self):
        assert self.settled
        return self.html

    def is_closed(self):
        return False


@pytest.fixture
def fake_playwright(monkeypatch):
    module = SimpleNamespace(Error=FakeError, TimeoutError=FakeTimeout)
    monkeypatch.setitem(sys.modules, "playwright.sync_api", module)
    return module


@pytest.mark.parametrize("kind, status", [
    ("html", "ok"), ("pdf", "ok"), ("rendered_pdf", "ok"), ("empty", "empty_render"),
    ("http", "http_error"), ("timeout", "timeout"), ("unreachable", "unreachable"),
])
def test_response_metadata(fake_playwright, tmp_path, kind, status):
    page = FakePage(
        html="<html><script>onlyScript()</script></html>" if kind == "empty" else "<html><body>Evidence</body></html>",
        status=403 if kind == "http" else 200,
        content_type="Application/PDF; charset=binary" if kind in {"pdf", "rendered_pdf"} else "text/html; charset=utf-8",
        failure={"timeout": FakeTimeout("timed out"), "unreachable": FakeError("no route")}.get(kind),
        download_pdf=kind != "rendered_pdf",
    )
    url = "https://example.test/über?query=1"
    body, metadata = monitor_browser.fetch_page(page, url, 30)
    monitor_browser.write_result(tmp_path, body, metadata)
    folder = tmp_path / hashlib.sha256(url.encode("utf-8")).hexdigest()
    assert (folder / "response.bin").read_bytes() == body
    assert json.loads((folder / "metadata.json").read_text()) == metadata
    assert metadata["url"] == url and metadata["method"] == "headless"
    assert metadata["status"] == status
    assert metadata["sha256"] == hashlib.sha256(body).hexdigest()
    assert metadata["retrieved"].endswith("Z") and len(metadata["retrieved"]) == 20
    assert not list(folder.glob("*.tmp"))
    if page.failure:
        assert metadata["error"] and metadata["http_status"] is None and body == b""
    else:
        assert metadata["final_url"] == page.response.url
        assert metadata["http_status"] == page.response.status
        assert metadata["content_type"] == page.response.headers["content-type"]
    if kind in {"pdf", "rendered_pdf"}:
        assert body == b"%PDF-1.7 fixture" and not page.settled
    assert page.downloaded == (kind == "pdf")


@pytest.mark.parametrize("content", [
    "", "<noscript>Enable JavaScript</noscript>",
    "<template>Fallback</template><script>start()</script><style>body { color: red }</style>",
    "<div hidden>Content is not ready</div>",
])
def test_titled_empty_body_is_empty_render(fake_playwright, content):
    html = f"<html><head><title>Institute</title></head><body>{content}</body></html>"
    body, metadata = monitor_browser.fetch_page(FakePage(html=html), "https://example.test/", 30)
    assert body == html.encode("utf-8")
    assert metadata["status"] == "empty_render"


def test_metadata_published_after_response(monkeypatch, tmp_path):
    original = Path.replace

    def interrupted(path, target):
        if path.name == "metadata.json.tmp":
            assert (path.parent / "response.bin").read_bytes() == b"complete"
            assert not Path(target).exists()
            raise OSError("interrupted metadata write")
        return original(path, target)

    monkeypatch.setattr(Path, "replace", interrupted)
    with pytest.raises(OSError, match="interrupted metadata"):
        monitor_browser.write_result(tmp_path, b"complete", {"url": "https://example.test/"})
    assert not list(tmp_path.rglob("metadata.json"))


def test_helper_uses_one_browser_paces_hosts_and_closes_pages(fake_playwright, monkeypatch, tmp_path):
    chrome = tmp_path / "chrome"
    chrome.touch()
    chrome.chmod(0o755)
    urls = tmp_path / "urls.txt"
    urls.write_text("https://one.test/a\nhttps://one.test/b\n\nhttps://two.test/c\n")
    clock, starts, closed, launched = [0.0], [], [], []
    monkeypatch.setattr(monitor_browser.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(monitor_browser.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds))

    def new_page(**kwargs):
        assert kwargs == {"user_agent": monitor_browser.USER_AGENT}
        assert len(closed) == len(starts), "only one page at a time"
        starts.append(clock[0])
        page = FakePage()
        page.close = lambda: closed.append("page")
        return page

    browser = SimpleNamespace(new_page=new_page, close=lambda: closed.append("browser"))

    def launch(**kwargs):
        assert kwargs["headless"] is True
        assert kwargs["env"]["DBUS_SESSION_BUS_ADDRESS"] == "disabled:"
        launched.append(True)
        return browser

    fake_playwright.sync_playwright = lambda: nullcontext(SimpleNamespace(
        chromium=SimpleNamespace(executable_path=str(chrome), launch=launch),
    ))
    assert monitor_browser.main(["--urls", str(urls), "--out", str(tmp_path / "out")]) == 0
    assert launched == [True] and starts == [0.0, 2.0, 2.0]
    assert closed == ["page", "page", "page", "browser"]
    assert len(list((tmp_path / "out").glob("*/metadata.json"))) == 3


@pytest.mark.parametrize("missing, expected", [(True, 3), (False, 1)])
def test_helper_missing_chrome_and_tool_failure(fake_playwright, tmp_path, missing, expected):
    chrome = tmp_path / "chrome"
    if not missing:
        chrome.touch()
        chrome.chmod(0o755)
    urls = tmp_path / "urls.txt"
    urls.write_text("https://example.test/\n")

    def launch(**kwargs):
        assert not missing, "missing Chromium must never be launched"
        raise FakeError("tool failed")

    fake_playwright.sync_playwright = lambda: nullcontext(SimpleNamespace(
        chromium=SimpleNamespace(executable_path=str(chrome), launch=launch),
    ))
    assert monitor_browser.main(["--urls", str(urls), "--out", str(tmp_path / "out")]) == expected
