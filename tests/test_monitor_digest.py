"""Monitor digests and notification failures, without network access."""

import json
import subprocess
import sys
import traceback
import urllib.request

import pytest

from scripts import monitor_digest as digest


@pytest.fixture(autouse=True)
def no_requests(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Unexpected network request")

    monkeypatch.setattr(urllib.request, "urlopen", forbidden)


@pytest.fixture
def summary():
    return {
        "run_id": "20261110T050000Z-abcdef",
        "dataset_revision": {
            "commit": "e8389e60123456789abcdef0123456789abcdef0123",
            "data_sha256": "0" * 64,
            "version": "1.2.3",
        },
        "seconds": 830,
        "counts": {
            "skipped": 6, "failed": 93, "blocked": 64, "archived": 2,
            "unchanged": 699, "trivial": 317, "gone": 6, "substantive": 8,
        },
        "live_unchecked": 159,
        "baselines": [["8 Nov", 188], ["10 Nov", 157]],
        "target": 100,
        "by_route": {"wayback": 2, "jina": 4, "headless": 3},
        "substantive": [
            {"source_id": "src-changed", "reason": "quote_lost", "route": "headless+jina"},
        ],
        "gone": [{"source_id": "src-gone", "reason": "HTTP 404", "route": "direct"}],
        "unconfirmed": [
            {"source_id": "src-partial", "reason": "fallback_quote_unconfirmed", "route": "jina"},
            {"source_id": "src-archive", "reason": "archive_quote_missing", "route": "wayback"},
        ],
        "next_checks_due": ["GB", "FR", "DE", "IT", "ES", "PT"],
        "report": "work/monitor/monitor.md",
        "error": "monitor stopped",
        "interrupted": False,
        "complete": False,
    }


def test_full_summary(summary):
    title, body = digest.build(summary)
    assert title == "FAILED: AISI monitor 2026-11-10"
    assert body.splitlines() == [
        "Run 20261110T050000Z-abcdef",
        "Dataset 1.2.3 (e8389e6); 13.8 minutes",
        "substantive 8, gone 6, trivial 317, unchanged 699, archived 2, blocked 64, failed 93, skipped 6",
        "Live-unchecked 159 (8 Nov 188, 10 Nov 157; target under 100)",
        "Decided by fallback: headless 3, jina 4, wayback 2",
        "Substantive:",
        "src-changed (quote_lost, headless+jina)",
        "Gone:",
        "src-gone (HTTP 404, direct)",
        "Unconfirmed:",
        "src-archive (archive_quote_missing, wayback)",
        "src-partial (fallback_quote_unconfirmed, jina)",
        "next_check due: 6 (GB, FR, DE, IT, ES)",
        "Not complete: resume with --resume 20261110T050000Z-abcdef",
        "Error: monitor stopped",
        "work/monitor/monitor.md",
    ]
    assert len(body) <= 1500


@pytest.mark.parametrize("group", ["substantive", "gone", "unconfirmed"])
def test_at_most_eight_sources(summary, group):
    summary[group] = [
        {"source_id": f"src-{i:02}", "reason": "quote_lost", "route": "direct"}
        for i in reversed(range(11))
    ]
    _, body = digest.build(summary)
    for i in range(8):
        assert f"src-{i:02} (quote_lost, direct)" in body
    assert "src-08" not in body
    assert "src-09" not in body
    assert "src-10" not in body
    assert "src-07 (quote_lost, direct)\nand 3 more" in body


def test_reread_alerts_count_and_first_three(summary):
    summary["reread_alerts"] = [f"source s{n}: offline since 2026-11-01 (1 quote); triage was due 2026-11-08" for n in range(5)]
    summary["reread_alerts"][0] += " " + "x" * 200
    _, body = digest.build(summary)
    lines = body.splitlines()
    start = lines.index("Re-read alerts: 5")
    assert lines[start + 1].endswith("...") and len(lines[start + 1]) == 120
    assert lines[start + 2:start + 4] == summary["reread_alerts"][1:3] and "source s3" not in body


def test_empty_lists_and_zero_counts_omitted(summary):
    summary.update(
        substantive=[], gone=[], unconfirmed=[], next_checks_due=[],
        by_route={"headless": 0}, counts={"gone": 0, "unchanged": 12},
        error=None, interrupted=False, complete=True,
    )
    title, body = digest.build(summary)
    assert title == "AISI monitor 2026-11-10"
    assert body.splitlines() == [
        "Run 20261110T050000Z-abcdef",
        "Dataset 1.2.3 (e8389e6); 13.8 minutes",
        "unchanged 12",
        "Live-unchecked 159 (8 Nov 188, 10 Nov 157; target under 100)",
        "work/monitor/monitor.md",
    ]
    summary["counts"] = {}
    summary["by_route"] = {}
    assert "\n\n" not in digest.build(summary)[1]


@pytest.mark.parametrize(
    "error,interrupted,prefix",
    [(None, False, ""), ("failure", False, "FAILED: "),
     (None, True, "INTERRUPTED: "), ("SIGTERM", True, "INTERRUPTED: ")],
)
def test_titles(summary, error, interrupted, prefix):
    summary.update(error=error, interrupted=interrupted)
    assert digest.build(summary)[0] == prefix + "AISI monitor 2026-11-10"


def test_extended_run_stamp_and_missing_commit(summary):
    summary["run_id"] = "2026-11-10T05-00-00Z-abcdef"
    summary["dataset_revision"]["commit"] = None
    title, body = digest.build(summary)
    assert title == "FAILED: AISI monitor 2026-11-10"
    assert "Dataset 1.2.3 (unknown)" in body
    assert "Not complete: resume with --resume 2026-11-10T05-00-00Z-abcdef" in body


def test_baselines_and_routes_use_summary_values(summary):
    summary.update(baselines=[["17 Nov", 120]], target=80, by_route={"jina": 7})
    _, body = digest.build(summary)
    assert "Live-unchecked 159 (17 Nov 120; target under 80)" in body
    assert "Decided by fallback: jina 7\n" in body
    summary["baselines"] = []
    assert "Live-unchecked 159 (target under 80)" in digest.build(summary)[1]


@pytest.mark.parametrize("reason", ["quote_lost", "long failure " * 200])
def test_large_lists_keep_body_short_and_preserve_tail(summary, reason):
    for group in ("substantive", "gone", "unconfirmed"):
        summary[group] = [
            {"source_id": f"{group}-source-{i:03}", "reason": reason, "route": "headless"}
            for i in range(100)
        ]
    summary["error"] = "failure\n" * 200
    _, body = digest.build(summary)
    assert len(body) <= 1500
    for group in ("substantive", "gone", "unconfirmed"):
        section = body.split(group.capitalize() + ":\n", 1)[1]
        rows = section.splitlines()
        visible = sum(row.startswith(f"{group}-source-") for row in rows)
        assert visible <= 8
        assert f"and {100 - visible} more" in rows
    assert "next_check due: 6 (GB, FR, DE, IT, ES)" in body
    assert "Not complete: resume with --resume 20261110T050000Z-abcdef" in body
    assert "Error: failure failure" in body
    assert body.endswith("...\nwork/monitor/monitor.md")


def test_source_details_do_not_include_personal_fields(summary):
    summary["substantive"][0].update(name="PRIVATE NAME", email="private@example.org")
    summary["dataset_revision"]["maintainer"] = "PRIVATE MAINTAINER"
    _, body = digest.build(summary)
    assert "PRIVATE" not in body
    assert "private@example.org" not in body


@pytest.fixture
def fake_sender(tmp_path, monkeypatch):
    sender = tmp_path / "notify"
    sender.write_text(
        f"#!{sys.executable}\n"
        "import json, os, pathlib, sys\n"
        "pathlib.Path(os.environ['DIGEST_RECORD']).write_text(json.dumps({'argv': sys.argv}))\n"
        "sys.stderr.write('  rejected by test sender  \\n')\n"
        "sys.exit(int(os.environ['DIGEST_EXIT']))\n",
        encoding="utf-8",
    )
    sender.chmod(0o755)
    record = tmp_path / "record.json"
    monkeypatch.setenv("DIGEST_RECORD", str(record))
    return sender, record


@pytest.mark.parametrize("exit_code", [0, 1])
def test_send_runs_the_configured_command(fake_sender, monkeypatch, exit_code):
    sender, record = fake_sender
    monkeypatch.setenv("DIGEST_EXIT", str(exit_code))
    monkeypatch.setenv(digest.COMMAND, f"{sender} 'a channel'")
    title = 'AISI monitor "test"'
    body = "first line\nsecond line; $(no-shell)"
    if exit_code:
        with pytest.raises(RuntimeError) as exc:
            digest.send(title, body)
        assert str(exc.value) == "notify exited 1: rejected by test sender"
    else:
        assert digest.send(title, body) is None
    assert json.loads(record.read_text())["argv"] == [str(sender), "a channel", title, body]


@pytest.mark.parametrize("value", [None, "", "   ", "''"])
def test_send_needs_a_command(monkeypatch, value):
    if value is None:
        monkeypatch.delenv(digest.COMMAND, raising=False)
    else:
        monkeypatch.setenv(digest.COMMAND, value)
    with pytest.raises(RuntimeError, match=digest.COMMAND):
        digest.send("title", "body")


@pytest.mark.parametrize("stderr", [b"  timed out detail \n", "  timed out detail \n", None])
def test_send_timeout(monkeypatch, stderr):
    monkeypatch.setenv(digest.COMMAND, "notify --flag")

    def timeout(cmd, **kwargs):
        assert cmd == ["notify", "--flag", "title", "body"]
        assert kwargs["timeout"] == 60
        raise subprocess.TimeoutExpired(cmd, 60, stderr=stderr)

    monkeypatch.setattr(digest.subprocess, "run", timeout)
    with pytest.raises(RuntimeError) as exc:
        digest.send("title", "body")
    expected = "notify timed out after 60 seconds"
    if stderr is not None:
        expected += ": timed out detail"
    assert str(exc.value) == expected


def test_send_missing(tmp_path, monkeypatch):
    monkeypatch.setenv(digest.COMMAND, str(tmp_path / "absent"))
    with pytest.raises(RuntimeError, match="absent not found"):
        digest.send("title", "body")


@pytest.mark.parametrize("path", ["exit", "timeout"])
def test_send_errors_never_carry_a_url(monkeypatch, path):
    monkeypatch.setenv(digest.COMMAND, "notify")
    stderr = "curl: (22) https://notify.example/secret-topic returned 403\n"

    def run(cmd, **kwargs):
        if path == "timeout":
            raise subprocess.TimeoutExpired(cmd, 60, stderr=stderr)
        return subprocess.CompletedProcess(cmd, 1, "", stderr)

    monkeypatch.setattr(digest.subprocess, "run", run)
    with pytest.raises(RuntimeError) as exc:
        digest.send("title", "body")
    assert "secret-topic" not in str(exc.value)
    assert "<url>" in str(exc.value)
    assert "secret-topic" not in "".join(traceback.format_exception(exc.value))


def test_timeout_traceback_leaves_out_the_command(monkeypatch):
    monkeypatch.setenv(digest.COMMAND, "notify https://notify.example/secret-topic")

    def run(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, 60)

    monkeypatch.setattr(digest.subprocess, "run", run)
    with pytest.raises(RuntimeError) as exc:
        digest.send("title", "body")
    assert "secret-topic" not in "".join(traceback.format_exception(exc.value))


def test_send_rejects_an_unparsable_command(monkeypatch):
    monkeypatch.setenv(digest.COMMAND, "notify 'unclosed")
    with pytest.raises(RuntimeError, match="not a valid command line"):
        digest.send("title", "body")


def test_send_accepts_a_quoted_path_with_spaces(fake_sender, monkeypatch, tmp_path):
    sender, record = fake_sender
    spaced = tmp_path / "with space"
    spaced.mkdir()
    target = spaced / "notify"
    target.write_bytes(sender.read_bytes())
    target.chmod(0o755)
    monkeypatch.setenv("DIGEST_EXIT", "0")
    monkeypatch.setenv(digest.COMMAND, f"'{target}' channel")
    digest.send("title", "body")
    assert json.loads(record.read_text())["argv"] == [str(target), "channel", "title", "body"]


def test_body_limit_holds_without_source_rows(summary):
    for name in ("substantive", "gone", "unconfirmed", "next_checks_due"):
        summary[name] = []
    summary["baselines"] = [[f"day {i}", i] for i in range(400)]
    summary["report"] = "/srv/" + "r" * 300 + "/monitor.md"
    _, body = digest.build(summary)
    assert len(body) <= digest.BODY_LIMIT
    assert body.endswith(summary["report"])
