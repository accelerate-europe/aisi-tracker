# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Refuse internal names in a public source export before it is published.

  uv run scripts/export_scan.py EXPORT_DIR                  # terms from the private store
  uv run scripts/export_scan.py EXPORT_DIR --terms FILE
  uv run scripts/export_scan.py EXPORT_DIR --store DIR      # the source store, in place of AISI_SNAPSHOTS

Scans every text file under EXPORT_DIR (the output of `git archive`, so export-ignored files are already
gone) for three kinds of internal detail:

- a personal home path (/home/<name>/ or /Users/<name>/; CI's /home/runner and one-letter placeholders are allowed);
- a dated note citing a commit ID: a date and a 7 to 12 or 40 character hex commit ID on one line;
- a private term: one per line in the terms file, matched case-insensitively anywhere. Lines starting
  with # are comments. The file lives in the private store, never in the repository, so the export
  does not name what it hides: AISI_EXPORT_TERMS, else export-terms.txt beside the source store (--store,
  default AISI_SNAPSHOTS). This is the only place that resolves it; check.py passes --store.

Exit status: 0 clean, 1 findings, 3 the terms file is not configured or missing (the generic checks
still ran and found nothing). Standard library only, so check.py can run it without syncing.
"""

import argparse
import os
import re
import sys
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


def text_files(root: Path):
    for directory, dirs, files in os.walk(root):
        dirs[:] = sorted(name for name in dirs if name != ".git")
        for name in sorted(files):
            path = Path(directory) / name
            data = path.read_bytes()
            if b"\0" not in data[:8192]:
                yield path, data.decode("utf-8", errors="replace")


def scan(root: Path, terms: list[str]) -> list[str]:
    """One finding per match: path:line: kind 'match'."""
    term = re.compile("|".join(re.escape(t) for t in terms), re.I) if terms else None
    findings = []
    for path, text in text_files(root):
        where = path.relative_to(root).as_posix()
        for number, line in enumerate(text.splitlines(), 1):
            found = [("home path", m.group(0)) for m in HOME_PATH.finditer(line)]
            if DATE.search(line):
                found += [("dated note citing a commit ID", m.group(0)) for m in COMMIT_ID.finditer(line)]
            if term:
                found += [("private term", m.group(0)) for m in term.finditer(line)]
            findings += [f"{where}:{number}: {kind} {match!r}" for kind, match in found]
    return findings


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("export", type=Path, help="the exported tree to scan")
    ap.add_argument("--terms", type=Path, help="private terms file (default: AISI_EXPORT_TERMS, else export-terms.txt beside the source store)")
    ap.add_argument("--store", type=Path, help="the private source store (default: AISI_SNAPSHOTS)")
    args = ap.parse_args(argv)
    args.terms = args.terms or default_terms(args.store)
    if not args.export.is_dir():
        ap.error(f"{args.export} is not a directory")
    configured = args.terms is not None and args.terms.is_file()
    findings = scan(args.export, load_terms(args.terms) if configured else [])
    for finding in findings:
        print(finding)
    if findings:
        print(f"{len(findings)} internal names in the export; remove them before publishing", file=sys.stderr)
        return 1
    if not configured:
        where = f"not found at {args.terms}" if args.terms else "not configured (set AISI_EXPORT_TERMS or AISI_SNAPSHOTS)"
        print(f"generic checks clean; private terms file {where}", file=sys.stderr)
        return NOT_CONFIGURED
    print(f"clean: no home paths, dated commit notes or any of {len(load_terms(args.terms))} private terms in {args.terms}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
