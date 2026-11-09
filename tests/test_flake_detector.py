"""Detector tests run only fake check_page.py subprocesses, never a browser or build."""

import json
from pathlib import Path
import subprocess

import pytest

from scripts import check_page, flake_detector as detector


JOB = "hydration/hydration_paint"
OTHER = "interactions/check_arrival"


@pytest.fixture
def fake_checker(tmp_path, monkeypatch):
    calls, pending = [], []
    clock = [0.0]
    monkeypatch.setattr(detector.time, "monotonic", lambda: clock[0])

    def run(command, **kwargs):
        calls.append(command)
        assert command[:5] == ["uv", "run", "--locked", "-q", "scripts/check_page.py"]
        assert kwargs["cwd"] == detector.ROOT
        if "--list-jobs" in command:
            assert kwargs == {"cwd": detector.ROOT, "capture_output": True, "text": True, "check": True}
            return subprocess.CompletedProcess(command, 0, "\n".join(detector.RISKY_JOBS) + "\n", "")
        assert command[7:9] == ["--jobs", "1"]
        assert kwargs["check"] is False and kwargs.get("timeout") == int(command[command.index("--job-deadline") + 1]) + detector.PARENT_GRACE
        assert pending, "unexpected extra attempt or retry"
        result = pending.pop(0)
        job = command[command.index("--job") + 1]
        timings = Path(command[command.index("--timings") + 1])
        if result.get("line", True):
            exception = result.get("exception")
            timing = {"job": job, "started": 0.1, "seconds": result.get("seconds", 1.0),
                      "errors": None if exception else result.get("errors_override", int(result.get("exit", 0) == 1)), "exception": exception}
            timings.write_text(json.dumps(timing) + "\n")
        if "raw_timing" in result:
            timings.write_text(result["raw_timing"])
        kwargs["stdout"].write(result.get("stdout", "ok: fake job\n").encode())
        kwargs["stderr"].write(result.get("stderr", "error: fake finding\n" if result.get("exit") == 1 else "").encode())
        clock[0] += result.get("cost", 0)
        return subprocess.CompletedProcess(command, result.get("exit", 0))

    monkeypatch.setattr(detector.subprocess, "run", run)
    monkeypatch.setattr(detector, "run_process", lambda command, **kwargs: run(command, check=False, **kwargs).returncode)
    dist = tmp_path / "dist"
    dist.mkdir()
    out = tmp_path / "results"

    def batch(results, jobs=JOB, runs=None, extra=()):
        pending.extend(results)
        code = detector.main(["--dist", str(dist), "--job", jobs, "--runs", str(runs if runs is not None else len(results)), "--out", str(out), *extra])
        report = json.loads((out / "report.json").read_text())
        return code, report

    return batch, calls, out


def test_stable_retains_every_exit_log_and_timing(fake_checker):
    batch, calls, out = fake_checker
    code, report = batch([{"seconds": 3, "stdout": "first\n"}, {"seconds": 1}, {"seconds": 2}])
    assert code == 0
    assert report["jobs"][JOB] == {
        "status": "stable", "attempts": 3, "planned": 3, "passes": 3, "failures": 0, "cut_by_budget": 0,
        "exceptions": {}, "NOT_RUN": 0, "error_lines": {}, "seconds": {"min": 1, "median": 2, "max": 3},
    }
    assert report["completed"] == report["planned"] == 3
    assert len(calls) == 4  # one discovery plus exactly three attempts
    for number, attempt in enumerate(report["attempts"], 1):
        assert attempt["attempt"] == number and attempt["exit_code"] == 0
        paths = {name: out / path for name, path in attempt["files"].items()}
        assert all(path.is_file() for path in paths.values())
        assert json.loads(paths["timings.jsonl"].read_text())["job"] == JOB
        assert json.loads((paths["stdout.log"].parent / "attempt.json").read_text()) == attempt
    assert (out / report["attempts"][0]["files"]["stdout.log"]).read_text() == "first\n"
    markdown = (out / "report.md").read_text()
    assert "Ran 3 of 3 planned attempts" in markdown and "| stable |" in markdown


