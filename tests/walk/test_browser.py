"""Static rule probes. Run only on CI; a missing Chromium is a skip, never a pass."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = Path(__file__).with_name("fixtures")
pytestmark = pytest.mark.chromium


@pytest.mark.parametrize("name, rules", [
    ("clean", set()),
    ("overflow", {"horizontal-overflow"}),
    ("overlap", {"text-overlap"}),
    ("clipping", {"clipped-text"}),
    ("hit", {"hit-size"}),
    ("contrast", {"contrast"}),
    ("pitch", {"line-pitch"}),
    ("console", {"console-error"}),
    ("page-error", {"page-error"}),
    ("take-over", {"take-over"}),
])
def test_static_page(tmp_path, name, rules):
    dist = tmp_path / "site"
    dist.mkdir()
    shutil.copyfile(FIXTURES / f"{name}.html", dist / "index.html")
    out = tmp_path / "out"
    result = subprocess.run(["uv", "run", "--locked", "scripts/walk.py", "--dist", str(dist),
                             "--out", str(out), "--views", "320-light"],
                            cwd=ROOT, capture_output=True, text=True, timeout=90)
    if result.returncode == 3:
        pytest.skip(result.stderr.strip())
    assert result.returncode == (1 if rules else 0), result.stdout + result.stderr
    findings = json.loads((out / "findings.json").read_text())["findings"]
    assert {finding["rule"] for finding in findings} == rules, findings
