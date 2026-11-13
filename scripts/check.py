# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""One check command for agents, the pre-commit hook and CI.

  uv run --locked scripts/check.py                       # every step on the staged snapshot, compared with HEAD
  uv run --locked scripts/check.py --base origin/main    # compared with origin/main, when handing back a branch
  uv run --locked scripts/check.py --release             # a release, compared with the last v* tag
  uv run --locked scripts/check.py --only fast           # validate,lint,export,tests: the pre-commit hook
  uv run --locked scripts/check.py --only export --commits A..B   # names and secrets in commits being pushed
  uv run --locked scripts/check.py --preflight           # can this agent run every step?
  uv run --locked scripts/check.py --fail-fast           # stop at the first blocking result
  uv run --locked scripts/check.py --changed --base origin/main   # only the steps the changed files need
  uv run --locked scripts/check.py --skip-browser --base origin/main   # starts no Chromium: CI builds and gates the
                                                                       # pushed commit; the sheet waits for a visual pass

The staged index (not the working tree) is exported to a temporary directory and every step
runs there with the locked dependencies, so results describe exactly what would be committed
and nothing is written into the checkout. The report (Markdown, logs, screenshots) goes to
--report-dir (which must be empty) or a new temporary directory. A step that cannot run is
NOT RUN, which fails unless --allow-skip is given (refused in CI). This coordinator uses the
standard library only, so starting it never syncs the checkout's environment; the pre-commit
hook runs the staged copy of this file. With --commits the snapshot comes from HEAD instead, and the
export step checks the selected commits in the original repository, including full commit messages.
"""

import argparse
import datetime as dt
import hashlib
import io
import json
import os
import shutil
import re
import shlex
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urljoin, urlsplit
from zoneinfo import ZoneInfo

sys.dont_write_bytecode = True


def toplevel() -> Path:
    """The repository in the current directory; the hook runs this file from stdin."""
    found = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True)
    return Path(found.stdout.strip()) if found.returncode == 0 else Path(__file__).resolve().parents[1]


ROOT = toplevel()
# Cheapest first, so --fail-fast reaches a blocking result soon: export and compare read the snapshot and its evidence, not the build.
STEPS = ("validate", "lint", "export", "tests", "copies", "passes", "compare", "build", "content", "gate", "sheet")
# The pre-commit hook runs "fast"; CI runs "fast,build,content,gate". One definition for both.
# export is fast (about 2 s): a private name is refused at commit, not only at the full check.
GROUPS = {"fast": ("validate", "lint", "export", "tests")}
# --skip-browser: until merges can be blocked on CI, CI's build and four gate shards on the pushed commit are the
# authority for every step that starts Chromium or needs its build; a local run checks the rest,
# so a shared host runs a done-check without taking its one browser.
BROWSER_ELSEWHERE = {
    "build": "left to CI's build of the pushed commit (--skip-browser: the build pre-renders in Chromium); read that run's result",
    "content": "left to CI's content checks on the pushed commit (--skip-browser: content needs the pre-rendered build); read that run's result",
    "gate": "left to CI's gate shards on the pushed commit (--skip-browser); read that run's result",
    "sheet": "not taken (--skip-browser starts no Chromium); a visual pass runs check.py without it",
}
# Local fast checks without --commits may lack the private terms file. Push checks and CI require it,
# except for a fork pull request's names scan; gitleaks must still run there.
OPTIONAL_IN_FAST = ("export",)
NOT_RUN = 3  # check_page.py's exit code when Chrome is missing
EXCERPT_CHARS = 240
MAX_ROWS = 300
SERVE_URL = "http://127.0.0.1:8767/"
# This coordinator and snapshot_sources.py both read AISI_SNAPSHOTS.
_snapshot_directory = os.environ.get("AISI_SNAPSHOTS")
SNAPSHOTS = Path(_snapshot_directory).expanduser() if _snapshot_directory else None
# The run date every step uses (release_checks.today reads it; same name and zone there): one date per run.
RUN_DATE_ENV, RUN_ZONE = "AISI_RUN_DATE", "Europe/Vienna"


@dataclass
class Result:
    step: str
    status: str  # PASS, FAIL or NOT RUN
    seconds: float = 0.0
    detail: str = ""
    log: Path | None = None
    peak_mb: float | None = None  # sampled peak aggregate RSS while the step ran (see PeakSampler)
    ended: float = 0.0  # seconds from the start of the run to the end of this step
    blocks: bool = True  # False only for a NOT RUN that this run may skip (OPTIONAL_IN_FAST, browser steps left to CI, a sheet without rendering impact)
    missing_terms_only: bool = False  # the names scan is unconfigured, but generic checks and gitleaks passed


@dataclass
class Context:
    repo: Path
    snapshot: Path
    work: Path
    report: Path
    base: str | None
    release: bool
    dist: Path
    cache: Path
    env: dict = field(default_factory=dict)
    built: bool = False  # a passing build step in this run, or an explicit --gate-dist
    sections: dict[str, str] = field(default_factory=dict)  # report sections by step
    serve_url: str = SERVE_URL  # where the owner serves the staged build for the Where to look links
    snapshots: Path | None = None  # private source snapshots that seed quote verification
    gate_args: list[str] = field(default_factory=list)  # CI: shard or merge options for check_page.py
    test_workers: int = 4
    commits: list[str] = field(default_factory=list)
    ref_names: list[str] = field(default_factory=list)  # with commits: the refs being pushed


# Which steps a change needs (--changed): the first rule whose path or directory prefix matches a changed path wins,
# and a path no rule names runs every step. Deliberately coarse: page, data, build and gate changes run every browser
# job, because nearly every job reads them; narrower selection waits for measured gains.
DATA_STEPS = (*GROUPS["fast"], "copies", "passes", "compare", "build", "content", "gate")
BUILD_STEPS = (*GROUPS["fast"], "compare", "build", "content", "gate")
# Scripts that neither the build nor the gate imports, runs or reads (tests/test_check.py keeps this true).
NO_BROWSER_SCRIPTS = ("archive_sources", "make_roster", "migrate_credit", "migrate_facts", "monitor_sources", "protocol_checks",
                      "release_tag", "research_backfill", "research_log", "rule_tables", "slide_map",
                      "snapshot_sources", "social_preview", "state_languages", "zenodo_metadata")
CHANGE_RULES = (
    (("data/aisi.json",), DATA_STEPS, "authored data: its evidence, then every content check"),
    (("tests/content/",), BUILD_STEPS, "content checks need the pre-rendered build"),
    (("tests/fixtures/",), BUILD_STEPS, "test fixtures, which the gate also reads"),
    (("docs/", "tests/", ".github/", ".githooks/", "README.md", "AGENTS.md", "CLAUDE.md", "LICENSE", "LICENSE-DATA",
      "CITATION.cff", ".zenodo.json", ".gitignore"), GROUPS["fast"], "documentation, tests or tooling the build does not read"),
    (("scripts/verify_excerpts.py",), (*GROUPS["fast"], "compare"), "quote verification, which compare runs"),
    (("scripts/research_pass.py",), (*GROUPS["fast"], "passes"), "research-pass checks, which passes runs"),
    (("scripts/dependency_checks.py",), GROUPS["fast"], "dependency rules, which lint runs"),
    (tuple(f"scripts/{name}.py" for name in NO_BROWSER_SCRIPTS), GROUPS["fast"], "a script neither the build nor the gate uses"),
    (("site/", "schema/", "scripts/", "pyproject.toml", "uv.lock"), BUILD_STEPS, "page, schema, build or gate code"),
)


def matches(path: str, prefix: str) -> bool:
    return path.startswith(prefix) if prefix.endswith("/") else path == prefix


def changed_steps(paths: list[str]) -> tuple[tuple[str, ...], list[tuple[str, str, tuple[str, ...]]]]:
    """The steps the changed paths need, in STEPS order, and per path the reason and its steps. No change: fast only.
    Every rule includes the fast group, whose export scan covers any file (any file can carry an internal name)."""
    rows = []
    for path in paths:
        rule = next(((steps, why) for prefixes, steps, why in CHANGE_RULES if any(matches(path, p) for p in prefixes)), None)
        rows.append((path, *(rule[::-1] if rule else ("not in the change map: every step", STEPS))))
    wanted = {step for _, _, steps in rows for step in steps} or set(GROUPS["fast"])
    return tuple(step for step in STEPS if step in wanted), rows


def changed_paths(repo: Path, index: Path, base: str) -> list[str]:
    """Paths that differ between base and the frozen snapshot index; a rename counts as both its paths."""
    env = {**os.environ, "GIT_INDEX_FILE": str(index)}
    out = subprocess.run(["git", "-C", str(repo), "diff", "--cached", "--name-only", "--no-renames", "-z", base],
                         capture_output=True, check=True, env=env).stdout
    return sorted({p for p in out.decode("utf-8", "surrogateescape").split("\0") if p})


def changed_section(base: str, steps: tuple[str, ...], rows: list) -> str:
    lines = ["## Changed files", "", f"Steps chosen by --changed from the changes against {base[:12]}: {', '.join(steps)}. "
             f"Not selected (no change needs them): {', '.join(s for s in STEPS if s not in steps) or 'none'}. "
             "The fast checks run for every change; their export scan covers any file.", ""]
    if not rows:
        return "\n".join(lines + ["No changed files: fast checks only.", ""])
    lines += ["| Path | Why | Steps |", "| --- | --- | --- |"]
    shown = rows[:MAX_ROWS]
    lines += [f"| {path} | {why} | {', '.join(s)} |" for path, why, s in shown]
    if len(rows) > len(shown):
        lines.append(f"| ... | {len(rows) - len(shown)} more paths | |")
    return "\n".join(lines + [""])


UV_KEPT = ("UV_CACHE_DIR", "UV_PYTHON_INSTALL_DIR", "UV_PYTHON_DOWNLOADS", "UV_INDEX", "UV_DEFAULT_INDEX",
           "UV_EXTRA_INDEX_URL", "UV_NATIVE_TLS", "UV_OFFLINE", "UV_HTTP_TIMEOUT", "UV_EXCLUDE_NEWER")


def clean_env() -> dict:
    """Children run in the snapshot: no hook index, no outer virtualenv, no bytecode files."""
    # Any other uv setting could redirect the project, environment or sync and bypass the lock,
    # so only cache, interpreter and index settings and the resolution cutoff (which uv.lock records) pass through.
    env = {
        k: v for k, v in os.environ.items()
        if not k.startswith("GIT_") and k != "VIRTUAL_ENV" and (not k.startswith("UV_") or k.startswith(UV_KEPT))
    }
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def git(repo: Path, *args: str, check: bool = True) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=check).stdout


def resolve_base(repo: Path, base: str | None, release: bool) -> str:
    """The commit the staged snapshot is compared with. Without --base: HEAD, or for a release the latest v* tag
    reachable from HEAD (the last release). A local branch that differs from its upstream is refused: it is
    usually stale, and compare would report changes that are not in the release, so the caller names the ref they mean."""
    if base is None and release:
        tag = git(repo, "describe", "--tags", "--abbrev=0", "--match", "v[0-9]*", "HEAD", check=False).strip()
        if not tag:
            raise ValueError("--release without --base compares with the last v* release tag, and HEAD has none; pass --base")
        base = f"refs/tags/{tag}"  # qualified: a branch of the same name must not stand in
    if base is None:
        base = "HEAD"
    full = ""  # the ref git resolves the name to; HEAD (the checked-out work itself) is never a stale branch
    if base not in ("HEAD", "@"):
        found = subprocess.run(["git", "-C", str(repo), "-c", "core.warnAmbiguousRefs=true", "rev-parse", "--verify", "--quiet", "--symbolic-full-name", base],
                               capture_output=True, text=True)
        if "ambiguous" in found.stderr:
            raise ValueError(f"--base {base} is ambiguous (a local branch and another ref share the name); "
                             f"name it fully, for example refs/remotes/{base} or refs/tags/{base}")
        full = found.stdout.strip() if found.returncode == 0 else ""
    if full.startswith("refs/heads/"):
        upstream = git(repo, "for-each-ref", "--format=%(upstream:short)", full, check=False).strip()
        here = git(repo, "rev-parse", full).strip()
        there = git(repo, "rev-parse", "--verify", "--quiet", f"{upstream}^{{commit}}", check=False).strip() if upstream else ""
        if there and there != here:
            raise ValueError(f"--base {base} is the local branch at {here[:12]}, but its upstream {upstream} is at {there[:12]}; "
                             f"name the ref you mean, for example --base {upstream} (after git fetch), or a tag or commit")
    resolved = git(repo, "rev-parse", "--verify", "--quiet", f"{base}^{{commit}}", check=False).strip()
    if not resolved:
        raise ValueError(f"--base {base} is not a commit in this repository")
    return resolved


def run(ctx: Context, step: str, cmd: list[str], cwd: Path | None = None) -> tuple[int, float, Path]:
    log = ctx.report / "logs" / f"{step}.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    start = time.monotonic()
    with log.open("a", encoding="utf-8") as out:
        out.write("$ " + " ".join(cmd) + "\n")
        out.flush()
        code = subprocess.run(cmd, cwd=cwd or ctx.snapshot, env=ctx.env, stdout=out, stderr=subprocess.STDOUT).returncode
    return code, time.monotonic() - start, log


SAMPLE_SECONDS = 0.5
PEAK_METHOD = (f"sampled peak aggregate RSS of this check and its current descendants, read with ps every {SAMPLE_SECONDS} s: "
               "memory shared between processes counts in each of them, a process that detaches from the tree drops out, "
               "and peaks shorter than the interval can be missed")


def tree_rss_kb(root: int, listing: str) -> int:
    """Summed RSS (KB) of root and its descendants in `ps -A -o pid=,ppid=,rss=` output."""
    children, rss = {}, {}
    for line in listing.splitlines():
        fields = line.split()
        if len(fields) == 3 and all(f.isdigit() for f in fields):
            pid, ppid, kb = map(int, fields)
            children.setdefault(ppid, []).append(pid)
            rss[pid] = kb
    total, stack, seen = 0, [root], set()
    while stack:
        pid = stack.pop()
        if pid not in seen:
            seen.add(pid)
            total += rss.get(pid, 0)
            stack += children.get(pid, [])
    return total


class PeakSampler:
    """Highest summed RSS of a process tree, sampled with ps (Linux and macOS both report RSS in KB) from start() to
    stop(). peak_mb is None when the sampler could not start or ps could not be read; the step runs either way."""

    def __init__(self, root: int, interval: float = SAMPLE_SECONDS):
        self.root, self.interval, self.peak_kb = root, interval, None
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)

    def _sample(self) -> None:
        try:
            listing = subprocess.run(["ps", "-A", "-o", "pid=,ppid=,rss="], capture_output=True, text=True, timeout=2).stdout
        except (OSError, subprocess.SubprocessError):
            return
        kb = tree_rss_kb(self.root, listing)
        if kb:
            self.peak_kb = max(self.peak_kb or 0, kb)

    def _loop(self) -> None:
        while True:
            self._sample()
            if self._stop.wait(self.interval):
                return

    def start(self) -> "PeakSampler":
        try:
            self._thread.start()
        except RuntimeError:  # no thread to spare: no measurement, but the step still runs
            pass
        return self

    def stop(self) -> None:
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join()

    def __enter__(self) -> "PeakSampler":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()

    @property
    def peak_mb(self) -> float | None:
        return None if self.peak_kb is None else self.peak_kb / 1024


def first_blocking(results: list[Result], allow_skip: bool) -> Result | None:
    """The blocking result that ended first: how long an agent waits before it has something to fix."""
    return min(blocking(results, allow_skip), key=lambda r: r.ended, default=None)


def timing_line(results: list[Result], allow_skip: bool, total: float) -> str:
    first = first_blocking(results, allow_skip)
    found = f"first blocking result: {first.step} {first.status} after {first.ended:.1f} s" if first else "no blocking result"
    return f"Timing: {total:.1f} s in all; {found}."


# Time budgets (test strategy D9): warnings only until a baseline is stable, then a hard budget on the fast steps,
# which every commit waits on. The figures are the plan's, measured on the maintainer's Mac; on any other host the
# section says so and only reports the times.
TIME_BUDGETS = (("fast checks", GROUPS["fast"], 30.0), ("content", ("content",), 10.0), ("browser gate", ("gate",), 90.0), ("sheet", ("sheet",), 60.0))
BUDGET_HOST = "darwin"


def budget_section(results: list[Result], platform: str = sys.platform) -> str:
    """The run's time against each budget, for the steps that ran; never blocking."""
    ran = {r.step: r.seconds for r in results if r.status in ("PASS", "FAIL")}
    rows = []
    for name, steps, budget in TIME_BUDGETS:
        if not set(steps) & set(ran):
            continue
        took = sum(ran.get(step, 0.0) for step in steps)
        if not set(steps) <= set(ran):  # a budget covers its steps together: part of them is no measurement
            verdict = f"partial ({', '.join(s for s in steps if s in ran)} only): not compared"
        else:
            verdict = ("over budget" if took > budget else "within budget") if platform == BUDGET_HOST else "not compared on this host"
        rows.append(f"| {name} | {took:.1f} | {budget:.0f} | {verdict} |")
    if not rows:
        return ""
    note = ("Warnings only (test strategy D9); a step over its budget is a reason to look, not a failure."
            if platform == BUDGET_HOST else "The budgets are the Mac's figures; on this host the times are reported, not compared.")
    return "\n".join(["## Time budgets", "", note, "", "| Steps | Seconds | Budget | Result |", "| --- | --- | --- | --- |", *rows, ""])


