#!/usr/bin/env python3
"""Research passes: extract and validate pass folders in the private store.

A pass is one dedicated research session with its own folder, keyed by pass ID
(docs/research-passes.md has the process and the folder layout). `extract`
copies the pass's transcripts into its folder and writes its log with
scripts/research_log.py; `validate` checks the pass file, the findings and that
every logged execution belongs to a session the pass names. Internal tooling:
nothing here enters the published data or the build.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from jsonschema import Draft202012Validator

try:
    from . import research_log
except ImportError:
    import research_log  # type: ignore[no-redef]  # Direct script entry point.

Json = dict[str, Any]
ROOT = Path(__file__).resolve().parents[1]
PASS_FILE = "pass.json"
LOG_FILE = "log.jsonl"
FINDINGS_FILE = "findings.jsonl"
LOG_FORMAT = research_log.FORMAT
DIAGNOSTICS_FILE = "log.diagnostics.json"
PASS_ID = r"^[0-9]{4}-[0-9]{2}-[0-9]{2}-[a-z0-9]+(-[a-z0-9]+)*$"
TRAILER = "Research-pass"
NONE_TRAILER = re.compile(r"^none \((.+)\)$")
# Commits authored before research passes began are not checked for a trailer.
PASSES_BEGIN = datetime.fromisoformat("2026-11-07T00:00:00+01:00")
RECORD_LISTS = ("jurisdictions", "institutes", "sources")
NOT_RUN = 3  # exit code: a named pass could not be checked, the store is not configured

# Engine per search tool, filled only where the tool itself is the engine or
# reports it; every other tool's engine stays null (never inferred).
KNOWN_ENGINES = {
    "mcp__exa__web_search_exa": "exa",
}

PASS_TYPES = ["full-recheck", "news-sweep", "access-check", "field-fill", "targeted"]

def personal_profile(url: str) -> bool:
    """A person's own LinkedIn page, never a source or a finding; organisation pages (linkedin.com/company/)
    and official accounts on other platforms stay allowed."""
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return False
    host = (parts.hostname or "").casefold()
    first = parts.path.strip("/").split("/")[0].casefold()
    return (host == "linkedin.com" or host.endswith(".linkedin.com")) and first in {"in", "pub"}

_ids = {"type": "array", "items": {"type": "string", "minLength": 1}, "uniqueItems": True}
_when = {"type": "string", "minLength": 10}

PASS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["pass_id", "who", "harness", "models", "sessions", "started", "ended", "type",
                 "scope", "search_tools", "stopping_reason", "status"],
    "properties": {
        "pass_id": {"type": "string", "pattern": PASS_ID},
        "who": {"type": "string", "minLength": 1, "description": "the person who ran the pass"},
        "harness": {
            "type": "object",
            "additionalProperties": False,
            "required": ["name", "version"],
            "properties": {"name": {"enum": ["claude_code", "codex"]},  # research_log.py names
                           "version": {"type": "string", "minLength": 1}},
        },
        "models": {**_ids, "description": "model IDs that made calls, including subagents"},
        "sessions": {**_ids, "description": "native session IDs; the log's session_id values"},
        "started": _when,
        "ended": {"anyOf": [_when, {"type": "null"}]},
        "type": {"enum": PASS_TYPES},
        "scope": {
            "type": "object",
            "additionalProperties": False,
            "required": ["states", "institutes", "fields"],
            "properties": {"states": _ids, "institutes": _ids, "fields": _ids},
        },
        "search_tools": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["tool", "engine"],
                "properties": {"tool": {"type": "string", "minLength": 1},
                               "engine": {"type": ["string", "null"]},
                               "options": {"type": "string"}},
            },
        },
        "stopping_reason": {"type": ["string", "null"]},
        "status": {"enum": ["open", "closed"]},
        "notes": {"type": "string"},
    },
}

FINDING_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["url", "scope", "verdict", "reason"],
    "properties": {
        "url": {"type": "string", "pattern": "^https?://"},
        "scope": {"type": "string", "minLength": 1, "description": "a state, institute or field in the pass scope"},
        "verdict": {"enum": ["candidate", "rejected"]},
        "reason": {"type": "string", "minLength": 1},
        "excerpt": {"type": "string"},
        "title": {"type": "string"},
        "found_by": {"type": "string", "description": "the query or page that led here, as recorded"},
    },
}


# The fields of a research_log.py event that the pass checks read.
EVENT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["format_version", "session_id", "model", "harness", "tool", "operations"],
    "properties": {
        "format_version": {"type": "string"},
        "session_id": {"type": "string"},
        "model": {"type": ["string", "null"]},
        "harness": {"type": "object", "required": ["name", "version"],
                    "properties": {"name": {"type": "string"}, "version": {"type": ["string", "null"]}}},
        "tool": {"type": "object", "required": ["name"], "properties": {"name": {"type": "string"}}},
        "operations": {"type": "array", "items": {"type": "object", "required": ["kind"], "properties": {"kind": {"type": "string"}}}},
    },
}


def store_directory(explicit: Path | None = None) -> Path | None:
    """--store, else AISI_PASSES, else passes/ beside the AISI_SNAPSHOTS source store."""
    if explicit is not None:
        return explicit
    if os.environ.get("AISI_PASSES"):
        return Path(os.environ["AISI_PASSES"]).expanduser()
    if os.environ.get("AISI_SNAPSHOTS"):
        return Path(os.environ["AISI_SNAPSHOTS"]).expanduser().parent / "passes"
    return None


def _day(value: str) -> date | None:
    try:
        return datetime.fromisoformat(value).date() if "T" in value else date.fromisoformat(value)
    except ValueError:
        return None


def _jsonl(path: Path, problems: list[str]) -> list[Any]:
    rows = []
    for number, line in enumerate(path.read_text(encoding="utf-8").split("\n"), 1):
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except ValueError as exc:
            problems.append(f"{path.name}:{number}: not JSON ({exc})")
    return rows


def _paths(value: Any, prefix: str = "") -> set[str]:
    """Every dotted key path in a record; list items share their list's path."""
    if isinstance(value, list):
        return set().union(*(_paths(item, prefix) for item in value))
    if not isinstance(value, dict):
        return set()
    found = set()
    for key, child in value.items():
        path = f"{prefix}.{key}" if prefix else key
        found |= {path} | _paths(child, path)
    return found


