"""The one check command: snapshot binding, outcome rules and the base comparison helpers."""

import hashlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import pytest

from scripts import check


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def test_snapshot_is_the_staged_index_not_the_working_tree(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    (repo / "a.txt").write_text("committed\n")
    git(repo, "add", "a.txt")
    git(repo, "-c", "user.name=t", "-c", "user.email=t@example.org", "commit", "-q", "-m", "base")
    (repo / "a.txt").write_text("staged\n")
    git(repo, "add", "a.txt")
    (repo / "a.txt").write_text("unstaged\n")
    (repo / "untracked.txt").write_text("untracked\n")
    first = check.make_snapshot(repo, tmp_path / "one")
    assert (tmp_path / "one/a.txt").read_text() == "staged\n"
    assert not (tmp_path / "one/untracked.txt").exists()
    # The snapshot carries its own index so tools that list tracked files still work.
    listed = subprocess.run(["git", "-C", str(tmp_path / "one"), "ls-files"], capture_output=True, text=True, check=True).stdout
    assert listed.split() == ["a.txt"]
    (repo / "a.txt").write_text("other\n")
    assert check.make_snapshot(repo, tmp_path / "two") == first
    git(repo, "add", "a.txt")
    assert check.make_snapshot(repo, tmp_path / "three") != first
    check.make_snapshot(repo, tmp_path / "head", "HEAD")
    assert (tmp_path / "head/a.txt").read_text() == "committed\n"


@pytest.mark.parametrize(("statuses", "allow_skip", "blocked"), [
    (["PASS", "PASS"], False, 0),
    (["PASS", "NOT RUN"], False, 1),
    (["PASS", "NOT RUN"], True, 0),
    (["FAIL", "NOT RUN"], True, 1),
])
def test_not_run_fails_unless_skips_are_allowed(statuses, allow_skip, blocked):
    results = [check.Result(f"s{i}", status) for i, status in enumerate(statuses)]
    assert len(check.blocking(results, allow_skip)) == blocked


def test_the_base_build_drops_only_a_leading_script_header():
    # build.py as released in 1.0.8: a header and no lock, which `uv run --locked` refuses.
    old = '# /// script\n# requires-python = ">=3.11"\n# dependencies = ["pyyaml>=6"]\n# ///\n"""Doc."""\nimport json\n'
    assert check.without_script_header(old) == '"""Doc."""\nimport json\n'
    plain = '"""Doc."""\n# /// script\n# ///\nimport json\n'
    assert check.without_script_header(plain) == plain


def test_tree_rss_sums_a_process_and_its_descendants_only():
    listing = "  10     1  100\n  11    10  200\n  12    11   50\n  13     1 9999\n garbage line\n"
    assert check.tree_rss_kb(10, listing) == 350
    assert check.tree_rss_kb(11, listing) == 250
    assert check.tree_rss_kb(99, listing) == 0


def test_peak_sampler_counts_memory_held_by_a_child_process():
    hold = "b = bytearray(80 * 1024 * 1024); b[::4096] = b'x' * len(b[::4096]); import time; time.sleep(2)"
    with check.PeakSampler(os.getpid(), interval=0.1) as alone:
        time.sleep(0.3)
    with check.PeakSampler(os.getpid(), interval=0.1) as with_child:
        subprocess.run([sys.executable, "-c", hold], check=True)
    assert alone.peak_mb is not None and with_child.peak_mb is not None
    assert with_child.peak_mb - alone.peak_mb > 60


def test_a_sampler_without_a_thread_measures_nothing_and_does_not_raise(monkeypatch):
    sampler = check.PeakSampler(os.getpid())
    monkeypatch.setattr(sampler._thread, "start", lambda: (_ for _ in ()).throw(RuntimeError("can't start new thread")))
    sampler.start().stop()
    assert sampler.peak_mb is None


def test_step_time_ends_before_the_sampler_stops(tmp_path, monkeypatch):
    samplers, stop = [], check.PeakSampler.stop

    def slow_stop(self):
        samplers.append(self)
        time.sleep(2)  # far above any setup time, so a busy machine cannot blur the comparison
        stop(self)

    monkeypatch.setattr(check.PeakSampler, "stop", slow_stop)
    monkeypatch.setattr(check, "make_snapshot", lambda repo, dest: dest.mkdir(parents=True) or "0" * 16)
    monkeypatch.setattr(check, "RUNNERS", {**check.RUNNERS, "lint": lambda ctx: check.Result("lint", "NOT RUN", detail="no ruff")})
    monkeypatch.setattr(sys, "argv", ["check.py", "--only", "lint", "--report-dir", str(tmp_path / "r")])
    assert check.main() == 1
    row = next(line for line in (tmp_path / "r/report.md").read_text().splitlines() if line.startswith("| lint "))
    assert float(row.split(" | ")[2]) < 1.5  # the early NOT RUN is timed without the sampler's shutdown
    ended = re.search(r"first blocking result: lint NOT RUN after (\d+\.\d) s", (tmp_path / "r/report.md").read_text())
    assert ended and float(ended[1]) < 1.5  # and so is when it ended
    assert samplers and not any(s._thread.is_alive() for s in samplers)


def test_report_times_every_step_and_names_the_first_blocking_result(tmp_path, monkeypatch, capsys):
    def slow_fail(ctx):
        time.sleep(0.2)
        return check.Result("lint", "FAIL", 0.0, "error: E501")

    def crash(ctx):
        raise RuntimeError("boom")

    monkeypatch.setattr(check, "make_snapshot", lambda repo, dest: dest.mkdir(parents=True) or "0" * 16)
    monkeypatch.setattr(check, "RUNNERS", {**check.RUNNERS, "validate": lambda ctx: check.Result("validate", "PASS"), "lint": slow_fail, "tests": crash})
    monkeypatch.setattr(sys, "argv", ["check.py", "--only", "fast", "--report-dir", str(tmp_path / "r")])
    assert check.main() == 1
    report = (tmp_path / "r/report.md").read_text()
    rows = {line.split(" | ")[0].strip("| "): line.split(" | ") for line in report.splitlines() if line.startswith("| ") and "---" not in line}
    assert rows["Step"][:4] == ["| Step", "Result", "Seconds", "Peak MB"]
    assert float(rows["lint"][2]) >= 0.2  # timed around the step, not what the step reported
    assert rows["tests"][1] == "FAIL" and "RuntimeError: boom" in rows["tests"][4]
    assert re.search(r"first blocking result: lint FAIL after \d+\.\d s", report)
    assert "Peak MB: sampled peak aggregate RSS" in report


def timing(job, started, seconds, errors=0, exception=None, shard=None):
    line = {"job": job, "started": started, "seconds": seconds, "errors": None if exception else errors, "exception": exception}
    return {**line, "shard": shard} if shard else line


def test_browser_job_section_names_the_first_finding_and_the_slowest_jobs():
    timings = [timing("a", 0, 50), timing("b", 0, 5, errors=2), timing("c", 1, 40, exception="TimeoutError"), *(timing(f"x{n}", 2, n) for n in range(12))]
    section = check.browser_job_section(timings)
    assert "15 jobs timed" in section
    assert "First job with a finding: b, ended 5.0 s after" in section  # b ended before c, though c is listed first by speed
    rows = [line for line in section.splitlines() if line.startswith("| ") and "---" not in line][1:]
    assert len(rows) == check.SLOWEST_JOBS and rows[0] == "| a | 50.0 | pass |" and rows[1] == "| c | 40.0 | TimeoutError |"
    # Shards keep their own clocks: each gets its own first finding, even when a later shard's ended "sooner".
    sharded = check.browser_job_section([timing("a", 0, 30, errors=1, shard="1/4"), timing("b", 0, 1, errors=1, shard="2/4"), timing("c", 0, 9, errors=1, shard="1/4")])
    assert "First job with a finding (shard 1/4): c, ended 9.0 s after its shard started." in sharded
    assert "First job with a finding (shard 2/4): b, ended 1.0 s after its shard started." in sharded
    assert "No job reported a finding." in check.browser_job_section([timing("a", 0, 1)])
    assert "No browser job timings" in check.browser_job_section([])


@pytest.mark.parametrize("gate_args, timed", [([], True), (["--merge=/r"], True), (["--shard=1/4", "--results=/r/s.json"], False)])
def test_gate_asks_for_timings_except_in_a_shard(tmp_path, monkeypatch, gate_args, timed):
    ctx = check.Context(tmp_path, tmp_path, tmp_path, tmp_path, None, False, tmp_path / "dist", tmp_path, built=True, gate_args=gate_args)
    seen = []

    def fake_run(ctx, step, cmd, cwd=None):
        seen.extend(cmd)
        for arg in cmd:
            if arg.startswith("--timings="):
                Path(arg.split("=", 1)[1]).write_text(json.dumps(timing("hydration/hydration_paint", 0, 3)) + "\n")
        (tmp_path / "gate.log").write_text("")
        return 0, 1.0, tmp_path / "gate.log"

    monkeypatch.setattr(check, "run", fake_run)
    assert check.step_gate(ctx).status == "PASS"
    assert any(a.startswith("--timings=") for a in seen) == timed
    assert ("hydration/hydration_paint" in ctx.sections.get("jobs", "")) == timed


@pytest.mark.parametrize("code, status", [(0, "PASS"), (1, "FAIL")])
def test_gate_report_lines_get_their_own_section(tmp_path, monkeypatch, code, status):
    ctx = check.Context(tmp_path, tmp_path, tmp_path, tmp_path, None, False, tmp_path / "dist", tmp_path, built=True)

    def fake_run(ctx, step, cmd, cwd=None):
        (tmp_path / "gate.log").write_text("$ uv run --locked\nreport line: gap 6px, not 8px\nreport line: gap 6px, not 8px\nerror: x\nreport line: weight 500\n")
        return code, 1.0, tmp_path / "gate.log"

    monkeypatch.setattr(check, "run", fake_run)
    result = check.step_gate(ctx)
    assert result.status == status
    assert ctx.sections["report lines"].count("- gap 6px, not 8px") == 1 and "- weight 500" in ctx.sections["report lines"]
    assert ("2 report lines" in result.detail) == (status == "PASS")
    assert check.REPORT_ORDER.index("report lines") < check.REPORT_ORDER.index("sheet")


def test_many_report_lines_are_cut_with_a_pointer_to_the_log(tmp_path, monkeypatch):
    ctx = check.Context(tmp_path, tmp_path, tmp_path, tmp_path, None, False, tmp_path / "dist", tmp_path, built=True)
    log = tmp_path / "gate.log"
    monkeypatch.setattr(check, "run", lambda *a, **k: (log.write_text("".join(f"report line: n{i}\n" for i in range(45))), (0, 1.0, log))[1])
    check.step_gate(ctx)
    section = ctx.sections["report lines"]
    assert "- n39" in section and "- n40" not in section and "and 5 more in the [gate log]" in section


def test_unreadable_timings_never_fail_the_gate(tmp_path, monkeypatch):
    ctx = check.Context(tmp_path, tmp_path, tmp_path, tmp_path, None, False, tmp_path / "dist", tmp_path, built=True)

    def fake_run(ctx, step, cmd, cwd=None):
        (tmp_path / "gate-timings.jsonl").write_text("{not json\n")
        (tmp_path / "gate.log").write_text("")
        return 0, 1.0, tmp_path / "gate.log"

    monkeypatch.setattr(check, "run", fake_run)
    assert check.step_gate(ctx).status == "PASS" and "Timings unreadable" in ctx.sections["jobs"]


@pytest.mark.parametrize("allow_skip, ran", [(False, ["validate", "lint"]), (True, ["validate", "lint", "export", "tests"])])
def test_fail_fast_stops_at_the_first_blocking_result(tmp_path, monkeypatch, allow_skip, ran):
    calls = []

    def runner(status):
        def run(ctx):
            calls.append(run.step)
            return check.Result(run.step, status)
        return run

    runners = {step: runner("PASS") for step in check.STEPS}
    runners["lint"] = runner("NOT RUN")
    runners["tests"] = runner("FAIL")
    for step, run in runners.items():
        run.step = step
    monkeypatch.setattr(check, "make_snapshot", lambda repo, dest: dest.mkdir(parents=True) or "0" * 16)
    monkeypatch.setattr(check, "RUNNERS", runners)
    monkeypatch.delenv("CI", raising=False)  # --allow-skip is refused in CI, where this test also runs
    monkeypatch.setattr(sys, "argv", ["check.py", "--only", "fast,copies", "--fail-fast", "--report-dir", str(tmp_path / "r"),
                                      *(["--allow-skip"] if allow_skip else [])])
    assert check.main() == 1
    assert calls == ran  # a skip that is allowed does not stop the run; a FAIL does, and copies never runs
    report = (tmp_path / "r/report.md").read_text()
    blocker = "lint NOT RUN" if not allow_skip else "tests FAIL"
    assert "| copies | NOT RUN |" in report and f"fail-fast: stopped after {blocker}" in report
    assert f"first blocking result: {blocker}" in report  # the skipped steps share its end but do not displace it


@pytest.mark.parametrize("argv, blocks", [
    (["--only", "commit"], False),  # the local hook: no terms file is not a reason to refuse a commit
    (["--only", "fast"], False),
    (["--only", "export"], False),
    (["--release", "--only", "fast"], False),  # CI's publish job
    (["--only", "fast,build"], True),  # anything beyond the fast group needs the scan
    (["--changed"], True),
    ([], True),  # the full check
    (["--only", "fast", "--commits", "HEAD~1..HEAD"], True),
    (["--only", "export", "--commits", "HEAD", "^HEAD~1"], True),
])
def test_export_without_terms_blocks_only_beyond_the_fast_group(tmp_path, monkeypatch, argv, blocks):
    def runner(step):
        return lambda ctx: check.Result(step, "NOT RUN" if step == "export" else "PASS", detail="no terms file" if step == "export" else "")

    monkeypatch.delenv("CI", raising=False)
    monkeypatch.delenv("GITHUB_EVENT_NAME", raising=False)
    monkeypatch.setattr(check, "make_snapshot", lambda repo, dest, *args: dest.mkdir(parents=True) or "0" * 16)
    monkeypatch.setattr(check, "resolve_base", lambda repo, base, release: "b" * 40)
    monkeypatch.setattr(check, "changed_paths", lambda repo, index, base: ["docs/review.md"])
    monkeypatch.setattr(check, "RUNNERS", {step: runner(step) for step in check.STEPS})
    monkeypatch.setattr(sys, "argv", ["check.py", *argv, "--report-dir", str(tmp_path / "r")])
    assert check.main() == (1 if blocks else 0)
    report = (tmp_path / "r/report.md").read_text()
    assert ("not blocking in a fast run" in report) == (not blocks)


@pytest.mark.parametrize("head, missing_terms_only, status, blocks", [
    ({"full_name": "contributor/tracker"}, True, "NOT RUN", False),
    (None, True, "NOT RUN", False),
    ({"full_name": "accelerate-europe/aisi-tracker"}, True, "NOT RUN", True),
    ({"full_name": "contributor/tracker"}, False, "NOT RUN", True),  # gitleaks missing
    ({"full_name": "contributor/tracker"}, True, "FAIL", True),
])
def test_commit_scan_exempts_only_missing_terms_on_fork_prs(tmp_path, monkeypatch, head, missing_terms_only, status, blocks):
    event = tmp_path / "event.json"
    event.write_text(json.dumps({"pull_request": {"head": {"repo": head}, "base": {"repo": {"full_name": "accelerate-europe/aisi-tracker"}}}}))
    monkeypatch.setenv("GITHUB_EVENT_NAME", "pull_request")
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event))
    monkeypatch.setenv("CI", "true")
    monkeypatch.setattr(check, "make_snapshot", lambda repo, dest, revision: dest.mkdir(parents=True) or revision)

    def runner(ctx):
        assert ctx.commits == ["HEAD~1..HEAD"]
        return check.Result("export", status, detail="no terms file" if missing_terms_only else "gitleaks NOT RUN: not on PATH",
                            missing_terms_only=missing_terms_only)

    monkeypatch.setattr(check, "RUNNERS", {"export": runner})
    monkeypatch.setattr(sys, "argv", ["check.py", "--only", "export", "--commits", "HEAD~1..HEAD", "--report-dir", str(tmp_path / "report")])
    assert check.main() == int(blocks)
    report = (tmp_path / "report/report.md").read_text()
    assert ("a maintainer runs the names scan before merging" in report) == (not blocks)


