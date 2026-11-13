"""Host state for the browser steps (docs/dependencies.md, Browser): the locked Playwright's Chromium build and the
number of browser jobs the memory holds, in preflight and at the start of the gate."""

import subprocess

import pytest

from scripts import check, host_checks


def chrome_at(folder, build="chromium-1243"):
    executable = folder / build / "chrome-linux64" / "chrome"
    return executable


def test_the_expected_build_installed_is_no_problem(tmp_path):
    executable = chrome_at(tmp_path)
    executable.parent.mkdir(parents=True)
    executable.write_text("")
    executable.chmod(0o755)
    assert host_checks.chrome_problem(str(executable), "1.63.0") is None


def test_a_different_build_names_both_and_the_fix(tmp_path):
    (tmp_path / "chromium-1200" / "chrome-linux64").mkdir(parents=True)
    problem = host_checks.chrome_problem(str(chrome_at(tmp_path)), "1.63.0")
    assert "the locked Playwright 1.63.0 expects Chromium build chromium-1243" in problem
    assert f"installed in {tmp_path}: chromium-1200" in problem
    assert problem.endswith("Fix: uv run --locked scripts/check_page.py --install-chrome")


def test_no_build_at_all_says_so(tmp_path):
    assert f"no Chromium build in {tmp_path}" in host_checks.chrome_problem(str(chrome_at(tmp_path)), "1.63.0")


@pytest.mark.parametrize("memory, jobs, fits", [(7.6, 1, True), (7.6, 2, False), (15.6, 3, True), (15.6, 4, False), (None, 8, True)])
def test_jobs_must_fit_the_memory(memory, jobs, fits):
    problem = host_checks.jobs_problem(jobs, memory)
    assert (problem is None) == fits
    if not fits:
        fit = host_checks.jobs_that_fit(memory)
        assert f"Fix: set AISI_GATE_JOBS={fit}" in problem


def test_default_jobs_follow_the_setting_then_the_cpus_and_memory(monkeypatch):
    monkeypatch.setattr(host_checks.os, "cpu_count", lambda: 16)
    assert host_checks.default_jobs({"AISI_GATE_JOBS": "5"}, 7.6) == 5  # explicit: refused later if it does not fit
    assert host_checks.default_jobs({}, 7.6) == 1
    assert host_checks.default_jobs({}, 64) == 8
    with pytest.raises(ValueError, match="AISI_GATE_JOBS='two'"):
        host_checks.default_jobs({"AISI_GATE_JOBS": "two"}, 64)


def test_preflight_reports_not_run_with_the_fix_when_chromium_does_not_match(monkeypatch):
    problem = "the locked Playwright 1.63.0 expects Chromium build chromium-1243 (/x), which is not installed; " \
              "installed in /b: chromium-1200. Fix: uv run --locked scripts/check_page.py --install-chrome"
    monkeypatch.setattr(check.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a[0], 3, "\n", problem + "\n"))
    path, why = check.chrome_preflight()
    assert path == "" and why == problem


def test_preflight_reports_too_many_jobs(monkeypatch):
    monkeypatch.setattr(host_checks.os, "cpu_count", lambda: 16)
    name, ok, detail = check.gate_jobs_preflight({"AISI_GATE_JOBS": "3"}, memory=7.6)
    assert not ok and detail.startswith("NOT RUN: 3 browser jobs at once need about 12 GiB") and "AISI_GATE_JOBS=1" in detail
    assert check.gate_jobs_preflight({}, memory=7.6)[1:] == (True, "1 (default; 7.6 GiB holds 1 at 4 GiB each)")
    assert check.gate_jobs_preflight({"AISI_GATE_JOBS": "3"}, memory=15.6)[1:] == (True, "3 (AISI_GATE_JOBS; 15.6 GiB holds 3 at 4 GiB each)")
    assert check.gate_jobs_preflight({"AISI_GATE_JOBS": "abc"}, memory=7.6)[1:] == (False, "NOT RUN: AISI_GATE_JOBS='abc': needs a positive whole number")


def test_gate_not_run_carries_check_pages_reason(tmp_path):
    log = tmp_path / "gate.log"
    log.write_text("$ uv run --locked -q scripts/check_page.py\nNOT RUN: 3 browser jobs at once need about 12 GiB; rendered page checks were not run.\n")
    result = check.gate_result("gate", check.NOT_RUN, 1.0, log)
    assert result.status == "NOT RUN" and result.detail.startswith("3 browser jobs at once need about 12 GiB")