def search_tools(events: list[Json]) -> set[str]:
    """Tools of logged executions that ran a search operation."""
    return {e["tool"]["name"] for e in events if any(op.get("kind") == "search" for op in e["operations"])}


def _events(log: Path, problems: list[str]) -> list[Json]:
    """Log rows that have the fields the checks read; the others are reported."""
    events = []
    for number, row in enumerate(_jsonl(log, problems), 1):
        errors = list(Draft202012Validator(EVENT_SCHEMA).iter_errors(row))
        problems += [f"{LOG_FILE} row {number}: {e.message}" for e in errors]
        if not errors:
            events.append(row)
    return events


def _scope_problems(scope: dict[str, list[str]], data: dict[str, Any]) -> list[str]:
    known = {
        "states": {j["id"] for j in data["jurisdictions"]},
        "institutes": {i["id"] for i in data["institutes"]},
        "fields": {path for rows in (data["jurisdictions"], data["institutes"]) for row in rows for path in _paths(row)},
    }
    problems = []
    if not any(scope.values()):
        problems.append("scope: name at least one state, institute or field")
    for kind, values in scope.items():
        if "all" in values and len(values) > 1:
            problems.append(f"scope.{kind}: 'all' stands alone")
        for value in values:
            if value != "all" and value not in known[kind]:
                problems.append(f"scope.{kind}: unknown {kind[:-1]} {value!r}")
    return problems


