"""The export scan, on hand-written trees. Every internal-looking string here is built by concatenation, so
this file itself passes the scan when it is exported."""

import subprocess
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


def test_scan_skips_binary_files_and_git_but_reads_every_text_file(tmp_path):
    root = tree(tmp_path, {"img.png": b"\0" + HOME.encode(), ".git/config": HOME, "deep/x.py": f"# {TERM}\n"})
    assert export_scan.scan(root, [TERM]) == [f"deep/x.py:1: private term {TERM!r}"]


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
    for name, content in {**files, "scripts/export_scan.py": Path(export_scan.__file__).read_text()}.items():
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / name).write_text(content)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    work = tmp_path / "work"
    check.make_snapshot(repo, work / "snapshot")
    return check.Context(repo, work / "snapshot", work, tmp_path / "report", "HEAD", True, work / "dist", tmp_path,
                         env=check.clean_env(), snapshots=store / "sources" if store else None)


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
