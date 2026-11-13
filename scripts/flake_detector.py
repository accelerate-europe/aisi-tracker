"""Repeat browser jobs against one existing build, retaining every attempt as evidence.

uv run --locked scripts/flake_detector.py --dist DIR --job ID[,ID...] --runs N
Jobs run round-robin in fresh check_page.py processes, with --no-quarantine. The default ten-minute budget holds:
an attempt starts only when its expected length still fits, and runs with check_page.py's --job-deadline set to the
time left, which the child keeps from its own start (start-up included): the job's own watchdog stops it at the
deadline and writes its JobTimeout line, or the child reports NOT_RUN when nothing is left; such an attempt is "cut by budget",
neither a pass nor a failure, and its job is inconclusive unless it also failed. Exit 1 if any job is flaky or
failing, 2 if one is inconclusive, 3 if a job only reported NOT_RUN (no Chromium), otherwise 0 (jobs the budget
never reached are listed as not reached). No retries.
"""

import argparse
import math
import os
import signal
from collections import Counter
from datetime import datetime, timezone
import json
from pathlib import Path
import statistics
import subprocess
import sys
import time


ROOT = Path(__file__).resolve().parents[1]
CHECK_PAGE = ["uv", "run", "--locked", "-q", "scripts/check_page.py"]
RISKY_JOBS = (
    "hydration/hydration_paint",
    "hydration/hydration_edges",
    "interactions/check_arrival",
    "interactions/check_touch_tips",
    "interactions/check_theme_default",
)


def read_timing(path: Path, job: str) -> tuple[dict | None, str | None]:
    """An attempt must have exactly one matching line; keep the raw file even when it is malformed."""
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    lines = [line for line in lines if line.strip()]
    if not lines:
        return None, "no line"
    try:
        if len(lines) != 1:
            raise ValueError("expected one line")
        (raw,) = lines
        line = json.loads(raw)
        if not isinstance(line, dict) or line.get("job") != job:
            raise ValueError("wrong job")
        if not {"started", "seconds", "errors", "exception"} <= line.keys():
            raise ValueError("missing timing keys")
        for key in ("started", "seconds"):
            value = line[key]
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
                raise ValueError("invalid duration")
        errors, exception = line["errors"], line["exception"]
        if exception is None:
            if type(errors) is not int or errors < 0:
                raise ValueError("invalid errors")
        elif not isinstance(exception, str) or not exception or errors is not None:
            raise ValueError("invalid exception")
        return line, exception
    except (ValueError, TypeError):
        return None, "invalid line"


MIN_ATTEMPT = 30  # seconds: with less of the budget left, no attempt starts
DEADLINE_LOWERED = "job deadline: watchdog lowered to"  # check_page.DEADLINE_LOWERED (tests keep them equal)
DEADLINE_PASSED = "NOT RUN: job deadline passed"  # check_page.DEADLINE_PASSED
PARENT_GRACE = 30  # seconds past the child's deadline before the detector itself stops it
PARENT_KILLED = "flake detector: stopped the attempt at its deadline plus"


def run_process(command: list[str], cwd: Path, stdout, stderr, timeout: float | None) -> int:
    """Run one attempt in its own process group and return its exit code. Past timeout, the whole group (uv, the
    check_page.py worker and its browsers) is killed and TimeoutExpired raised, so nothing of it outlives the attempt."""
    process = subprocess.Popen(command, cwd=cwd, stdout=stdout, stderr=stderr, start_new_session=True)
    try:
        return process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        # Every descendant by parent id first (Playwright starts its browser in a group of its own), then the group.
        for pid in [*descendants(process.pid), process.pid]:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()
        raise


