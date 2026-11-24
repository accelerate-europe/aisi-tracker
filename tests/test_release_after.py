"""The release coordinator stops on failures and keeps dry runs and credentials isolated."""

import copy
import json
import subprocess
from datetime import datetime, timezone

import pytest

from scripts import release_after as ra

SHA = "a" * 40
OTHER = "b" * 40
TOKEN = "sentinel-token-never-print"
NOW = 1795510800
DATA = {
    "dataset": {"version": "1.1.2", "as_of": "2026-11-23", "citation": "Unchanged."},
    "changelog": [{"version": "1.1.2", "summary": "Release summary.", "changes": ["One change.", "Another change."]}],
}
UV = ["uv", "run", "--locked"]
DIFF = UV + ["scripts/zenodo_metadata.py"]
MISSING = "refused: 0 Zenodo versions of concept 12345 carry version '1.1.2'; expected one\n"


class Runner:
    def __init__(self):
        self.data = copy.deepcopy(DATA)
        self.main = SHA
        self.ancestor = True
        self.release = True
        self.published = NOW - 60
        self.target = SHA
        self.local_branch = False
        self.remote_branch = False
        self.zenodo = [(0, "metadata matches; nothing to do", "")]
        self.apply = (0, "published; record 12345 now matches", "")
        self.fail = None
        self.calls = []
        self.time = NOW
        self.sleeps = []
        self.created_notes = None

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.time += seconds

    def __call__(self, argv, *, cwd, env, capture_output, text, check):
        assert isinstance(argv, list) and capture_output and text and not check
        self.calls.append((argv.copy(), cwd, env.copy()))
        code, out, err = 0, "", ""
        if argv == self.fail:
            return subprocess.CompletedProcess(argv, 7, "", "synthetic failure")
        if argv in (["git", "show", "HEAD:data/aisi.json"], ["git", "show", "origin/next:data/aisi.json"]):
            out = json.dumps(self.data, indent=2) + "\n"
        elif argv == ["git", "rev-parse", "HEAD"]:
            out = SHA
        elif argv == ["git", "rev-parse", "origin/main"]:
            out = self.main
        elif argv[:3] == ["git", "merge-base", "--is-ancestor"]:
            code = 0 if self.ancestor else 1
        elif argv in (["git", "fetch", "origin", "main", "next", "--tags"], ["git", "fetch", "origin", "--tags"]):
            pass
        elif argv[:4] == UV + ["scripts/ci_run.py"]:
            out = "CI passed"
        elif argv[:4] == UV + ["scripts/smoke_site.py"]:
            out = "PASS live site"
        elif argv == UV + ["scripts/release_tag.py"]:
            out = "v1.1.2\n"
        elif argv[:3] == ["gh", "release", "view"]:
            if self.release:
                out = json.dumps({"tagName": "v1.1.2", "isDraft": False,
                                  "publishedAt": datetime.fromtimestamp(self.published, timezone.utc).isoformat()})
            else:
                code, err = 1, "release not found\n"
        elif argv[:3] == ["gh", "release", "create"]:
            self.created_notes = (cwd / argv[argv.index("--notes-file") + 1]).read_text()
            self.release = True
            self.published = self.time
        elif argv[:3] == ["git", "ls-remote", "--tags"]:
            # The peeled commit must win over the annotated tag's object id.
            out = f"{OTHER}\trefs/tags/v1.1.2\n{self.target}\trefs/tags/v1.1.2^{{}}\n"
        elif argv == DIFF:
            code, out, err = self.zenodo.pop(0) if len(self.zenodo) > 1 else self.zenodo[0]
        elif argv == DIFF + ["--apply"]:
            code, out, err = self.apply
        elif argv[:2] == ["git", "show-ref"]:
            code = 0 if self.local_branch else 1
        elif argv[:3] == ["git", "ls-remote", "--heads"]:
            out = f"{SHA}\trefs/heads/dev-1.1.3\n" if self.remote_branch else ""
        elif argv[:3] == ["git", "worktree", "add"]:
            self.local_branch = True
            path = cwd / argv[-2] / "data/aisi.json"
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps(self.data, indent=2) + "\n")
        elif argv == UV + ["scripts/build.py", "--update-readme"]:
            assert json.loads((cwd / "data/aisi.json").read_text())["dataset"]["version"] == "1.1.3-dev"
        elif argv == ["git", "add", "-A"]:
            assert cwd.name == "dev"
        elif argv == ["git", "diff", "--cached", "--stat"]:
            out = "data/aisi.json | 2 +-"
        else:
            raise AssertionError(f"unexpected command: {argv}")
        return subprocess.CompletedProcess(argv, code, out, err)