@pytest.mark.parametrize("results", [[{}, {"exit": 1}], [{"exit": 1}, {}]])
def test_one_pass_and_one_failure_is_flaky_in_either_order(fake_checker, results):
    batch, calls, _ = fake_checker
    code, report = batch(results)
    summary = report["jobs"][JOB]
    assert code == 1 and summary["status"] == "flaky"
    assert summary["attempts"] == 2 and summary["passes"] == summary["failures"] == 1
    assert [attempt["exit_code"] for attempt in report["attempts"]] == [r.get("exit", 0) for r in results]
    assert len(calls) == 3  # the failure neither disappears nor adds an attempt


def test_failing_counts_distinct_error_lines_without_retrying(fake_checker):
    batch, calls, _ = fake_checker
    code, report = batch([{"exit": 1, "stderr": "error: one\nerror: one\nwarning: noise\n"},
                          {"exit": 1, "stdout": "error: two\n", "stderr": "error: one\n"}])
    summary = report["jobs"][JOB]
    assert code == 1 and summary["status"] == "failing" and summary["failures"] == 2
    assert summary["error_lines"] == {"error: one": 3, "error: two": 1}
    assert summary["exceptions"] == {} and len(calls) == 3


@pytest.mark.parametrize(("exception", "exit_code"), [("JobTimeout", 124), ("WorkerCrashed", 1), ("RuntimeError", 1)])
def test_exceptions_come_from_the_job_timing_line(fake_checker, exception, exit_code):
    batch, calls, _ = fake_checker
    code, report = batch([{"exit": exit_code, "exception": exception, "seconds": 600}, {}], extra=("--max-minutes", "30"))  # room for a 600 s attempt
    summary = report["jobs"][JOB]
    assert code == 1 and summary["status"] == "flaky" and summary["exceptions"] == {exception: 1}
    assert summary["failures"] == 0 and summary["passes"] == 1
    assert summary["seconds"] == {"min": 1, "median": 300.5, "max": 600}
    assert report["attempts"][0]["exit_code"] == exit_code and len(calls) == 3


@pytest.mark.parametrize("exit_code", [-9, 0, 1])
def test_missing_timing_is_counted_and_raw_logs_survive(fake_checker, exit_code):
    batch, _, out = fake_checker
    code, report = batch([{"exit": exit_code, "line": False}])
    assert code == 1 and report["jobs"][JOB]["status"] == "failing"
    assert report["jobs"][JOB]["exceptions"] == {"no line": 1}
    assert report["jobs"][JOB]["seconds"] is None
    (attempt,) = report["attempts"]
    assert attempt["exit_code"] == exit_code
    assert (out / attempt["files"]["timings.jsonl"]).read_text() == ""


def test_not_run_is_explicit_and_never_a_pass(fake_checker):
    batch, _, _ = fake_checker
    code, report = batch([{"exit": 3, "line": False, "stderr": "NOT RUN: missing Chromium\n"}])
    summary = report["jobs"][JOB]
    assert code == 3 and summary["NOT_RUN"] == 1 and summary["passes"] == summary["failures"] == 0
    assert summary["exceptions"] == {} and summary["status"] == "not run"  # no Chromium: nothing ran, nothing passed


def test_round_robin_keeps_all_planned_attempts_including_later_failures(fake_checker):
    batch, calls, _ = fake_checker
    results = [{"exit": code} for code in (1, 0, 0, 1, 1, 0)]
    code, report = batch(results, jobs=f"{JOB},{OTHER}", runs=3)
    commands = [command for command in calls if "--job" in command]
    assert [command[command.index("--job") + 1] for command in commands] == [JOB, OTHER] * 3
    assert [(a["job"], a["attempt"], a["exit_code"]) for a in report["attempts"]] == [
        (JOB, 1, 1), (OTHER, 1, 0), (JOB, 2, 0), (OTHER, 2, 1), (JOB, 3, 1), (OTHER, 3, 0)]
    assert code == 1 and all(s["status"] == "flaky" for s in report["jobs"].values())
    assert report["completed"] == report["planned"] == len(commands) == 6