def tail(log: Path, lines: int = 12) -> str:
    return "\n".join(log.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:])


def command_step(ctx: Context, step: str, cmd: list[str]) -> Result:
    code, seconds, log = run(ctx, step, cmd)
    if code == 0:
        return Result(step, "PASS", seconds, log=log)
    return Result(step, "FAIL", seconds, tail(log), log)


def throwaway_index(tree: Path) -> None:
    """Some checks list tracked files with git, so an exported tree gets its own throwaway index."""
    clean = clean_env()
    subprocess.run(["git", "init", "-q", str(tree)], check=True, env=clean)
    subprocess.run(["git", "-C", str(tree), "add", "-A"], check=True, env=clean)


def make_snapshot(repo: Path, dest: Path, revision: str | None = None) -> str:
    """Export a frozen index (or revision); the id hashes every path, mode and blob in it."""
    live = Path(os.environ.get("GIT_INDEX_FILE") or git(repo, "rev-parse", "--git-path", "index").strip())
    if not live.is_absolute():
        live = repo / live
    frozen = dest.parent / f"{dest.name}.index"
    dest.parent.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "GIT_INDEX_FILE": str(frozen)}
    if revision:
        subprocess.run(["git", "-C", str(repo), "read-tree", revision], check=True, env=env)
    else:
        shutil.copyfile(live, frozen)
    subprocess.run(["git", "-C", str(repo), "checkout-index", "-a", f"--prefix={dest}/"], check=True, env=env)
    listing = subprocess.run(["git", "-C", str(repo), "ls-files", "-s", "-z"], capture_output=True, check=True, env=env).stdout
    throwaway_index(dest)
    return hashlib.sha256(listing).hexdigest()[:16]