def descendants(root: int) -> list[int]:
    """Every process below root, from `ps -A -o pid=,ppid=` (Linux and macOS), deepest first."""
    try:
        listing = subprocess.run(["ps", "-A", "-o", "pid=,ppid="], capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    children: dict[int, list[int]] = {}
    for line in listing.splitlines():
        fields = line.split()
        if len(fields) == 2 and all(f.isdigit() for f in fields):
            children.setdefault(int(fields[1]), []).append(int(fields[0]))
    found, frontier = [], [root]
    while frontier:
        frontier = [child for pid in frontier for child in children.get(pid, [])]
        found += frontier
    return found[::-1]


def run_attempt(dist: Path, job: str, number: int, directory: Path, out: Path, deadline: int | None = None) -> dict:
    directory.mkdir(parents=True)
    timing_path = directory / "timings.jsonl"
    timing_path.touch()  # an empty file records that this attempt produced no line
    command = [*CHECK_PAGE, "--dist", str(dist), "--jobs", "1", "--job", job, "--timings", str(timing_path), "--no-quarantine", "--skip-static"]  # a quarantined flake still counts; no render or static checks, only the job
    if deadline is not None:  # the child keeps it, start-up included: its own watchdog stops the job and writes its JobTimeout line
        command += ["--job-deadline", str(deadline)]
    launch_exception, began, killed = None, time.monotonic(), False
    with (directory / "stdout.log").open("wb") as stdout, (directory / "stderr.log").open("wb") as stderr:
        try:
            # A safety net only: the child keeps the deadline itself; this bounds what precedes its guard (uv, imports).
            exit_code = run_process(command, cwd=ROOT, stdout=stdout, stderr=stderr,
                                    timeout=None if deadline is None else deadline + PARENT_GRACE)
        except subprocess.TimeoutExpired:  # run() has killed the child; nothing it wrote is lost
            exit_code, killed = None, True
            stderr.write(f"{PARENT_KILLED} {deadline + PARENT_GRACE} s\n".encode())
        except OSError as exc:
            exit_code, launch_exception = None, type(exc).__name__
            stderr.write(f"error: could not start check_page.py: {exc}\n".encode())
    wall = time.monotonic() - began
    timing, exception = read_timing(timing_path, job)
    error_lines = Counter(
        line for name in ("stdout.log", "stderr.log")
        for line in (directory / name).read_text(encoding="utf-8", errors="replace").splitlines()
        if line.startswith("error: ")
    )
    err = (directory / "stderr.log").read_text(encoding="utf-8", errors="replace")
    recorded = timing["exception"] if timing else None  # what the job's own line says, written before any deadline exit
    deadline_exit = exit_code == 3 and DEADLINE_PASSED in err
    # Stopped by the budget, as the child reports it: a watchdog the deadline lowered, or the deadline timer before the
    # job wrote its line. Only then is an attempt a cut; whatever the job itself recorded takes precedence.
    # Any reported error line counts too, whatever the exit code: a deadline exit right after it must not erase it.
    failure = bool(timing and timing["errors"]) or (bool(error_lines) and not recorded)
    budget_stop = (recorded == "JobTimeout" and DEADLINE_LOWERED in err) or deadline_exit or killed
    own_exception = recorded and not (recorded == "JobTimeout" and DEADLINE_LOWERED in err)
    cut = budget_stop and not failure and not own_exception  # a cut never passes; recorded evidence wins over it
    exceptions = [recorded] if own_exception else []
    if timing is None and exception and exit_code != 3 and not killed:  # no or an invalid line; NOT_RUN runs no job
        exceptions.append(exception)
    if launch_exception:
        exceptions.append(launch_exception)
    elif exit_code not in (0, 3) and not failure and not exceptions and not cut:
        exceptions.append(f"exit {exit_code}")
    passed = exit_code == 0 and not failure and not exceptions and not cut
    record = {
        "job": job, "attempt": number, "exit_code": exit_code,
        "passed": passed, "failure": failure, "exceptions": exceptions, "NOT_RUN": exit_code == 3 and not deadline_exit, "cut_by_budget": cut, "deadline": deadline, "wall_seconds": round(wall, 3),
        "error_lines": dict(error_lines), "timing": timing,
        "files": {name: (directory / name).relative_to(out).as_posix()
                  for name in ("stdout.log", "stderr.log", "timings.jsonl")},
    }
    (directory / "attempt.json").write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    return record


def summarise(attempts: list[dict], planned: int) -> dict:
    passes = sum(a["passed"] for a in attempts)
    failures = sum(a["failure"] for a in attempts)
    exceptions = Counter(e for a in attempts for e in a["exceptions"])
    errors = Counter()
    for attempt in attempts:
        errors.update(attempt["error_lines"])
    seconds = [a["timing"]["seconds"] for a in attempts if a["timing"] is not None]
    return {
        # A job the budget never reached, or that only reported NOT_RUN, says nothing about flakiness.
        # A cut by the budget can hide the very hang being looked for: without other failures it is inconclusive.
        "status": ("failing" if (failures or exceptions) and not passes else "flaky" if failures or exceptions
                   else "inconclusive" if any(a.get("cut_by_budget") for a in attempts)
                   else "not run" if not passes and any(a["NOT_RUN"] for a in attempts)
                   else "not reached" if not passes else "stable"),
        "attempts": len(attempts), "planned": planned, "passes": passes, "failures": failures,
        "exceptions": dict(exceptions), "NOT_RUN": sum(a["NOT_RUN"] for a in attempts),
        "cut_by_budget": sum(a.get("cut_by_budget", False) for a in attempts),
        "error_lines": dict(errors),
        "seconds": {"min": min(seconds), "median": statistics.median(seconds), "max": max(seconds)} if seconds else None,
    }


def markdown(report: dict) -> str:
    lines = ["# Browser flake detector", "", f"Build: `{report['dist']}`", "",
             f"Ran {report['completed']} of {report['planned']} planned attempts. Start budget: {report['max_minutes']:g} minutes; budget exhausted: {'yes' if report['budget_exhausted'] else 'no'}.", "",
             "Every attempt counts. No failures are retried or replaced. A job with no passes but a failure or exception is failing; one that only reported NOT_RUN (no Chromium) is not run and the batch exits 3; one the budget never reached is not reached and does not fail the batch. An attempt the budget stopped is cut by budget: neither a pass nor a failure; its job is inconclusive unless it also failed, and the batch exits 2. NOT_RUN is counted separately; missing timings also count as an exception (no line). Seconds come from the job's timings line.", "",
             "| Job | Status | Attempts / planned | Passes | Failures | NOT_RUN | Cut by budget | Seconds min / median / max |",
             "| --- | --- | --- | --- | --- | --- | --- | --- |"]
    for job, summary in report["jobs"].items():
        seconds = summary["seconds"]
        duration = " / ".join(f"{seconds[key]:g}" for key in ("min", "median", "max")) if seconds else "no timings"
        lines.append(f"| {job} | {summary['status']} | {summary['attempts']} / {summary['planned']} | {summary['passes']} | {summary['failures']} | {summary['NOT_RUN']} | {summary['cut_by_budget']} | {duration} |")
    for job, summary in report["jobs"].items():
        lines += ["", f"## {job}", "", "Exceptions: " + (", ".join(f"{kind}: {count}" for kind, count in sorted(summary["exceptions"].items())) or "none"), "", "Error lines (occurrences):", ""]
        lines += [f"- {count}: {line}" for line, count in sorted(summary["error_lines"].items())] or ["None."]
    lines += ["", "## Attempts", ""]
    for attempt in report["attempts"]:
        files = ", ".join(f"[{name}]({path})" for name, path in attempt["files"].items())
        lines.append(f"- {attempt['job']} attempt {attempt['attempt']}: exit {attempt['exit_code']}; {files}")
    return "\n".join(lines) + "\n"


def positive_runs(text: str) -> int:
    value = int(text)
    if value < 1:
        raise argparse.ArgumentTypeError("runs must be at least 1")
    return value


def minutes(text: str) -> float:
    value = float(text)
    if not math.isfinite(value) or value < 0:
        raise argparse.ArgumentTypeError("max-minutes must be finite and non-negative")
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dist", type=Path, help="existing build to use for every attempt")
    parser.add_argument("--job", help="comma-separated job ids from check_page.py --list-jobs")
    parser.add_argument("--runs", type=positive_runs, help="planned attempts per job")
    parser.add_argument("--max-minutes", type=minutes, default=10.0, help="stop starting attempts after this many minutes (default: 10)")
    parser.add_argument("--out", type=Path, help="new or empty output directory (default: work/flake/<UTC timestamp>)")
    parser.add_argument("--list-risky", action="store_true", help="print the default risky job set and exit")
    args = parser.parse_args(argv)
    if args.list_risky:
        print("\n".join(RISKY_JOBS))
        return 0
    if args.dist is None or args.job is None or args.runs is None:
        parser.error("--dist, --job and --runs are required unless --list-risky is given")
    jobs = [job.strip() for job in args.job.split(",")]
    try:
        listed = subprocess.run([*CHECK_PAGE, "--list-jobs"], cwd=ROOT, capture_output=True, text=True, check=True)
    except (OSError, subprocess.CalledProcessError) as exc:
        parser.error(f"could not list check_page.py jobs: {exc}")
    known = set(listed.stdout.splitlines())
    if not all(job in known for job in jobs):
        parser.error(f"unknown browser job(s): {', '.join(repr(job) for job in jobs if job not in known)}; see check_page.py --list-jobs")
    if len(set(jobs)) != len(jobs):
        parser.error("duplicate --job ids; each job runs exactly --runs times")
    dist = args.dist.resolve()
    if not dist.is_dir():
        parser.error(f"build directory does not exist: {dist}")
    started = datetime.now(timezone.utc)
    out = (args.out or ROOT / "work/flake" / started.strftime("%Y%m%dT%H%M%S.%fZ")).resolve()
    if out == dist or dist in out.parents:
        parser.error("--out must be outside --dist so attempts do not change the build")
    if out.exists() and (not out.is_dir() or any(out.iterdir())):
        parser.error(f"output must be new or empty; refusing to overwrite evidence: {out}")
    out.mkdir(parents=True, exist_ok=True)
    origin = time.monotonic()
    attempts, budget_exhausted = [], False
    for number in range(1, args.runs + 1):
        for job in jobs:
            # Admit an attempt only if it is expected to end within the budget: its own longest earlier attempt, or
            # before it has one, the longest attempt of the batch so far. So a batch stays within the browser time
            # the shared host granted, give or take one attempt's variation.
            # Whole attempts as measured from outside, start-up included: that job's longest, else the batch's.
            took = [a["wall_seconds"] for a in attempts]
            mine = [a["wall_seconds"] for a in attempts if a["job"] == job]
            expected = max(mine or took or [0.0])
            left = args.max_minutes * 60 - (time.monotonic() - origin)
            if left < MIN_ATTEMPT or expected >= left:
                budget_exhausted = True
                break
            directory = out / "attempts" / f"{len(attempts) + 1:04d}"
            # Less 2 s: the child counts from its own start, after uv and its imports.
            attempts.append(run_attempt(dist, job, number, directory, out, math.floor(left) - 2))
        if budget_exhausted:
            break
    report = {
        "dist": str(dist), "started": started.isoformat(), "max_minutes": args.max_minutes,
        "elapsed_seconds": time.monotonic() - origin, "budget_exhausted": budget_exhausted,
        "planned": args.runs * len(jobs), "completed": len(attempts),
        "jobs": {job: summarise([a for a in attempts if a["job"] == job], args.runs) for job in jobs},
        "attempts": attempts,
    }
    (out / "report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    (out / "report.md").write_text(markdown(report), encoding="utf-8")
    print(f"Ran {len(attempts)} of {report['planned']} attempts; reports: {out / 'report.json'} and {out / 'report.md'}")
    statuses = {job["status"] for job in report["jobs"].values()}
    # 1 flaky or failing; 2 inconclusive (an attempt cut by the budget); 3 only NOT_RUN, as check_page.py; else 0.
    return 1 if statuses & {"flaky", "failing"} else 2 if "inconclusive" in statuses else 3 if "not run" in statuses else 0


if __name__ == "__main__":
    sys.exit(main())