@pytest.mark.parametrize(("extra", "cost"), [((), 250), (("--max-minutes", "2"), 50)])
def test_budget_stops_new_attempts_at_boundary_and_keeps_completed_ones(fake_checker, extra, cost):
    batch, calls, out = fake_checker
    # Each attempt takes cost seconds from outside, 5 of them start-up before the job's watchdog.
    code, report = batch([{"cost": cost, "seconds": cost - 5}] * 2, jobs=f"{JOB},{OTHER}", runs=3, extra=extra)
    assert code == 0 and report["budget_exhausted"] and report["completed"] == 2 and report["planned"] == 6
    assert len(calls) == 3 and all(s["attempts"] == 1 for s in report["jobs"].values())
    assert "Ran 2 of 6 planned attempts" in (out / "report.md").read_text()


def test_one_long_attempt_finishes_but_the_next_job_does_not_start(fake_checker):
    batch, calls, _ = fake_checker
    code, report = batch([{"cost": 90, "seconds": 85}], jobs=f"{JOB},{OTHER}", runs=2, extra=("--max-minutes", "3"))
    assert code == 0 and report["completed"] == 1 and len(calls) == 2  # the one attempt passed; OTHER was never reached
    assert report["jobs"][OTHER]["attempts"] == 0 and report["jobs"][OTHER]["status"] == "not reached"


def test_zero_budget_starts_no_attempts_and_still_writes_reports(fake_checker):
    batch, calls, _ = fake_checker
    code, report = batch([], runs=2, extra=("--max-minutes", "0"))
    assert code == 0 and report["completed"] == 0 and report["planned"] == 2 and len(calls) == 1
    assert all(job["status"] == "not reached" for job in report["jobs"].values())


@pytest.mark.parametrize("raw", ['{"job":', '{}\n', '[]\n', '{}\n{}\n'])
def test_damaged_timing_is_reported_and_preserved(fake_checker, raw):
    batch, _, out = fake_checker
    code, report = batch([{"raw_timing": raw}])
    assert code == 1 and report["jobs"][JOB]["exceptions"] == {"invalid line": 1}
    assert (out / report["attempts"][0]["files"]["timings.jsonl"]).read_text() == raw


def test_unexpected_exit_with_a_normal_timing_still_blocks(fake_checker):
    batch, _, _ = fake_checker
    code, report = batch([{"exit": 2}, {}])
    assert code == 1 and report["jobs"][JOB]["exceptions"] == {"exit 2": 1}


@pytest.mark.parametrize("jobs", ["hydration/not_real", "", f"{JOB},", f"{JOB},{JOB}"])
def test_ids_are_validated_against_list_jobs_before_any_attempt(fake_checker, jobs):
    batch, calls, out = fake_checker
    with pytest.raises(SystemExit) as exc:
        batch([], jobs=jobs, runs=1)
    assert exc.value.code == 2 and len(calls) == 1 and calls[0][-1] == "--list-jobs"
    assert not out.exists()


def test_list_risky_prints_only_the_five_existing_jobs(fake_checker, capsys):
    _, calls, _ = fake_checker
    assert detector.main(["--list-risky"]) == 0
    jobs = capsys.readouterr().out.splitlines()
    assert jobs == ["hydration/hydration_paint", "hydration/hydration_edges", "interactions/check_arrival",
                    "interactions/check_touch_tips", "interactions/check_theme_default"]
    assert set(jobs) <= set(check_page.all_job_ids()) and calls == []