@pytest.fixture
def scenario(tmp_path):
    token_file = tmp_path / "zenodo-token.fixture"
    token_file.write_text(f'# token fixture\nIGNORED=value\nexport ZENODO_TOKEN="{TOKEN}" # comment\n')
    runner = Runner()
    work = tmp_path / "work"

    def run(*args, **kwargs):
        return ra.main(["--work", str(work), *args], root=tmp_path, runner=runner,
                       token_file=token_file, sleep=runner.sleep, clock=lambda: runner.time, **kwargs)

    return runner, work, token_file, run


def commands(runner):
    return [argv for argv, _, _ in runner.calls]


def test_dry_run_only_probes_and_never_reads_token(scenario, monkeypatch, capsys):
    runner, work, token_file, run = scenario
    runner.zenodo = [(1, "creators differ", "")]
    monkeypatch.setattr(ra, "read_token", lambda path: pytest.fail("dry run read the token"))
    assert run() == 0
    assert not work.exists()
    calls = commands(runner)
    assert DIFF in calls
    assert UV + ["scripts/smoke_site.py", "--version", "1.1.2"] in calls
    assert not any(c[:3] in (["gh", "release", "create"], ["git", "worktree", "add"]) for c in calls)
    assert DIFF + ["--apply"] not in calls
    assert ["git", "add", "-A"] not in calls
    assert ["git", "fetch", "origin", "--tags"] not in calls
    assert UV + ["scripts/build.py", "--update-readme"] not in calls
    ci, = [c for c in calls if "scripts/ci_run.py" in c]
    assert ci[-4:] == ["--timeout", "5", "--interval", "1"]
    output = capsys.readouterr().out
    assert "1. Live site" in output and "7. Summary" in output
    assert "would run (--apply" in output
    assert f"Token file {token_file}: exists (not read)" in output
    assert "Within three days" in output
    assert "$ git fetch origin --tags" in output
    assert "5. Tags: would run (fetch tags)" in output


@pytest.mark.parametrize("token_exists", [True, False])
def test_fresh_release_dry_run_defers_zenodo(scenario, token_exists, monkeypatch, capsys):
    runner, work, token_file, run = scenario
    runner.release = False
    runner.zenodo = [(2, "", MISSING)]
    if not token_exists:
        token_file.rename(token_file.with_suffix(".unused"))
    monkeypatch.setattr(ra, "read_token", lambda path: pytest.fail("dry run read the token"))
    assert run() == 0
    assert not any("scripts/zenodo_metadata.py" in c for c in commands(runner))
    assert ["git", "fetch", "origin", "--tags"] not in commands(runner)
    assert not runner.sleeps
    assert not work.exists()
    output = capsys.readouterr().out
    assert "would run (after the GitHub release: wait for Zenodo to archive it, diff, --apply, verify)" in output
    assert f"Token file {token_file}: {'exists' if token_exists else 'missing'} (not read)" in output
    zenodo_commands = [line for line in output.splitlines() if line.startswith("  $ ") and "scripts/zenodo_metadata.py" in line]
    assert zenodo_commands == ["  $ " + ra.shlex.join(c) for c in (DIFF, DIFF + ["--apply"], DIFF)]