# Steps -------------------------------------------------------------------------------------------


def step_validate(ctx: Context) -> Result:
    cmd = ["uv", "run", "--locked", "-q", "scripts/build.py", "--check", "--require-roster",
           "--report-dir", str(ctx.report / "source-origins")]
    result = command_step(ctx, "validate", cmd + (["--release"] if ctx.release else []))
    # build.py writes its release warnings beside the source-origin report; they never fail the step.
    warnings = ctx.report / "source-origins" / "release-warnings.md"
    if result.status == "PASS" and warnings.is_file():
        ctx.sections["warnings"] = warnings.read_text(encoding="utf-8")
        count = sum(line.startswith("- ") for line in ctx.sections["warnings"].splitlines())
        result.detail = f"{count} release warning{'' if count == 1 else 's'} (see Release warnings)" if count else ""
    return result


def step_lint(ctx: Context) -> Result:
    """Ruff and the dependency rules of docs/dependencies.md (standard library only, so it runs with this interpreter),
    both always, so one run reports every finding."""
    ruff = command_step(ctx, "lint", ["uv", "run", "--locked", "-q", "ruff", "check", "--no-cache", "."])
    deps = command_step(ctx, "lint-deps", [sys.executable, "scripts/dependency_checks.py", "."])
    failed = [r for r in (ruff, deps) if r.status != "PASS"]
    detail = "\n".join(r.detail for r in failed)
    return Result("lint", "FAIL" if failed else "PASS", ruff.seconds + deps.seconds, detail, failed[0].log if failed else deps.log)


def step_tests(ctx: Context) -> Result:
    # Four pytest-xdist workers by default: 50 s to 18 s on an M3 Max, where more are not faster (session fixtures
    # dominate); CI's two-core runner is fastest with two (--test-workers 2).
    return command_step(ctx, "tests", ["uv", "run", "--locked", "-q", "pytest", "-q", "-p", "no:cacheprovider", "-n", str(ctx.test_workers)])


def snapshot_location(store: Path | None) -> str:
    return (f"not found at {store} (set AISI_SNAPSHOTS or pass --snapshots DIR)" if store is not None
            else "not configured (set AISI_SNAPSHOTS or pass --snapshots DIR)")


def step_copies(ctx: Context) -> Result:
    """Every recorded snapshot hash names a file held in the private store, checksum-verified.

    Mirrors snapshot_sources.stored_copy; repeated here because this coordinator is stdlib-only.
    """
    start = time.monotonic()
    store = ctx.snapshots
    if store is None or not store.is_dir():
        return Result("copies", "NOT RUN", 0.0, f"private store {snapshot_location(store)}; "
                      "a maintainer with the private store must run this step")
    data = json.loads((ctx.snapshot / "data/aisi.json").read_text(encoding="utf-8"))
    wanted = {s["id"]: s["snapshot"]["sha256"] for s in data["sources"] if s.get("snapshot")}
    missing = []
    for sid, digest in sorted(wanted.items()):
        manifest = store / sid / "manifest.json"
        rows = json.loads(manifest.read_text(encoding="utf-8")) if manifest.is_file() else []
        held = False
        for row in rows:
            path = (store / row["path"]).resolve()
            if (row.get("sha256") == digest and path.is_relative_to(store.resolve()) and path.is_file()
                    and hashlib.sha256(path.read_bytes()).hexdigest() == digest):
                held = True
                break
        if not held:
            missing.append(sid)
    seconds = time.monotonic() - start
    if missing:
        return Result("copies", "FAIL", seconds, f"{len(missing)} recorded snapshots have no matching file in {store} "
                      f"(save source copies there, see AGENTS.md): {', '.join(missing)}")
    return Result("copies", "PASS", seconds, f"{len(wanted)} recorded snapshots held in {store}")


def pass_store(snapshots: Path | None) -> Path | None:
    """Research passes: AISI_PASSES, else passes/ beside the source store (as research_pass.py resolves it)."""
    if os.environ.get("AISI_PASSES"):
        return Path(os.environ["AISI_PASSES"]).expanduser()
    return snapshots.parent / "passes" if snapshots else None


def step_passes(ctx: Context) -> Result:
    """Every data commit since the base names a closed, valid research pass, or none with a reason (docs/research-passes.md)."""
    store = pass_store(ctx.snapshots)
    cmd = ["uv", "run", "--locked", "-q", "scripts/research_pass.py"] + (["--store", str(store)] if store else []) + [
        "commits", "--base", ctx.base or "HEAD", "--repo", str(ctx.repo), "--data", "data/aisi.json"]
    code, seconds, log = run(ctx, "passes", cmd)
    detail = tail(log)
    if code == 3:
        return Result("passes", "NOT RUN", seconds, f"{detail}; a maintainer with the private store must run this step", log)
    return Result("passes", "PASS" if code == 0 else "FAIL", seconds, detail, log)


def step_export(ctx: Context) -> Result:
    """Names and secrets in the exported tree, or in every selected commit and its message."""
    if ctx.commits:
        scan_args = ["--repo", str(ctx.repo), *(["--ref-names", *ctx.ref_names] if ctx.ref_names else []), "--commits", *ctx.commits]
    else:
        export = ctx.work / "export"
        export.mkdir()
        tree = subprocess.run(["git", "-C", str(ctx.snapshot), "write-tree"], capture_output=True, text=True, check=True, env=ctx.env).stdout.strip()
        archive = subprocess.run(["git", "-C", str(ctx.snapshot), "archive", "--format=tar", tree], capture_output=True, check=True, env=ctx.env).stdout
        with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
            tar.extractall(export, filter="data")
        scan_args = [str(export)]
    store = ["--store", str(ctx.snapshots)] if ctx.snapshots else []
    code, seconds, log = run(ctx, "export", [sys.executable, "scripts/export_scan.py", *store, *scan_args])
    status = "PASS" if code == 0 else "NOT RUN" if code == 3 else "FAIL"
    details = [f"names scan {status}: {tail(log)}"]
    if not shutil.which("gitleaks", path=ctx.env.get("PATH")):
        details.append("gitleaks NOT RUN: not on PATH")
        with log.open("a", encoding="utf-8") as out:
            out.write("\n" + details[-1] + "\n")
        return Result("export", "FAIL" if status == "FAIL" else "NOT RUN", seconds, "\n".join(details), log)
    flags = ["--redact", "--no-banner", "--no-color", "--config", str(ctx.snapshot / ".gitleaks.toml")]
    if ctx.commits:
        messages = ctx.work / "commit-messages"
        messages.mkdir()
        messages.joinpath("messages.txt").write_text(git(ctx.repo, "log", "--format=%B", *ctx.commits, "--"), encoding="utf-8")
        # Git omits merge patches by default; include each parent to catch secrets added by a merge resolution.
        # gitleaks splits --log-opts on spaces without shell parsing, so quoting would reach git literally;
        # revisions cannot contain spaces.
        scans = [("gitleaks commits", ["gitleaks", "git", *flags, f"--log-opts={' '.join(['-m', *ctx.commits])}", str(ctx.repo)], ctx.repo),
                 ("gitleaks messages", ["gitleaks", "dir", *flags, "."], messages)]
    else:
        # Relative paths from inside the export preserve the data/aisi.json allowlist.
        scans = [("gitleaks tree", ["gitleaks", "dir", *flags, "."], export)]
    for label, cmd, cwd in scans:
        leak_code, elapsed, leak_log = run(ctx, label.replace(" ", "-"), cmd, cwd=cwd)
        seconds += elapsed
        # gitleaks exits 0 after some errors (an unknown revision scans 0 commits): any ERR line fails the step.
        if leak_code == 0 and re.search(r"^\S+ ERR ", leak_log.read_text(encoding="utf-8", errors="replace"), re.M):
            leak_code = 1
        details.append(f"{label} {'PASS' if leak_code == 0 else 'FAIL'}: {tail(leak_log)}")
        if leak_code:
            status = "FAIL"
    if status == "NOT RUN":
        details.append("private terms unavailable; a maintainer with the private store must run the names scan")
    # Keep all component diagnostics in the report's linked log, including an unavailable scanner.
    detail = "\n".join(details)
    with log.open("a", encoding="utf-8") as out:
        out.write("\n" + detail + "\n")
    return Result("export", status, seconds, detail, log, missing_terms_only=status == "NOT RUN")