@pytest.mark.parametrize("extra", [("--runs", "0"), ("--runs", "-1"), ("--max-minutes", "-1"), ("--max-minutes", "nan"), ("--max-minutes", "inf")])
def test_invalid_limits_are_rejected(fake_checker, extra):
    batch, calls, _ = fake_checker
    with pytest.raises(SystemExit) as exc:
        batch([], runs=1, extra=extra)
    assert exc.value.code == 2 and calls == []


def test_existing_evidence_is_never_overwritten(fake_checker):
    batch, calls, out = fake_checker
    batch([{}])
    previous = (out / "report.json").read_bytes()
    with pytest.raises(SystemExit) as exc:
        batch([{}])
    assert exc.value.code == 2 and (out / "report.json").read_bytes() == previous
    assert len([call for call in calls if "--job" in call]) == 1


def test_reports_cannot_mutate_the_build(fake_checker):
    batch, calls, out = fake_checker
    with pytest.raises(SystemExit) as exc:
        batch([{}], extra=("--out", str(out.parent / "dist/reports")))
    assert exc.value.code == 2 and len(calls) == 1


def test_an_attempt_starts_only_when_its_expected_length_fits_the_budget(fake_checker):
    batch, calls, _ = fake_checker
    # The first attempt took 70 s of a 2-minute budget: a second one of the same job would end past it, so it never starts.
    code, report = batch([{"cost": 70, "seconds": 68}, {"cost": 70, "seconds": 68}], runs=2, extra=("--max-minutes", "2"))
    assert report["completed"] == 1 and report["budget_exhausted"] and len(calls) == 2


def test_the_deadline_markers_match_check_page():
    from scripts import check_page

    assert (detector.DEADLINE_LOWERED, detector.DEADLINE_PASSED) == (check_page.DEADLINE_LOWERED, check_page.DEADLINE_PASSED)


def test_a_late_attempt_gets_the_time_left_as_its_deadline_and_a_cut_is_inconclusive(fake_checker):
    batch, calls, _ = fake_checker
    # JOB's attempts are short; OTHER's use up the budget. At 540 s JOB starts again and hangs. The child keeps the
    # deadline of 60 s from its own start, start-up included, and its watchdog stops the job there (JobTimeout from the
    # job's process, with the child's marker). That cut is neither a pass nor a failure.
    results = [{"cost": 10, "seconds": 5}, {"cost": 270, "seconds": 265}, {"cost": 10, "seconds": 5}, {"cost": 250, "seconds": 245},
               {"exit": 124, "exception": "JobTimeout", "seconds": 40, "cost": 60, "stderr": "job deadline: watchdog lowered to 40 s\n"}]
    code, report = batch(results, jobs=f"{JOB},{OTHER}", runs=3, extra=("--max-minutes", "10"))
    late = calls[-1]
    assert late[late.index("--job") + 1] == JOB and late[late.index("--job-deadline") + 1] == "58"  # 60 s left, less 2 s for the child's start
    summary = report["jobs"][JOB]
    assert summary["cut_by_budget"] == 1 and summary["exceptions"] == {} and summary["passes"] == 2 and summary["failures"] == 0
    # The cut may have hidden the very hang looked for: inconclusive, not stable.
    assert summary["status"] == "inconclusive" and code == 2 and report["elapsed_seconds"] <= 600


def test_a_deadline_that_passed_is_a_cut_not_a_not_run(fake_checker):
    batch, _, _ = fake_checker
    code, report = batch([{"exit": 3, "line": False, "stderr": "NOT RUN: job deadline passed\n"}])
    summary = report["jobs"][JOB]
    assert summary["cut_by_budget"] == 1 and summary["NOT_RUN"] == 0 and summary["status"] == "inconclusive" and code == 2


def test_a_timeout_without_the_marker_is_the_job_s_own(fake_checker):
    batch, _, _ = fake_checker
    code, report = batch([{"exit": 124, "exception": "JobTimeout", "seconds": 600, "cost": 640}], extra=("--max-minutes", "30"))
    summary = report["jobs"][JOB]
    assert summary["exceptions"] == {"JobTimeout": 1} and summary["cut_by_budget"] == 0 and code == 1


