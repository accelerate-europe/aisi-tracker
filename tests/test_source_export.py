"""Source exports work without Git metadata or one-time migration tools."""

import builtins
import importlib
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from scripts import release_checks


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("module_name", ["scripts.published", "published"])
@pytest.mark.parametrize("legacy", [False, True])
def test_prepared_facts_imports_migration_only_for_legacy(
    monkeypatch, module_name, legacy
):
    monkeypatch.syspath_prepend(str(ROOT / "scripts"))
    module = importlib.import_module(module_name)
    data = {
        "dataset": {"exchange_rates": []},
        "jurisdictions": [{"id": "GB", "timeline": []}],
        "institutes": [],
        "sources": [
            {
                "id": "source",
                "excerpts": [
                    {
                        "text": "Synthetic jurisdiction.",
                        "supports": [
                            "/jurisdictions/0/id"
                            if legacy
                            else ["jurisdictions", {"id": "GB"}, "id"]
                        ],
                    }
                ],
            }
        ],
    }
    original_import = builtins.__import__
    imports = []

    def import_without_migration(name, *args, **kwargs):
        if name.rsplit(".", 1)[-1] == "migrate_references":
            imports.append(name)
            if not legacy:
                raise ModuleNotFoundError("migration tool omitted from source export")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", import_without_migration)
    result = module.prepared_facts(data, strict=not legacy)
    assert result["sources"][0]["excerpts"][0]["supports"] == ["/jurisdictions/0/id"]
    assert bool(imports) is legacy


@pytest.mark.parametrize("missing", ["CLAUDE.md", "AGENTS.md", "both"])
def test_launch_wording_allows_missing_agent_instructions(tmp_path, missing):
    (tmp_path / "README.md").write_text("Released dataset.")
    for name in ("CLAUDE.md", "AGENTS.md"):
        if missing not in (name, "both"):
            (tmp_path / name).write_text("Project instructions.")
    assert (
        release_checks.check_launch_wording(
            {"dataset": {"citation": "Released dataset."}}, {}, tmp_path
        )
        == []
    )


@pytest.mark.parametrize("name", ["README.md", "CLAUDE.md", "AGENTS.md"])
def test_launch_wording_keeps_real_read_errors(tmp_path, monkeypatch, name):
    def unreadable(path, *args, **kwargs):
        if path.name == name:
            raise PermissionError("permission denied")
        return "Released dataset."

    monkeypatch.setattr(Path, "read_text", unreadable)
    errors = release_checks.check_launch_wording(
        {"dataset": {"citation": "Released dataset."}}, {}, tmp_path
    )
    assert len(errors) == 1
    assert name + ": cannot check release wording:" in errors[0]
    assert "permission denied" in errors[0]


def test_launch_wording_still_requires_readme(tmp_path):
    errors = release_checks.check_launch_wording(
        {"dataset": {"citation": "Released dataset."}}, {}, tmp_path
    )
    assert len(errors) == 1
    assert errors[0].startswith("README.md: cannot check release wording:")


@pytest.mark.parametrize("git_available", [False, True])
def test_markdown_scan_without_checkout_prunes_outputs_and_dependencies(
    tmp_path, monkeypatch, git_available
):
    for name in ("z.md", "docs/a.md", ".github/review.md"):
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("Source prose.")
    for folder in (
        ".venv",
        "node_modules",
        "work",
        "dist",
        ".git",
        "__pycache__",
        "tmp-export",
        ".pytest_cache",
    ):
        path = tmp_path / "docs" / folder / "ignored.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("Ignored\u2014generated prose.")
    if not git_available:
        monkeypatch.setenv("PATH", "")
    assert [
        p.relative_to(tmp_path).as_posix()
        for p in release_checks.tracked_markdown(tmp_path)
    ] == [".github/review.md", "docs/a.md", "z.md"]
    assert release_checks.check_markdown(tmp_path) == []
    (tmp_path / "docs/a.md").write_text("Authored\u2014prose.")
    assert release_checks.check_markdown(tmp_path) == [
        "docs/a.md: tracked Markdown contains U+2014"
    ]


def test_markdown_in_checkout_still_uses_tracked_files(tmp_path):
    subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
    (tmp_path / "tracked.md").write_text("Source prose.")
    (tmp_path / "untracked.md").write_text("Untracked\u2014prose.")
    subprocess.run(["git", "-C", str(tmp_path), "add", "tracked.md"], check=True)
    assert release_checks.tracked_markdown(tmp_path) == [tmp_path / "tracked.md"]


def test_untracked_export_inside_another_checkout_is_scanned(tmp_path):
    subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
    export = tmp_path / "export"
    export.mkdir()
    (export / "README.md").write_text("Exported source prose.")
    assert release_checks.tracked_markdown(export) == [export / "README.md"]
    assert release_checks.check_markdown(export) == []
    (export / "README.md").write_text("Exported\u2014source prose.")
    assert release_checks.check_markdown(export) == [
        "README.md: tracked Markdown contains U+2014"
    ]


