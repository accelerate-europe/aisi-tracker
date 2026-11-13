"""The dependency rules of docs/dependencies.md, as check.py's lint step enforces them."""

import shutil
import subprocess
import time
from pathlib import Path

import pytest

from scripts import check, dependency_checks

ROOT = Path(__file__).resolve().parents[1]
BROWSER_SCRIPT = "from playwright.sync_api import sync_playwright\n"
LOCK = """version = 1
requires-python = ">=3.11"

[[package]]
name = "playwright"
version = "{version}"
source = {{ registry = "https://pypi.org/simple" }}
"""


def header(*dependencies: str) -> str:
    listed = ", ".join(f'"{d}"' for d in dependencies)
    return f'# /// script\n# requires-python = ">=3.11"\n# dependencies = [{listed}]\n# ///\n'


@pytest.fixture
def repo(tmp_path):
    (tmp_path / "scripts").mkdir()
    (tmp_path / "docs").mkdir()
    return tmp_path


def test_the_repository_follows_its_rules():
    assert dependency_checks.findings(ROOT) == []


def test_an_unpinned_install_line_in_a_doc_fails(repo):
    (repo / "docs" / "sweep.md").write_text('Run `uv run --with "playwright>=1.51" python sweep.py URL OUT`.\n')
    found = dependency_checks.findings(repo)
    assert any(f.startswith("docs/sweep.md:1: --with installs a package outside any lock") for f in found), found
    assert any(f.startswith("docs/sweep.md:1: uv run without --locked") for f in found), found


@pytest.mark.parametrize("line, why", [
    ("uv run scripts/build.py --out dist", "uv run without --locked"),
    ("uv run --locked python scripts/build.py", "python ignores a script's header and lock"),
    ("uv run --locked --group evidence scripts/verify_excerpts.py", "--group"),
    ("pip install playwright==1.63.0", "pip install"),
    ("uv run --no-project --with pillow python - <<EOF", "--with"),
    ("uv run --locked --no-project scripts/build.py", "--no-project"),
    ("uvx ruff check .", "uvx"),
    ("uv tool run ruff check .", "uv tool run"),
    ("python3 scripts/build.py --out dist", "python scripts/"),
    ("uv sync --group dev", "uv sync without --locked"),
])
def test_each_forbidden_form_is_named(repo, line, why):
    (repo / "README.md").write_text(line + "\n")
    assert any(why in f for f in dependency_checks.findings(repo))


@pytest.mark.parametrize("line", [
    "uv run --locked scripts/build.py --out dist",
    "uv run --locked -q scripts/check_page.py --install-chrome",
    "uv run --locked pytest -q",
    "printf '%s\\n' \"$script\" | uv run -q --no-project --python '>=3.11' python - --only fast",
    "uv run --locked scripts/check_page.py --install-chrome --with-deps",
])
def test_the_locked_forms_and_the_hook_form_pass(repo, line):
    (repo / "README.md").write_text(line + "\n")
    assert dependency_checks.findings(repo) == []


def test_argument_lists_in_python_are_checked_across_lines(repo):
    (repo / "scripts" / "runner.py").write_text('cmd = ["uv", "run", "--locked", "-q",\n       "python", "scripts/build.py"]\n'
                                                'ok = ["uv", "run", "--locked", "-q", "scripts/build.py"]\n'
                                                'bad = ["uv", "run", "scripts/build.py"]\n')
    found = dependency_checks.findings(repo)
    assert found == ["scripts/runner.py:1: uv run ... python: run the script or tool itself (python ignores a script's header and lock) (docs/dependencies.md)",
                     "scripts/runner.py:4: uv run without --locked (docs/dependencies.md)"]


def test_a_browser_script_without_a_lock_fails(repo):
    (repo / "scripts" / "sweep.py").write_text(BROWSER_SCRIPT)
    assert dependency_checks.findings(repo) == [
        "scripts/sweep.py: imports Playwright but has no PEP 723 header and lock (docs/dependencies.md)"]
    (repo / "scripts" / "sweep.py").write_text(header("playwright==1.63.0") + BROWSER_SCRIPT)
    assert dependency_checks.findings(repo) == [
        "scripts/sweep.py: a PEP 723 header without its lock; run uv lock --script scripts/sweep.py"]
    (repo / "scripts" / "sweep.py.lock").write_text(LOCK.format(version="1.63.0"))
    assert dependency_checks.findings(repo) == []


def test_heavy_dependencies_are_pinned_exactly_and_match_the_lock(repo):
    (repo / "scripts" / "sweep.py").write_text(header("playwright>=1.51") + BROWSER_SCRIPT)
    (repo / "scripts" / "sweep.py.lock").write_text(LOCK.format(version="1.63.0"))
    assert dependency_checks.findings(repo) == ["scripts/sweep.py: header must pin playwright exactly (==), not >=1.51"]
    (repo / "scripts" / "sweep.py").write_text(header("playwright==1.64.0") + BROWSER_SCRIPT)
    assert dependency_checks.findings(repo) == [
        "scripts/sweep.py.lock: playwright ['1.63.0'] does not match the header's ==1.64.0; relock"]


def test_a_lock_without_its_header_or_script_fails(repo):
    (repo / "scripts" / "plain.py").write_text("print('no header')\n")
    (repo / "scripts" / "plain.py.lock").write_text(LOCK.format(version="1.63.0"))
    (repo / "scripts" / "gone.py.lock").write_text(LOCK.format(version="1.63.0"))
    assert dependency_checks.findings(repo) == ["scripts/plain.py.lock: a lock without a PEP 723 header in its script",
                                                "scripts/gone.py.lock: a lock without its script"]


def test_locks_must_agree_on_every_package(repo):
    for name, version in (("a", "1.63.0"), ("b", "1.64.0")):
        (repo / "scripts" / f"{name}.py").write_text(header(f"playwright=={version}") + BROWSER_SCRIPT)
        (repo / "scripts" / f"{name}.py.lock").write_text(LOCK.format(version=version))
    assert dependency_checks.findings(repo) == [
        "playwright: locks disagree (scripts/a.py.lock 1.63.0; scripts/b.py.lock 1.64.0); relock so every lock resolves one version"]


def test_check_lint_step_fails_on_either_problem(repo, monkeypatch):
    """check.py --only fast runs lint: ruff (stubbed here), then dependency_checks.py on the snapshot itself."""
    shutil.copy(ROOT / "scripts" / "dependency_checks.py", repo / "scripts" / "dependency_checks.py")
    ctx = check.Context(repo, repo, repo, repo / "report", None, False, repo / "dist", repo)

    def run(ctx, step, cmd, cwd=None):
        start = time.monotonic()
        log = ctx.report / "logs" / f"{step}.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        if "ruff" in cmd:
            return 0, 0.0, log
        with log.open("a") as out:
            code = subprocess.run(cmd, cwd=ctx.snapshot, stdout=out, stderr=subprocess.STDOUT).returncode
        return code, time.monotonic() - start, log

    monkeypatch.setattr(check, "run", run)
    assert check.step_lint(ctx).status == "PASS"
    (repo / "AGENTS.md").write_text("uv run --with playwright python sweep.py\n")
    result = check.step_lint(ctx)
    assert result.status == "FAIL" and "AGENTS.md:1: --with" in result.detail
    (repo / "AGENTS.md").unlink()
    (repo / "scripts" / "sweep.py").write_text(BROWSER_SCRIPT)
    result = check.step_lint(ctx)
    assert result.status == "FAIL" and "scripts/sweep.py: imports Playwright but has no PEP 723 header" in result.detail