def test_act_requires_explicit_work_before_any_probe(scenario, capsys):
    runner, work, token_file, _ = scenario
    with pytest.raises(SystemExit) as exc:
        ra.main(["--act"], root=work.parent, runner=runner, token_file=token_file)
    assert exc.value.code == 2
    assert "--act requires --work" in capsys.readouterr().err
    assert not runner.calls
    assert not work.exists()


def test_default_work_is_printed_but_not_created(scenario, monkeypatch, capsys):
    runner, work, token_file, _ = scenario
    monkeypatch.setattr(ra.tempfile, "gettempdir", lambda: str(work.parent))
    monkeypatch.setattr(ra.uuid, "uuid4", lambda: type("Id", (), {"hex": "fixed"})())
    assert ra.main([], root=work.parent, runner=runner, token_file=token_file) == 0
    planned = work.parent / "aisi-release-fixed"
    assert str(planned) in capsys.readouterr().out
    assert not planned.exists()


@pytest.mark.parametrize("failure", ["development", "main", "ancestry", "ci", "fetch"])
def test_precondition_failure_stops_before_live_site(scenario, failure, capsys):
    runner, work, _, run = scenario
    if failure == "development":
        runner.data["dataset"]["version"] = "1.1.3-dev"
    elif failure == "main":
        runner.main = OTHER
    elif failure == "ancestry":
        runner.ancestor = False
    elif failure == "fetch":
        runner.fail = ["git", "fetch", "origin", "main", "next", "--tags"]
    else:
        runner.fail = UV + ["scripts/ci_run.py", "wait", SHA, "--branch", "main"]
    assert run("--act") == 1
    assert not work.exists()
    assert not any("scripts/smoke_site.py" in c for c in commands(runner))
    assert "precondition" in capsys.readouterr().err
    if failure == "development":
        assert commands(runner) == [["git", "show", "HEAD:data/aisi.json"]]


@pytest.mark.parametrize("step,command", [
    (1, UV + ["scripts/smoke_site.py", "--version", "1.1.2"]),
    (3, UV + ["scripts/release_tag.py"]),
    (4, DIFF),
    (4, DIFF + ["--apply"]),
    (5, ["git", "fetch", "origin", "--tags"]),
    (6, UV + ["scripts/build.py", "--update-readme"]),
    (6, ["git", "add", "-A"]),
    (6, ["git", "diff", "--cached", "--stat"]),
])
def test_act_stops_at_first_failed_subprocess(scenario, step, command, capsys):
    runner, _, _, run = scenario
    runner.fail = command
    if "--apply" in command:
        runner.zenodo = [(1, "differs", "")]
    assert run("--act") == 1
    assert commands(runner)[-1] == command
    error = capsys.readouterr().err
    assert f"STOPPED at step {step} ({ra.STEPS[step - 1]})" in error
    assert f"{ra.shlex.join(command)} exited 7" in error


def test_failed_release_creation_stops_before_zenodo(scenario, capsys):
    runner, work, _, run = scenario
    runner.release = False
    runner.fail = ["gh", "release", "create", "v1.1.2", "--target", SHA, "--title", "v1.1.2",
                   "--notes-file", str(work / "notes-1.1.2.md")]
    assert run("--act") == 1
    assert commands(runner)[-1] == runner.fail
    assert "STOPPED at step 3" in capsys.readouterr().err


@pytest.mark.parametrize("target,code", [(SHA, 0), (OTHER, 1)])
def test_existing_github_release_target(scenario, target, code, capsys):
    runner, _, _, run = scenario
    runner.target = target
    assert run() == code
    assert not any(c[:3] == ["gh", "release", "create"] for c in commands(runner))
    output = capsys.readouterr()
    if code:
        assert "not HEAD" in output.err
        assert DIFF not in commands(runner)
    else:
        assert "already done (GitHub release tag points at HEAD)" in output.out


