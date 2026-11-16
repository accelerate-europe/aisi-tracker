"""The preview job in pages.yml: scope, guards and pins (needs scripts/check_preview.py.lock)."""

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def test_workflow_scope_and_all_steps_guarded():
    workflow = yaml.safe_load((ROOT / ".github/workflows/pages.yml").read_text())
    job = workflow["jobs"]["preview"]
    assert job["needs"] == ["plan", "site"]
    assert "!cancelled()" in job["if"] and "always()" not in job["if"]
    assert "refs/heads/main" in job["if"] and "github.ref_type == 'branch'" in job["if"]
    assert job["permissions"] == {"contents": "read"}
    steps = job["steps"]
    assert steps[0]["id"] == "enabled"
    assert "secret PREVIEW_DEPLOY_KEY is not set; nothing published" in steps[0]["run"]
    assert "this run built no site; nothing published" in steps[0]["run"]
    assert all("steps.enabled.outputs.publish == 'true'" in step["if"] for step in steps[1:])
    pins = {step["uses"] for name, old in workflow["jobs"].items() if name != "preview" for step in old.get("steps", []) if "uses" in step}
    assert all(step["uses"] in pins for step in steps if "uses" in step)
    assert (ROOT / "scripts/check_preview.py.lock").read_bytes() == (ROOT / "scripts/check_page.py.lock").read_bytes()