def validate_pass(folder: Path, data: dict[str, Any]) -> list[str]:
    """Problems with one pass folder; empty when it is valid."""
    path = folder / PASS_FILE
    if not path.is_file():
        return [f"{path}: no pass file"]
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        return [f"{path}: not JSON ({exc})"]
    problems = [f"pass.json: {'/'.join(map(str, e.absolute_path)) or '(top)'}: {e.message}"
                for e in sorted(Draft202012Validator(PASS_SCHEMA).iter_errors(record), key=lambda e: list(e.absolute_path))]
    if problems:
        return problems
    if record["pass_id"] != folder.name:
        problems.append(f"pass_id {record['pass_id']!r} differs from its folder {folder.name!r}")
    started, ended = _day(record["started"]), _day(record["ended"]) if record["ended"] else None
    if started is None:
        problems.append(f"started: not an ISO date or time: {record['started']!r}")
    elif record["pass_id"][:10] != started.isoformat():
        problems.append("pass_id must start with the start date")
    if record["ended"] and ended is None:
        problems.append(f"ended: not an ISO date or time: {record['ended']!r}")
    if started and ended and ended < started:
        problems.append("ended is before started")
    problems += _scope_problems(record["scope"], data)
    for tool in record["search_tools"]:
        if tool["engine"] != KNOWN_ENGINES.get(tool["tool"]):
            problems.append(f"search_tools: {tool['tool']} engine must be {KNOWN_ENGINES.get(tool['tool'])!r} "
                            "(engines come from KNOWN_ENGINES, never inferred)")
    if not any(KNOWN_ENGINES.get(t["tool"]) for t in record["search_tools"]):
        problems.append(f"search_tools: at least one tool with a known engine ({', '.join(KNOWN_ENGINES)})")
    findings = folder / FINDINGS_FILE
    if findings.is_file():
        for number, row in enumerate(_jsonl(findings, problems), 1):
            for e in Draft202012Validator(FINDING_SCHEMA).iter_errors(row):
                problems.append(f"findings.jsonl row {number}: {e.message}")
            if isinstance(row, dict) and personal_profile(str(row.get("url", ""))):
                problems.append(f"findings.jsonl row {number}: a personal profile URL; leave the result out "
                                "(personal data never enters a pass folder)")
    log = folder / LOG_FILE
    if record["status"] == "closed":
        for key in ("ended", "stopping_reason"):
            if not record[key]:
                problems.append(f"closed pass needs {key}")
        if not record["sessions"]:
            problems.append("closed pass needs its session IDs")
        if not log.is_file():
            problems.append(f"closed pass needs {LOG_FILE} (extract it with scripts/research_log.py)")
        if not findings.is_file():
            problems.append(f"closed pass needs {FINDINGS_FILE} (empty if nothing was screened)")
    if log.is_file():
        events = _events(log, problems)
        expected = {
            "format": {LOG_FORMAT},
            "sessions": set(record["sessions"]),
            "harness": {f"{record['harness']['name']} {record['harness']['version']}"},
            "models": set(record["models"]),
            "search tools": {t["tool"] for t in record["search_tools"]},
        }
        for key, values in logged(events).items():
            extra = sorted(set(values) - expected[key])
            if extra:
                problems.append(f"{LOG_FILE}: {key} the pass file does not name: {', '.join(extra)}")
        if record["status"] == "closed" and not any(KNOWN_ENGINES.get(t) for t in search_tools(events)):
            problems.append(f"{LOG_FILE}: no search with a known-engine tool ({', '.join(KNOWN_ENGINES)}) was logged")
    return problems


def logged(events: list[Json]) -> dict[str, list[str]]:
    """What a pass file must name for its log: formats, sessions, harness, models and search tools."""
    return {
        "format": sorted({e["format_version"] for e in events}),
        "sessions": sorted({e["session_id"] for e in events}),
        "harness": sorted({f"{e['harness']['name']} {e['harness']['version']}" for e in events}),
        "models": sorted({e["model"] for e in events if e["model"]}),
        "search tools": sorted(search_tools(events)),
    }


# Each harness runs passes from its own neutral home in the store: (home file, file in scripts/).
HOME_FILES = {
    "claude": {"settings.json": "research_pass_claude.json", "mcp.json": "research_pass_mcp.json"},
    "codex": {"config.toml": "research_pass_codex.toml"},
}


def home(store: Path, harness: str) -> Path:
    return store / ".homes" / harness


