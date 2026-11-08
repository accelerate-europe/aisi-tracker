#!/usr/bin/env python3
"""Search-log basis for the research done before passes began (standard library only).

`build` lists the past session transcripts of the tracker work on this host, records
each one's path, size and SHA-256 (a live file is hashed up to its size at that
moment) and extracts their log with research_log.py, keeping only executions that
started before the cutoff. It never copies a transcript: past sessions mix the
research with personal data, so the basis holds only the inventory and the log,
and the originals (kept indefinitely) can be re-extracted and checked by hash.

The inclusion rule (RULE) is path based: Claude Code project folders and Codex
sessions whose working folder matches it, subagents included through
research_log.py. Transcripts elsewhere that mention the tracker and contain research
calls are listed as residual, not included, for a person to check.

`merge` combines the per-host outputs (run `build` on each host) into one basis.
Output is private: it goes to the private store, never into the repository. Before
the cutoff a build is marked provisional; the basis is built once after it:

    uv run scripts/research_backfill.py build --out work/part-this.json
    ssh other-host mkdir -p /tmp/aisi-backfill
    scp scripts/research_log.py scripts/research_backfill.py other-host:/tmp/aisi-backfill/
    ssh other-host 'cd /tmp/aisi-backfill && python3 research_backfill.py build --out part-other-host.json'
    scp other-host:/tmp/aisi-backfill/part-other-host.json work/
    uv run scripts/research_backfill.py merge work/part-this.json work/part-other-host.json \
        --out "$AISI_SNAPSHOTS/../backfill/2026-11-07-before-passes"
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import socket
import sys
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import research_log  # noqa: E402

Json = dict[str, Any]
BASIS_FORMAT = "research-backfill/1"
# Working folders of the tracker work: the repository and its worktrees, and the
# transformative-ki-at project folders where the early research ran (13.10.2026 on).
RULE = re.compile(r"aisi|transformative-ki-at", re.IGNORECASE)
# Test scratch (smoke tests of the pass tooling) is not research: Claude project
# folder names and Codex working folders under /tmp.
EXCLUDED = re.compile(r"^-private-tmp-|^-tmp-")
EXCLUDED_CWD = re.compile(r"^/(?:private/)?tmp/")
CUTOFF = "2026-11-07T00:00:00+01:00"  # passes began; research_pass.PASSES_BEGIN
# Tracker-specific strings only; "AI Security Institute" appears across all AI safety work.
MARKERS = (b"aisi-tracker", b"data/aisi.json", b"aisi.accelerate-europe.org")
# Research calls as the extractor recognises them, built from its own name sets:
# Claude tools, Codex direct web tools, research MCP tools (Claude names and Codex
# McpToolCall servers) and Codex hosted search items.
_names = ["WebSearch", "WebFetch", *sorted(research_log.WEB_NAMES)]
_servers = sorted(research_log.RESEARCH_MCP_SERVERS)
RESEARCH_CALLS = re.compile(
    rb'"name":\s*"(?:'
    + b"|".join(re.escape(n.encode()) for n in _names)
    + b"|mcp__(?:"
    + b"|".join(re.escape(s.encode()) for s in _servers)
    + rb')__[\w-]+)"'
    + rb'|"server":\s*"(?:'
    + b"|".join(re.escape(s.encode()) for s in _servers)
    + rb')"|"kind":\s*"web\.search"|"type":\s*"web_search_call"'
)


def codex_cwd(path: Path) -> str | None:
    with path.open("rb") as handle:
        for line in handle:
            if b'"session_meta"' not in line:
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                return None
            payload = obj.get("payload") if isinstance(obj, dict) else None
            cwd = payload.get("cwd") if isinstance(payload, dict) else None
            return cwd if isinstance(cwd, str) else None
    return None


def candidates(home: Path) -> tuple[list[Path], list[Json]]:
    """Inputs under the rule, and residual transcripts that mention the tracker."""
    inputs: list[Path] = []
    residual: list[Json] = []
    projects = home / ".claude" / "projects"
    for folder in sorted(p for p in projects.iterdir() if p.is_dir()):
        files = sorted(folder.rglob("*.jsonl"))
        if RULE.search(folder.name) and not EXCLUDED.search(folder.name):
            inputs.extend(files)
        else:
            residual.extend(r for f in files if (r := residual_entry(f)))
    for path in sorted((home / ".codex" / "sessions").rglob("*.jsonl")):
        cwd = codex_cwd(path)
        if cwd and RULE.search(cwd) and not EXCLUDED_CWD.search(cwd):
            inputs.append(path)
        elif r := residual_entry(path):
            residual.append(r)
    return inputs, residual


def residual_entry(path: Path) -> Json | None:
    blob = path.read_bytes()
    if any(marker in blob for marker in MARKERS) and RESEARCH_CALLS.search(blob):
        return {"path": str(path), "research_calls": len(RESEARCH_CALLS.findall(blob))}
    return None


def event_time(ev: Json) -> datetime | None:
    value = ev.get("started_at") or ev.get("ended_at")
    if not isinstance(value, str):
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def build(home: Path, cutoff: str) -> Json:
    built = datetime.now(timezone.utc)
    limit = datetime.fromisoformat(cutoff)
    inputs, residual = candidates(home)
    events, diagnostics = research_log.extract(inputs) if inputs else ([], [])
    # The inventory is what extract read: size and hash of exactly the bytes it
    # parsed, a live file included, and the subagent and Codex child files it
    # brought in beyond the rule.
    inventory = sorted(
        (
            {"path": d["path"], "bytes": d["bytes"], "sha256": d["sha256"]}
            for d in diagnostics
            if d["code"] in ("file_inventory", "skipped_unrecognised_file")
        ),
        key=lambda item: item["path"],
    )
    kept, untimed, later = [], 0, 0
    for ev in events:
        when = event_time(ev)
        if when is None:
            untimed += 1
            kept.append(ev)  # kept: an execution without a time cannot be dated out
        elif when < limit:
            kept.append(ev)
        else:
            later += 1
    leaks = [
        ev["event_id"]
        for ev in kept
        for op in ev["operations"]
        if op["name"] == "shell_http" and research_log.has_credential(op["arguments"])
    ]
    return {
        "format_version": BASIS_FORMAT,
        "log_format": research_log.FORMAT,
        "host": socket.gethostname(),
        "home": str(home),
        "built_at": built.isoformat(),
        "cutoff": cutoff,
        "provisional": built < limit,
        "rule": {
            "include": RULE.pattern,
            "exclude": EXCLUDED.pattern,
            "residual_markers": [m.decode() for m in MARKERS],
        },
        "inventory": inventory,
        "residual": residual,
        "counts": {
            "files": len(inventory),
            "executions_kept": len(kept),
            "executions_after_cutoff": later,
            "executions_without_time": untimed,
        },
        "shell_commands_with_credentials": leaks,
        "events": kept,
        "diagnostics": diagnostics,
    }


def write_basis(parts: list[Json], out: Path) -> None:
    """Write inventory.json, log.jsonl, log.diagnostics.json and summary.json."""
    if out.exists() and any(out.iterdir()):
        raise ValueError(f"{out} is not empty; a basis is never overwritten")
    leaks = [e for part in parts for e in part["shell_commands_with_credentials"]]
    if leaks:
        raise ValueError(f"shell commands with credentials in the log: {leaks[:5]}")
    events: dict[str, Json] = {}
    for part in parts:
        for ev in part["events"]:
            research_log.insert_event(events, ev)
    log = sorted(events.values(), key=lambda ev: ev["event_id"])
    out.mkdir(parents=True, exist_ok=True)
    meta = [
        {k: v for k, v in p.items() if k not in ("events", "diagnostics")}
        for p in parts
    ]
    (out / "inventory.json").write_text(
        json.dumps(
            {"format_version": BASIS_FORMAT, "hosts": meta},
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    research_log.write_jsonl(out / "log.jsonl", log)
    (out / "log.diagnostics.json").write_text(
        json.dumps(
            {p["host"]: p["diagnostics"] for p in parts}, ensure_ascii=False, indent=2
        )
        + "\n",
        encoding="utf-8",
    )
    (out / "summary.json").write_text(
        json.dumps(research_log.summarise(log), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    b = commands.add_parser(
        "build", help="Inventory and extract this host's transcripts"
    )
    b.add_argument("--home", type=Path, default=Path.home())
    b.add_argument("--cutoff", default=CUTOFF)
    b.add_argument("--out", required=True, type=Path, help="Host part (JSON) to write")
    m = commands.add_parser("merge", help="Combine host parts into a basis folder")
    m.add_argument("parts", nargs="+", type=Path)
    m.add_argument("--out", required=True, type=Path, help="New, empty basis folder")
    args = parser.parse_args(argv)
    try:
        if args.command == "build":
            part = build(args.home, args.cutoff)
            args.out.write_text(
                json.dumps(part, ensure_ascii=False) + "\n", encoding="utf-8"
            )
            print(json.dumps(part["counts"]), file=sys.stderr)
        else:
            parts = [json.loads(p.read_text(encoding="utf-8")) for p in args.parts]
            write_basis(parts, args.out)
            print(f"Wrote basis to {args.out}", file=sys.stderr)
    except (OSError, ValueError) as exc:
        print(f"research_backfill: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
