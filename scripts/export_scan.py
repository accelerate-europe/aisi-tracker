# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Refuse internal names in a public source export or commits before publication.

  uv run scripts/export_scan.py EXPORT_DIR                  # terms from the private store
  uv run scripts/export_scan.py EXPORT_DIR --terms FILE
  uv run scripts/export_scan.py EXPORT_DIR --store DIR      # the source store, in place of AISI_SNAPSHOTS
  uv run scripts/export_scan.py --repo DIR --commits A..B   # rev-list arguments, also B ^C ^D

Scans every file under EXPORT_DIR (the output of `git archive`, so export-ignored files are already
gone) for three kinds of internal detail:

- a personal home path (/home/<name>/ or /Users/<name>/; CI's /home/runner and one-letter placeholders are allowed);
- a dated note citing a commit ID: a date and a 7 to 12 or 40 character hex commit ID on one line;
- a private term: one per line in the terms file, matched case-insensitively anywhere. Lines starting
  with # are comments. The file lives in the private store, never in the repository, so the export
  does not name what it hides: AISI_EXPORT_TERMS, else export-terms.txt beside the source store (--store,
  default AISI_SNAPSHOTS). This is the only place that resolves it; check.py passes --store.

File names are scanned for private terms too. Binary files get the home-path rule and private terms only.

Commit mode scans every added or modified blob, including export-ignored files, at <short commit>/<path>.
Added or changed export-ignored paths are also reported as private files. Full commit messages, including trailers,
are scanned for home paths and private terms only: dates beside commit IDs are normal in messages.
--ref-names adds the names of the refs being pushed, scanned the same way as messages.

Exit status: 0 clean, 1 findings, 3 the terms file is not configured or missing (the generic checks
still ran and found nothing). Standard library only, so check.py can run it without syncing.
"""

import argparse
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

# A one-letter name (/Users/x) is a placeholder in test fixtures; /home/runner is GitHub's CI account, exactly.
HOME_PATH = re.compile(r"(?<![\w.-])/(?:home/(?!runner(?![\w.-]))|Users/)[a-z_][\w.-]+", re.I)
# Each form has its own boundaries, so a version number such as 1.0.5 is not a date.
DATE = re.compile(
    r"(?<![\d.-])\d{4}-\d{2}-\d{2}(?![\d-])"  # 2026-11-08
    r"|(?<![\d.])(?:0?[1-9]|[12]\d|3[01])\.(?:0?[1-9]|1[0-2])\.(?:\d{2}|\d{4})?(?![\d.])"  # 8.11. or 08.11.2026
    r"|(?<![\d.])(?:0[1-9]|[12]\d|3[01])\.(?:0[1-9]|1[0-2])(?![\d.])"  # 08.11 without the closing dot
    r"|\b\d{1,2} (?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\b"  # 8 Nov, 8 November
)
# A short or full commit ID: hex with at least one letter and one digit, standing alone. Content hashes
# (64 hex) and longer runs inside minified code do not match.
COMMIT_ID = re.compile(r"(?<![\w/#=:.-])(?=[0-9a-f]*[a-f])(?=[0-9a-f]*\d)(?:[0-9a-f]{7,12}|[0-9a-f]{40})(?![\w-])")
NOT_CONFIGURED = 3


def default_terms(store: Path | None = None) -> Path | None:
    """AISI_EXPORT_TERMS, else export-terms.txt beside the source store (store, else AISI_SNAPSHOTS)."""
    if os.environ.get("AISI_EXPORT_TERMS"):
        return Path(os.environ["AISI_EXPORT_TERMS"]).expanduser()
    if store is None and os.environ.get("AISI_SNAPSHOTS"):
        store = Path(os.environ["AISI_SNAPSHOTS"])
    return store.expanduser().parent / "export-terms.txt" if store else None


def load_terms(path: Path) -> list[str]:
    lines = (line.strip() for line in path.read_text(encoding="utf-8").splitlines())
    return [line for line in lines if line and not line.startswith("#")]


def files(root: Path):
    """Every file under root as (path, text, binary). Binary files (a NUL in the first 8 KiB) are read byte for byte."""
    for directory, dirs, names in os.walk(root):
        dirs[:] = sorted(name for name in dirs if name != ".git")
        for name in sorted(names):
            path = Path(directory) / name
            data = path.read_bytes()
            binary = b"\0" in data[:8192]
            # A binary file is read twice, byte for byte and as UTF-8, so a non-ASCII term in its metadata matches too.
            text = data.decode("latin-1") + "\n" + data.decode("utf-8", errors="replace") if binary else data.decode("utf-8", errors="replace")
            yield path, text, binary


def scan_text(where: str, text: str, term: re.Pattern | None, *, dated_notes: bool) -> list[str]:
    findings = []
    for number, line in enumerate(text.splitlines(), 1):
        found = [("home path", m.group(0)) for m in HOME_PATH.finditer(line)]
        if dated_notes and DATE.search(line):
            found += [("dated note citing a commit ID", m.group(0)) for m in COMMIT_ID.finditer(line)]
        if term:
            found += [("private term", m.group(0)) for m in term.finditer(line)]
        findings += [f"{where}:{number}: {kind} {match!r}" for kind, match in found]
    return findings


def scan(root: Path, terms: list[str], *, dated_notes: bool = True, prefix_depth: int = 0) -> list[str]:
    """One finding per match: path:line: kind 'match'. File names are scanned for private terms too (line 0),
    leaving out the first prefix_depth parts (commit mode's short commit). Binary files get only the home-path
    rule and private terms: a dated-note match in compressed bytes would be noise."""
    term = re.compile("|".join(re.escape(t) for t in terms), re.I) if terms else None
    findings = []
    for path, text, binary in files(root):
        where = path.relative_to(root).as_posix()
        name = "/".join(path.relative_to(root).parts[prefix_depth:])
        if term:
            findings += [f"{where}:0: private term in the path {m.group(0)!r}" for m in term.finditer(name)]
        found = scan_text(where, text, term, dated_notes=dated_notes and not binary)
        if binary:  # read twice: each match once, at its first line
            first = {}
            for finding in found:
                first.setdefault(finding.split(": ", 1)[1], finding)
            found = list(first.values())
        findings += found
    return findings


def scan_names(names: list[str], terms: list[str]) -> list[str]:
    """Ref names being pushed: home paths and private terms."""
    term = re.compile("|".join(re.escape(t) for t in terms), re.I) if terms else None
    return [finding for name in names for finding in scan_text(f"ref {name}", name, term, dated_notes=False)]


def git(repo: Path, *args: str, env: dict | None = None, input: bytes | None = None) -> bytes:
    return subprocess.run(["git", "-C", str(repo), *args], env=env, input=input, capture_output=True, check=True).stdout


def scan_commits(repo: Path, revisions: list[str], terms: list[str]) -> list[str]:
    """Scan full changed blobs, changed private paths and messages, using each commit's attributes."""
    findings = []
    commits = git(repo, "rev-list", *revisions, "--").decode().splitlines()
    with tempfile.TemporaryDirectory(prefix="aisi-commits-") as tmp:
        root = Path(tmp)
        blobs, messages = root / "blobs", root / "messages"
        blobs.mkdir()
        messages.mkdir()
        env = {**os.environ, "GIT_INDEX_FILE": str(root / "attributes.index")}
        for commit in commits:
            short = git(repo, "rev-parse", "--short", commit).decode().strip()
            (messages / f"message {short}").write_bytes(git(repo, "show", "-s", "--format=%B", commit))
            # Compare merges to every parent, and treat renames as deletion plus addition.
            raw = git(repo, "diff-tree", "--root", "-m", "-r", "--no-commit-id", "--raw", "--no-abbrev",
                      "--no-renames", "--diff-filter=AMT", "-z", commit).split(b"\0")
            changed = {}
            for header, name in zip(raw[0::2], raw[1::2]):
                _, mode, _, blob, _ = header.decode().split()
                # A gitlink names a commit, not a blob: its path is still checked, as an empty file.
                changed[name.decode("utf-8", "surrogateescape")] = None if mode == "160000" else blob
            for path, blob in changed.items():
                dest = blobs / short / path
                dest.parent.mkdir(parents=True, exist_ok=True)
                # Symlink contents are scanned as ordinary bytes, never followed.
                dest.write_bytes(git(repo, "cat-file", "blob", blob) if blob else b"")
            if changed:
                # Use this commit's attributes from a temporary index, never the working tree's.
                git(repo, "read-tree", commit, env=env)
                candidates = set(changed)
                for path in changed:
                    candidates.update(f"{p.as_posix()}/" for p in Path(path).parents if p != Path("."))
                attrs = git(repo, "check-attr", "--cached", "-z", "--stdin", "export-ignore", env=env,
                            input=b"\0".join(p.encode("utf-8", "surrogateescape") for p in sorted(candidates)) + b"\0").split(b"\0")
                ignored = {p.decode("utf-8", "surrogateescape") for p, _, value in zip(attrs[0::3], attrs[1::3], attrs[2::3])
                           if value == b"set"}
                # Directory attributes apply to archive traversal, not automatically to child paths.
                for path in sorted(changed):
                    if path in ignored or any(f"{p.as_posix()}/" in ignored for p in Path(path).parents):
                        findings.append(f"{short}/{path}:1: private file (export-ignore)")
        return findings + scan(blobs, terms, prefix_depth=1) + scan(messages, terms, dated_notes=False, prefix_depth=1)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("export", type=Path, nargs="?", help="the exported tree to scan")
    ap.add_argument("--repo", type=Path, help="repository containing the commits")
    ap.add_argument("--commits", nargs="+", metavar="REV", help="scan commits selected by these rev-list arguments")
    ap.add_argument("--ref-names", nargs="+", default=[], metavar="REF", help="commit mode: also scan these ref names (the refs being pushed)")
    ap.add_argument("--terms", type=Path, help="private terms file (default: AISI_EXPORT_TERMS, else export-terms.txt beside the source store)")
    ap.add_argument("--store", type=Path, help="the private source store (default: AISI_SNAPSHOTS)")
    args = ap.parse_args(argv)
    args.terms = args.terms or default_terms(args.store)
    if bool(args.repo) != bool(args.commits) or bool(args.export) == bool(args.commits):
        ap.error("give an exported tree, or --repo DIR --commits REV [REV ...]")
    if args.export and not args.export.is_dir():
        ap.error(f"{args.export} is not a directory")
    configured = args.terms is not None and args.terms.is_file()
    terms = load_terms(args.terms) if configured else []
    findings = (scan_names(args.ref_names, terms) + scan_commits(args.repo, args.commits, terms)) if args.commits else scan(args.export, terms)
    for finding in findings:
        print(finding)
    if findings:
        print(f"{len(findings)} findings in the {'commits' if args.commits else 'export'}; remove them before publishing", file=sys.stderr)
        return 1
    if not configured:
        where = f"not found at {args.terms}" if args.terms else "not configured (set AISI_EXPORT_TERMS or AISI_SNAPSHOTS)"
        print(f"generic checks clean; private terms file {where}", file=sys.stderr)
        return NOT_CONFIGURED
    print(f"clean: no home paths, dated commit notes or any of {len(terms)} private terms in {args.terms}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