def test_less_than_a_minimum_left_starts_no_attempt(fake_checker):
    batch, calls, _ = fake_checker
    code, report = batch([{"cost": 580, "seconds": 2}, {}], runs=2, extra=("--max-minutes", "10"))
    assert report["completed"] == 1 and report["budget_exhausted"] and len(calls) == 2



@pytest.mark.parametrize("line, kind", [({"exit": 3, "seconds": 5, "errors_override": 2}, "failure"), ({"exit": 3, "exception": "RuntimeError", "seconds": 5}, "exception")])
def test_a_recorded_failure_survives_a_deadline_exit_after_it(fake_checker, line, kind):
    batch, _, _ = fake_checker
    marker = "NOT RUN: job deadline passed\n"
    code, report = batch([{**line, "stderr": marker}, {}])
    summary = report["jobs"][JOB]
    assert summary["cut_by_budget"] == 0 and summary["status"] == "flaky" and code == 1
    assert (summary["failures"] == 1) if kind == "failure" else (summary["exceptions"] == {"RuntimeError": 1})


def test_a_clean_line_before_a_deadline_exit_is_still_a_cut(fake_checker):
    batch, _, _ = fake_checker
    code, report = batch([{"exit": 3, "seconds": 5, "stderr": "NOT RUN: job deadline passed\n"}])
    summary = report["jobs"][JOB]
    assert summary["passes"] == 0 and summary["cut_by_budget"] == 1 and summary["status"] == "inconclusive" and code == 2


def test_the_parent_stops_an_attempt_past_its_deadline_plus_grace(fake_checker, monkeypatch):
    batch, calls, _ = fake_checker

    def hang(command, **kwargs):
        assert kwargs["timeout"] == int(command[command.index("--job-deadline") + 1]) + detector.PARENT_GRACE
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    monkeypatch.setattr(detector, "run_process", hang)
    code, report = batch([], runs=1)
    attempt = report["attempts"][0]
    assert attempt["cut_by_budget"] and not attempt["passed"] and report["jobs"][JOB]["status"] == "inconclusive" and code == 2


def test_an_error_line_before_a_deadline_exit_is_a_failure(fake_checker):
    batch, _, _ = fake_checker
    code, report = batch([{"exit": 3, "seconds": 5, "stderr": "error: a static finding\nNOT RUN: job deadline passed\n"}, {}])
    summary = report["jobs"][JOB]
    assert summary["failures"] == 1 and summary["passes"] == 1 and summary["status"] == "flaky" and code == 1



def test_a_timeout_kills_the_whole_attempt_including_a_detached_hanging_grandchild(tmp_path):
    import os
    import sys
    import time

    pidfile = tmp_path / "grandchild.pid"
    wrapper = tmp_path / "wrapper.py"  # stands in for uv: it starts a worker that hangs, and waits for it
    wrapper.write_text(
        "import subprocess, sys\n"
        f"worker = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(300)'], start_new_session=True)\n"  # detached, as Playwright's browser
        f"open({str(pidfile)!r}, 'w').write(str(worker.pid))\n"
        "worker.wait()\n"
    )
    with (tmp_path / "out.log").open("wb") as out, (tmp_path / "err.log").open("wb") as err:
        with pytest.raises(subprocess.TimeoutExpired):
            detector.run_process([sys.executable, str(wrapper)], cwd=tmp_path, stdout=out, stderr=err, timeout=3)
    pid = int(pidfile.read_text())
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        try:
            if open(f"/proc/{pid}/stat").read().split()[2] == "Z":  # Linux: killed, awaiting its reaper
                break
        except OSError:  # no /proc (macOS), or already gone
            pass
        time.sleep(0.1)
    else:
        pytest.fail(f"the hanging grandchild {pid} outlived its attempt")