def step_build(ctx: Context) -> Result:
    result = command_step(ctx, "build", ["uv", "run", "--locked", "-q", "scripts/build.py", "--out", str(ctx.dist)])
    ctx.built = result.status == "PASS"
    return result


def step_content(ctx: Context) -> Result:
    if not ctx.built:
        return Result("content", "NOT RUN", detail="no successful build of this snapshot in this run (or --gate-dist)")
    return command_step(ctx, "content", ["uv", "run", "--locked", "-q", "pytest", "-q", "-p", "no:cacheprovider",
                                         "-n", str(ctx.test_workers), "tests/content", "--site", str(ctx.dist)])


def gate_result(step: str, code: int, seconds: float, log: Path) -> Result:
    if code == 0:
        return Result(step, "PASS", seconds, log=log)
    if code == NOT_RUN:  # check_page.py names the cause and its fix on a NOT RUN: line (a Chromium build, too many jobs)
        said = [line.removeprefix("NOT RUN: ") for line in log.read_text(encoding="utf-8", errors="replace").splitlines()
                if line.startswith("NOT RUN: ")] if log.is_file() else []
        return Result(step, "NOT RUN", seconds, said[-1] if said else "Playwright's Chromium is not installed (uv run --locked scripts/check_page.py --install-chrome)", log)
    return Result(step, "FAIL", seconds, tail(log), log)


SLOWEST_JOBS = 10


def browser_job_section(timings: list[dict]) -> str:
    """The gate's browser jobs by time: the first with a finding (when an agent could have started on it) and the
    slowest. Each started value counts from its own gate or shard's start; shards on different machines are not compared."""
    lines = ["## Browser jobs", ""]
    if not timings:
        return "\n".join(lines + ["No browser job timings (the static checks failed first, or the gate did not run its browser jobs).", ""])
    where = lambda t: f" (shard {t['shard']})" if t.get("shard") else ""  # noqa: E731
    lines.append(f"{len(timings)} jobs timed, {sum(t['seconds'] for t in timings):.1f} job-seconds in all.")
    # One first finding per clock: each shard counts from its own start, so shards are never compared with each other.
    firsts = {}
    for t in timings:
        if (t["exception"] or t["errors"]) and (t.get("shard") not in firsts or t["started"] + t["seconds"] < firsts[t.get("shard")]["started"] + firsts[t.get("shard")]["seconds"]):
            firsts[t.get("shard")] = t
    for shard in sorted(firsts, key=str):
        first = firsts[shard]
        lines.append(f"First job with a finding{where(first)}: {first['job']}, ended {first['started'] + first['seconds']:.1f} s after "
                     f"{'its shard' if shard else 'the browser jobs'} started.")
    if not firsts:
        lines.append("No job reported a finding.")
    lines += ["", f"| Slowest {min(SLOWEST_JOBS, len(timings))} | Seconds | Result |", "| --- | --- | --- |"]
    for t in sorted(timings, key=lambda t: -t["seconds"])[:SLOWEST_JOBS]:
        result = t["exception"] or (f"{t['errors']} errors" if t["errors"] else "pass")
        lines.append(f"| {t['job']}{where(t)} | {t['seconds']:.1f} | {result} |")
    stepped = [t for t in timings if t.get("steps")]
    if stepped:  # jobs that mark their steps (interaction_pass, once per width and theme)
        totals: dict[str, float] = {}
        slowest: dict[str, tuple[float, str]] = {}
        for t in stepped:
            for name, seconds in t["steps"].items():
                totals[name] = totals.get(name, 0) + seconds
                if seconds > slowest.get(name, (-1, ""))[0]:
                    slowest[name] = (seconds, t["job"])
        lines += ["", f"Steps of the {len(stepped)} jobs that mark them, summed:", "", "| Step | Seconds | Slowest job (seconds) |", "| --- | --- | --- |"]
        for name in sorted(totals, key=lambda n: -totals[n]):
            lines.append(f"| {name} | {totals[name]:.1f} | {slowest[name][1]} ({slowest[name][0]:.1f}) |")
    return "\n".join(lines + [""])


def step_gate(ctx: Context) -> Result:
    if not ctx.built:
        return Result("gate", "NOT RUN", detail="no successful build of this snapshot in this run")
    # A shard's own results carry its timings; the merge (or a local gate) writes them all here.
    timings = None if any(a.startswith("--shard") for a in ctx.gate_args) else ctx.report / "gate-timings.jsonl"
    code, seconds, log = run(ctx, "gate", ["uv", "run", "--locked", "-q", "scripts/check_page.py", "--dist", str(ctx.dist), *ctx.gate_args,
                                           *([f"--timings={timings}"] if timings else [])])
    if timings:
        try:
            lines = [json.loads(line) for line in timings.read_text(encoding="utf-8").splitlines() if line.strip()] if timings.is_file() else []
            ctx.sections["jobs"] = browser_job_section(lines)
        except (OSError, ValueError, KeyError, TypeError) as exc:  # timings are a report aid: never fail the gate over them
            ctx.sections["jobs"] = f"## Browser jobs\n\nTimings unreadable ({type(exc).__name__}: {exc}).\n"
    result = gate_result("gate", code, seconds, log)
    if notes := report_lines(log):
        rest = [f"- and {len(notes) - REPORT_LINES_SHOWN} more in the [gate log]({log})"] if len(notes) > REPORT_LINES_SHOWN else []
        ctx.sections["report lines"] = "\n".join(["## Report lines", "", "Cosmetic drift the gate reports but does not block on (test strategy D1); "
                                                   "the reviewer judges each against the design principles.", "",
                                                   *(f"- {n}" for n in notes[:REPORT_LINES_SHOWN]), *rest, ""])
        if result.status == "PASS":
            result.detail = f"{len(notes)} report line{'' if len(notes) == 1 else 's'} (see Report lines)"
    return result


REPORT_LINES_SHOWN = 40  # one cosmetic slip can repeat in every state and element; the log keeps them all
REPORT_LINE = "report line: "  # check_page.REPORT_LINE: the gate prints each report line on its own line


def report_lines(log: Path) -> list[str]:
    """The gate's report lines in its log, in order, each once."""
    try:
        text = log.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    return list(dict.fromkeys(line[len(REPORT_LINE):].rstrip() for line in text.splitlines() if line.startswith(REPORT_LINE)))


# The pre-render and the locks that pin its browser count too: a new browser can draw the page differently.
RENDERING_PATHS = ("site/", "scripts/build.py", "scripts/published.py", "scripts/display.py", "scripts/references.py", "scripts/row_checks.py",
                   "scripts/prerender.py", "scripts/prerender.py.lock", "scripts/check_page.py.lock")


def extremes(data, pointer: str = "", out: dict | None = None) -> dict:
    """Longest string and longest list per field pattern (array indices replaced by *)."""
    out = {} if out is None else out
    pattern = re.sub(r"/\d+(?=/|$)", "/*", pointer)
    if isinstance(data, dict):
        for key, value in data.items():
            extremes(value, f"{pointer}/{key}", out)
    elif isinstance(data, list):
        out[pattern + "[]"] = max(out.get(pattern + "[]", 0), len(data))
        for index, value in enumerate(data):
            extremes(value, f"{pointer}/{index}", out)
    elif isinstance(data, str):
        out[pattern] = max(out.get(pattern, 0), len(data))
    return out


def rendering_impact(ctx: Context) -> list[str]:
    """Why rendering can differ from the base: page or renderer changes, or longer extreme content."""
    env = {**os.environ, "GIT_INDEX_FILE": str(ctx.work / "snapshot.index")}
    changed = subprocess.run(["git", "-C", str(ctx.repo), "diff", "--cached", "--name-only", ctx.base],
                             capture_output=True, text=True, check=True, env=env).stdout.split()
    reasons = [f"{path} changed" for path in changed if path.startswith(RENDERING_PATHS)]
    if "data/aisi.json" in changed:
        shown = git(ctx.repo, "show", f"{ctx.base}:data/aisi.json", check=False)
        old = extremes(json.loads(shown)) if shown else {}
        new = extremes(json.loads((ctx.snapshot / "data/aisi.json").read_text(encoding="utf-8")))
        grown = sorted(k for k, v in new.items() if v > old.get(k, 0))
        if grown:
            reasons.append(f"longest content grew in {len(grown)} fields, e.g. {', '.join(grown[:3])}")
    return reasons