def test_github_probe_failure_is_not_absence(scenario, capsys):
    runner, _, _, run = scenario
    base_runner = runner.__call__

    def unavailable(argv, **kwargs):
        if argv[:3] == ["gh", "release", "view"]:
            runner.calls.append((argv, kwargs["cwd"], kwargs["env"]))
            return subprocess.CompletedProcess(argv, 1, "", "network unavailable")
        return base_runner(argv, **kwargs)

    assert ra.main([], root=scenario[1].parent, runner=unavailable, token_file=scenario[2]) == 1
    assert not any(c[:3] == ["gh", "release", "create"] for c in commands(runner))
    assert "could not establish" in capsys.readouterr().err


def test_poll_apply_verify_and_token_isolation(scenario, monkeypatch, capsys):
    runner, work, token_file, run = scenario
    runner.release = False
    runner.zenodo = [(2, "", MISSING), (1, f"differs {TOKEN}", f"diagnostic {TOKEN}"),
                     (1, f"old record {TOKEN}", ""), (0, "matches", "")]
    runner.apply = (0, f"published; record 12345 now matches {TOKEN}", f"echo {TOKEN}")
    monkeypatch.setenv("ZENODO_TOKEN", "inherited-sentinel")
    assert run("--act") == 0
    assert runner.sleeps == [30, 60]
    assert runner.created_notes == "Release summary.\n\n- One change.\n- Another change.\n"
    assert (work / "notes-1.1.2.md").read_text() == runner.created_notes
    apply_calls = []
    for argv, _, env in runner.calls:
        assert TOKEN not in " ".join(argv)
        assert "inherited-sentinel" not in " ".join(argv)
        if argv == DIFF + ["--apply"]:
            apply_calls.append(argv)
            assert env["ZENODO_TOKEN"] == TOKEN
        else:
            assert "ZENODO_TOKEN" not in env
    assert len(apply_calls) == 1
    captured = capsys.readouterr()
    assert TOKEN not in captured.out + captured.err
    assert "inherited-sentinel" not in captured.out + captured.err
    assert "[redacted]" in captured.out
    calls = commands(runner)
    assert ["git", "fetch", "origin", "--tags"] in calls
    create_index = next(i for i, c in enumerate(calls) if c[:3] == ["gh", "release", "create"])
    assert calls[create_index + 1][:3] == ["gh", "release", "view"]
    assert "Reviewed-by" in captured.out and "base=origin/next" in captured.out


@pytest.mark.parametrize("contents", [None, "IGNORED=foo\n", "ZENODO_TOKEN=\n", 'ZENODO_TOKEN="unterminated\n'])
def test_missing_or_invalid_token_stops_before_zenodo(scenario, contents, capsys):
    runner, work, token_file, run = scenario
    if contents is None:
        token_file.rename(token_file.with_suffix(".unused"))
    else:
        token_file.write_text(contents)
    assert run("--act") == 1
    assert DIFF not in commands(runner)
    assert ["git", "fetch", "origin", "--tags"] not in commands(runner)
    assert str(token_file) in capsys.readouterr().err
    assert not (work / "dev").exists()


@pytest.mark.parametrize("assignment", ["ZENODO_TOKEN=plain # comment", "export ZENODO_TOKEN='plain'", 'ZENODO_TOKEN="plain"'])
def test_token_file_syntax(tmp_path, assignment):
    path = tmp_path / "token.fixture"
    path.write_text(f"# comment\nOTHER=ignored\n{assignment}\n")
    assert ra.read_token(path) == "plain"


@pytest.mark.parametrize("error,age", [(MISSING, 601), ("refused: unexpected edit draft", 10),
                                     (MISSING.replace("0 Zenodo", "2 Zenodo"), 10)])
def test_zenodo_refusals_are_not_polled(scenario, error, age, capsys):
    runner, _, _, run = scenario
    runner.published = NOW - age
    runner.zenodo = [(2, "", error)]
    assert run("--act") == 1
    assert not runner.sleeps
    assert commands(runner)[-1] == DIFF
    assert "STOPPED at step 4" in capsys.readouterr().err