def test_ci_tree_fallback_still_requires_terms(tmp_path, monkeypatch):
    monkeypatch.setenv("CI", "true")
    monkeypatch.setenv("GITHUB_EVENT_NAME", "push")
    monkeypatch.setattr(check, "make_snapshot", lambda repo, dest: dest.mkdir(parents=True) or "snapshot")
    monkeypatch.setattr(check, "RUNNERS", {"export": lambda ctx: check.Result("export", "NOT RUN", missing_terms_only=True)})
    monkeypatch.setattr(sys, "argv", ["check.py", "--only", "export", "--report-dir", str(tmp_path / "report")])
    assert check.main() == 1


@pytest.mark.parametrize("payload", [None, "not json", "{}", '{"pull_request": {"head": {"repo": {}}, "base": {"repo": {}}}}'])
def test_unreadable_or_incomplete_pr_events_do_not_exempt_the_scan(tmp_path, monkeypatch, payload):
    event = tmp_path / "event.json"
    if payload is not None:
        event.write_text(payload)
    monkeypatch.setenv("GITHUB_EVENT_NAME", "pull_request")
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event))
    assert not check.fork_pull_request()


@pytest.fixture
def push_repo(tmp_path):
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    git(repo, "init", "-q")
    script = repo / "scripts/check.py"
    script.write_text(
        "import json, os, sys\n"
        "with open(os.environ['HOOK_CALLS'], 'a') as out:\n"
        "    out.write(json.dumps(sys.argv[1:]) + '\\n')\n"
        "print('report at test-report.md', file=sys.stderr)\n"
        "sys.exit(int(os.environ.get('HOOK_EXIT', '0')))\n"
    )
    git(repo, "add", "scripts/check.py")
    git(repo, "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-qm", "Base")
    base = check.git(repo, "rev-parse", "HEAD").strip()
    git(repo, "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "--allow-empty", "-qm", "Change")
    head = check.git(repo, "rev-parse", "HEAD").strip()
    script.write_text("raise SystemExit('staged version must not run')\n")
    git(repo, "add", "scripts/check.py")
    script.write_text("raise SystemExit('working version must not run')\n")
    # Replace only the uv launcher; Python still executes the hook's HEAD script from stdin.
    uv = tmp_path / "bin/uv"
    uv.parent.mkdir()
    uv.write_text(f"#!{sys.executable}\nimport os, sys\nos.execv(sys.executable, [sys.executable, *sys.argv[sys.argv.index('python') + 1:]])\n")
    uv.chmod(0o755)
    calls = tmp_path / "calls.jsonl"
    env = {**os.environ, "PATH": str(uv.parent) + os.pathsep + os.environ["PATH"], "HOOK_CALLS": str(calls)}
    return repo, base, head, calls, env


def bare_remote(tmp_path, repo, held=None):
    """A local stand-in for the destination; held is the commit it already has (on a branch named held)."""
    remote = tmp_path / "remote"  # no .git suffix, so the listed-address cases can add one
    git(tmp_path, "init", "-q", "--bare", str(remote))
    if held:
        git(repo, "push", "-q", str(remote), f"{held}:refs/heads/held")
    return str(remote)


def hook(repo, env, url, line):
    return subprocess.run([str(check.ROOT / ".githooks/pre-push"), "origin", url], cwd=repo, env=env, input=line + "\n",
                          capture_output=True, text=True)


@pytest.mark.parametrize("remote_state", ["known", "new", "missing", "deleted", "unchanged", "already_remote"])
def test_pre_push_selects_commits_from_what_the_destination_holds(tmp_path, push_repo, remote_state):
    repo, base, head, calls, env = push_repo
    url = bare_remote(tmp_path, repo, head if remote_state == "already_remote" else base)
    git(repo, "update-ref", "refs/remotes/origin/main", head)  # a local tracking ref proves nothing about the destination
    remote = {"known": base, "missing": "f" * 40, "unchanged": head}.get(remote_state, "0" * 40)
    local = "0" * 40 if remote_state == "deleted" else head
    result = hook(repo, env, url, f"refs/heads/main {local} refs/heads/main {remote}")
    assert result.returncode == 0, result.stderr
    if remote_state == "deleted":
        assert not calls.exists()
        return
    revisions = {  # nothing new (unchanged, already_remote): the ref names are still checked
        "known": [f"{base}..{head}"], "new": [head, f"^{base}"], "missing": [head, f"^{base}"],
        "unchanged": [head, f"^{head}"], "already_remote": [head, f"^{head}"],
    }[remote_state]
    assert json.loads(calls.read_text()) == ["--only", "export", "--ref-names", "refs/heads/main", "refs/heads/main", "--commits", *revisions]


def test_pre_push_refuses_when_the_destination_cannot_be_listed(tmp_path, push_repo):
    repo, base, head, calls, env = push_repo
    result = hook(repo, env, str(tmp_path / "no-such-remote.git"), f"refs/heads/main {head} refs/heads/main {'0' * 40}")
    assert result.returncode == 1 and "cannot list" in result.stderr and not calls.exists()


def test_pre_push_refuses_an_annotated_tag(tmp_path, push_repo):
    repo, base, head, calls, env = push_repo
    git(repo, "-c", "user.name=t", "-c", "user.email=t@example.invalid", "tag", "-a", "-m", "Release notes", "v9", head)
    tag = check.git(repo, "rev-parse", "v9").strip()
    result = hook(repo, env, bare_remote(tmp_path, repo, base), f"refs/tags/v9 {tag} refs/tags/v9 {'0' * 40}")
    assert result.returncode == 1 and "points at a tag, not a commit" in result.stderr and not calls.exists()


def test_pre_push_refuses_a_ref_to_a_blob(tmp_path, push_repo):
    repo, base, head, calls, env = push_repo
    blob = subprocess.run(["git", "-C", str(repo), "hash-object", "-w", "--stdin"], input=b"private notes\n",
                          capture_output=True, check=True).stdout.decode().strip()
    result = hook(repo, env, bare_remote(tmp_path, repo, base), f"refs/tags/notes {blob} refs/tags/notes {'0' * 40}")
    assert result.returncode == 1 and "points at a blob" in result.stderr and not calls.exists()


SKIPPED_REMOTE = "https://example.invalid/maintainers/" + "private-repo"  # the real name is a private term


@pytest.mark.parametrize("listed, skipped", [
    (SKIPPED_REMOTE + ".git", True),
    (SKIPPED_REMOTE + "/", True),
    (SKIPPED_REMOTE + "-fork.git", False),  # only an exact listed address is skipped
    (None, False),  # nothing listed: every remote is scanned
])
def test_pre_push_skips_only_listed_remotes_and_propagates_failure(tmp_path, push_repo, listed, skipped):
    repo, base, head, calls, env = push_repo
    url = bare_remote(tmp_path, repo, base)
    if listed:
        git(repo, "config", "--add", "aisi.leakScanSkipUrl", listed.replace(SKIPPED_REMOTE, url))
    env["HOOK_EXIT"] = "1"
    result = hook(repo, env, url + "/", f"refs/heads/main {head} refs/heads/main {base}")
    assert result.returncode == (0 if skipped else 1)
    assert calls.exists() != skipped
    assert ("report at test-report.md" in result.stderr) != skipped


@pytest.mark.parametrize("allow_skip", [False, True])
def test_a_failed_export_blocks_even_in_a_fast_run(allow_skip):
    failed = check.Result("export", "FAIL", detail="private term", blocks=False)
    assert check.blocking([failed], allow_skip=allow_skip) == [failed]


def test_compare_runs_before_the_build_it_does_not_need():
    assert check.STEPS.index("compare") < check.STEPS.index("build")
    assert check.STEPS[:4] == check.GROUPS["fast"] and check.STEPS.index("export") < check.STEPS.index("tests")  # cheap first


def test_content_runs_between_build_and_gate():
    start = check.STEPS.index("build")
    assert check.STEPS[start:start + 3] == ("build", "content", "gate")
    assert check.RUNNERS["content"] is check.step_content


def test_content_without_a_build_blocks_even_if_dist_exists(tmp_path, monkeypatch):
    (tmp_path / "dist").mkdir()
    ctx = check.Context(tmp_path, tmp_path, tmp_path, tmp_path, None, False, tmp_path / "dist", tmp_path)
    monkeypatch.setattr(check, "run", lambda *a, **k: pytest.fail("must not run without this snapshot's build"))
    result = check.step_content(ctx)
    assert result.status == "NOT RUN" and "no successful build" in result.detail
    assert check.blocking([result], allow_skip=False) == [result]


@pytest.mark.parametrize("workers, code", [(0, 0), (4, 0), (2, 1), (4, 3)])
def test_content_runs_locked_pytest_in_snapshot_with_context_env(tmp_path, monkeypatch, workers, code):
    ctx = check.Context(tmp_path, tmp_path / "snapshot", tmp_path, tmp_path, None, False, tmp_path / "dist", tmp_path,
                        built=True, test_workers=workers, env={"PYTHONDONTWRITEBYTECODE": "1"})
    calls = []

    def child(cmd, **kwargs):
        calls.append((cmd, kwargs["cwd"], kwargs["env"]))
        return subprocess.CompletedProcess(cmd, code)

    monkeypatch.setattr(check.subprocess, "run", child)
    result = check.step_content(ctx)
    assert result.status == ("PASS" if code == 0 else "FAIL")
    assert calls == [(["uv", "run", "--locked", "-q", "pytest", "-q", "-p", "no:cacheprovider",
                      "-n", str(workers), "tests/content", "--site", str(ctx.dist)], ctx.snapshot, ctx.env)]


def test_skip_browser_leaves_content_to_ci_without_blocking(tmp_path, monkeypatch):
    monkeypatch.setattr(check, "make_snapshot", lambda repo, dest: dest.mkdir(parents=True) or "snapshot")
    monkeypatch.setattr(check, "RUNNERS", {step: lambda ctx: pytest.fail("a skipped runner must not run")
                                         for step in ("build", "content", "gate")})
    monkeypatch.setattr(sys, "argv", ["check.py", "--only", "build,content,gate", "--skip-browser",
                                      "--fail-fast", "--report-dir", str(tmp_path / "r")])
    assert check.main() == 0
    report = (tmp_path / "r/report.md").read_text()
    assert "| content | NOT RUN |" in report
    assert "left to CI's content checks" in report and "content needs the pre-rendered build" in report
    assert "no blocking result" in report


@pytest.mark.parametrize("selected", ["content", "gate,content"])
def test_content_accepts_gate_dist_and_runs_before_merge(tmp_path, monkeypatch, selected):
    calls = []

    def runner(step):
        def run(ctx):
            calls.append((step, ctx.built, ctx.dist, ctx.gate_args))
            return check.Result(step, "PASS")
        return run

    monkeypatch.setattr(check, "make_snapshot", lambda repo, dest: dest.mkdir(parents=True) or "snapshot")
    monkeypatch.setattr(check, "RUNNERS", {step: runner(step) for step in ("content", "gate")})
    merge = ["--gate-merge", str(tmp_path / "shards")] if "gate" in selected else []
    monkeypatch.setattr(sys, "argv", ["check.py", "--only", selected, "--gate-dist", str(tmp_path / "site"),
                                      *merge, "--report-dir", str(tmp_path / "r")])
    assert check.main() == 0
    expected_args = [f"--merge={tmp_path / 'shards'}"] if merge else []
    assert calls == [(step, True, tmp_path / "site", expected_args)
                     for step in ("content", "gate") if step in selected]


def test_gate_exit_codes(tmp_path):
    log = tmp_path / "gate.log"
    log.write_text("error: card misaligned\n")
    assert check.gate_result("gate", 0, 1, log).status == "PASS"
    assert check.gate_result("gate", check.NOT_RUN, 1, log).status == "NOT RUN"
    failed = check.gate_result("gate", 1, 1, log)
    assert failed.status == "FAIL" and "card misaligned" in failed.detail


def test_allow_skip_is_refused_in_ci(monkeypatch, capsys):
    monkeypatch.setenv("CI", "true")
    monkeypatch.setattr(sys, "argv", ["check.py", "--allow-skip"])
    with pytest.raises(SystemExit) as caught:
        check.main()
    assert caught.value.code == 2
    assert "local runs only" in capsys.readouterr().err


def test_fast_group_expands_and_unknown_steps_fail(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["check.py", "--only", "fast,bogus"])
    with pytest.raises(SystemExit):
        check.main()
    assert "unknown steps: bogus" in capsys.readouterr().err
    assert check.GROUPS["fast"] == ("validate", "lint", "export", "tests")
    # The commit hook keeps the names scan and gitleaks (export); the tests run on CI only.
    assert check.GROUPS["commit"] == ("validate", "lint", "export")


def test_the_pre_commit_hook_runs_the_commit_group():
    hook = (check.ROOT / ".githooks/pre-commit").read_text(encoding="utf-8")
    assert hook.rstrip().endswith("python - --only commit")


@pytest.mark.parametrize("argv, message", [
    (["--only", "fast", "--gate-merge", "results"], "--gate-merge needs the gate step"),
    (["--only", "content", "--gate-dist", "site", "--gate-merge", "results"], "--gate-merge needs the gate step"),
    (["--only", "tests", "--gate-dist", "site"], "--gate-dist needs the content or gate step"),
    (["--only", "build,content", "--gate-dist", "site"], "checks an existing build"),
    (["--only", "gate", "--gate-results", "shard.json"], "go together"),
    (["--only", "gate", "--gate-shard", "1/4"], "go together"),
    (["--only", "gate", "--gate-shard", "1/4", "--gate-results", "s.json", "--gate-merge", "results"], "exclude each other"),
    (["--only", "build,gate", "--gate-dist", "site"], "checks an existing build"),
    (["--only", "gate", "--gate-dist", "site", "--out", "dist"], "checks an existing build"),
    (["--only", "gate", "--gate-jobs", "0"], "--gate-jobs needs at least 1"),
    (["--only", "tests", "--test-workers", "-1"], "--test-workers needs 0 or more"),
    *((["--only", "gate", "--gate-shard", spec, "--gate-results", "s.json"], "expects K/N") for spec in ("", "0/4", "5/4", "4", "a/b", "1/0")),
])
def test_gate_and_test_options_are_checked_before_anything_runs(monkeypatch, capsys, argv, message):
    monkeypatch.setattr(sys, "argv", ["check.py", *argv])
    with pytest.raises(SystemExit) as caught:
        check.main()
    assert caught.value.code == 2
    assert message in capsys.readouterr().err


def test_gate_arguments_reach_check_page(tmp_path):
    from argparse import Namespace

    args = Namespace(gate_shard="2/4", gate_results=tmp_path / "s.json", gate_merge=None, gate_jobs=3)
    assert check.gate_arguments(args) == ["--shard=2/4", f"--results={tmp_path / 's.json'}", "--jobs=3"]
    args = Namespace(gate_shard=None, gate_results=None, gate_merge=tmp_path / "gate", gate_jobs=None)
    assert check.gate_arguments(args) == [f"--merge={tmp_path / 'gate'}"]
    assert check.gate_arguments(Namespace(gate_shard=None, gate_results=None, gate_merge=None, gate_jobs=None)) == []


def test_diff_matches_records_by_id_and_reports_new_pointers():
    old = {"jurisdictions": [{"id": "AT", "status": "exploring"}, {"id": "GB", "status": "planned"}]}
    new = {"jurisdictions": [{"id": "GB", "status": "established"}, {"id": "FR", "status": "planned"}]}
    assert check.diff(old, new) == [
        ("/jurisdictions/0/status", "planned", "established"),
        ("/jurisdictions/1", None, {"id": "FR", "status": "planned"}),
        ("/jurisdictions/0 (removed)", {"id": "AT", "status": "exploring"}, None),
    ]


def test_removed_record_is_matched_to_its_own_evidence_in_the_base():
    old = {"jurisdictions": [{"id": "AT"}, {"id": "GB"}], "sources": [{"id": "s", "excerpts": [
        {"text": "Austria", "supports": ["/jurisdictions/0/status"]},
        {"text": "UK", "supports": ["/jurisdictions/1/status"]},
    ]}]}
    [(pointer, _, after)] = check.diff(old["jurisdictions"], [{"id": "GB"}], "/jurisdictions")
    assert after is None
    assert [e["text"] for _, e in check.supporting(old, pointer)] == ["Austria"]


def test_nested_removal_keeps_its_base_pointer_after_reordering():
    old = [{"id": "AT", "name": "Austria"}, {"id": "GB", "name": "UK", "name_local": "UK"}]
    new = [{"id": "GB", "name": "UK"}]
    assert check.diff(old, new, "/institutes") == [
        ("/institutes/1/name_local (removed)", "UK", None),
        ("/institutes/0 (removed)", old[0], None),
    ]


def test_supporting_excerpts_cover_the_claim_its_parts_and_its_record():
    data = {"sources": [{"id": "s", "excerpts": [
        {"text": "exact", "supports": ["/jurisdictions/0/status"]},
        {"text": "inside", "supports": ["/jurisdictions/0/timeline/1/date"]},
        {"text": "unrelated", "supports": ["/jurisdictions/10/status"]},
    ]}]}
    assert [e["text"] for _, e in check.supporting(data, "/jurisdictions/0/status")] == ["exact"]
    assert [e["text"] for _, e in check.supporting(data, "/jurisdictions/0")] == ["exact", "inside"]
    assert [e["text"] for _, e in check.supporting(data, "/jurisdictions/0/timeline/1/date/x")] == ["inside"]


@pytest.mark.parametrize(("report", "status"), [
    ({"sources": [{"excerpts": [{"source_id": "s", "text": "a", "status": "matched"}, {"source_id": "s", "text": "b", "status": "not_cached"}]}]}, "NOT RUN"),
    ({"sources": [{"excerpts": [{"source_id": "s", "text": "a", "status": "mismatch"}, {"source_id": "s", "text": "b", "status": "matched"}]}]}, "FAIL"),
    ({"sources": [{"excerpts": [{"source_id": "s", "text": "a", "status": "matched"}]}]}, "FAIL"),
    ({"partial": True, "sources": [{"excerpts": [{"source_id": "s", "text": "a", "status": "matched"}, {"source_id": "s", "text": "b", "status": "matched"}]}]}, "FAIL"),
])
def test_quote_verification_needs_a_complete_report(tmp_path, monkeypatch, report, status):
    (tmp_path / "cache").mkdir()
    ctx = check.Context(tmp_path, tmp_path, tmp_path, tmp_path, "HEAD", False, tmp_path / "dist", tmp_path / "cache")

    def fake_run(ctx, step, cmd, cwd=None):
        (tmp_path / "excerpt-verification.json").write_text(json.dumps(report))
        log = tmp_path / "log"
        log.write_text("verifier output\n")
        return 1, 0.0, log

    monkeypatch.setattr(check, "run", fake_run)
    assert check.verify_quotes(ctx, {}, [("s", "a"), ("s", "b")])[0] == status


def test_child_environment_drops_git_and_uv_routing(monkeypatch):
    routing = {"GIT_INDEX_FILE", "VIRTUAL_ENV", "UV_WORKING_DIR", "UV_PROJECT", "UV_PROJECT_ENVIRONMENT", "UV_NO_PROJECT", "UV_NO_SYNC", "UV_FROZEN"}
    for name in routing | {"UV_CACHE_DIR"}:
        monkeypatch.setenv(name, "/checkout")
    monkeypatch.setenv("UV_EXCLUDE_NEWER", "7 days")  # CI sets the window uv.lock records
    env = check.clean_env()
    assert not routing & set(env)
    assert env["UV_CACHE_DIR"] == "/checkout"
    assert env["UV_EXCLUDE_NEWER"] == "7 days"
    assert env["PYTHONDONTWRITEBYTECODE"] == "1"


def test_extremes_track_longest_strings_and_lists_per_field():
    data = {"jurisdictions": [{"name": "Chad", "timeline": [1, 2]}, {"name": "Liechtenstein", "timeline": []}]}
    assert check.extremes(data) == {"/jurisdictions[]": 2, "/jurisdictions/*/name": 13, "/jurisdictions/*/timeline[]": 2}


@pytest.mark.parametrize(("path", "content", "impact"), [
    ("site/index.html", "<p>changed</p>", "site/index.html changed"),
    ("scripts/published.py", "derived facts", "scripts/published.py changed"),
    ("scripts/display.py", "field citations", "scripts/display.py changed"),
    ("scripts/row_checks.py", "row citations", "scripts/row_checks.py changed"),
    ("data/aisi.json", '{"jurisdictions": [{"name": "Liechtensteinerland"}]}', "longest content grew"),
    ("data/aisi.json", '{"jurisdictions": [{"name": "Chad"}]}', None),
    ("README.md", "words", None),
])
def test_rendering_impact_from_page_renderer_or_longer_content(tmp_path, path, content, impact):
    repo = tmp_path / "repo"
    (repo / "site").mkdir(parents=True)
    (repo / "data").mkdir()
    (repo / "scripts").mkdir()
    (repo / "site/index.html").write_text("<p>page</p>")
    (repo / "data/aisi.json").write_text('{"jurisdictions": [{"name": "Liechtenstein"}]}')
    (repo / "README.md").write_text("readme")
    git(repo, "init", "-q")
    git(repo, "add", "-A")
    git(repo, "-c", "user.name=t", "-c", "user.email=t@example.org", "commit", "-q", "-m", "base")
    (repo / path).write_text(content)
    git(repo, "add", path)
    work = tmp_path / "work"
    check.make_snapshot(repo, work / "snapshot")
    ctx = check.Context(repo, work / "snapshot", work, tmp_path, "HEAD", False, work / "dist", tmp_path)
    reasons = check.rendering_impact(ctx)
    assert (impact is None and reasons == []) or any(impact in r for r in reasons)


def test_crop_links_name_a_missing_side(tmp_path):
    (tmp_path / "after").mkdir()
    (tmp_path / "after/390-light-hero.png").write_bytes(b"png")
    links = check.crop_links(tmp_path, "390-light-hero")
    assert links.startswith("before: absent, [after](")


@pytest.fixture
def sheet_run(tmp_path, monkeypatch):
    ctx = check.Context(tmp_path, tmp_path, tmp_path, tmp_path / "report", "HEAD", False,
                        tmp_path / "dist", tmp_path / "cache", built=True)
    ctx.dist.mkdir()
    calls = []

    def impact(ctx):
        calls.append("impact")
        return []

    def capture(ctx, dist, out):
        calls.append((dist, out.name))
        return 0, 0.0, tmp_path / "log"

    def build_base(ctx):
        calls.append("base")
        return tmp_path / "base-dist"

    def run(ctx, step, cmd):
        calls.append("sheets")
        assert "scripts/contact_sheet.py" in cmd
        assert ("--before" in cmd) == ("base" in calls)
        sheets = ctx.report / "sheets"
        sheets.mkdir(parents=True)
        (sheets / "index.json").write_text(json.dumps({"changes": [], "compared": 1, "sheets": {}}))
        return 0, 0.0, tmp_path / "log"

    monkeypatch.setattr(check, "rendering_impact", impact)
    monkeypatch.setattr(check, "capture", capture)
    monkeypatch.setattr(check, "build_base", build_base)
    monkeypatch.setattr(check, "run", run)
    return ctx, calls


@pytest.mark.parametrize("built", [False, True])
def test_sheet_without_impact_skips_capture_and_does_not_block(sheet_run, built):
    ctx, calls = sheet_run
    ctx.built = built
    result = check.step_sheet(ctx)
    assert calls == ["impact"]
    assert result.status == "NOT RUN" and not result.blocks
    assert check.blocking([result], False) == []
    assert "No rendering impact against HEAD" in result.detail
    assert "no sheet" in result.detail and "no visual pass needed" in result.detail
    assert ctx.sections["sheet"] == f"## Visual pass\n\n{result.detail}\n"


@pytest.mark.parametrize("raises", [False, True])
def test_sheet_with_impact_captures_before_and_after(sheet_run, monkeypatch, raises):
    ctx, calls = sheet_run

    def impact(ctx):
        calls.append("impact")
        if raises:
            raise RuntimeError("dependency unknown")
        return ["site/index.html changed"]

    monkeypatch.setattr(check, "rendering_impact", impact)
    result = check.step_sheet(ctx)
    assert result.status == "PASS"
    assert calls == ["impact", (ctx.dist, "after"), "base", (ctx.work / "base-dist", "before"), "sheets"]
    assert "where" in ctx.sections
    if raises:
        assert "RuntimeError: dependency unknown" in result.detail
        assert "counted as impact" in result.detail
        assert "unknown rendering impact" in ctx.sections["sheet"]
    else:
        assert "site/index.html changed" in ctx.sections["sheet"]


def test_sheet_at_release_captures_after_even_without_impact(sheet_run):
    ctx, calls = sheet_run
    ctx.release = True
    assert check.step_sheet(ctx).status == "PASS"
    assert calls == ["impact", (ctx.dist, "after"), "sheets"]
    assert "Full sheets" in ctx.sections["sheet"]


@pytest.mark.parametrize("failure", ["after", "before", "sheets"])
def test_sheet_keeps_unknown_impact_detail_when_capture_or_sheet_fails(sheet_run, monkeypatch, failure):
    ctx, _ = sheet_run
    log = ctx.work / "log"
    log.write_text("capture or sheet failed\n")
    capture = check.capture

    def impact(ctx):
        raise RuntimeError("dependency unknown")

    def failing_capture(ctx, dist, out):
        return (1, 0.0, log) if out.name == failure else capture(ctx, dist, out)

    monkeypatch.setattr(check, "rendering_impact", impact)
    monkeypatch.setattr(check, "capture", failing_capture)
    if failure == "sheets":
        monkeypatch.setattr(check, "run", lambda *args: (1, 0.0, log))
    result = check.step_sheet(ctx)
    assert result.status == "FAIL"
    assert "capture or sheet failed" in result.detail
    assert "RuntimeError: dependency unknown" in result.detail
    assert "counted as impact" in result.detail


def test_sheet_without_a_base_keeps_after_capture(sheet_run):
    ctx, calls = sheet_run
    ctx.base = None
    assert check.step_sheet(ctx).status == "PASS"
    assert calls == [(ctx.dist, "after"), "sheets"]


def test_where_to_look_then_visual_pass_come_before_the_claims_in_the_report():
    # compare runs before sheet, so insertion order would put the claims first.
    sections = {"compare": "## Changes against base", "sheet": "## Visual pass", "where": "## Where to look"}
    assert check.ordered_sections(sections) == ["## Where to look", "## Visual pass", "## Changes against base"]


def test_where_to_look_links_each_change_into_the_served_build(tmp_path):
    (tmp_path / "dist").mkdir()
    (tmp_path / "dist/index.html").write_text("page")
    report = tmp_path / "report"
    report.mkdir()
    ctx = check.Context(tmp_path, tmp_path, tmp_path, report, "HEAD", False, tmp_path / "dist", tmp_path / "cache", serve_url="http://127.0.0.1:9000/")
    change = {"number": 1, "label": "Sweden card, Budget field", "anchor": "c-se", "what": "changed", "states": "390 px dark"}
    text = check.where_to_look(ctx, [change])
    assert "1. Sweden card, Budget field: changed; at 390 px dark. [Open](http://127.0.0.1:9000/#c-se)" in text
    assert "http.server 9000" in text and (report / "site/index.html").read_text() == "page"


def test_binding_only_edits_appear_in_report_with_full_references(tmp_path, monkeypatch):
    old_target = ["institutes", {"id": "first"}, "mandate_short"]
    new_target = ["institutes", {"id": "second"}, "mandate_short"]
    def dataset(target):
        return {"sources": [{"id": "shared", "url": "https://example.org/source", "excerpts": [
            {"text": "Unchanged retained quotation", "supports": [target]},
            {"text": "Unchanged retained quotation", "supports": [["institutes", {"id": "neighbour"}, "name"]]},
        ]}]}
    old, new = dataset(old_target), dataset(new_target)
    (tmp_path / "data").mkdir()
    (tmp_path / "data/aisi.json").write_text(json.dumps(new))
    monkeypatch.setattr(check, "git", lambda *args, **kwargs: json.dumps(old))
    ctx = check.Context(tmp_path, tmp_path, tmp_path, tmp_path, "HEAD", False, tmp_path / "dist", tmp_path / "absent-cache")
    result = check.step_compare(ctx)
    assert result.status == "PASS"
    assert "0 changed claims, 1 changed evidence entries, 0 new or edited excerpts" in result.detail
    assert "Changed evidence bindings and glosses (1)" in ctx.sections["compare"]
    assert "Unchanged retained quotation" in ctx.sections["compare"]
    assert "not unchanged quotations whose bindings changed" in ctx.sections["compare"]
    rows = json.loads((tmp_path / "evidence-bindings.json").read_text())
    assert rows == [{
        "source_id": "shared", "excerpt_id": "sha256:" + hashlib.sha256(b"Unchanged retained quotation").hexdigest(), "text": "Unchanged retained quotation", "url": "https://example.org/source",
        "added": [new_target], "removed": [old_target],
    }]
    new["sources"][0]["excerpts"].reverse()
    assert check.binding_changes(old, new) == rows
    assert check.binding_changes(new, new) == []


def test_gloss_only_edit_is_visible_without_claiming_literal_verification(tmp_path, monkeypatch):
    old = {"sources": [{"id": "s", "url": "https://example.org/source", "excerpts": [
        {"text": "Seguridad", "gloss": "Safety", "supports": ["/institutes/0/name"]}
    ]}]}
    new = json.loads(json.dumps(old))
    new["sources"][0]["excerpts"][0]["gloss"] = "Security"
    (tmp_path / "data").mkdir()
    (tmp_path / "data/aisi.json").write_text(json.dumps(new))
    monkeypatch.setattr(check, "git", lambda *args, **kwargs: json.dumps(old))
    ctx = check.Context(tmp_path, tmp_path, tmp_path, tmp_path, "HEAD", False, tmp_path / "dist", tmp_path / "absent-cache")
    result = check.step_compare(ctx)
    assert result.status == "PASS"
    assert "1 changed evidence entries, 0 new or edited excerpts" in result.detail
    assert "Safety" in ctx.sections["compare"] and "Security" in ctx.sections["compare"]
    assert json.loads((tmp_path / "evidence-bindings.json").read_text()) == [{
        "source_id": "s", "excerpt_id": "sha256:" + hashlib.sha256(b"Seguridad").hexdigest(), "text": "Seguridad", "url": "https://example.org/source",
        "added": [], "removed": [], "gloss_before": ["Safety"], "gloss_after": ["Security"],
    }]


@pytest.mark.parametrize("value", [None, "", "~/private-sources"])
def test_snapshot_defaults_follow_environment(value):
    env = dict(os.environ)
    env.pop("AISI_SNAPSHOTS", None)
    if value is not None:
        env["AISI_SNAPSHOTS"] = value
    result = subprocess.run(
        [sys.executable, "-c", "from scripts import check, snapshot_sources as s; "
         "print(check.SNAPSHOTS); print(s.PRIVATE_SNAPSHOTS)"],
        env=env, capture_output=True, text=True, check=True,
    )
    expected = str(Path(value).expanduser()) if value else "None"
    assert result.stdout.splitlines() == [expected, expected]


def test_snapshot_cli_overrides_environment_default(tmp_path, monkeypatch):
    monkeypatch.setattr(check, "SNAPSHOTS", tmp_path / "environment-store")
    selected = []
    monkeypatch.setattr(check, "preflight", lambda cache, store: selected.append(store) or 0)
    monkeypatch.setattr(sys, "argv", ["check.py", "--preflight", "--snapshots", str(tmp_path / "cli-store")])
    assert check.main() == 0
    assert selected == [tmp_path / "cli-store"]


def test_preflight_reports_unconfigured_store(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(check.shutil, "which", lambda tool: "/bin/" + tool)
    monkeypatch.setattr(check.subprocess, "run", lambda *a, **kw: subprocess.CompletedProcess(a, 0, stdout="/browser"))
    assert check.preflight(tmp_path, None) == 1
    assert "not configured (set AISI_SNAPSHOTS" in capsys.readouterr().out


def test_quote_verification_runs_from_snapshots_without_a_cache(tmp_path, monkeypatch):
    (tmp_path / "snapshots").mkdir()
    ctx = check.Context(tmp_path, tmp_path, tmp_path, tmp_path, "HEAD", False, tmp_path / "dist", tmp_path / "no-cache",
                        snapshots=tmp_path / "snapshots")
    seen = []

    def fake_run(ctx, step, cmd, cwd=None):
        seen.append(cmd)
        (tmp_path / "excerpt-verification.json").write_text(json.dumps({"sources": [{"excerpts": [{"source_id": "s", "text": "a", "status": "matched"}]}]}))
        log = tmp_path / "log"
        log.write_text("verifier output\n")
        return 0, 0.0, log

    monkeypatch.setattr(check, "run", fake_run)
    assert check.verify_quotes(ctx, {}, [("s", "a")])[0] == "PASS"
    assert "--seed-snapshots" in seen[0] and "--seed-cache" not in seen[0]
    for store in (None, tmp_path / "absent"):
        ctx.snapshots = store
        status, lines = check.verify_quotes(ctx, {}, [("s", "a")])
        assert status == "NOT RUN"
        detail = " ".join(lines)
        assert "at None" not in detail
        assert ("not configured (set AISI_SNAPSHOTS or pass --snapshots DIR)" if store is None else f"at {store}") in detail


def copies_ctx(tmp_path, store):
    snapshot = tmp_path / "snapshot"
    (snapshot / "data").mkdir(parents=True)
    body = b"<html>cited copy</html>"
    digest = hashlib.sha256(body).hexdigest()
    data = {"sources": [
        {"id": "held", "snapshot": {"retrieved": "2026-10-17T14:00:00Z", "sha256": digest}},
        {"id": "none"},
    ]}
    (snapshot / "data/aisi.json").write_text(json.dumps(data))
    if store is not None:
        (store / "held").mkdir(parents=True)
        (store / "held/copy.html").write_bytes(body)
        (store / "held/manifest.json").write_text(json.dumps([{"path": "held/copy.html", "sha256": digest}]))
    return check.Context(tmp_path, snapshot, tmp_path, tmp_path, "HEAD", False, tmp_path / "dist",
                         tmp_path / "cache", snapshots=store)


def test_copies_step_requires_every_recorded_snapshot_in_the_private_store(tmp_path):
    store = tmp_path / "store"
    ctx = copies_ctx(tmp_path, store)
    assert check.step_copies(ctx).status == "PASS"
    (store / "held/copy.html").write_bytes(b"<html>other bytes</html>")
    result = check.step_copies(ctx)
    assert result.status == "FAIL" and result.detail.endswith(": held")
    (store / "held/copy.html").unlink()
    assert check.step_copies(ctx).status == "FAIL"


def test_copies_step_is_not_run_without_the_private_store(tmp_path):
    ctx = copies_ctx(tmp_path, None)
    for store in (None, tmp_path / "absent"):
        ctx.snapshots = store
        result = check.step_copies(ctx)
        assert result.status == "NOT RUN" and "private store" in result.detail
        assert "AISI_SNAPSHOTS" in result.detail
        assert "at None" not in result.detail
        assert ("not configured (set AISI_SNAPSHOTS or pass --snapshots DIR)" if store is None else f"at {store}") in result.detail
        assert check.blocking([result], allow_skip=False) == [result]
        assert check.blocking([result], allow_skip=True) == []


@pytest.mark.parametrize("unverified", ["not_cached", "unreachable", "rejected_content", "unextractable"])
@pytest.mark.parametrize("other,expected", [("matched", "NOT RUN"), ("mismatch", "FAIL")])
def test_unverified_changed_quotes_block_and_list_identity(tmp_path, monkeypatch, unverified, other, expected):
    (tmp_path / "cache").mkdir()
    ctx = check.Context(tmp_path, tmp_path, tmp_path, tmp_path, "HEAD", False, tmp_path / "dist", tmp_path / "cache")
    rows = [
        {"source_id": "s", "excerpt_id": "unavailable-evidence", "text": "a", "status": unverified},
        {"source_id": "s", "excerpt_id": "other-evidence", "text": "b", "status": other},
    ]

    def fake_run(ctx, step, cmd, cwd=None):
        (tmp_path / "excerpt-verification.json").write_text(json.dumps({"sources": [{"excerpts": rows}]}))
        log = tmp_path / "log"
        log.write_text("verifier output\n")
        return 1, 0.0, log

    monkeypatch.setattr(check, "run", fake_run)
    status, lines = check.verify_quotes(ctx, {}, [("s", "a"), ("s", "b")])
    assert status == expected
    assert any("unavailable-evidence" in line and unverified in line for line in lines)
    assert "1 unverified" in "\n".join(lines)


def test_no_changed_quotes_need_no_cache(tmp_path):
    ctx = check.Context(tmp_path, tmp_path, tmp_path, tmp_path, "HEAD", False, tmp_path / "dist", tmp_path / "absent-cache")
    assert check.verify_quotes(ctx, {}, [])[0] == "PASS"


def test_release_warnings_follow_where_to_look():
    sections = {"compare": "## Changes against base", "warnings": "## Release warnings", "where": "## Where to look"}
    assert check.ordered_sections(sections) == ["## Where to look", "## Release warnings", "## Changes against base"]


@pytest.mark.parametrize("warnings, detail", [(["- source eu-act: status never verified"], "1 release warning (see Release warnings)"), ([], "")])
def test_validate_carries_release_warnings_into_the_report_without_failing(tmp_path, monkeypatch, warnings, detail):
    ctx = check.Context(tmp_path, tmp_path, tmp_path, tmp_path, "HEAD", False, tmp_path / "dist", tmp_path / "cache")
    section = "\n".join(["## Release warnings", "", *(warnings or ["None."])]) + "\n"

    def validated(ctx, step, cmd):
        report_dir = Path(cmd[cmd.index("--report-dir") + 1])
        report_dir.mkdir(parents=True)
        (report_dir / "release-warnings.md").write_text(section)
        return check.Result(step, "PASS")

    monkeypatch.setattr(check, "command_step", validated)
    result = check.step_validate(ctx)
    assert (result.status, result.detail) == ("PASS", detail)
    assert ctx.sections["warnings"] == section


def commit(repo: Path, text: str) -> str:
    (repo / "a.txt").write_text(text)
    git(repo, "add", "a.txt")
    git(repo, "-c", "user.name=t", "-c", "user.email=t@example.org", "-c", "commit.gpgsign=false", "commit", "-q", "-m", text)
    return subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()


@pytest.fixture
def clone(tmp_path):
    """A clone whose main tracks origin/main, both at the second of two tagged releases. Git runs without the
    user's or system configuration (conftest isolated_git)."""
    origin = tmp_path / "origin"
    origin.mkdir()
    git(origin, "init", "-q", "-b", "main")
    first = commit(origin, "1.0.1")
    git(origin, "tag", "v1.0.1")
    second = commit(origin, "1.0.2")
    git(origin, "tag", "-a", "v1.0.2", "-m", "release")
    git(tmp_path, "clone", "-q", str(origin), "clone")
    return tmp_path / "clone", origin, first, second


def test_release_compares_with_the_last_release_tag_by_default(clone):
    repo, _, first, second = clone
    work = commit(repo, "1.0.3 work")
    assert check.resolve_base(repo, None, release=True) == second  # an annotated tag resolves to its commit
    assert check.resolve_base(repo, None, release=False) == work  # HEAD, as before, though main is now ahead of origin/main
    assert check.resolve_base(repo, "HEAD", release=False) == work
    assert check.resolve_base(repo, "v1.0.1", release=True) == first  # an explicit --base wins
    with pytest.raises(ValueError, match="not a commit"):
        check.resolve_base(repo, "", release=True)  # an empty --base is not "no --base"
    # A stale local branch named like the release tag does not stand in for the tag.
    git(repo, "branch", "-q", "v1.0.2", first)
    git(repo, "branch", "-q", "--set-upstream-to=origin/main", "v1.0.2")
    assert check.resolve_base(repo, None, release=True) == second
    bare = repo.parent / "untagged"
    bare.mkdir()
    git(bare, "init", "-q")
    commit(bare, "only")
    with pytest.raises(ValueError, match="no.*v\\* release tag|HEAD has none"):
        check.resolve_base(bare, None, release=True)


def test_a_local_branch_that_differs_from_its_upstream_is_refused(clone):
    """A stale local main behind origin/main would make compare report changes that are not in the release."""
    repo, origin, _, second = clone
    assert check.resolve_base(repo, "main", release=False) == second  # in step with origin/main: fine
    newer = commit(origin, "1.0.3")
    git(repo, "fetch", "-q")
    with pytest.raises(ValueError, match=r"--base main is the local branch at .* its upstream origin/main is at .*--base origin/main"):
        check.resolve_base(repo, "main", release=False)
    with pytest.raises(ValueError, match="upstream origin/main"):
        check.resolve_base(repo, "refs/heads/main", release=False)  # the full spelling is the same branch
    assert check.resolve_base(repo, "origin/main", release=False) == newer
    git(repo, "branch", "-q", "--no-track", "scratch")
    assert check.resolve_base(repo, "scratch", release=False) == second  # no upstream: nothing to differ from
    for missing in ("no-such-ref", "refs/heads/missing", ""):
        with pytest.raises(ValueError, match="not a commit"):
            check.resolve_base(repo, missing, release=False)
    # A stale local branch that shares its name with the remote branch: refused, not silently preferred.
    git(repo, "branch", "-q", "origin/main", second)
    with pytest.raises(ValueError, match="ambiguous"):
        check.resolve_base(repo, "origin/main", release=False)
    git(repo, "config", "--local", "core.warnAmbiguousRefs", "false")  # the repository's setting does not hide it
    with pytest.raises(ValueError, match="ambiguous"):
        check.resolve_base(repo, "origin/main", release=False)
    assert check.resolve_base(repo, "refs/remotes/origin/main", release=False) == newer


def test_an_unborn_branch_is_not_a_base(tmp_path):
    git(tmp_path, "init", "-q", "-b", "main", "empty")
    for base in ("main", "refs/heads/main"):
        with pytest.raises(ValueError, match="not a commit"):
            check.resolve_base(tmp_path / "empty", base, release=False)


def test_the_coordinator_and_release_checks_share_the_run_date_setting():
    from scripts import release_checks

    assert (check.RUN_DATE_ENV, check.RUN_ZONE) == (release_checks.RUN_DATE_ENV, release_checks.RUN_ZONE)


@pytest.mark.parametrize("paths, steps", [
    ([], check.GROUPS["fast"]),
    (["docs/review.md", "README.md", "tests/test_api.py", ".githooks/pre-commit"], check.GROUPS["fast"]),
    ([".github/workflows/pages.yml", "docs/x.md"], check.BUILD_STEPS),  # a workflow change gets the full CI run
    ([".github/actions/fonts/action.yml"], check.BUILD_STEPS),
    (["scripts/research_log.py"], check.GROUPS["fast"]),
    (["scripts/research_pass.py"], (*check.GROUPS["fast"], "passes")),
    (["scripts/verify_excerpts.py"], (*check.GROUPS["fast"], "compare")),
    (["data/aisi.json"], check.DATA_STEPS),
    (["site/index.html"], check.BUILD_STEPS),
    (["scripts/check_page.py", "docs/x.md"], check.BUILD_STEPS),
    (["tests/fixtures/earliest_credit.json"], check.BUILD_STEPS),  # the gate reads it
    (["tests/content/test_saved_page.py"], check.BUILD_STEPS),
    (["tests/content/conftest.py"], check.BUILD_STEPS),
    (["schema/aisi.schema.json"], check.BUILD_STEPS),
    (["uv.lock"], check.BUILD_STEPS),
    (["newdir/thing.txt", "docs/x.md"], check.STEPS),  # a path no rule names: every step, the sheet included
    (["READMEX.md"], check.STEPS),  # a file rule is an exact name, not a prefix
])
def test_changed_files_choose_their_steps(paths, steps):
    chosen, rows = check.changed_steps(paths)
    assert chosen == steps and [row[0] for row in rows] == paths
    assert set(check.GROUPS["fast"]) <= set(chosen)  # the fast checks, export among them, run for every change


def test_sheet_needs_an_unknown_path_and_build_steps_keep_compare():
    assert "sheet" not in check.DATA_STEPS + check.BUILD_STEPS  # sheets run at merge or release, not per commit
    assert {"compare", "build", "content", "gate"} <= set(check.BUILD_STEPS)
    assert {"build", "content", "gate"} <= set(check.DATA_STEPS)


def docstring_free_strings(tree):
    """String constants of a module that are not docstrings."""
    import ast

    docs = {id(node.body[0].value) for node in ast.walk(tree)
            if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.body
            and isinstance(node.body[0], ast.Expr) and isinstance(node.body[0].value, ast.Constant)}
    return [n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in docs]


def imported_scripts(tree) -> set[str]:
    """Names a module could import from scripts/: `import x`, `import scripts.x`, `from x import`, `from scripts.x
    import`, `from scripts import x`, `from . import x` and `from .x import` (any name; callers keep the scripts)."""
    import ast

    def local(dotted: str) -> list[str]:
        parts = dotted.split(".")
        return parts[1:2] if parts[0] == "scripts" else parts[:1]

    names = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            names |= {m for a in n.names for m in local(a.name)}
        elif isinstance(n, ast.ImportFrom):
            if n.module and n.module != "scripts":
                names |= set(local(n.module))
            if not n.module or n.module == "scripts":  # from . import x, from scripts import x
                names |= {a.name for a in n.names}
    return names


def test_import_forms_are_all_seen():
    import ast

    code = "import scripts.a\nfrom scripts.b import f\nfrom scripts import c\nfrom . import d\nfrom .e import g\nimport h.i\nfrom j import k"
    assert imported_scripts(ast.parse(code)) == {"a", "b", "c", "d", "e", "h", "j"}


def test_scripts_without_browser_steps_are_not_used_by_the_build_or_the_gate():
    import ast

    scripts = Path(check.__file__).parent
    modules = {p.stem: p for p in scripts.glob("*.py")}
    used, todo = set(), ["build", "check_page", "prerender", "check"]
    while todo:  # every module the build, the gate, the pre-render and this coordinator import, transitively
        name = todo.pop()
        if name in used or name not in modules:
            continue
        used.add(name)
        todo += sorted(imported_scripts(ast.parse(modules[name].read_text())))
    assert not used & set(check.NO_BROWSER_SCRIPTS)
    for name in used:  # nor run or read by file name (docstrings may name them)
        strings = docstring_free_strings(ast.parse(modules[name].read_text()))
        assert not [s for s in strings for other in check.NO_BROWSER_SCRIPTS if f"{other}.py" in s], name
    assert set(check.NO_BROWSER_SCRIPTS) <= set(modules)


def test_changed_paths_come_from_the_frozen_index_with_both_sides_of_a_rename(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    (repo / "docs").mkdir()
    (repo / "docs/old name.md").write_text("same text\n" * 20)
    (repo / "gone.txt").write_text("x")
    git(repo, "add", "-A")
    base = commit(repo, "base")
    git(repo, "mv", "docs/old name.md", "docs/new name.md")
    git(repo, "rm", "-q", "gone.txt")
    frozen = tmp_path / "frozen.index"
    frozen.write_bytes((repo / ".git/index").read_bytes())
    (repo / "later.txt").write_text("staged after the snapshot")
    git(repo, "add", "later.txt")
    assert check.changed_paths(repo, frozen, base) == ["docs/new name.md", "docs/old name.md", "gone.txt"]


def test_changed_runs_only_the_chosen_steps_and_says_why(tmp_path, monkeypatch):
    calls = []

    def runner(step):
        return lambda ctx: calls.append(step) or check.Result(step, "PASS")

    monkeypatch.setattr(check, "make_snapshot", lambda repo, dest: dest.mkdir(parents=True) or "0" * 16)
    monkeypatch.setattr(check, "resolve_base", lambda repo, base, release: "b" * 40)
    monkeypatch.setattr(check, "changed_paths", lambda repo, index, base: ["docs/review.md", "scripts/research_log.py"])
    monkeypatch.setattr(check, "RUNNERS", {step: runner(step) for step in check.STEPS})
    monkeypatch.setattr(sys, "argv", ["check.py", "--changed", "--report-dir", str(tmp_path / "r")])
    assert check.main() == 0
    assert calls == list(check.GROUPS["fast"])
    report = (tmp_path / "r/report.md").read_text()
    assert "## Changed files" in report and "Not selected (no change needs them): copies, passes, compare, build, content, gate, sheet." in report
    assert "| scripts/research_log.py | a script neither the build nor the gate uses | validate, lint, export, tests |" in report
    assert "their export scan covers any file" in report
    assert "No changed files: fast checks only." in check.changed_section("b" * 40, check.GROUPS["fast"], [])


@pytest.mark.parametrize("argv, message", [
    (["--changed", "--only", "fast"], "leave out --only"),
    (["--changed", "--gate-merge", "results"], "not --changed"),
    (["--changed", "--gate-jobs", "2", "--gate-merge", "results"], "--gate-merge is for CI's fixed steps"),
    (["--changed", "--release"], "a release runs every step"),
    (["--plan"], "add --changed"),
    (["--release", "--release-wording", "--only", "validate"], "leave out --release-wording"),
])
def test_changed_refuses_options_that_fix_the_steps(monkeypatch, capsys, argv, message):
    monkeypatch.setattr(sys, "argv", ["check.py", *argv])
    with pytest.raises(SystemExit) as caught:
        check.main()
    assert caught.value.code == 2 and message in capsys.readouterr().err


@pytest.mark.parametrize("paths, printed", [
    (["docs/review.md"], "validate,lint,export,tests"),  # CI's plan: checks only, no build
    (["docs/review.md", ".github/workflows/pages.yml"], ",".join(check.BUILD_STEPS)),  # a workflow change: everything
    (["data/aisi.json"], ",".join(check.DATA_STEPS)),
])
def test_plan_prints_the_chosen_steps_and_runs_none(tmp_path, monkeypatch, capsys, paths, printed):
    monkeypatch.setattr(check, "make_snapshot", lambda repo, dest: dest.mkdir(parents=True) or "0" * 16)
    monkeypatch.setattr(check, "resolve_base", lambda repo, base, release: "b" * 40)
    monkeypatch.setattr(check, "changed_paths", lambda repo, index, base: paths)
    monkeypatch.setattr(check, "RUNNERS", {step: lambda ctx: pytest.fail("--plan ran a step") for step in check.STEPS})
    monkeypatch.setattr(sys, "argv", ["check.py", "--changed", "--plan", "--base", "origin/main"])
    assert check.main() == 0
    out, err = capsys.readouterr()
    assert out == printed + "\n" and "## Changed files" in err


def test_browser_job_section_sums_the_steps_of_jobs_that_mark_them():
    lines = [{**timing("interactions/interaction_pass/1440/light", 0, 10), "steps": {"load": 2.0, "first screen": 6.0}},
             {**timing("interactions/interaction_pass/390/dark", 0, 8), "steps": {"load": 3.0, "first screen": 1.0}},
             timing("hydration/hydration_paint", 0, 5)]
    section = check.browser_job_section(lines)
    assert "Steps of the 2 jobs that mark them, summed:" in section
    rows = [line for line in section.splitlines() if line.startswith("| first screen") or line.startswith("| load")]
    assert rows == ["| first screen | 7.0 | interactions/interaction_pass/1440/light (6.0) |", "| load | 5.0 | interactions/interaction_pass/390/dark (3.0) |"]
    assert "Steps of" not in check.browser_job_section([timing("a", 0, 1)])



def test_time_budgets_warn_on_the_mac_and_only_report_elsewhere():
    results = [check.Result("validate", "PASS", 5.0), check.Result("lint", "PASS", 1.0), check.Result("export", "PASS", 2.0),
               check.Result("tests", "PASS", 40.0), check.Result("gate", "PASS", 80.0), check.Result("sheet", "NOT RUN", 0.0)]
    mac = check.budget_section(results, "darwin")
    assert "| fast checks | 48.0 | 30 | over budget |" in mac and "| browser gate | 80.0 | 90 | within budget |" in mac
    assert "sheet" not in mac.split("| Steps |")[1]  # a step that did not run has no row
    other = check.budget_section(results, "linux")
    assert "not compared on this host" in other and "over budget" not in other
    assert check.budget_section([check.Result("compare", "PASS", 1.0)], "darwin") == ""
    partial = check.budget_section([check.Result("lint", "PASS", 1.0)], "darwin")
    assert "| fast checks | 1.0 | 30 | partial (lint only): not compared |" in partial