def build_base(ctx: Context) -> Path | None:
    """Export and build the base revision with its own build script; None if that fails."""
    base_dir = ctx.work / "base"
    base_dir.mkdir()
    archive = subprocess.run(["git", "-C", str(ctx.repo), "archive", "--format=tar", ctx.base], capture_output=True, check=True).stdout
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(base_dir, filter="data")
    throwaway_index(base_dir)
    # No --out: older revisions only build into their own dist/, here inside the temporary export.
    code, _, _ = run(ctx, "sheet", ["uv", "run", "--locked", "-q", "scripts/build.py"], cwd=base_dir)
    return base_dir / "dist" if code == 0 and (base_dir / "dist/index.html").is_file() else None


def capture(ctx: Context, dist: Path, out: Path) -> tuple[int, float, Path]:
    # The staged check_page.py captures both builds, so before and after use the same states.
    return run(ctx, "sheet", ["uv", "run", "--locked", "-q", "scripts/check_page.py", "--dist", str(dist), "--capture", str(out)])


def crop_links(shots: Path, crop: str) -> str:
    """Links to the before and after capture of one crop; a side without a capture says so."""
    sides = []
    for side in ("before", "after"):
        path = shots / side / f"{crop}.png"
        sides.append(f"[{side}]({path})" if path.is_file() else f"{side}: absent")
    return ", ".join(sides)


def where_to_look(ctx: Context, changes: list[dict]) -> str:
    """The owner's list: one line per logical change, with where it shows and a link into the
    locally served staged build (copied into the report so it outlives this run)."""
    site = ctx.report / "site"
    shutil.copytree(ctx.dist, site)
    port = urlsplit(ctx.serve_url).port or 80
    lines = ["## Where to look", "",
             f"Serve the staged build with `python3 -m http.server {port} --bind 127.0.0.1 -d {site}`, then open each link and compare with the live site. Widths are window widths; the page opens light, and the theme toggle at the top right switches to dark.", ""]
    if not changes:
        lines.append("No visible change in the captured states.")
    for change in changes:
        link = urljoin(ctx.serve_url, f"#{change['anchor']}")
        lines.append(f"{change['number']}. {change['label']}: {change['what']}; at {change['states']}. [Open]({link})")
    return "\n".join(lines + [""])


def step_sheet(ctx: Context) -> Result:
    """Capture fixed page states; with rendering impact against the base, lead with before/after pairs."""
    start = time.monotonic()
    reasons, impact_error = [], None
    if ctx.base:
        try:
            reasons = rendering_impact(ctx)
        except Exception as exc:
            impact_error = f"unknown rendering impact ({type(exc).__name__}: {exc}); counted as impact"
            reasons = [impact_error]
        if not reasons and not ctx.release:
            detail = f"No rendering impact against {ctx.base[:12]}: no sheet captured and no visual pass needed."
            ctx.sections["sheet"] = f"## Visual pass\n\n{detail}\n"
            return Result("sheet", "NOT RUN", time.monotonic() - start, detail, blocks=False)

    def with_impact(result: Result) -> Result:
        if impact_error:
            result.detail += f"; {impact_error}"
        return result

    if not ctx.built:
        return with_impact(Result("sheet", "NOT RUN", detail="no successful build of this snapshot in this run"))
    shots, sheets = ctx.report / "screenshots", ctx.report / "sheets"
    code, _, log = capture(ctx, ctx.dist, shots / "after")
    if code != 0:
        return with_impact(gate_result("sheet", code, time.monotonic() - start, log))
    before = None
    if reasons:
        base_dist = build_base(ctx)
        if base_dist is None:
            return Result("sheet", "NOT RUN", time.monotonic() - start, f"rendering can change ({'; '.join(reasons)}) but the base {ctx.base[:12]} did not build", log)
        code, _, log = capture(ctx, base_dist, shots / "before")
        if code != 0:
            return with_impact(gate_result("sheet", code, time.monotonic() - start, log))
        before = shots / "before"
    cmd = ["uv", "run", "--locked", "-q", "scripts/contact_sheet.py", "--after", str(shots / "after"), "--out", str(sheets)]
    code, _, log = run(ctx, "sheet", cmd + (["--before", str(before)] if before else []))
    if code != 0:
        return with_impact(Result("sheet", "FAIL", time.monotonic() - start, tail(log), log))
    index = json.loads((sheets / "index.json").read_text(encoding="utf-8"))
    lines = ["## Visual pass", ""]
    if not reasons:
        lines.append(f"No rendering impact against {ctx.base[:12] if ctx.base else 'the base'}: no change to {' or '.join(RENDERING_PATHS)} and no longer extreme content. Visual pass not required; full sheets below.")
    else:
        changes = index["changes"]
        ctx.sections["where"] = where_to_look(ctx, changes)
        lines += [f"Rendering can change: {'; '.join(reasons)}.", "",
                  f"{len(changes)} change{'' if len(changes) == 1 else 's'} in {index['compared']} crops. Open each Where to look link on the live page at 1440, 1000 and 390 px, light and dark, and judge it against the design principles in site/index.html; the images below are optional.", ""]
        for change in changes:
            lines.append(f"- {change['number']}. [{change['image']}]({sheets / change['image']}): {change['label']} ({change['states']})")
            if change["left_out"]:
                lines.append(f"  - {len(change['left_out'])} smaller regions not in the image; all regions in {sheets / 'index.json'}")
        crops = sorted({r.split(" ")[0] for c in changes for r in c["regions"]})
        if crops:
            lines += ["", "Whole crops, full resolution:", ""]
            lines += [f"- {c}: {crop_links(shots, c)}" for c in crops]
    lines += ["", "### Full sheets (light beside dark)", ""]
    for width, pages in index["sheets"].items():
        lines.append(f"- {width} px: " + ", ".join(f"[{name}]({sheets / name})" for name, _ in pages))
    lines += ["", f"Full-resolution crops: {shots / 'after'}", ""]
    ctx.sections["sheet"] = "\n".join(lines)
    detail = f"{len(index['changes'])} changes" if reasons else "no rendering impact"
    return with_impact(Result("sheet", "PASS", time.monotonic() - start, detail, log))


# Base comparison ---------------------------------------------------------------------------------


REMOVED = " (removed)"


def child(pointer: str, key) -> str:
    return f"{pointer}/{str(key).replace('~', '~0').replace('/', '~1')}"


def diff(old, new, pointer: str = "", base: str | None = None) -> list[tuple[str, object, object]]:
    """Changed leaves; records match permanent IDs or documented natural keys.

    A change or addition is named by its pointer into the staged data; a removal by its pointer
    into the base data plus " (removed)", because that is where its evidence is looked up.
    """
    base = pointer if base is None else base
    if isinstance(old, dict) and isinstance(new, dict):
        changes = []
        for key in list(new) + [k for k in old if k not in new]:
            if key not in old:
                changes.append((child(pointer, key), None, new[key]))
            elif key not in new:
                changes.append((child(base, key) + REMOVED, old[key], None))
            else:
                changes += diff(old[key], new[key], child(pointer, key), child(base, key))
        return changes
    if isinstance(old, list) and isinstance(new, list):
        # Keep the staged coordinator self-contained: the pre-commit hook executes
        # it from stdin, before the snapshot exists, without checkout imports.
        collection = pointer.rsplit("/", 1)[-1]
        identity_key = {"outputs": "lineage", "operating": "kind", "exchange_rates": "currency", "memberships": "body", "modes": "code"}.get(collection, "id")
        keyed = all(isinstance(x, dict) and identity_key in x for x in old + new)
        if keyed and any(len({x[identity_key] for x in rows}) != len(rows) for rows in (old, new)):
            raise ValueError(f"{pointer}: duplicate stable record identity")
        if keyed:
            before = {x[identity_key]: (index, x) for index, x in enumerate(old)}
            changes = []
            for index, item in enumerate(new):
                if item[identity_key] in before:
                    old_index, old_item = before.pop(item[identity_key])
                    changes += diff(old_item, item, child(pointer, index), child(base, old_index))
                else:
                    changes.append((child(pointer, index), None, item))
            return changes + [(child(base, index) + REMOVED, item, None) for index, item in before.values()]
        changes = []
        for index in range(max(len(old), len(new))):
            if index >= len(new):
                changes.append((child(base, index) + REMOVED, old[index], None))
            elif index >= len(old):
                changes.append((child(pointer, index), None, new[index]))
            else:
                changes += diff(old[index], new[index], child(pointer, index), child(base, index))
        return changes
    return [] if old == new else [(pointer, old, new)]


def short(value, limit: int = 160) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    text = " ".join(text.split()).replace("|", "\\|")
    return text if len(text) <= limit else text[: limit - 1] + "…"