@pytest.mark.parametrize("git_marker", ["directory", "file"])
def test_empty_checkout_does_not_scan_untracked_markdown(
    tmp_path, monkeypatch, git_marker
):
    if git_marker == "directory":
        (tmp_path / ".git").mkdir()
    else:
        (tmp_path / ".git").write_text("gitdir: /synthetic/linked/worktree")
    (tmp_path / "untracked.md").write_text("Untracked\u2014prose.")
    monkeypatch.setattr(
        release_checks.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args, 0, stdout=b""),
    )
    assert release_checks.check_markdown(tmp_path) == []


def test_noncheckout_diagnostic_uses_c_locale_and_falls_back(tmp_path, monkeypatch):
    (tmp_path / "README.md").write_text("Source prose.")
    monkeypatch.setenv("LC_ALL", "de_AT.UTF-8")

    def outside_checkout(*args, **kwargs):
        assert kwargs["env"]["LC_ALL"] == "C"
        raise subprocess.CalledProcessError(
            128, "git", stderr=b"fatal: not a git repository (or any parent directory)"
        )

    monkeypatch.setattr(release_checks.subprocess, "run", outside_checkout)
    assert release_checks.tracked_markdown(tmp_path) == [tmp_path / "README.md"]


def test_source_scan_reports_walk_permission_errors(tmp_path, monkeypatch):
    def missing_git(*args, **kwargs):
        raise FileNotFoundError("git unavailable")

    def denied_walk(root, onerror=None):
        if onerror is not None:
            onerror(PermissionError("source directory unreadable"))
        return iter(())

    monkeypatch.setattr(release_checks.subprocess, "run", missing_git)
    monkeypatch.setattr(release_checks.os, "walk", denied_walk)
    assert release_checks.check_markdown(tmp_path) == [
        "Cannot check tracked Markdown: source directory unreadable"
    ]


def test_markdown_scan_reports_git_errors_other_than_missing_checkout(
    tmp_path, monkeypatch
):
    def denied(*args, **kwargs):
        raise subprocess.CalledProcessError(
            128, "git", stderr=b"fatal: detected dubious ownership in repository"
        )

    monkeypatch.setattr(release_checks.subprocess, "run", denied)
    errors = release_checks.check_markdown(tmp_path)
    assert len(errors) == 1
    assert errors[0].startswith("Cannot check tracked Markdown:")


def test_build_check_works_from_source_export(tmp_path, monkeypatch):
    for folder in ("scripts", "data", "schema"):
        shutil.copytree(
            ROOT / folder,
            tmp_path / folder,
            ignore=shutil.ignore_patterns("__pycache__", "migrate_references.py"),
        )
    for name in ("README.md", "CITATION.cff", ".zenodo.json"):
        shutil.copyfile(ROOT / name, tmp_path / name)
    monkeypatch.setenv("PATH", "")
    result = subprocess.run(
        [sys.executable, str(tmp_path / "scripts/build.py"), "--check"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "is valid" in result.stdout


@pytest.mark.parametrize("git_kind", ["directory", "file"])
@pytest.mark.parametrize("missing", ["README.md", "CLAUDE.md", "AGENTS.md"])
def test_checkout_requires_release_instruction_files(tmp_path, git_kind, missing):
    if git_kind == "directory":
        (tmp_path / ".git").mkdir()
    else:
        (tmp_path / ".git").write_text("gitdir: /example/worktree")
    for name in ("README.md", "CLAUDE.md", "AGENTS.md"):
        (tmp_path / name).write_text("Released dataset.")
    data = {"dataset": {"citation": "Released dataset."}}
    assert release_checks.check_launch_wording(data, {}, tmp_path) == []
    # Deliberate missing-file mutation; restoration must clear this exact error.
    path = tmp_path / missing
    held = tmp_path / (missing + ".held")
    path.rename(held)
    errors = release_checks.check_launch_wording(data, {}, tmp_path)
    assert len(errors) == 1
    assert errors[0].startswith(missing + ": cannot check release wording:")
    held.rename(path)
    assert release_checks.check_launch_wording(data, {}, tmp_path) == []


def test_shipped_markdown_links_resolve_in_the_public_export():
    """The export (git archive of the staged tree, honouring .gitattributes) leaves out the internal docs, and no shipped Markdown links to a file it lacks."""
    import re
    import tarfile
    from io import BytesIO

    if not (ROOT / ".git").exists() or shutil.which("git") is None:
        pytest.skip("the export needs a development checkout")

    def git(*args):
        return subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True, check=True).stdout

    tree = git("write-tree").decode().strip()
    with tarfile.open(fileobj=BytesIO(git("archive", "--format=tar", tree))) as archive:
        shipped = {m.name for m in archive.getmembers() if m.isfile()}
    assert not {"docs/review.md", "docs/decisions.md"} & shipped and not any(name.startswith("docs/history/") for name in shipped)
    assert {"README.md", "AGENTS.md", "LICENSE", "LICENSE-DATA", "CITATION.cff", ".zenodo.json"} <= shipped
    bad = []
    for name in sorted(n for n in shipped if n.endswith(".md")):
        text = (ROOT / name).read_text(encoding="utf-8")
        for target in re.findall(r"\]\(([^)\s]+)\)", text):
            path = target.split("#")[0]
            if not path or re.match(r"[a-z]+:", path):
                continue
            rel = Path(os.path.normpath(Path(name).parent / path)).as_posix()
            if rel not in shipped and not any(n.startswith(rel.rstrip("/") + "/") for n in shipped):
                bad.append(f"{name} -> {target}")
    assert bad == []
