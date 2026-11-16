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
    # A run by hand publishes only on request; a push to next needs no input.
    request = workflow["on"]["workflow_dispatch"]["inputs"]["preview"] if "on" in workflow else workflow[True]["workflow_dispatch"]["inputs"]["preview"]
    assert request["type"] == "boolean" and request["default"] is False
    assert steps[0]["env"]["REQUESTED"] == "${{ inputs.preview }}"
    run = steps[0]["run"]
    assert run.index('[ "$EVENT" = workflow_dispatch ] && [ "$REQUESTED" != true ]') < run.index("publish=true")
    assert "preview not requested for this run; nothing published" in run
    assert all("steps.enabled.outputs.publish == 'true'" in step["if"] for step in steps[1:])
    pins = {step["uses"] for name, old in workflow["jobs"].items() if name != "preview" for step in old.get("steps", []) if "uses" in step}
    assert all(step["uses"] in pins for step in steps if "uses" in step)
    assert (ROOT / "scripts/check_preview.py.lock").read_bytes() == (ROOT / "scripts/check_page.py.lock").read_bytes()


def run_gate(tmp_path, **env):
    """Run the job's first step as Actions would and return (log, outputs)."""
    import os
    import subprocess

    workflow = yaml.safe_load((ROOT / ".github/workflows/pages.yml").read_text())
    output = tmp_path / "out"
    output.write_text("")
    values = {"KEY": "", "SITE": "success", "EVENT": "push", "REQUESTED": "", **env}
    done = subprocess.run(["bash", "-e", "-c", workflow["jobs"]["preview"]["steps"][0]["run"]], capture_output=True, text=True, check=True,
                          env={**os.environ, **values, "GITHUB_OUTPUT": str(output)})
    return done.stdout.strip(), output.read_text().strip()


def test_hand_runs_publish_only_on_request(tmp_path):
    assert run_gate(tmp_path, KEY="k", EVENT="workflow_dispatch", REQUESTED="false") == ("preview: preview not requested for this run; nothing published", "")
    assert run_gate(tmp_path, KEY="k", EVENT="workflow_dispatch", REQUESTED="true") == ("", "publish=true")
    assert run_gate(tmp_path, KEY="k", EVENT="push") == ("", "publish=true")
    assert run_gate(tmp_path, EVENT="workflow_dispatch", REQUESTED="true")[0] == "preview: secret PREVIEW_DEPLOY_KEY is not set; nothing published"
    assert run_gate(tmp_path, KEY="k", EVENT="workflow_dispatch", REQUESTED="true", SITE="skipped") == ("preview: this run built no site; nothing published", "")
