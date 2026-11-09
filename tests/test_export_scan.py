"""The export scan, on hand-written trees. Every internal-looking string here is built by concatenation, so
this file itself passes the scan when it is exported."""

import os
import subprocess
import shutil
from pathlib import Path

import pytest

from scripts import check, export_scan

HOME = "/ho" + "me/alice"  # the match ends at the user name
COMMIT = "9adf" + "338"
TERM = "example" + "host"


def tree(tmp_path: Path, files: dict[str, str | bytes]) -> Path:
    root = tmp_path / "export"
    for name, content in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content if isinstance(content, bytes) else content.encode("utf-8"))
    return root


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        (f"cache under {HOME}/Code/tracker/work", [f"a.md:1: home path {HOME!r}"]),
        ("/Us" + "ers/bob/x", ["a.md:1: home path '/Us" + "ers/bob'"]),
        ("CI writes to /home/runner/work", []),  # GitHub's runner home is public
        ("/ho" + "me/runner-private/x", ["a.md:1: home path '/ho" + "me/runner-private'"]),
        ("/ho" + "me/runner.personal/x", ["a.md:1: home path '/ho" + "me/runner.personal'"]),
        ("/Us" + "ers/runner/x", ["a.md:1: home path '/Us" + "ers/runner'"]),  # the exemption is the Linux CI account only
        ("see /home/ for the mount", []),  # no user name
        ('("real", "/Us' + 'ers/x/Code/ae/tracker"),', []),  # a one-letter placeholder in a fixture
        (f"usually stale (08.11: --base main named a local {COMMIT} while", [f"a.md:1: dated note citing a commit ID {COMMIT!r}"]),
        (f"2026-11-08 merged {COMMIT}", [f"a.md:1: dated note citing a commit ID {COMMIT!r}"]),
        (f"on 8 Nov the fix {COMMIT} landed", [f"a.md:1: dated note citing a commit ID {COMMIT!r}"]),
        (f"the fix {COMMIT} landed", []),  # a commit ID without a date
        (f"8.11. fix {COMMIT}", [f"a.md:1: dated note citing a commit ID {COMMIT!r}"]),
        (f"08.11.2026 fix {COMMIT}", [f"a.md:1: dated note citing a commit ID {COMMIT!r}"]),
        (f"version 1.0.5 uses {COMMIT}", []),  # a version number is not a date
        (f"version 1.0.15 and 10.05.3 use {COMMIT}", []),
        ("2026-11-08 sha256 " + "ab12" * 16, []),  # a content hash is not a commit ID
        ("2026-11-08 deadbeef", []),  # hex without a digit is a word
        ("2026-11-08 version 1234567", []),  # digits without a letter are a number
        (f"run on {TERM.upper()} nightly", [f"a.md:1: private term {TERM.upper()!r}"]),
        ("nothing internal here", []),
    ],
)
def test_scan_handworked(tmp_path, line, expected):
    assert export_scan.scan(tree(tmp_path, {"a.md": line + "\n"}), [TERM]) == expected


def test_scan_skips_git_but_reads_every_other_file(tmp_path):
    root = tree(tmp_path, {"img.png": b"\0" + HOME.encode(), ".git/config": HOME, "deep/x.py": f"# {TERM}\n"})
    assert export_scan.scan(root, [TERM]) == [f"img.png:1: home path {HOME!r}", f"deep/x.py:1: private term {TERM!r}"]


def test_terms_file_comments_and_blanks(tmp_path):
    terms = tmp_path / "terms.txt"
    terms.write_text(f"# private names\n\n  {TERM}  \n")
    assert export_scan.load_terms(terms) == [TERM]


def test_cli_exit_codes(tmp_path, capsys):
    clean = tree(tmp_path, {"a.md": "nothing\n"})
    terms = tmp_path / "terms.txt"
    terms.write_text(TERM + "\n")
    assert export_scan.main([str(clean), "--terms", str(terms)]) == 0
    assert export_scan.main([str(clean), "--terms", str(tmp_path / "absent.txt")]) == export_scan.NOT_CONFIGURED
    (clean / "b.md").write_text(TERM)
    assert export_scan.main([str(clean), "--terms", str(terms)]) == 1
    # Without the terms file a generic finding still fails rather than reporting NOT RUN.
    (clean / "b.md").write_text(HOME)
    assert export_scan.main([str(clean), "--terms", str(tmp_path / "absent.txt")]) == 1
    assert f"b.md:1: home path {HOME!r}" in capsys.readouterr().out