def test_zenodo_polling_is_bounded(scenario):
    runner, _, _, run = scenario
    runner.published = NOW
    runner.zenodo = [(2, "", MISSING)]
    assert run("--act") == 1
    assert runner.sleeps == [30] * 20
    assert commands(runner)[-1] == DIFF


def test_zenodo_verification_still_differs_after_one_lag_wait(scenario, capsys):
    runner, _, _, run = scenario
    runner.zenodo = [(1, "differs", "")]
    assert run("--act") == 1
    assert runner.sleeps == [60]
    assert commands(runner).count(DIFF + ["--apply"]) == 1
    assert commands(runner).count(DIFF) == 3
    assert commands(runner)[-1] == DIFF
    assert "still differs" in capsys.readouterr().err


def test_apply_failure_does_not_retry(scenario):
    runner, _, _, run = scenario
    runner.zenodo = [(1, "differs", "")]
    runner.apply = (1, "after publishing, the record still differs", TOKEN)
    assert run("--act") == 1
    assert not runner.sleeps
    assert commands(runner)[-1] == DIFF + ["--apply"]


@pytest.mark.parametrize("field,value", [("summary", " "), ("changes", [])])
def test_invalid_release_notes_stop_before_release(scenario, field, value, capsys):
    runner, work, _, run = scenario
    runner.data["changelog"][0][field] = value
    assert run("--act") == 1
    assert not work.exists()
    assert UV + ["scripts/release_tag.py"] not in commands(runner)
    assert "STOPPED at step 2" in capsys.readouterr().err


@pytest.mark.parametrize("where", ["local_branch", "remote_branch"])
def test_existing_development_branch_is_refused(scenario, where, capsys):
    runner, work, _, run = scenario
    setattr(runner, where, True)
    assert run() == 1
    assert not work.exists()
    assert not any(c[:3] == ["git", "worktree", "add"] for c in commands(runner))
    output = capsys.readouterr()
    assert "dev-1.1.3: already done or in progress" in output.err
    assert "would refuse" in output.out


def test_rerun_reuses_notes_and_refuses_occupied_dev_branch(scenario, capsys):
    runner, work, _, run = scenario
    assert run("--act") == 0
    notes = (work / "notes-1.1.2.md").read_bytes()
    runner.calls.clear()
    assert run("--act") == 1
    assert (work / "notes-1.1.2.md").read_bytes() == notes
    assert not any(c[:3] == ["git", "worktree", "add"] for c in commands(runner))
    assert "already done (notes match HEAD changelog)" in capsys.readouterr().out


def test_conflicting_notes_are_not_overwritten(scenario):
    runner, work, _, run = scenario
    work.mkdir()
    notes = work / "notes-1.1.2.md"
    notes.write_text("Other notes\n")
    assert run("--act") == 1
    assert notes.read_text() == "Other notes\n"
    assert UV + ["scripts/release_tag.py"] not in commands(runner)


def test_development_edit_preserves_every_other_byte(tmp_path):
    path = tmp_path / "aisi.json"
    raw = json.dumps(DATA, ensure_ascii=False, indent=2).replace("\n", "\r\n").encode() + b"\r\n"
    path.write_bytes(raw)
    ra.edit_development_version(path, "1.1.2", "1.1.3-dev")
    changed = path.read_bytes()
    assert changed == raw.replace(b'"version": "1.1.2"', b'"version": "1.1.3-dev"', 1)
    before, after = json.loads(raw), json.loads(changed)
    assert after["dataset"].pop("version") == "1.1.3-dev"
    assert before["dataset"].pop("version") == "1.1.2"
    assert before == after


def test_development_edit_refuses_wrong_base_without_writing(tmp_path):
    path = tmp_path / "aisi.json"
    raw = json.dumps(DATA, indent=2).encode()
    path.write_bytes(raw)
    with pytest.raises(ra.Refusal, match="expected 1.1.1"):
        ra.edit_development_version(path, "1.1.1", "1.1.2-dev")
    assert path.read_bytes() == raw