def setup_homes(store: Path) -> list[Path]:
    """Write each harness home's configuration from scripts/; logins and session files in a home are kept."""
    written = []
    for harness, files in HOME_FILES.items():
        folder = home(store, harness)
        folder.mkdir(parents=True, exist_ok=True)
        for name, source in files.items():
            shutil.copyfile(ROOT / "scripts" / source, folder / name)
            written.append(folder / name)
    return written


def pass_transcripts(folder: Path, store: Path) -> list[Path]:
    """The transcripts of sessions started in the pass folder, from both homes."""
    found = []
    where = folder.resolve()
    # Claude Code: projects/<cwd with every non-alphanumeric character as ->
    claude = home(store, "claude") / "projects" / re.sub(r"[^A-Za-z0-9]", "-", str(where))
    if claude.is_dir():
        found.append(claude)
    # Codex: sessions/YYYY/MM/DD/rollout-*.jsonl, whose first line records the session's cwd
    for path in sorted((home(store, "codex") / "sessions").rglob("*.jsonl")):
        with path.open(encoding="utf-8") as lines:
            try:
                first = json.loads(lines.readline())
            except ValueError:
                continue
        cwd = first.get("payload", {}).get("cwd") if first.get("type") == "session_meta" else None
        if cwd and Path(cwd).resolve() == where:
            found.append(path)
    return found