def test_default_terms_file_sits_beside_the_snapshot_store(tmp_path, monkeypatch):
    monkeypatch.delenv("AISI_EXPORT_TERMS", raising=False)
    monkeypatch.delenv("AISI_SNAPSHOTS", raising=False)
    assert export_scan.default_terms() is None
    monkeypatch.setenv("AISI_SNAPSHOTS", str(tmp_path / "store/sources"))
    assert export_scan.default_terms() == tmp_path / "store/export-terms.txt"
    assert export_scan.default_terms(tmp_path / "other/sources") == tmp_path / "other/export-terms.txt"  # --store wins
    monkeypatch.setenv("AISI_EXPORT_TERMS", str(tmp_path / "t.txt"))
    assert export_scan.default_terms() == tmp_path / "t.txt"


def export_ctx(tmp_path: Path, files: dict[str, str], store: Path | None) -> check.Context:
    """A staged repository with these files plus the scanner, exported the way check.py does."""
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    for name, content in {**files, "scripts/export_scan.py": Path(export_scan.__file__).read_text(),
                          ".gitleaks.toml": (check.ROOT / ".gitleaks.toml").read_text()}.items():
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / name).write_text(content)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    work = tmp_path / "work"
    check.make_snapshot(repo, work / "snapshot")
    return check.Context(repo, work / "snapshot", work, tmp_path / "report", "HEAD", True, work / "dist", tmp_path,
                         env=check.clean_env(), snapshots=store / "sources" if store else None)


@pytest.mark.skipif(shutil.which("gitleaks") is None, reason="gitleaks is not on PATH")
def test_export_step_scans_only_what_git_archive_exports(tmp_path, monkeypatch):
    monkeypatch.delenv("AISI_EXPORT_TERMS", raising=False)
    store = tmp_path / "store"
    store.mkdir()
    (store / "export-terms.txt").write_text(TERM + "\n")
    files = {".gitattributes": "notes/ export-ignore\n", "notes/private.md": TERM, "README.md": "Public.\n"}
    assert check.step_export(export_ctx(tmp_path / "clean", files, store)).status == "PASS"
    result = check.step_export(export_ctx(tmp_path / "leak", {**files, "README.md": f"Built on {TERM}.\n"}, store))
    assert result.status == "FAIL" and f"README.md:1: private term {TERM!r}" in result.detail


def test_export_step_is_not_run_without_the_terms_file(tmp_path, monkeypatch):
    # The scanner's own default reads both variables, so a maintainer's environment must not leak in.
    monkeypatch.delenv("AISI_EXPORT_TERMS", raising=False)
    monkeypatch.delenv("AISI_SNAPSHOTS", raising=False)
    result = check.step_export(export_ctx(tmp_path, {"README.md": "Public.\n"}, None))
    assert result.status == "NOT RUN" and "private terms file not configured" in result.detail



def test_preflight_reads_the_terms_file_through_the_scanner(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("AISI_EXPORT_TERMS", raising=False)
    monkeypatch.delenv("AISI_SNAPSHOTS", raising=False)
    store = tmp_path / "store"
    (store / "sources").mkdir(parents=True)
    check.preflight(tmp_path, store / "sources")
    assert "| private terms file (export step) | NO | generic checks clean; private terms file not found at" in capsys.readouterr().out
    (store / "export-terms.txt").write_text(TERM + "\n")
    check.preflight(tmp_path, store / "sources")
    assert f"| private terms file (export step) | yes | clean: no home paths, dated commit notes or any of 1 private terms in {store / 'export-terms.txt'} |" in capsys.readouterr().out


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True).stdout.decode().strip()


