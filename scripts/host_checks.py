"""Host state the browser steps need (docs/dependencies.md, Browser): the locked Playwright's own Chromium, and no
more parallel browser jobs than the host's memory holds. Standard library only; check_page.py, prerender.py,
monitor_browser.py and check.py --preflight share it.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

INSTALL_CHROME = "uv run --locked scripts/check_page.py --install-chrome"
# Memory one browser job may take at its peak: on a 7.5 GB host two jobs at once timed out under memory pressure.
JOB_MEMORY_GIB = 4
GIB = 1024 ** 3


def chrome_problem(executable: str, playwright_version: str) -> str | None:
    """None when the Chromium build the locked Playwright expects is installed; otherwise what is missing, what is
    installed instead and the fix."""
    path = Path(executable)
    if path.is_file() and os.access(path, os.X_OK):
        return None
    build = next((part for part in path.parts if re.fullmatch(r"chromium-\d+", part)), None)
    folder = next((parent.parent for parent in path.parents if parent.name == build), path.parent) if build else path.parent
    installed = sorted(p.name for p in folder.glob("chromium-*")) if folder.is_dir() else []
    have = f"installed in {folder}: {', '.join(installed)}" if installed else f"no Chromium build in {folder}"
    return (f"the locked Playwright {playwright_version} expects Chromium build {build or path.name} ({path}), which is not "
            f"installed; {have}. Fix: {INSTALL_CHROME}")


def memory_gib() -> float | None:
    """The host's physical memory, or None where it cannot be read."""
    try:
        if sys.platform.startswith("linux"):
            for line in Path("/proc/meminfo").read_text().splitlines():
                if line.startswith("MemTotal:"):
                    return int(line.split()[1]) * 1024 / GIB
        if sys.platform == "darwin":
            return int(subprocess.run(["sysctl", "-n", "hw.memsize"], capture_output=True, text=True, check=True).stdout) / GIB
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    return None


def jobs_that_fit(memory: float | None) -> int | None:
    """How many browser jobs at once the memory holds (at least one), or None when the memory is unknown."""
    return None if memory is None else max(1, int(memory // JOB_MEMORY_GIB))


def jobs_problem(jobs: int, memory: float | None) -> str | None:
    """None when this many browser jobs at once fit the host's memory; otherwise why not, and the fix."""
    fit = jobs_that_fit(memory)
    if fit is None or jobs <= fit:
        return None
    return (f"{jobs} browser jobs at once need about {jobs * JOB_MEMORY_GIB} GiB, and this host has {memory:.1f} GiB. "
            f"Fix: set AISI_GATE_JOBS={fit}, or pass --jobs {fit} to check_page.py or --gate-jobs {fit} to check.py")


def default_jobs(environ, memory: float | None) -> int:
    """Browser jobs at once when none are asked for: AISI_GATE_JOBS where a host sets it, else the CPUs, at most 8
    and at most what the memory holds. ValueError for a setting that is not a positive whole number."""
    if text := environ.get("AISI_GATE_JOBS", "").strip():
        if not text.isdigit() or int(text) < 1:
            raise ValueError(f"AISI_GATE_JOBS={text!r}: needs a positive whole number")
        return int(text)
    return min(os.cpu_count() or 1, 8, jobs_that_fit(memory) or 8)


def playwright_version() -> str:
    """The installed Playwright's version, or "(version unknown)" outside an environment that holds it."""
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version("playwright")
    except PackageNotFoundError:
        return "(version unknown)"
