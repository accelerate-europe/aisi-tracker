"""scripts/ci_run.py: waiting for CI on a commit and downloading its site, with gh replaced by fakes."""

import io
import json
import tarfile
import zipfile

import pytest

from scripts import ci_run

REPO = "owner/name"
SHA = "a" * 40


def run(status="completed", conclusion="success", run_id=7, created="2026-11-13T20:00:00Z"):
    return {"databaseId": run_id, "status": status, "conclusion": conclusion, "createdAt": created, "url": f"https://ci/{run_id}"}


def fake_gh(answers):
    """gh that answers each call from a queue per subcommand ('run list', 'run view', 'api ...')."""
    calls = []

    def call(args):
        calls.append(args)
        key = args[0] if args[0] == "api" else " ".join(args[:2])
        return json.dumps(answers[key].pop(0) if isinstance(answers[key], list) else answers[key])
    call.calls = calls
    return call


def tar_bytes(members):
    """members: (name, bytes) for files, (name, None) for folders, (name, 'link:target') for a symlink."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        for name, content in members:
            info = tarfile.TarInfo(name)
            if content is None:
                info.type = tarfile.DIRTYPE
                archive.addfile(info)
            elif isinstance(content, str):
                info.type, info.linkname = tarfile.SYMTYPE, content.removeprefix("link:")
                archive.addfile(info)
            else:
                info.size = len(content)
                archive.addfile(info, io.BytesIO(content))
    return buffer.getvalue()


def zipped(files):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    return buffer.getvalue()


def test_wait_polls_until_the_newest_run_completes():
    gh = fake_gh({"run list": [[], [run(status="in_progress")], [run(conclusion="failure"), run(run_id=8, created="2026-11-13T21:00:00Z")]]})
    sleeps = []
    finished = ci_run.wait(gh, REPO, SHA, sleep=sleeps.append, clock=lambda: 0)
    assert finished["databaseId"] == 8 and finished["conclusion"] == "success"
    assert len(sleeps) == 2
    assert gh.calls[0][:6] == ["run", "list", "--repo", REPO, "--commit", SHA]


def test_wait_gives_up_after_the_timeout():
    clock = iter([0, 10, 4000]).__next__
    gh = fake_gh({"run list": [[], [run(status="queued")]]})
    with pytest.raises(ci_run.CiError, match="after 3600 s .run 7 is queued"):
        ci_run.wait(gh, REPO, SHA, sleep=lambda s: None, clock=clock)


def test_wait_names_failed_jobs_and_exits_1(monkeypatch, capsys):
    gh = fake_gh({"run list": [[run(conclusion="failure")]], "run view": {"jobs": [
        {"name": "checks", "conclusion": "success"}, {"name": "gate (2)", "conclusion": "failure"}, {"name": "deploy", "conclusion": "skipped"}]}})
    monkeypatch.setattr(ci_run, "gh", gh)
    monkeypatch.setattr(ci_run, "full_sha", lambda rev: SHA)
    assert ci_run.main(["wait", "--repo", REPO]) == 1
    out = capsys.readouterr().out
    assert "- gate (2): failure" in out and "checks" not in out and f"gh run view 7 --repo {REPO} --log-failed" in out


def test_site_extracts_the_tar_inside_the_artifact(tmp_path):
    site = tar_bytes([("./", None), ("./index.html", b"<html>"), ("./api/", None), ("./api/v1.json", b"{}")])
    gh = fake_gh({"api": [{"artifacts": [{"id": 5, "name": "gate-shard-1"}, {"id": 9, "name": "site"}]}]})
    fetched = []

    def get_bytes(args):
        fetched.append(args)
        return zipped({"site.tar": site})
    assert ci_run.download_site(gh, get_bytes, REPO, run(), tmp_path / "out") == 2
    assert (tmp_path / "out/index.html").read_bytes() == b"<html>" and (tmp_path / "out/api/v1.json").is_file()
    assert fetched == [["api", f"repos/{REPO}/actions/artifacts/9/zip"]]


@pytest.mark.parametrize("members", [
    [("../escape.html", b"x")],
    [("/etc/escape", b"x")],
    [("./a/../../escape", b"x")],
    [("./link", "link:/etc/passwd")],
])
def test_site_refuses_members_outside_the_folder(tmp_path, members):
    with pytest.raises(ci_run.CiError):
        ci_run.extract_tar(tar_bytes(members), tmp_path / "out")
    assert not (tmp_path / "escape.html").exists() and not (tmp_path / "out").exists()


def test_site_refuses_a_zip_with_anything_but_one_site_tar():
    with pytest.raises(ci_run.CiError, match="outside"):
        ci_run.site_tar(zipped({"../site.tar": b""}))
    with pytest.raises(ci_run.CiError, match="expected one site.tar"):
        ci_run.site_tar(zipped({"site.tar": b"", "other": b""}))


def test_site_needs_a_passed_run_and_an_empty_folder(tmp_path):
    with pytest.raises(ci_run.CiError, match="ended failure"):
        ci_run.download_site(fake_gh({}), lambda a: b"", REPO, run(conclusion="failure"), tmp_path)
    (tmp_path / "out").mkdir()
    (tmp_path / "out/old").write_text("x")
    gh = fake_gh({"api": [{"artifacts": [{"id": 9, "name": "site"}]}]})
    with pytest.raises(ci_run.CiError, match="not empty"):
        ci_run.download_site(gh, lambda a: b"", REPO, run(), tmp_path / "out")


def test_runs_are_push_runs_on_the_named_branch():
    gh = fake_gh({"run list": [[run()]]})
    ci_run.wait(gh, REPO, SHA, sleep=lambda s: None, clock=lambda: 0, branch="main")
    args = gh.calls[0]
    assert args[args.index("--event") + 1] == "push" and args[args.index("--branch") + 1] == "main"


def test_a_cancelled_run_is_reported_as_superseded(monkeypatch, capsys):
    gh = fake_gh({"run list": [[run(conclusion="cancelled")]]})
    monkeypatch.setattr(ci_run, "gh", gh)
    monkeypatch.setattr(ci_run, "full_sha", lambda rev: SHA)
    assert ci_run.main(["wait", "--repo", REPO]) == 1
    assert "superseded by a newer push" in capsys.readouterr().out


def test_a_dot_slash_member_name_is_read_by_its_own_name():
    assert ci_run.site_tar(zipped({"./site.tar": b"tar bytes"})) == b"tar bytes"


def test_an_out_path_that_is_a_file_is_an_error_not_a_traceback(monkeypatch, tmp_path, capsys):
    (tmp_path / "out").write_text("x")
    gh = fake_gh({"run list": [[run()]], "api": [{"artifacts": [{"id": 9, "name": "site"}]}]})
    monkeypatch.setattr(ci_run, "gh", gh)
    monkeypatch.setattr(ci_run, "full_sha", lambda rev: SHA)
    assert ci_run.main(["site", "--repo", REPO, "--out", str(tmp_path / "out")]) == 2
    assert capsys.readouterr().err.startswith("error:")