def commit(repo, files, message="Public change"):
    for name, content in files.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    git(repo, "add", "-A")
    git(repo, "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-q", "--allow-empty", "-m", message)
    return git(repo, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q")
    return root


def test_commits_scan_full_changed_blobs_and_messages_but_not_older_blobs(repo):
    before = commit(repo, {"old.txt": TERM, "modified.txt": "Public\n"})
    after = commit(repo, {"new.txt": TERM, "modified.txt": HOME}, f"Public change\n\nReviewed-by: {TERM}")
    short = git(repo, "rev-parse", "--short", after)
    assert set(export_scan.scan_commits(repo, [f"{before}..{after}"], [TERM])) == {
        f"{short}/new.txt:1: private term {TERM!r}",
        f"{short}/modified.txt:1: home path {HOME!r}",
        f"message {short}:3: private term {TERM!r}",
    }
    assert export_scan.scan_commits(repo, [after, f"^{before}"], [TERM]) == export_scan.scan_commits(repo, [f"{before}..{after}"], [TERM])


def test_dated_commit_notes_are_allowed_only_in_messages(repo):
    note = f"2026-11-08 merged {COMMIT}"
    commit(repo, {"note.txt": note}, f"{note}\n\nPath: {HOME}")
    short = git(repo, "rev-parse", "--short", "HEAD")
    assert export_scan.scan_commits(repo, ["HEAD"], []) == [
        f"{short}/note.txt:1: dated note citing a commit ID {COMMIT!r}",
        f"message {short}:3: home path {HOME!r}",
    ]


def test_term_only_in_a_commit_message_is_found(repo):
    commit(repo, {"public.txt": "Public\n"}, f"Reviewed on {TERM}")
    short = git(repo, "rev-parse", "--short", "HEAD")
    assert export_scan.scan_commits(repo, ["HEAD"], [TERM]) == [f"message {short}:1: private term {TERM!r}"]


def test_merge_blobs_are_scanned_once_even_with_excluded_parents(repo):
    base = commit(repo, {"note.txt": "Public\n"})
    git(repo, "checkout", "-qb", "side")
    side = commit(repo, {"note.txt": TERM})
    git(repo, "checkout", "-qb", "mainline", base)
    main = commit(repo, {"other.txt": "Public\n"})
    git(repo, "merge", "--no-ff", "--no-commit", "side")
    merged = commit(repo, {}, "Merge changes")
    short = git(repo, "rev-parse", "--short", merged)
    assert export_scan.scan_commits(repo, [merged, f"^{side}", f"^{main}"], [TERM]) == [f"{short}/note.txt:1: private term {TERM!r}"]


@pytest.mark.parametrize("path", ["docs/review.md", "docs/history/nested/note.md", "data/internal/doors.json"])
def test_added_export_ignored_paths_use_attributes_from_the_commit(repo, path):
    commit(repo, {".gitattributes": "docs/review.md export-ignore\ndocs/history/ export-ignore\ndata/internal/ export-ignore\n", path: TERM})
    short = git(repo, "rev-parse", "--short", "HEAD")
    (repo / ".gitattributes").write_text("")  # working changes cannot remove the private-file guard
    assert export_scan.scan_commits(repo, ["HEAD"], [TERM]) == [
        f"{short}/{path}:1: private file (export-ignore)", f"{short}/{path}:1: private term {TERM!r}",
    ]


def test_a_modified_export_ignored_file_is_a_private_file_too(repo):
    before = commit(repo, {".gitattributes": "docs/review.md export-ignore\n", "docs/review.md": "Old\n"})
    commit(repo, {"docs/review.md": "New\n"})
    short = git(repo, "rev-parse", "--short", "HEAD")
    assert export_scan.scan_commits(repo, [f"{before}..HEAD"], []) == [f"{short}/docs/review.md:1: private file (export-ignore)"]


def test_commits_scan_a_blob_even_if_it_was_later_removed(repo):
    before = commit(repo, {"public.txt": "Public\n"})
    leaked = commit(repo, {"gone.txt": TERM})
    git(repo, "update-index", "--force-remove", "gone.txt")
    git(repo, "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-qm", "Remove file")
    short = git(repo, "rev-parse", "--short", leaked)
    assert export_scan.scan_commits(repo, [f"{before}..HEAD"], [TERM]) == [f"{short}/gone.txt:1: private term {TERM!r}"]


def test_commits_scan_symlink_contents_without_following_them(repo):
    (repo / "link").symlink_to(HOME + "/unavailable")
    commit(repo, {})
    short = git(repo, "rev-parse", "--short", "HEAD")
    assert export_scan.scan_commits(repo, ["HEAD"], []) == [f"{short}/link:1: home path {HOME!r}"]


def test_commit_cli_without_terms_still_runs_generic_checks(repo, monkeypatch, capsys):
    monkeypatch.delenv("AISI_EXPORT_TERMS", raising=False)
    monkeypatch.delenv("AISI_SNAPSHOTS", raising=False)
    commit(repo, {"clean.txt": "Public\n"})
    assert export_scan.main(["--repo", str(repo), "--commits", "HEAD"]) == 3
    assert "generic checks clean" in capsys.readouterr().err
    commit(repo, {"path.txt": HOME})
    assert export_scan.main(["--repo", str(repo), "--commits", "HEAD"]) == 1


def test_missing_gitleaks_does_not_hide_a_names_failure(tmp_path, monkeypatch):
    monkeypatch.delenv("AISI_EXPORT_TERMS", raising=False)
    monkeypatch.delenv("AISI_SNAPSHOTS", raising=False)
    monkeypatch.setattr(check.shutil, "which", lambda *a, **kw: None)
    result = check.step_export(export_ctx(tmp_path, {"README.md": HOME}, None))
    assert result.status == "FAIL" and "names scan FAIL" in result.detail and "gitleaks NOT RUN" in result.detail


@pytest.mark.skipif(shutil.which("gitleaks") is None, reason="gitleaks is not on PATH")
def test_gitleaks_tree_respects_dataset_allowlist_but_finds_secrets_elsewhere(tmp_path, monkeypatch):
    monkeypatch.delenv("AISI_EXPORT_TERMS", raising=False)
    monkeypatch.delenv("AISI_SNAPSHOTS", raising=False)
    fingerprint = '  "testing-and-access": "' + "a17bc492e3d05f68" * 4 + '",\n'
    clean = check.step_export(export_ctx(tmp_path / "clean", {"data/aisi.json": fingerprint}, None))
    assert clean.status == "NOT RUN" and clean.missing_terms_only
    leaked = check.step_export(export_ctx(tmp_path / "leak", {"other.json": fingerprint}, None))
    assert leaked.status == "FAIL" and "gitleaks tree FAIL" in leaked.detail


@pytest.mark.skipif(shutil.which("gitleaks") is None, reason="gitleaks is not on PATH")
@pytest.mark.parametrize("location", ["file", "message", "merge"])
def test_gitleaks_commit_step_finds_secrets_in_files_and_messages(tmp_path, monkeypatch, location):
    monkeypatch.delenv("AISI_EXPORT_TERMS", raising=False)
    monkeypatch.delenv("AISI_SNAPSHOTS", raising=False)
    ctx = export_ctx(tmp_path, {"README.md": "Public\n"}, None)
    base = commit(ctx.repo, {})
    if location == "merge":
        git(ctx.repo, "checkout", "-qb", "side")
        commit(ctx.repo, {"side.txt": "Public\n"})
        git(ctx.repo, "checkout", "-qb", "mainline", base)
        base = commit(ctx.repo, {"main.txt": "Public\n"})
        git(ctx.repo, "merge", "--no-ff", "--no-commit", "side")
    # Synthetic token, assembled so this regression fixture has no credential to flag.
    secret = "gh" + "p_" + "aB3dE5fG7hI9jK1mN3pQ5rS7tU9vW1xY3zA5"
    commit(ctx.repo, {} if location == "message" else {"token.txt": secret}, secret if location == "message" else "Public change")
    ctx.commits = [f"{base}..HEAD"]
    result = check.step_export(ctx)
    assert result.status == "FAIL"
    assert f"gitleaks {'messages' if location == 'message' else 'commits'} FAIL" in result.detail
    assert secret not in result.detail  # --redact applies to every gitleaks call


def test_a_private_term_only_in_a_file_name_is_found(repo, tmp_path):
    commit(repo, {f"docs/{TERM}-notes.md": "Public\n"})
    short = git(repo, "rev-parse", "--short", "HEAD")
    assert export_scan.scan_commits(repo, ["HEAD"], [TERM]) == [f"{short}/docs/{TERM}-notes.md:0: private term in the path {TERM!r}"]
    assert export_scan.scan(tree(tmp_path, {f"{TERM}.txt": "Public\n"}), [TERM]) == [f"{TERM}.txt:0: private term in the path {TERM!r}"]


def test_binary_files_get_terms_and_home_paths_but_not_dated_notes(tmp_path):
    blob = b"\0\x89PNG" + f"{TERM} {HOME}/x 2026-11-08 {COMMIT}".encode()
    assert export_scan.scan(tree(tmp_path, {"img.png": blob}), [TERM]) == [
        f"img.png:1: home path {HOME!r}", f"img.png:1: private term {TERM!r}",
    ]


def test_ref_names_are_scanned_for_terms(repo, monkeypatch, capsys):
    terms = repo.parent / "terms.txt"
    terms.write_text(TERM + "\n")
    commit(repo, {"public.txt": "Public\n"})
    assert export_scan.main(["--repo", str(repo), "--terms", str(terms), "--ref-names", f"refs/heads/{TERM}-fix", "--commits", "HEAD"]) == 1
    assert f"ref refs/heads/{TERM}-fix:1: private term {TERM!r}" in capsys.readouterr().out


@pytest.mark.skipif(shutil.which("gitleaks") is None, reason="gitleaks is not on PATH")
def test_gitleaks_commit_step_reads_revisions_with_shell_characters(tmp_path, monkeypatch):
    monkeypatch.delenv("AISI_EXPORT_TERMS", raising=False)
    monkeypatch.delenv("AISI_SNAPSHOTS", raising=False)
    ctx = export_ctx(tmp_path, {"README.md": "Public\n"}, None)
    commit(ctx.repo, {})
    secret = "gh" + "p_" + "aB3dE5fG7hI9jK1mN3pQ5rS7tU9vW1xY3zA5"
    commit(ctx.repo, {"token.txt": secret})
    ctx.commits = ["HEAD~1..HEAD"]  # a tilde, which shell quoting would wrap in quotes that git then refuses
    result = check.step_export(ctx)
    assert result.status == "FAIL" and "gitleaks commits FAIL" in result.detail


def test_a_gitleaks_error_line_fails_even_with_exit_status_0(tmp_path, monkeypatch):
    monkeypatch.delenv("AISI_EXPORT_TERMS", raising=False)
    monkeypatch.delenv("AISI_SNAPSHOTS", raising=False)
    ctx = export_ctx(tmp_path, {"README.md": "Public\n"}, None)
    stub = tmp_path / "bin/gitleaks"
    stub.parent.mkdir()
    stub.write_text("#!/bin/sh\necho '10:00PM ERR [git] fatal: bad revision'\necho '10:00PM INF no leaks found'\n")
    stub.chmod(0o755)
    ctx.env = {**ctx.env, "PATH": f"{stub.parent}{os.pathsep}{os.environ['PATH']}"}
    result = check.step_export(ctx)
    assert result.status == "FAIL" and "gitleaks tree FAIL" in result.detail


def test_a_gitlink_path_is_checked_for_private_terms(repo):
    commit(repo, {"README.md": "Public\n"})
    target = git(repo, "rev-parse", "HEAD")
    git(repo, "update-index", "--add", "--cacheinfo", f"160000,{target},vendor/{TERM}")
    git(repo, "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-qm", "Add a submodule")
    short = git(repo, "rev-parse", "--short", "HEAD")
    assert export_scan.scan_commits(repo, ["HEAD~1..HEAD"], [TERM]) == [f"{short}/vendor/{TERM}:0: private term in the path {TERM!r}"]


def test_a_non_ascii_term_in_a_binary_file_is_found_once(tmp_path):
    term = "caf" + "\u00e9-" + "intern"
    blob = b"\0meta " + term.encode("utf-8") + b" " + TERM.encode()
    assert export_scan.scan(tree(tmp_path, {"img.png": blob}), [term, TERM]) == [
        f"img.png:1: private term {TERM!r}", f"img.png:2: private term {term!r}",
    ]