def supporting(data: dict, pointer: str) -> list[tuple[str, dict]]:
    pointer = pointer.removesuffix(REMOVED)
    found = []
    for source in data.get("sources", []):
        for excerpt in source.get("excerpts", []):
            if any(s == pointer or s.startswith(pointer + "/") or pointer.startswith(s + "/") for s in excerpt["supports"]):
                found.append((source["id"], excerpt))
    return found


def excerpt_keys(data: dict) -> set[tuple[str, str]]:
    return {(s["id"], e["text"]) for s in data.get("sources", []) for e in s.get("excerpts", [])}


def binding_changes(old: dict, new: dict) -> list[dict]:
    """Report bindings and gloss edits even when quotation text is unchanged."""
    def bindings(data):
        result = {}
        for source in data.get("sources", []):
            for excerpt in source.get("excerpts", []):
                identity = excerpt.get("id") or "sha256:" + hashlib.sha256(excerpt["text"].encode("utf-8")).hexdigest()
                key = (source["id"], identity, excerpt["text"], excerpt.get("url", source.get("url", "")))
                group = result.setdefault(key, {"supports": set(), "glosses": set()})
                group["supports"].update(
                    json.dumps(ref, ensure_ascii=False, sort_keys=True) for ref in excerpt["supports"]
                )
                if "gloss" in excerpt:
                    group["glosses"].add(excerpt["gloss"])
        return result

    before, after = bindings(old), bindings(new)
    rows = []
    for key in sorted(before.keys() | after.keys()):
        previous = before.get(key, {"supports": set(), "glosses": set()})
        current = after.get(key, {"supports": set(), "glosses": set()})
        added = current["supports"] - previous["supports"]
        removed = previous["supports"] - current["supports"]
        before_gloss, after_gloss = sorted(previous["glosses"]), sorted(current["glosses"])
        if added or removed or before_gloss != after_gloss:
            row = {"source_id": key[0], "excerpt_id": key[1], "text": key[2], "url": key[3],
                   "added": [json.loads(ref) for ref in sorted(added)],
                   "removed": [json.loads(ref) for ref in sorted(removed)]}
            if before_gloss != after_gloss:
                row.update(gloss_before=before_gloss, gloss_after=after_gloss)
            rows.append(row)
    return rows


def verify_quotes(ctx: Context, data: dict, changed: list[tuple[str, str]]) -> tuple[str, list[str]]:
    """Offline literal quote check of new excerpt texts against the local source cache."""
    if not changed:
        return "PASS", ["No new or edited excerpt texts."]
    seeds = [*(["--seed-cache", str(ctx.cache)] if ctx.cache.is_dir() else []),
             *(["--seed-snapshots", str(ctx.snapshots)] if ctx.snapshots and ctx.snapshots.is_dir() else [])]
    if not seeds:
        return "NOT RUN", [f"{len(changed)} new or edited excerpt texts; no source cache at {ctx.cache} (pass --cache DIR) "
                           f"and private snapshots {snapshot_location(ctx.snapshots)}."]
    report = ctx.report / "excerpt-verification.json"
    # The real cache and the snapshots are only read: entries are copied into a scratch cache outside
    # the checkout, which absorbs re-extraction. A fresh clone without a cache is seeded from the snapshots.
    cmd = ["uv", "run", "--locked", "-q", "scripts/verify_excerpts.py", "--offline", "--all-excerpts",
           "--cache", str(ctx.work / "source-cache"), *seeds, "--report", str(report)]
    for source_id in sorted({s for s, _ in changed}):
        cmd += ["--source", source_id]
    # Exit 1 only means some quotation needs review; the rows below say which.
    code, _, log = run(ctx, "compare", cmd)
    if code not in (0, 1) or not report.is_file():
        return "FAIL", [f"verify_excerpts.py failed; see {log}", tail(log)]
    wanted = set(changed)
    verified = json.loads(report.read_text(encoding="utf-8"))
    rows = [r for s in verified["sources"] for r in s["excerpts"] if (r["source_id"], r["text"]) in wanted]
    missing = wanted - {(r["source_id"], r["text"]) for r in rows}
    if verified.get("partial") or missing:
        return "FAIL", [f"verify_excerpts.py did not finish or skipped {len(missing)} excerpts; see {log}", tail(log)]
    lines = ["| Source | Excerpt ID | Status | Excerpt |", "| --- | --- | --- | --- |"]
    for row in rows:
        identity = row.get("excerpt_id") or "sha256:" + hashlib.sha256(row["text"].encode("utf-8")).hexdigest()
        lines.append(f"| {row['source_id']} | `{identity}` | {row['status']} | {short(row['text'], EXCERPT_CHARS)} |")
    mismatched = [r for r in rows if r["status"] == "mismatch"]
    unverified = [r for r in rows if not r["status"].startswith("matched") and r["status"] != "mismatch"]
    lines.append("")
    lines.append(f"{len(rows) - len(mismatched) - len(unverified)} matched, {len(mismatched)} not found in the cached document (fails), {len(unverified)} unverified (no usable cached copy; NOT RUN).")
    return ("FAIL" if mismatched else "NOT RUN" if unverified else "PASS"), lines


def resolved_report_data(ctx: Context, data: dict) -> dict:
    """Use the staged resolver once per document, never the live checkout's code."""
    script = "import json,sys; sys.path.insert(0,'scripts'); from references import materialise_references; print(json.dumps(materialise_references(json.load(sys.stdin))))"
    result = subprocess.run([sys.executable, "-c", script], cwd=ctx.snapshot, env=ctx.env,
                            input=json.dumps(data), text=True, capture_output=True)
    if result.returncode:
        raise ValueError(f"staged reference resolver failed: {result.stderr.strip()}")
    return json.loads(result.stdout)


