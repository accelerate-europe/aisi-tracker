"""Monitor routes and durable run recovery, with no network or browser."""

import hashlib
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import HTTPError

import pytest

from scripts import monitor_sources as monitor
from scripts import snapshot_sources, verify_excerpts
from test_monitor_sources import OLD, URL, page


@pytest.fixture(autouse=True)
def no_external_calls(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Unexpected external call")

    monkeypatch.setattr(verify_excerpts, "urlopen", forbidden)
    monkeypatch.setattr(monitor, "run_browser", forbidden)
    monkeypatch.setattr(monitor, "send_digest", forbidden)


class Run:
    def __init__(self, tmp_path, monkeypatch, *, count=1, excerpts=None, url=URL):
        self.root = tmp_path
        self.store = tmp_path / "snapshots"
        self.data_path = tmp_path / "data.json"
        self.checks = tmp_path / "checks"
        self.report = tmp_path / "report"
        self.responses = {"direct": 403, "headless": 403, "jina": 403, "wayback": None}
        self.calls, self.browsers, self.digests = [], [], []
        self.on_fetch = None
        self.busy = False
        self.cited_paths = []
        digest = hashlib.sha256(OLD).hexdigest()
        sources = []
        for i in range(count):
            sid = f"src{i}"
            source_url = url if count == 1 else f"{url}/{i}"
            path = self.store / sid / "copy.html"
            path.parent.mkdir(parents=True)
            path.write_bytes(OLD)
            self.cited_paths.append(path)
            (path.parent / "manifest.json").write_text(json.dumps([{
                "source_id": sid, "requested_url": source_url, "final_url": source_url,
                "method": "live", "retrieved": "2026-10-17T00:00:00Z", "mime": "text/html",
                "sha256": digest, "path": str(path.relative_to(self.store)),
            }]))
            sources.append({
                "id": sid, "title": "News", "url": source_url,
                "snapshot": {"sha256": digest, "retrieved": "2026-10-17T00:00:00Z", **snapshot_sources.text_fields(OLD, "text/html")},
                "excerpts": [{"text": q, "supports": [["institutes", {"id": "xx"}, "name"]]}
                             for q in (excerpts if excerpts is not None else ["founded in 2024", "publishes reports"])],
            })
        self.data = {"dataset": {"version": "1.2.3"}, "sources": sources, "jurisdictions": [], "institutes": []}
        self.write_data()

        def fetch(fetcher, url, method="live", headers=None):
            route = {"live": "direct", "render": "jina", "archive_lookup": "lookup", "archive_raw": "wayback"}[method]
            self.calls.append((route, url, headers, fetcher.cache, fetcher.delay))
            if self.on_fetch:
                self.on_fetch(route, url)
            if route == "lookup":
                capture = {} if self.responses["wayback"] is None else {"closest": {
                    "available": True, "timestamp": "20260102030405", "url": "https://web.archive.org/web/20260102030405/" + URL,
                }}
                response = {"body": json.dumps({"archived_snapshots": capture}).encode(), "content_type": "application/json"}
            else:
                response = self.responses[route]
            return self.write_response(fetcher.entry(url), url, route, response)

        def browser(urls, out, **kwargs):
            self.browsers.append((urls, out, kwargs))
            if self.busy:
                return False, None
            for url in urls:
                folder = out / hashlib.sha256(url.encode()).hexdigest()
                self.write_response(folder, url, "headless", self.responses["headless"])
            return True, 0

        monkeypatch.setattr(verify_excerpts.Fetcher, "fetch", fetch)
        monkeypatch.setattr(monitor, "run_browser", browser)
        monkeypatch.setattr(monitor, "send_digest", self.digests.append)

    def write_data(self):
        self.data_path.write_text(json.dumps(self.data))

    def write_response(self, folder, url, route, response):
        if isinstance(response, bytes):
            response = {"body": response}
        elif isinstance(response, int):
            response = {"status": "http_error", "http_status": response, "error": f"HTTP {response}"}
        response = dict(response or {"status": "unreachable"})
        body = response.pop("body", None)
        meta = {"url": url, "final_url": url, "status": "ok", "http_status": 200,
                "retrieved": "2026-11-10T05:00:01Z", "content_type": "text/html", **response}
        folder.mkdir(parents=True, exist_ok=True)
        if body is not None:
            (folder / "response.bin").write_bytes(body)
            meta["sha256"] = hashlib.sha256(body).hexdigest()
        (folder / "metadata.json").write_text(json.dumps(meta))
        return meta

    def run(self, *extra):
        return monitor.main([
            "--data", str(self.data_path), "--directory", str(self.store),
            "--checks", str(self.checks), "--report", str(self.report), "--workers", "1", *extra,
        ])

    def rows(self):
        return monitor.CheckLog(self.checks).read("checks")

    def events(self):
        return monitor.CheckLog(self.checks).read("runs")

    def resume(self, *extra):
        starts = [e for e in self.events() if e["event"] == "start"]
        return self.run("--resume", starts[-1]["run_id"], *extra)


@pytest.fixture
def run(tmp_path, monkeypatch):
    return Run(tmp_path, monkeypatch)


def test_direct_blocked_headless_keeps_quotes(run):
    run.responses["headless"] = OLD
    before = run.data_path.read_bytes(), [p.read_bytes() for p in run.cited_paths]
    assert run.run("--digest") == 0
    row, = run.rows()
    assert (row["class"], row["route"], row["observation"]) == ("trivial", "headless", "live")
    assert row["response"] == "cited"
    assert [a["route"] for a in row["attempts"]] == ["direct", "headless"]
    assert row["attempts"][0]["http"] == 403
    assert len(run.browsers) == 1 and run.browsers[0][2]["wait"] == 5400
    assert [c[0] for c in run.calls] == ["direct"]
    assert before == (run.data_path.read_bytes(), [p.read_bytes() for p in run.cited_paths])
    assert run.digests[0]["live_unchecked"] == 0
    assert run.digests[0]["baselines"] == [["8 Nov", 188], ["10 Nov", 157]]


@pytest.mark.parametrize("browser", [403, page("<p>Verify you are human</p>")])
def test_jina_markdown_keeps_both_quotations(tmp_path, monkeypatch, browser):
    run = Run(tmp_path, monkeypatch, excerpts=["founded in 2024", "The institute was founded in 2024"])
    run.responses.update(headless=browser, jina={"body": b"The [institute](https://x) was **founded in 2024**", "content_type": "text/plain"})
    assert run.run() == 0
    row, = run.rows()
    assert (row["class"], row["route"]) == ("trivial", "jina")
    assert all(q["in_checked_copy"] for q in row["quotes"])
    request, = [c for c in run.calls if c[0] == "jina"]
    assert request[2] == {"X-No-Cache": "true"} and request[4] >= 3
    assert (run.checks / row["response"]).read_bytes() == run.responses["jina"]["body"]


def test_markdown_view():
    value = "# Heading\n> - The [institute](https://x) was **founded in 2024**.\n![ignore](image) *a* __b__ _c_ `d`"
    assert monitor.markdown_view(value) == "Heading\nThe institute was founded in 2024.\n a b c d"


def test_both_fallbacks_lose_same_quote(run):
    changed = OLD.replace(b"founded in 2024", b"founded in 2025")
    run.responses.update(headless=changed, jina=changed)
    assert run.run("--digest") == 0
    row, = run.rows()
    assert (row["class"], row["reason"], row["route"]) == ("substantive", "quote_lost", "headless+jina")
    assert not any(c[0] == "lookup" for c in run.calls)
    assert run.digests[0]["substantive"] == [{"source_id": "src0", "reason": "quote_lost", "route": "headless+jina"}]


@pytest.mark.parametrize("jina", [403, OLD])
def test_one_missing_or_disagreement_is_unconfirmed(run, jina):
    run.responses.update(headless=OLD.replace(b"founded in 2024", b"founded in 2025"), jina=jina)
    assert run.run("--digest") == 0
    row, = run.rows()
    assert (row["class"], row["reason"]) == ("failed", "fallback_quote_unconfirmed")
    assert row["attempts"][-1]["route"] == "wayback"
    assert run.digests[0]["unconfirmed"][0]["reason"] == "fallback_quote_unconfirmed"


def test_live_loss_outranks_an_archive_capture_that_still_has_the_quotation(run):
    run.responses.update(headless=OLD.replace(b"founded in 2024", b"founded in 2025"), wayback=OLD)
    assert run.run("--digest") == 0
    row, = run.rows()
    assert (row["class"], row["reason"]) == ("failed", "fallback_quote_unconfirmed")
    assert row["attempts"][-1]["route"] == "wayback" and row["attempts"][-1]["class"] == "archived"
    assert run.digests[0]["unconfirmed"][0]["source_id"] == "src0"


def test_partial_render_missing_one_of_several_quotes(tmp_path, monkeypatch):
    run = Run(tmp_path, monkeypatch, excerpts=["founded in 2024", "publishes reports"])
    run.responses["headless"] = page("<p>The institute was founded in 2024.</p>")
    assert run.run() == 0
    row, = run.rows()
    assert row["reason"] == "fallback_quote_unconfirmed"
    assert {q["in_checked_copy"] for q in row["quotes"]} == {True, False}


def test_different_missing_quotes_do_not_confirm(tmp_path, monkeypatch):
    run = Run(tmp_path, monkeypatch, excerpts=["founded in 2024", "publishes reports"])
    run.responses.update(headless=OLD.replace(b"founded in 2024", b"founded in 2025"), jina=OLD.replace(b"publishes reports", b"publishes papers"))
    assert run.run() == 0
    assert run.rows()[0]["reason"] == "fallback_quote_unconfirmed"


@pytest.mark.parametrize("route", ["headless", "jina"])
def test_non_comparable_copy_needs_positive_evidence(tmp_path, monkeypatch, route):
    run = Run(tmp_path, monkeypatch, excerpts=[])
    run.responses[route] = page("<p>" + "unrelated words " * 100 + "</p>")
    assert run.run() == 0
    row, = run.rows()
    outcome, = [a for a in row["attempts"] if a["route"] == route]
    assert (outcome["class"], outcome["reason"]) == ("failed", "not_comparable")
    assert (row["class"], row["reason"]) == ("blocked", "HTTP 403")


def test_no_quotes_long_surviving_run_is_trivial(tmp_path, monkeypatch):
    run = Run(tmp_path, monkeypatch, excerpts=[])
    run.responses["headless"] = OLD.replace(b"<nav>Menu</nav>", b"<nav>New</nav>")
    assert run.run() == 0
    assert (run.rows()[0]["class"], run.rows()[0]["route"]) == ("trivial", "headless")


@pytest.mark.parametrize("old,new", [("one two", "one two three"), ("one two three four five six", "one two three other")])
def test_short_fallback_copy_can_keep_half_the_text(tmp_path, old, new):
    # A short word count can still have enough readable content to pass the shell guard.
    old, new = (" ".join(word * 100 for word in text.split()) for text in (old, new))
    path = tmp_path / "cited.txt"
    path.write_text(old)
    source = {"id": "src", "url": URL, "snapshot": {"sha256": hashlib.sha256(old.encode()).hexdigest()}}
    manifest = {"method": "live", "requested_url": URL, "mime": "text/plain"}
    row = monitor.compare_copy(source, (manifest, path), new.encode(), "text/plain", {"route": "jina", "final_url": URL, "http": 200})
    assert (row["class"], row["reason"]) == ("trivial", "small_change")


@pytest.mark.parametrize("lost", [False, True])
def test_archive_capture_uses_returned_timestamp(run, lost):
    returned = "https://web.archive.org/web/20260304050607id_/" + URL
    run.responses["wayback"] = {"body": OLD.replace(b"founded in 2024", b"founded in 2025") if lost else OLD, "final_url": returned}
    assert run.run("--digest") == 0
    row, = run.rows()
    assert (row["class"], row["route"], row["observation"], row["capture"]) == ("archived", "wayback", "archive", "20260304050607")
    assert row["reason"] == ("archive_quote_missing" if lost else "quotes_kept")
    assert run.digests[0]["live_unchecked"] == 1
    assert all(c[4] >= 5 for c in run.calls if c[0] in {"lookup", "wayback"})
    lookup, = [c for c in run.calls if c[0] == "lookup"]
    assert lookup[1] == "https://archive.org/wayback/available?url=https%3A%2F%2Fexample.gov%2Fnews"
    capture, = [c for c in run.calls if c[0] == "wayback"]
    assert capture[1] == "https://web.archive.org/web/20260102030405id_/" + URL


@pytest.mark.parametrize("capture", [503, page("<p>Verify you are human</p>"), {"body": OLD, "final_url": "https://web.archive.org/"}])
def test_failed_archive_keeps_direct_result(run, capture):
    run.responses["wayback"] = capture
    assert run.run() == 0
    row, = run.rows()
    assert (row["class"], row["reason"], row["observation"]) == ("blocked", "HTTP 403", "none")


@pytest.mark.parametrize("redirect", [False, True])
@pytest.mark.parametrize("url,capture", [("https://web.archive.org/web/20260102030405/" + URL, "20260102030405"), ("https://archive.ph/AbCdE", None)])
def test_capture_url_and_redirect_are_archive_observations(tmp_path, monkeypatch, redirect, url, capture):
    run = Run(tmp_path, monkeypatch, url=URL if redirect else url)
    run.responses["direct"] = {"body": OLD, "final_url": url}
    assert run.run("--digest") == 0
    row, = run.rows()
    assert (row["class"], row["observation"], row["capture"]) == ("unchanged", "archive", capture)
    assert run.digests[0]["live_unchecked"] == 1


def test_fallback_404_is_not_terminal(run):
    run.responses.update(headless=404, jina=404, wayback=OLD)
    assert run.run() == 0
    assert run.rows()[0]["class"] == "archived"


def test_browser_busy_attempt_and_one_batch(run):
    run.busy = True
    run.responses["jina"] = OLD
    assert run.run("--browser-wait", "7") == 0
    row, = run.rows()
    assert row["attempts"][1] == {"route": "headless", "http": None, "fetch_status": "not_run", "class": "failed", "reason": "browser busy"}
    assert run.browsers[0][2]["wait"] == 7


def test_all_sources_skipped_missing_copy_and_exact_log_fields(tmp_path, monkeypatch):
    run = Run(tmp_path, monkeypatch, count=3)
    run.data["sources"][0].pop("snapshot")
    run.data["sources"][1]["snapshot"]["sha256"] = "0" * 64
    run.responses["direct"] = OLD
    run.write_data()
    assert run.run() == 0
    rows = {r["source_id"]: r for r in run.rows()}
    assert len(rows) == len(run.data["sources"]) == 3
    assert (rows["src0"]["class"], rows["src0"]["reason"], rows["src0"]["route"]) == ("skipped", "no_snapshot_hash", "none")
    assert rows["src0"]["observation"] == "none"
    assert rows["src1"]["reason"] == "no cited copy held privately"
    assert len(run.calls) == 1 and not run.browsers
    assert set(rows["src2"]) == {"run_id", "dataset_revision", "source_id", "url", "checked", "route", "observation", "attempts", "http", "final_url", "file_sha256", "text_sha256", "text_scheme", "response", "quotes", "class", "reason", "verifier"}
    assert rows["src2"]["quotes"] == [{"id": "sha256:" + hashlib.sha256(text).hexdigest(), "in_cited_copy": True, "in_checked_copy": True}
                                       for text in (b"founded in 2024", b"publishes reports")]
    assert rows["src2"]["checked"] == "2026-11-10T05:00:01Z"


def test_only_restricts_sources(tmp_path, monkeypatch):
    run = Run(tmp_path, monkeypatch, count=2)
    run.responses["direct"] = OLD
    assert run.run("--only", "src1") == 0
    assert [r["source_id"] for r in run.rows()] == ["src1"]


def test_torn_lines_are_repaired_under_journal_locks(run):
    run.responses["direct"] = OLD
    assert run.run() == 0
    for name in ("checks", "runs"):
        with (run.checks / f"{name}.jsonl").open("ab") as handle:
            handle.write(b'{"run_id": "torn')
    assert run.resume() == 0
    assert len(run.rows()) == 1 and len(run.calls) == 1
    assert [e["event"] for e in run.events()].count("lines_done") == 1


@pytest.mark.parametrize("stage", ["temp", "row"])
def test_crashes_during_response_and_before_row_are_resumable(run, monkeypatch, stage):
    run.responses["direct"] = OLD.replace(b"reports.", b"reports today.")
    failed = False
    original_replace, original_append = Path.replace, monitor.append_line

    def replace(path, target):
        nonlocal failed
        if stage == "temp" and path.parent.name == "responses" and not failed:
            failed = True
            raise OSError("crash before rename")
        return original_replace(path, target)

    def append(path, row):
        nonlocal failed
        if stage == "row" and path.name == "checks.jsonl" and not failed:
            failed = True
            raise OSError("crash before row")
        original_append(path, row)

    monkeypatch.setattr(Path, "replace", replace)
    monkeypatch.setattr(monitor, "append_line", append)
    assert run.run("--digest") == 1
    assert run.digests[0]["error"].startswith("crash before")
    paths = list((run.checks / "responses").iterdir())
    assert len(paths) == 1 and paths[0].name.endswith(".tmp") == (stage == "temp")
    assert run.resume("--digest") == 0
    row, = run.rows()
    assert hashlib.sha256((run.checks / row["response"]).read_bytes()).hexdigest() == row["file_sha256"]
    assert not list((run.checks / "responses").glob("*.tmp"))
    assert len(run.calls) == 2 and run.calls[0][3] != run.calls[1][3]


def test_resume_complete_rebuilds_report_and_digest_only(run):
    run.responses["direct"] = OLD
    assert run.run("--digest") == 0
    before = run.rows()
    (run.report / "monitor.md").write_text("damaged report")
    assert run.resume("--digest") == 0
    assert run.rows() == before and len(run.calls) == 1 and not run.browsers
    assert len(run.digests) == 2
    assert "Source change monitor" in (run.report / "monitor.md").read_text()
    assert [e["event"] for e in run.events()] == ["start", "lines_done", "report_written", "digest_sent", "report_written", "digest_sent"]


def test_failed_digest_is_retried_without_fetch(run, monkeypatch):
    run.responses["direct"] = OLD
    def failed(summary):
        raise RuntimeError("notification failed")
    monkeypatch.setattr(monitor, "send_digest", failed)
    assert run.run("--digest") == 1
    assert run.events()[-1]["event"] == "digest_failed"
    monkeypatch.setattr(monitor, "send_digest", run.digests.append)
    assert run.resume("--digest") == 0
    assert len(run.calls) == 1 and run.events()[-1]["event"] == "digest_sent"


def test_resume_refuses_different_data_hash(run):
    run.responses["direct"] = OLD
    assert run.run() == 0
    run.data["dataset"]["version"] = "1.2.4"
    run.write_data()
    assert run.resume() == 1
    assert len(run.calls) == 1


@pytest.mark.parametrize("cited", [False, True])
@pytest.mark.parametrize("damage", ["missing", "wrong_hash"])
def test_resume_refuses_damaged_response_without_a_second_row(run, capsys, cited, damage):
    run.responses["direct"] = OLD if cited else OLD.replace(b"reports.", b"reports today.")
    assert run.run() == 0
    row, = run.rows()
    journal = (run.checks / "checks.jsonl").read_bytes()
    response = run.cited_paths[0] if cited else run.checks / row["response"]
    if damage == "missing":
        response.unlink()
    else:
        response.write_bytes(b"corrupt")
    assert run.resume("--digest") == 1
    assert "invalid saved response for source src0" in capsys.readouterr().err
    assert len(run.calls) == 1 and run.rows() == [row]
    assert (run.checks / "checks.jsonl").read_bytes() == journal
    assert "src0" in run.digests[-1]["error"]


def test_record_refuses_a_duplicate_before_saving_a_response(run, monkeypatch):
    run.responses["direct"] = OLD
    assert run.run() == 0
    row, = run.rows()
    log = monitor.CheckLog(run.checks)
    def forbidden(*args):
        raise AssertionError("duplicate reached response storage")
    monkeypatch.setattr(log, "save_response", forbidden)
    result = {**row, "_body": b"different", "_mime": "text/plain"}
    with pytest.raises(ValueError, match="duplicate check row.*source src0"):
        log.record(row["run_id"], row["dataset_revision"], run.data["sources"][0], result)
    assert run.rows() == [row]


def test_resume_rejects_existing_duplicate_rows(run):
    run.responses["direct"] = OLD
    assert run.run() == 0
    row, = run.rows()
    with monitor.file_lock(run.checks / "checks.lock"):
        monitor.append_line(run.checks / "checks.jsonl", row)
    assert run.resume("--digest") == 1
    assert "duplicate check row" in run.digests[-1]["error"]
    assert len(run.calls) == 1 and len(run.rows()) == 2


@pytest.mark.parametrize("signum", [signal.SIGINT, signal.SIGTERM])
def test_signal_cancels_pending_sources_and_resume_keeps_written_rows(tmp_path, monkeypatch, signum):
    run = Run(tmp_path, monkeypatch, count=5)
    run.responses["direct"] = OLD
    def interrupt(route, url):
        if url.endswith("/1"):
            os.kill(os.getpid(), signum)
            time.sleep(0.05)
    run.on_fetch = interrupt
    assert run.run("--digest") == 1
    assert len(run.calls) == 2
    first, = run.rows()
    assert first["source_id"] == "src0"
    assert run.digests[0]["interrupted"] and not run.digests[0]["complete"]
    run.on_fetch = None
    assert run.resume("--digest") == 0
    assert len(run.rows()) == 5
    assert [r for r in run.rows() if r["source_id"] == "src0"] == [first]


def test_deadline_stops_new_fetches(run):
    assert run.run("--deadline-hours", "0", "--digest") == 1
    assert not run.calls and not run.rows()
    assert run.digests[0]["error"] == "deadline" and run.digests[0]["interrupted"]


def test_unexpected_error_sends_error_digest(run, monkeypatch):
    def fail(*args, **kwargs):
        raise RuntimeError("unexpected tool error")
    monkeypatch.setattr(monitor, "run_browser", fail)
    assert run.run("--digest") == 1
    assert run.digests[0]["error"] == "unexpected tool error"
    assert not run.digests[0]["interrupted"]
    assert any(e["event"] == "interrupted" for e in run.events())


def test_unreadable_fallbacks_cannot_confirm_quotation_loss(run):
    run.responses.update(headless=b"%PDF-1.7 broken", jina=b"%PDF-1.7 broken")
    assert run.run() == 0
    row, = run.rows()
    assert (row["class"], row["reason"]) == ("blocked", "HTTP 403")
    assert row["attempts"][-1]["route"] == "wayback"


def test_report_construction_exception_still_sends_error_digest(run, monkeypatch):
    run.responses["direct"] = OLD
    def broken(*args):
        raise RuntimeError("report construction failed")
    monkeypatch.setattr(monitor, "build_report", broken)
    assert run.run("--digest") == 1
    assert run.digests[0]["error"] == "report construction failed"
    assert run.digests[0]["complete"]
    assert [e["event"] for e in run.events()] == ["start", "lines_done", "interrupted", "digest_sent"]


def test_missing_capture_body_is_not_an_archive_observation(run):
    run.responses["wayback"] = {"status": "ok", "sha256": "0" * 64}
    assert run.run() == 0
    row, = run.rows()
    assert (row["class"], row["observation"]) == ("blocked", "none")


def test_headless_http_status_without_error_text(run):
    run.responses.update(headless={"status": "http_error", "http_status": 403}, jina=OLD)
    assert run.run() == 0
    assert run.rows()[0]["attempts"][1]["class"] == "blocked"


@pytest.mark.parametrize("where", ["fetch", "comparison"])
def test_direct_source_error_is_terminal_and_other_sources_finish(tmp_path, monkeypatch, where):
    run = Run(tmp_path, monkeypatch, count=2)
    run.responses["direct"] = OLD
    original = monitor.compare_copy
    def fail_fetch(route, url):
        if url.endswith("/0"):
            raise ValueError("invalid source response")
    def fail_comparison(source, *args, **kwargs):
        if source["id"] == "src0":
            raise ValueError("invalid source response")
        return original(source, *args, **kwargs)
    if where == "fetch":
        run.on_fetch = fail_fetch
    else:
        monkeypatch.setattr(monitor, "compare_copy", fail_comparison)
    assert run.run("--digest") == 0
    rows = {r["source_id"]: r for r in run.rows()}
    assert (rows["src0"]["class"], rows["src0"]["route"]) == ("failed", "direct")
    assert rows["src0"]["reason"] == "tool error: ValueError: invalid source response"
    assert rows["src1"]["class"] == "unchanged" and not run.browsers
    assert run.digests[-1]["complete"] and run.digests[-1]["error"] is None
    assert run.resume("--digest") == 0
    assert len(run.calls) == 2 and len(run.rows()) == 2


@pytest.mark.parametrize("where", ["metadata", "comparison"])
def test_headless_source_error_keeps_other_sources_running(tmp_path, monkeypatch, where):
    run = Run(tmp_path, monkeypatch, count=2)
    run.responses["headless"] = OLD
    original_browser, original_compare = monitor.run_browser, monitor.compare_copy
    def broken_metadata(urls, out, **kwargs):
        result = original_browser(urls, out, **kwargs)
        folder = out / hashlib.sha256((URL + "/0").encode()).hexdigest()
        (folder / "metadata.json").write_text("{")
        return result
    def broken_comparison(source, *args, **kwargs):
        if source["id"] == "src0":
            raise ValueError("bad headless comparison")
        return original_compare(source, *args, **kwargs)
    if where == "metadata":
        monkeypatch.setattr(monitor, "run_browser", broken_metadata)
    else:
        monkeypatch.setattr(monitor, "compare_copy", broken_comparison)
    assert run.run("--digest") == 0
    rows = {r["source_id"]: r for r in run.rows()}
    assert (rows["src0"]["class"], rows["src0"]["route"]) == ("failed", "headless")
    assert rows["src0"]["reason"].startswith("tool error:")
    assert [a["route"] for a in rows["src0"]["attempts"]] == ["direct", "headless"]
    assert rows["src1"]["class"] == "trivial" and run.digests[-1]["complete"]


@pytest.mark.parametrize("route", ["jina", "wayback"])
@pytest.mark.parametrize("where", ["fetch", "comparison"])
def test_later_fallback_errors_are_per_source(tmp_path, monkeypatch, route, where):
    run = Run(tmp_path, monkeypatch, count=2)
    run.responses[route] = OLD
    original = monitor.checked_result
    def failed_fetch(current, url):
        if current == route and url.endswith("/0"):
            raise ValueError("bad fallback response")
    def failed_comparison(source, cited, meta, path, current):
        if current == route and source["id"] == "src0":
            raise ValueError("bad fallback response")
        return original(source, cited, meta, path, current)
    if where == "fetch":
        run.on_fetch = failed_fetch
    else:
        monkeypatch.setattr(monitor, "checked_result", failed_comparison)
    assert run.run("--digest") == 0
    rows = {r["source_id"]: r for r in run.rows()}
    assert (rows["src0"]["class"], rows["src0"]["route"]) == ("failed", route)
    assert rows["src0"]["reason"] == "tool error: ValueError: bad fallback response"
    assert rows["src0"]["attempts"][-1]["route"] == route
    assert rows["src1"]["class"] == ("trivial" if route == "jina" else "archived")
    assert run.digests[-1]["complete"] and run.digests[-1]["error"] is None


def test_readable_shells_cannot_confirm_loss(run):
    run.responses.update(headless=page("<p>Choose your region</p>"), jina=page("<p>Page loading</p>"))
    assert run.run() == 0
    row, = run.rows()
    assert row["class"] == "blocked" and row["reason"] == "HTTP 403"
    for route in ("headless", "jina"):
        outcome, = [a for a in row["attempts"] if a["route"] == route]
        assert (outcome["class"], outcome["reason"]) == ("blocked", "near_empty_response")


@pytest.mark.parametrize("excerpts", [["founded in 2024"], ["founded in 2024", "new fragment"]])
def test_missing_all_carried_quotes_is_not_comparable_despite_text_overlap(tmp_path, monkeypatch, excerpts):
    run = Run(tmp_path, monkeypatch, excerpts=excerpts)
    body = OLD.replace(b"founded in 2024", b"founded in 2025") + b" new fragment"
    run.responses.update(headless=body, jina=body)
    assert run.run() == 0
    row, = run.rows()
    assert row["class"] == "blocked"
    for route in ("headless", "jina"):
        outcome, = [a for a in row["attempts"] if a["route"] == route]
        assert (outcome["class"], outcome["reason"]) == ("failed", "not_comparable")


def test_only_one_comparable_fallback_cannot_confirm_loss(run):
    run.responses.update(
        headless=OLD.replace(b"founded in 2024", b"founded in 2025"),
        jina=OLD.replace(b"founded in 2024", b"founded in 2025").replace(b"publishes reports", b"publishes papers"),
    )
    assert run.run() == 0
    row, = run.rows()
    assert (row["class"], row["reason"]) == ("failed", "fallback_quote_unconfirmed")
    assert row["attempts"][2]["reason"] == "not_comparable"


def test_short_fallback_without_quotes_is_a_shell(tmp_path, monkeypatch):
    run = Run(tmp_path, monkeypatch, excerpts=[])
    run.responses["headless"] = page("<p>The institute was founded in 2024.</p>")
    assert run.run() == 0
    assert run.rows()[0]["attempts"][1]["reason"] == "near_empty_response"


def test_report_confirms_only_the_shared_missing_quotations(tmp_path, monkeypatch):
    run = Run(tmp_path, monkeypatch, excerpts=["founded in 2024", "publishes reports", "budget of 10 million euro"])
    run.responses.update(
        headless=OLD.replace(b"founded in 2024", b"founded in 2025"),
        jina=OLD.replace(b"founded in 2024", b"founded in 2025").replace(b"publishes reports", b"publishes papers"),
    )
    assert run.run() == 0
    row, = run.rows()
    assert row["class"] == "substantive"
    assert row["confirmed_quote_ids"] == ["sha256:" + hashlib.sha256(b"founded in 2024").hexdigest()]
    assert sum(not q["in_checked_copy"] for q in row["quotes"]) == 2
    def check_report():
        text = (run.report / "monitor.md").read_text()
        confirmed = text.split("## Quotations no longer present\n", 1)[1].split("## Unconfirmed quotation losses", 1)[0]
        unconfirmed = text.split("## Unconfirmed quotation losses\n", 1)[1].split("## Page text changed", 1)[0]
        assert '"founded in 2024"' in confirmed and '"publishes reports"' not in confirmed
        assert '"publishes reports"' in unconfirmed and '"founded in 2024"' not in unconfirmed
    check_report()
    assert run.resume() == 0
    assert run.rows() == [row]
    check_report()


def test_run_lock_excludes_another_process(run):
    run.responses["direct"] = OLD
    assert run.run() == 0
    run_id = run.events()[0]["run_id"]
    lock = run.checks / "runs" / f"{run_id}.lock"
    code = "import fcntl,sys; f=open(sys.argv[1], 'a+b'); fcntl.flock(f, fcntl.LOCK_EX); print('locked', flush=True); sys.stdin.read()"
    process = subprocess.Popen([sys.executable, "-c", code, str(lock)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        assert process.stdout.readline().strip() == "locked"
        assert run.resume() == 1
        assert len(run.calls) == 1
    finally:
        process.communicate("", timeout=5)
    assert run.resume() == 0


def test_checks_directory_cannot_be_inside_snapshots(run):
    with pytest.raises(SystemExit):
        run.run("--checks", str(run.store / "checks"))
    assert not run.calls


def test_responses_directory_cannot_be_the_snapshot_directory(run):
    snapshots = run.checks / "responses"
    snapshots.mkdir(parents=True)
    sentinel = snapshots / "cited.tmp"
    sentinel.write_bytes(b"keep this snapshot")
    with pytest.raises(SystemExit):
        run.run("--directory", str(snapshots))
    assert sentinel.read_bytes() == b"keep this snapshot"
    assert sorted(p.name for p in run.checks.iterdir()) == ["responses"]
    assert not run.calls and not run.report.exists()


@pytest.mark.parametrize("store_name", ["primary", "fallback"])
@pytest.mark.parametrize("destination", ["checks", "responses", "report", "report_file", "cache", "cache_entry"])
def test_resolved_destinations_protect_both_snapshot_stores(run, monkeypatch, store_name, destination):
    if store_name == "primary":
        protected = run.store
    else:
        monkeypatch.setattr(monitor, "ROOT", run.root / "project")
        protected = monitor.ROOT / "work/snapshots"
        protected.mkdir(parents=True)
    sentinel = protected / "cited.tmp"
    sentinel.write_bytes(b"keep this snapshot")
    extra = []
    if destination == "checks":
        run.checks.symlink_to(protected, target_is_directory=True)
    elif destination == "responses":
        run.checks.mkdir()
        (run.checks / "responses").symlink_to(protected, target_is_directory=True)
    elif destination == "report":
        run.report.symlink_to(protected, target_is_directory=True)
    elif destination == "report_file":
        run.report.mkdir()
        (run.report / "monitor.json").symlink_to(sentinel)
    else:
        cache = run.root / "cache"
        extra = ["--cache", str(cache)]
        if destination == "cache":
            cache.symlink_to(protected, target_is_directory=True)
        else:
            cache.mkdir()
            (cache / hashlib.sha256(URL.encode()).hexdigest()).symlink_to(protected, target_is_directory=True)
    before = {p.relative_to(protected): p.read_bytes() for p in protected.rglob("*") if p.is_file()}
    if destination == "cache_entry":
        assert run.run(*extra) == 1
    else:
        with pytest.raises(SystemExit):
            run.run(*extra)
    assert not run.calls and not (run.checks / "checks.lock").exists()
    assert before == {p.relative_to(protected): p.read_bytes() for p in protected.rglob("*") if p.is_file()}


@pytest.mark.parametrize("store_name", ["primary", "fallback"])
def test_derived_route_cache_is_checked_before_log_cleanup(run, monkeypatch, store_name):
    run.responses["direct"] = OLD
    assert run.run() == 0
    if store_name == "primary":
        protected = run.store
    else:
        monkeypatch.setattr(monitor, "ROOT", run.root / "project")
        protected = monitor.ROOT / "work/snapshots"
        protected.mkdir(parents=True)
    run_id = run.events()[0]["run_id"]
    cache = run.report / f"cache-{run_id}" / "direct"
    cache.rename(cache.with_name("previous-direct"))
    cache.symlink_to(protected, target_is_directory=True)
    # Even ordinary log cleanup must not happen before all destinations are checked.
    leftover = run.checks / "responses" / "orphan.tmp"
    leftover.write_bytes(b"not cleaned yet")
    assert run.run("--resume", run_id) == 1
    assert leftover.read_bytes() == b"not cleaned yet"
    assert len(run.calls) == 1


def test_check_log_validates_before_creating_or_cleaning(run):
    sentinel = run.store / "cited.tmp"
    sentinel.write_bytes(b"keep this snapshot")
    run.checks.mkdir()
    (run.checks / "responses").symlink_to(run.store, target_is_directory=True)
    with pytest.raises(ValueError, match="outside both snapshot stores"):
        monitor.CheckLog(run.checks, protected=(run.store,))
    assert sentinel.read_bytes() == b"keep this snapshot"
    assert not (run.checks / "checks.lock").exists()


def test_fetcher_extra_headers_and_http_status(tmp_path, monkeypatch):
    seen = []
    class Response:
        status = 201
        headers = {"Content-Type": "text/html"}
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return False
        def geturl(self):
            return URL
        def read(self, size):
            return OLD

    def open_url(req, **kwargs):
        seen.append(req)
        return Response()

    monkeypatch.setattr(verify_excerpts, "urlopen", open_url)
    fetcher = verify_excerpts.Fetcher(tmp_path / "cache", delay=0)
    meta = fetcher.fetch(URL, "render", headers={"X-No-Cache": "true"})
    assert meta["http_status"] == 201
    assert seen[0].get_header("X-no-cache") == "true"
    assert seen[0].get_header("User-agent") == "AISI-tracker-evidence/1.0"
    def refused(req, **kwargs):
        raise HTTPError(req.full_url, 403, "forbidden", {}, None)
    monkeypatch.setattr(verify_excerpts, "urlopen", refused)
    assert fetcher.fetch(URL + "/other", "live")["http_status"] == 403


def _missing(row):
    return {q["id"] for q in row["quotes"] if q["in_cited_copy"] and not q["in_checked_copy"]}


def _qid(text):
    return "sha256:" + hashlib.sha256(text.encode()).hexdigest()


def test_headless_extra_loss_stays_unconfirmed_when_one_is_confirmed(tmp_path, monkeypatch):
    run = Run(tmp_path, monkeypatch, excerpts=["founded in 2024", "publishes reports", "budget of 10 million euro"])
    run.responses.update(
        headless=OLD.replace(b"founded in 2024", b"founded in 2025").replace(b"publishes reports", b"publishes papers"),
        jina=OLD.replace(b"founded in 2024", b"founded in 2025"),
    )
    assert run.run() == 0
    row, = run.rows()
    assert row["class"] == "substantive" and row["confirmed_quote_ids"] == [_qid("founded in 2024")]
    assert _missing(row) == {_qid("founded in 2024"), _qid("publishes reports")}
    unconfirmed = (run.report / "monitor.md").read_text().split("## Unconfirmed quotation losses\n", 1)[1]
    assert '"publishes reports"' in unconfirmed.split("## Page text changed", 1)[0]


def test_disjoint_losses_keep_both(tmp_path, monkeypatch):
    run = Run(tmp_path, monkeypatch, excerpts=["founded in 2024", "publishes reports", "budget of 10 million euro"])
    run.responses.update(
        headless=OLD.replace(b"founded in 2024", b"founded in 2025"),
        jina=OLD.replace(b"publishes reports", b"publishes papers"),
    )
    assert run.run() == 0
    row, = run.rows()
    assert (row["class"], row["reason"]) == ("failed", "fallback_quote_unconfirmed")
    assert _missing(row) == {_qid("founded in 2024"), _qid("publishes reports")}


def test_signal_during_browser_phase_is_resumable(tmp_path, monkeypatch):
    run = Run(tmp_path, monkeypatch, count=3)
    run.responses.update(direct=403, headless=OLD)
    browser = monitor.run_browser

    def interrupted(urls, out, **kwargs):
        os.kill(os.getpid(), signal.SIGTERM)  # the monitor's handler stops the run during the batch
        time.sleep(0.05)
        return browser(urls, out, **kwargs)

    monkeypatch.setattr(monitor, "run_browser", interrupted)
    assert run.run("--digest") == 1
    assert run.rows() == []
    assert run.digests[0]["interrupted"] and not run.digests[0]["complete"]
    assert any(e["event"] == "interrupted" for e in run.events())
    monkeypatch.setattr(monitor, "run_browser", browser)
    assert run.resume("--digest") == 0
    rows = run.rows()
    assert sorted(r["source_id"] for r in rows) == ["src0", "src1", "src2"]
    assert {(r["class"], r["route"]) for r in rows} == {("trivial", "headless")}