def extract_pass(folder: Path, sources: list[Path]) -> dict[str, list[str]]:
    """Copy the transcripts into transcripts/, write the log and diagnostics; return what the pass file must name."""
    target = folder / "transcripts"
    plan: dict[Path, Path] = {}
    for source in sources:
        if source.resolve().is_relative_to(target.resolve()):
            continue  # already archived: extract from the copies as they are
        if source.is_file():
            # A Claude Code session keeps its subagents in <session>/ beside <session>.jsonl: copy them too.
            sibling = source.with_suffix("")
            files = [(source, Path(source.name))] + [
                (path, Path(sibling.name) / path.relative_to(sibling)) for path in sorted(sibling.rglob("*.jsonl"))]
        else:
            files = [(path, path.relative_to(source)) for path in sorted(source.rglob("*.jsonl"))]
        for path, relative in files:
            copy = target / relative
            if copy in plan:
                raise ValueError(f"{path} and {plan[copy]} would both be copied to {copy}")
            if copy.is_file() and not path.read_bytes().startswith(copy.read_bytes()):
                raise ValueError(f"{copy} exists and {path} does not extend it; not overwriting")
            plan[copy] = path
    for copy, path in plan.items():
        copy.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, copy)
    events, diagnostics = research_log.extract([target])
    research_log.write_jsonl(folder / LOG_FILE, events)
    (folder / DIAGNOSTICS_FILE).write_text(json.dumps(diagnostics, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return logged(events)


def changed_records(old: str | None, new: str | None) -> list[str]:
    """Record IDs a change to data/aisi.json adds, removes or edits, from the file's text before and after."""
    if old == new:
        return []
    before, after = (json.loads(text) if text else {} for text in (old, new))
    changed = []
    for key in RECORD_LISTS:
        rows_before = {row["id"]: row for row in before.get(key, [])}
        rows_after = {row["id"]: row for row in after.get(key, [])}
        changed += sorted(i for i in rows_before.keys() | rows_after.keys() if rows_before.get(i) != rows_after.get(i))
    return changed


def trailer_values(message: str) -> list[str]:
    parsed = subprocess.run(["git", "interpret-trailers", "--parse"], input=message, capture_output=True, text=True,
                            check=True).stdout
    return [line.split(":", 1)[1].strip() for line in parsed.splitlines()
            if line.split(":", 1)[0].strip().lower() == TRAILER.lower()]


def message_problems(message: str, changed: list[str], store: Path | None, data: dict[str, Any]) -> tuple[list[str], list[str]]:
    """Problems with a commit message's Research-pass trailers, and passes that could not be checked."""
    values = trailer_values(message)
    if changed and not values:
        shown = ", ".join(changed[:5]) + (f" and {len(changed) - 5} more" if len(changed) > 5 else "")
        return [f"changes records ({shown}) but names no pass: add '{TRAILER}: <pass-id>' "
                f"or '{TRAILER}: none (<reason>)' (docs/research-passes.md)"], []
    problems, unchecked = [], []
    for value in values:
        if NONE_TRAILER.match(value):
            continue
        if not re.match(PASS_ID, value):
            problems.append(f"{TRAILER}: {value!r} is neither a pass ID nor 'none (<reason>)'")
        elif store is None or not store.is_dir():
            unchecked.append(value)
        else:
            folder = store / value
            found = validate_pass(folder, data)
            record = json.loads((folder / PASS_FILE).read_text(encoding="utf-8")) if not found else {}
            if not found and record["status"] != "closed":
                found = ["the pass is still open"]
            problems += [f"pass {value}: {p}" for p in found]
    return problems, unchecked


def author_date(value: str) -> datetime:
    """The author date git gives a hook: its internal '@<seconds> <offset>', or ISO; anything else counts as now."""
    raw = re.fullmatch(r"@?([0-9]+) [+-][0-9]{4}", value.strip())
    if raw:
        return datetime.fromtimestamp(int(raw.group(1)), timezone.utc)
    try:
        parsed = datetime.fromisoformat(value)
        return parsed if parsed.tzinfo else parsed.astimezone()
    except ValueError:
        return datetime.now().astimezone()


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True).stdout


def _blob(repo: Path, revision: str) -> str | None:
    found = subprocess.run(["git", "-C", str(repo), "show", f"{revision}:data/aisi.json"], capture_output=True, text=True)
    return found.stdout if found.returncode == 0 else None


def commit_report(repo: Path, base: str, store: Path | None, data: dict[str, Any]) -> tuple[list[str], int]:
    """Check every non-merge commit in base..HEAD authored since passes began; merges inherit from their parents."""
    lines, failed, unchecked, exempt = [], False, [], []
    for commit in _git(repo, "rev-list", "--no-merges", "--reverse", f"{base}..HEAD").split():
        authored = datetime.fromisoformat(_git(repo, "log", "-1", "--format=%aI", commit).strip())
        message = _git(repo, "log", "-1", "--format=%B", commit)
        title = message.splitlines()[0] if message else ""
        changed = changed_records(_blob(repo, f"{commit}^"), _blob(repo, commit))
        if authored < PASSES_BEGIN:
            if changed:
                exempt.append(commit[:12])
            continue
        problems, names = message_problems(message, changed, store, data)
        unchecked += names
        nones = [v for v in trailer_values(message) if NONE_TRAILER.match(v)]
        if problems:
            failed = True
            lines += [f"FAIL {commit[:12]} {title}"] + [f"  {p}" for p in problems]
        elif nones:
            lines.append(f"none {commit[:12]} {title}: {'; '.join(nones)}")
        elif trailer_values(message):
            lines.append(f"ok   {commit[:12]} {title}: {', '.join(trailer_values(message))}")
    if exempt:
        lines.append(f"not checked, authored before {PASSES_BEGIN.date()}: {len(exempt)} commit(s) changing records")
    if unchecked:
        lines.append(f"not checked, no pass store: {', '.join(sorted(set(unchecked)))}")
    if not lines:
        lines.append(f"no commits in {base}..HEAD change records")
    return lines, 1 if failed else NOT_RUN if unchecked else 0


def resolve(name: str, store: Path | None) -> Path:
    """A pass folder given by path, or by ID in the store."""
    path = Path(name)
    return path if path.is_dir() or os.sep in name or store is None else store / name


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--store", type=Path, help="pass store (default AISI_PASSES, else passes/ beside AISI_SNAPSHOTS)")
    commands = parser.add_subparsers(dest="command", required=True)
    extract = commands.add_parser("extract", help="copy a pass's transcripts into its folder and write its log")
    extract.add_argument("pass_", metavar="PASS", help="pass ID or folder")
    extract.add_argument("--transcripts", type=Path, nargs="+",
                         help="transcript files or folders (default: the sessions started in the pass folder, from both homes)")
    commands.add_parser("homes", help="write the Claude Code and Codex homes for passes from the configuration in scripts/")
    message = commands.add_parser("message", help="commit-msg hook: check the trailers against the staged data")
    message.add_argument("file", type=Path, help="the commit message file")
    commits = commands.add_parser("commits", help="check the Research-pass trailers of the commits in BASE..HEAD")
    commits.add_argument("--base", required=True)
    commits.add_argument("--repo", type=Path, default=Path("."), help="the repository whose history is checked")
    for sub in (message, commits):
        sub.add_argument("--data", type=Path, default=ROOT / "data/aisi.json")
    check = commands.add_parser("validate", help="validate pass folders")
    check.add_argument("passes", nargs="*", help="pass IDs or folders; default: every pass in the store")
    check.add_argument("--data", type=Path, default=ROOT / "data/aisi.json")
    args = parser.parse_args(argv)
    store = store_directory(args.store)
    try:
        if args.command == "extract":
            folder = resolve(args.pass_, store)
            if not folder.is_dir():
                raise ValueError(f"no pass folder {folder}")
            if args.transcripts:
                sources = args.transcripts
            elif store is not None:
                sources = pass_transcripts(folder, store)
            else:
                raise ValueError("no pass store to find transcripts in (set AISI_PASSES or AISI_SNAPSHOTS, or pass --transcripts)")
            missing = [str(s) for s in sources if not s.exists()]
            if missing or not sources:
                raise ValueError(f"no transcripts at {', '.join(missing) or 'the homes for ' + str(folder)}")
            found = extract_pass(folder, sources)
            print(f"Wrote {folder / LOG_FILE}; the pass file must name:")
            for key, values in found.items():
                print(f"  {key}: {', '.join(values) or '(none)'}")
            return 0
        if args.command == "homes":
            if store is None:
                raise ValueError("no pass store (set AISI_PASSES or AISI_SNAPSHOTS, or pass --store)")
            for path in setup_homes(store):
                print(f"wrote {path}")
            print(f"Log in once per home:\n  CLAUDE_CONFIG_DIR={shlex.quote(str(home(store, 'claude')))} claude auth login\n"
                  f"  CODEX_HOME={shlex.quote(str(home(store, 'codex')))} codex login")
            return 0
        if args.command in ("message", "commits"):
            data = json.loads(args.data.read_text(encoding="utf-8")) if args.data.is_file() else {}
            data = {"jurisdictions": [], "institutes": [], **data}  # a commit that deletes the dataset still gets checked
        if args.command == "message":
            # The hook checks a new commit; merges inherit, and amends are checked by the passes step of check.py.
            when = author_date(os.environ.get("GIT_AUTHOR_DATE", ""))
            if (Path(_git(Path("."), "rev-parse", "--git-path", "MERGE_HEAD").strip()).exists()
                    or when < PASSES_BEGIN):
                return 0
            staged = subprocess.run(["git", "show", ":data/aisi.json"], capture_output=True, text=True)
            changed = changed_records(_blob(Path("."), "HEAD"), staged.stdout if staged.returncode == 0 else None)
            problems, unchecked = message_problems(args.file.read_text(encoding="utf-8"), changed, store, data)
            for problem in problems:
                print(f"commit-msg: {problem}", file=sys.stderr)
            if unchecked and not problems:
                print(f"commit-msg: no pass store, not checked: {', '.join(unchecked)}; check.py --base checks them",
                      file=sys.stderr)
            return 1 if problems else 0
        if args.command == "commits":
            lines, code = commit_report(args.repo, args.base, store, data)
            print("\n".join(lines))
            return code
        if args.passes:
            folders = [resolve(p, store) for p in args.passes]
        elif store is None or not store.is_dir():
            raise ValueError("no pass store (set AISI_PASSES or AISI_SNAPSHOTS, or pass --store)")
        else:
            folders = sorted(p for p in store.iterdir() if p.is_dir() and re.match(PASS_ID, p.name))
        data = json.loads(args.data.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"research_pass: {exc}", file=sys.stderr)
        return 2
    failed = 0
    for folder in folders:
        problems = validate_pass(folder, data)
        failed += bool(problems)
        print(f"{folder.name}: {'FAIL' if problems else 'ok'}")
        for problem in problems:
            print(f"  {problem}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