def step_compare(ctx: Context) -> Result:
    start = time.monotonic()
    new = json.loads((ctx.snapshot / "data/aisi.json").read_text(encoding="utf-8"))
    shown = git(ctx.repo, "show", f"{ctx.base}:data/aisi.json", check=False)
    old = json.loads(shown) if shown else {}
    changes = diff({k: v for k, v in old.items() if k != "sources"}, {k: v for k, v in new.items() if k != "sources"})
    lines = [f"## Changes against {ctx.base}", "", f"### Changed claims ({len(changes)})", ""]
    if changes:
        old_resolved = resolved_report_data(ctx, old)
        new_resolved = resolved_report_data(ctx, new)
        lines += ["Pointers refer to the staged data; excerpts are those whose `supports` pointer is the claim, inside it or above it.", "",
                  "| Pointer | Before | After | Supporting excerpts |", "| --- | --- | --- | --- |"]
        for pointer, before, after in changes[:MAX_ROWS]:
            excerpts = supporting(old_resolved if pointer.endswith(REMOVED) else new_resolved, pointer)
            cited = "<br>".join(f"{sid}: \"{short(e['text'], EXCERPT_CHARS)}\"" for sid, e in excerpts[:4])
            if len(excerpts) > 4:
                cited += f"<br>and {len(excerpts) - 4} more"
            lines.append(f"| `{pointer}` | {short(before) if before is not None else ''} | {short(after) if after is not None else ''} | {cited or 'none'} |")
        if len(changes) > MAX_ROWS:
            lines.append(f"\n{len(changes) - MAX_ROWS} more changes are in changes.json.")
        (ctx.report / "changes.json").write_text(json.dumps([{"pointer": p, "before": b, "after": a} for p, b, a in changes], ensure_ascii=False, indent=1), encoding="utf-8")
    bindings = binding_changes(old, new)
    lines += ["", f"### Changed evidence bindings and glosses ({len(bindings)})", ""]
    if bindings:
        lines += ["References retain their authored representation (stable selectors or legacy pointers). Binding changes require semantic review; the quote check below rechecks only new or edited text, not unchanged quotations whose bindings changed.", "",
                  "| Source | Excerpt ID | Excerpt | Added references | Removed references | Gloss before / after |", "| --- | --- | --- | --- | --- | --- |"]
        lines += [f"| {row['source_id']} | `{row['excerpt_id']}` | {short(row['text'], EXCERPT_CHARS)} | {short(row['added'])} | {short(row['removed'])} | {short([row['gloss_before'], row['gloss_after']]) if 'gloss_before' in row else ''} |" for row in bindings[:MAX_ROWS]]
        lines.append("\nFull quotations, source URLs and references are in evidence-bindings.json.")
        (ctx.report / "evidence-bindings.json").write_text(json.dumps(bindings, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    changed_excerpts = sorted(excerpt_keys(new) - excerpt_keys(old))
    status, quote_lines = verify_quotes(ctx, new, changed_excerpts)
    lines += ["", f"### Quote verification of new or edited excerpts ({len(changed_excerpts)})", "", *quote_lines, "",
              "Not yet covered here: changed check dates with their basis and changed page copy beside its facts."]
    ctx.sections["compare"] = "\n".join(lines) + "\n"
    detail = f"{len(changes)} changed claims, {len(bindings)} changed evidence entries, {len(changed_excerpts)} new or edited excerpts"
    if status != "PASS":
        detail += "; quote verification " + ("found a quotation missing from its cached source" if status == "FAIL" else "NOT RUN")
    return Result("compare", status, time.monotonic() - start, detail)


# Preflight ---------------------------------------------------------------------------------------


def chrome_preflight() -> tuple[str, str]:
    """The Chromium path the locked Playwright uses, or "" and why not (the build it expects and the fix)."""
    try:  # check_page.py's own locked script environment, never the checkout's .venv
        found = subprocess.run(["uv", "run", "--locked", "-q", "scripts/check_page.py", "--find-chrome"], cwd=ROOT, env=clean_env(),
                               capture_output=True, text=True, timeout=300)
    except (OSError, subprocess.SubprocessError) as exc:
        return "", f"could not ask the locked Playwright ({exc})"
    lines = [line for line in (found.stderr or "").splitlines() if line.strip()]
    return (found.stdout or "").strip(), lines[-1] if lines else "Playwright's Chromium is not installed (uv run --locked scripts/check_page.py --install-chrome)"


def gate_jobs_preflight(environ, memory: float | None = None) -> tuple[str, bool, str]:
    """Whether the browser jobs the gate would start at once fit the host's memory (AISI_GATE_JOBS, else its default);
    memory in GiB, read from the host when not given."""
    sys.path.insert(0, str(ROOT / "scripts"))
    from host_checks import JOB_MEMORY_GIB, default_jobs, jobs_problem, jobs_that_fit, memory_gib  # noqa: PLC0415 - standard library only

    name, text = "browser jobs at once (AISI_GATE_JOBS)", environ.get("AISI_GATE_JOBS", "").strip()
    memory = memory_gib() if memory is None else memory
    try:
        jobs = default_jobs(environ, memory)
    except ValueError as exc:
        return (name, False, f"NOT RUN: {exc}")
    fit = jobs_that_fit(memory)
    source = "AISI_GATE_JOBS" if text else "default"
    if problem := jobs_problem(jobs, memory):
        return (name, False, f"NOT RUN: {problem}")
    held = f"{memory:.1f} GiB holds {fit} at {JOB_MEMORY_GIB} GiB each" if memory is not None else "memory unknown"
    return (name, True, f"{jobs} ({source}; {held})")


def preflight(cache: Path, snapshots: Path | None) -> int:
    rows = []
    try:
        with tempfile.TemporaryDirectory(prefix="aisi-check-") as tmp:
            (Path(tmp) / "probe").write_text("ok")
        rows.append(("temporary output", True, tempfile.gettempdir()))
    except OSError as exc:
        rows.append(("temporary output", False, str(exc)))
    for tool in ("git", "uv", "node", "gitleaks"):
        rows.append((tool, bool(shutil.which(tool)), shutil.which(tool) or "not on PATH"))
    chrome, why = chrome_preflight()
    rows.append(("Playwright Chromium (build, gate, sheet)", bool(chrome), chrome or f"NOT RUN: {why}; without it the build fails (pre-rendering needs it), so content, gate and sheet cannot run"))
    rows.append(gate_jobs_preflight(os.environ))
    print("| Capability | OK | Detail |\n| --- | --- | --- |")
    for name, ok, detail in rows:
        print(f"| {name} | {'yes' if ok else 'NO'} | {detail} |")
    print(f"| source cache (quote verification) | {'yes' if cache.is_dir() else 'no (optional)'} | {cache} |")
    store_detail = str(snapshots) if snapshots is not None else snapshot_location(None)
    rows.append(("private store (copies step; seeds quote verification)", bool(snapshots and snapshots.is_dir()), store_detail))
    print(f"| {rows[-1][0]} | {'yes' if rows[-1][1] else 'NO'} | {store_detail} |")
    passes = pass_store(snapshots)
    rows.append(("pass store (passes step, for commits naming a pass)", bool(passes and passes.is_dir()),
                 str(passes) if passes else "not configured (set AISI_PASSES or AISI_SNAPSHOTS)"))
    print(f"| {rows[-1][0]} | {'yes' if rows[-1][1] else 'NO'} | {rows[-1][2]} |")
    with tempfile.TemporaryDirectory(prefix="aisi-check-") as empty:  # export_scan.py alone resolves the terms file
        scan = subprocess.run([sys.executable, "scripts/export_scan.py", empty, *(["--store", str(snapshots)] if snapshots else [])],
                              cwd=ROOT, env=clean_env(), capture_output=True, text=True)
    rows.append(("private terms file (export step)", scan.returncode == 0, ((scan.stderr or "").strip().splitlines() or ["no output"])[-1]))
    print(f"| {rows[-1][0]} | {'yes' if rows[-1][1] else 'NO'} | {rows[-1][2]} |")
    return 0 if all(ok for _, ok, _ in rows) else 1


# The owner's list of where to look leads, then why --changed chose its steps, release warnings, the gate's report
# lines, the agent's visual pass, the claims and the browser job timings.
REPORT_ORDER = ("where", "changed", "warnings", "report lines", "sheet", "compare", "jobs", "budgets")


def ordered_sections(sections: dict[str, str]) -> list[str]:
    return [sections[step] for step in REPORT_ORDER if step in sections]


def blocking(results: list[Result], allow_skip: bool) -> list[Result]:
    """FAIL always blocks; NOT RUN blocks unless skips were explicitly allowed or the result says it may be skipped."""
    return [r for r in results if r.status == "FAIL" or (r.status == "NOT RUN" and not allow_skip and r.blocks)]


def fork_pull_request() -> bool:
    """Only an explicit fork event can excuse missing private terms; unreadable events fail closed."""
    if os.environ.get("GITHUB_EVENT_NAME") != "pull_request":
        return False
    try:
        event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text(encoding="utf-8"))
        pr = event["pull_request"]
        head, base = pr["head"]["repo"], pr["base"]["repo"]
        return head is None or (bool(head["full_name"]) and bool(base["full_name"]) and head["full_name"] != base["full_name"])
    except (OSError, ValueError, KeyError, TypeError):
        return False


RUNNERS = {
    "validate": step_validate, "lint": step_lint, "tests": step_tests, "copies": step_copies, "passes": step_passes, "export": step_export, "build": step_build,
    "content": step_content, "gate": step_gate, "compare": step_compare, "sheet": step_sheet,
}


# Main --------------------------------------------------------------------------------------------


def gate_arguments(args) -> list[str]:
    """check_page.py options for a CI shard or merge (validated in main)."""
    return ([f"--shard={args.gate_shard}", f"--results={args.gate_results.resolve()}"] if args.gate_shard else []) + (
        [f"--merge={args.gate_merge.resolve()}"] if args.gate_merge else []) + ([f"--jobs={args.gate_jobs}"] if args.gate_jobs else [])


def main() -> int:
    started = time.monotonic()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base", help="revision to compare the staged snapshot with: a remote branch (origin/main), tag or commit; "
                    "a local branch that differs from its upstream is refused (default: HEAD; with --release, the last v* tag)")
    ap.add_argument("--only", help=f"comma-separated steps from {','.join(STEPS)}, or fast (default: all)")
    ap.add_argument("--commits", nargs="+", metavar="REV", help="export step: scan commits selected by these rev-list arguments")
    ap.add_argument("--ref-names", nargs="+", default=[], metavar="REF", help="with --commits: also scan the names of the refs being pushed")
    ap.add_argument("--changed", action="store_true", help="run only the steps the files changed against the base need "
                    "(see CHANGE_RULES; a path it does not name runs every step); excludes --only")
    ap.add_argument("--out", type=Path, help="build into this directory instead of a temporary one (CI publishes it)")
    ap.add_argument("--allow-skip", action="store_true", help="let NOT RUN steps pass (local only; refused in CI)")
    ap.add_argument("--skip-browser", action="store_true", help="start no Chromium: build, content, gate and sheet are recorded as NOT RUN "
                    "without blocking (CI builds and gates the pushed commit); the other selected steps run and block as usual")
    ap.add_argument("--release", action="store_true", help="validate with build.py --release")
    ap.add_argument("--report-dir", type=Path, help="where to write report.md, logs and screenshots (default: a new temporary directory)")
    ap.add_argument("--cache", type=Path, default=ROOT / "work/source-cache", help="source cache for offline quote verification")
    ap.add_argument("--snapshots", type=Path, default=SNAPSHOTS, help="private store of source copies (default: AISI_SNAPSHOTS, otherwise unconfigured); checks copies and seeds quote verification")
    ap.add_argument("--serve-url", default=SERVE_URL, help=f"where the staged build is served for the Where to look links (default: {SERVE_URL})")
    ap.add_argument("--preflight", action="store_true", help="only check that this agent can run every step")
    ap.add_argument("--fail-fast", action="store_true", help="after the first blocking result (FAIL, or NOT RUN without --allow-skip), "
                    "record the remaining steps as NOT RUN instead of running them")
    ap.add_argument("--test-workers", type=int, default=4, help="pytest-xdist workers for the tests and content steps (default: 4; 0 runs in one process)")
    ap.add_argument("--gate-dist", type=Path, help="CI: content and gate check this already built site instead of this run's build")
    ap.add_argument("--gate-shard", help="CI: the gate runs only shard K/N of the browser jobs (needs --gate-results)")
    ap.add_argument("--gate-results", type=Path, help="CI: where the gate shard writes its results")
    ap.add_argument("--gate-jobs", type=int, help="browser jobs the gate runs at once (default: check_page.py's)")
    ap.add_argument("--gate-merge", type=Path, help="CI: the gate reports the shard results under this directory instead of running the browser jobs")
    args = ap.parse_args()
    if args.snapshots is not None:
        args.snapshots = args.snapshots.expanduser().resolve()
    if args.preflight:
        return preflight(args.cache, args.snapshots)
    steps = STEPS if not args.only else tuple(x for s in args.only.split(",") for x in GROUPS.get(s.strip(), (s.strip(),)))
    if unknown := set(steps) - set(STEPS):
        ap.error(f"unknown steps: {', '.join(sorted(unknown))}")
    if args.changed and args.only:
        ap.error("--changed chooses the steps itself: leave out --only")
    if args.changed and args.release:
        ap.error("a release runs every step: leave out --changed")
    if args.allow_skip and os.environ.get("CI"):
        ap.error("--allow-skip is for local runs only")
    if args.ref_names and not args.commits:
        ap.error("--ref-names needs --commits")
    if args.allow_skip and args.commits:
        ap.error("--commits requires the leak checks: leave out --allow-skip")
    if args.skip_browser and os.environ.get("CI"):
        ap.error("--skip-browser is for local runs: CI runs the browser gate")
    if args.skip_browser and args.release:
        ap.error("a release runs the browser gate: leave out --skip-browser")
    gate_options = [o for o in ("gate_dist", "gate_shard", "gate_results", "gate_jobs", "gate_merge") if getattr(args, o) is not None]
    if args.changed and (fixed := [o for o in gate_options if o != "gate_jobs"]):
        ap.error(f"--{fixed[0].replace('_', '-')} is for CI's fixed steps, not --changed")
    if not args.changed:
        if gate_only := [o for o in gate_options if o != "gate_dist"]:
            if "gate" not in steps:
                ap.error(f"--{gate_only[0].replace('_', '-')} needs the gate step")
        if args.gate_dist and not {"content", "gate"} & set(steps):
            ap.error("--gate-dist needs the content or gate step")
    if args.gate_dist and ("build" in steps or args.out):
        ap.error("--gate-dist checks an existing build: leave out the build step and --out")
    if args.gate_shard and args.gate_merge:
        ap.error("--gate-shard and --gate-merge exclude each other")
    if (args.gate_shard is None) != (args.gate_results is None):
        ap.error("--gate-shard and --gate-results go together")
    if args.gate_jobs is not None and args.gate_jobs < 1:
        ap.error("--gate-jobs needs at least 1")
    if args.test_workers < 0:
        ap.error("--test-workers needs 0 or more (0: no worker processes)")
    if args.gate_shard is not None and not re.fullmatch(r"([1-9]\d*)/([1-9]\d*)", args.gate_shard or ""):
        ap.error(f"--gate-shard expects K/N with 1 <= K <= N, not {args.gate_shard!r}")
    if args.gate_shard and int(args.gate_shard.split("/")[0]) > int(args.gate_shard.split("/")[1]):
        ap.error(f"--gate-shard expects K/N with 1 <= K <= N, not {args.gate_shard!r}")
    try:
        base = resolve_base(ROOT, args.base, args.release) if args.changed or {"compare", "sheet", "passes"} & set(steps) else None
    except ValueError as exc:
        ap.error(str(exc))
    report = (args.report_dir or Path(tempfile.mkdtemp(prefix="aisi-check-report-"))).resolve()
    if report.exists() and any(report.iterdir()):
        ap.error(f"--report-dir {report} is not empty; every report starts from an empty directory")
    report.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="aisi-check-") as tmp:
        work = Path(tmp)
        snapshot_id = make_snapshot(ROOT, work / "snapshot", "HEAD") if args.commits else make_snapshot(ROOT, work / "snapshot")
        selected = None
        if args.changed:  # chosen from exactly the snapshot being checked
            steps, rows = changed_steps(changed_paths(ROOT, work / "snapshot.index", base))
            selected = changed_section(base, steps, rows)
        dist = args.gate_dist.resolve() if args.gate_dist else args.out.resolve() if args.out else work / "dist"
        env = clean_env()
        env[RUN_DATE_ENV] = env.get(RUN_DATE_ENV) or dt.datetime.now(ZoneInfo(RUN_ZONE)).date().isoformat()
        ctx = Context(ROOT, work / "snapshot", work, report, base, args.release, dist, args.cache.resolve(), env, serve_url=args.serve_url,
                      snapshots=args.snapshots, gate_args=gate_arguments(args), test_workers=args.test_workers, built=bool(args.gate_dist),
                      commits=args.commits or [], ref_names=args.ref_names)
        if selected:
            ctx.sections["changed"] = selected
        results, stopped = [], None
        for step in STEPS:
            if step in steps and stopped:
                results.append(Result(step, "NOT RUN", detail=f"fail-fast: stopped after {stopped.step} {stopped.status}", ended=stopped.ended))
            elif step in steps:
                sampler = PeakSampler(os.getpid()).start()
                step_start = time.monotonic()
                try:
                    result = (Result(step, "NOT RUN", detail=BROWSER_ELSEWHERE[step], blocks=False)
                              if args.skip_browser and step in BROWSER_ELSEWHERE else RUNNERS[step](ctx))
                except Exception as exc:  # noqa: BLE001 - a crashed step is a failed step, and the report still gets written
                    result = Result(step, "FAIL", detail=f"{type(exc).__name__}: {exc}")
                # Timed here, around the whole step, so in-process work, early returns and crashes all count; read
                # before the sampler stops, so a slow last ps does not count as the step's time.
                step_end = time.monotonic()
                sampler.stop()
                result.seconds, result.ended, result.peak_mb = step_end - step_start, step_end - started, sampler.peak_mb
                if (result.status == "NOT RUN" and step in OPTIONAL_IN_FAST and set(steps) <= set(GROUPS["fast"])
                        and not args.changed and not args.commits and not os.environ.get("CI")):
                    result.blocks = False
                    result.detail += " (not blocking in a fast run; the full check requires it)"
                if result.status == "NOT RUN" and step == "export" and result.missing_terms_only and fork_pull_request():
                    result.blocks = False
                    result.detail += "; pull request from a fork, no terms list; a maintainer runs the names scan before merging"
                print(f"{result.status:8} {step} ({result.seconds:.1f} s)", file=sys.stderr, flush=True)
                results.append(result)
                if args.fail_fast and blocking([result], args.allow_skip):
                    stopped = result
    unstaged = git(ROOT, "diff", "--name-only").split()
    untracked = git(ROOT, "ls-files", "--others", "--exclude-standard").split()
    failed = blocking(results, args.allow_skip)
    lines = ["# Check report", "", f"{'HEAD' if args.commits else 'Staged'} snapshot {snapshot_id}" + (f", compared with {args.base} ({base[:12]})" if base else "")
             + f"; run date {ctx.env[RUN_DATE_ENV]}.", ""]
    if args.commits:
        lines += [f"Export step checked commits: {shlex.join(args.commits)}.", ""]
    if unstaged or untracked:
        lines += [f"Not checked (unstaged or untracked): {', '.join(unstaged + untracked)}.", ""]
    lines += ["| Step | Result | Seconds | Peak MB | Detail |", "| --- | --- | --- | --- | --- |"]
    for r in results:
        peak = "not measured" if r.peak_mb is None else f"{r.peak_mb:.0f}"
        lines.append(f"| {r.step} | {r.status} | {r.seconds:.1f} | {peak} | {short(r.detail.splitlines()[-1] if r.detail else '', 200)}{f' ([log]({r.log}))' if r.log else ''} |")
    lines += ["", f"Overall: {'FAIL' if failed else 'PASS'}" + (" (NOT RUN steps allowed by --allow-skip)" if args.allow_skip and any(r.status == 'NOT RUN' for r in results) else ""), ""]
    lines += [timing_line(results, args.allow_skip, time.monotonic() - started), "", f"Peak MB: {PEAK_METHOD}.", ""]
    if budgets := budget_section(results):
        ctx.sections["budgets"] = budgets
    for r in results:
        if r.status == "FAIL" and r.detail:
            lines += [f"### {r.step} output (last lines)", "", "```", r.detail, "```", ""]
    (report / "report.md").write_text("\n".join(lines + ordered_sections(ctx.sections)), encoding="utf-8")
    print(f"{'FAIL' if failed else 'PASS'}: report at {report / 'report.md'}", file=sys.stderr)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
