#!/usr/bin/env python3
"""Prepare sweep passes: per-state stale-fact re-checks and the monthly global topic sweep.

A sweep is a research pass (docs/research-passes.md) that this script sets up and an agent then runs;
docs/sweep.md has the process. `state` writes a `targeted` pass for a few states: per state a packet
with the record, the institutes of that state, every cited source with its excerpts and the text of
its newest private copy, the state's topic queries and the names the record states, plus the brief
from scripts/sweep_brief_state.md. `global` writes a `news-sweep` pass for one month from
scripts/sweep_brief_global.md. Both write only a new pass folder in the pass store.

    uv run --locked scripts/sweep.py state 2026-11-23-sweep-w48 --who "<name>" --states MT,LU,EE,AL,AM,AZ
    uv run --locked scripts/sweep.py global 2026-12-01-global-sweep --who "<name>" --month 2026-11

Search window per state: `--since`, else the start of the newest closed sweep pass that marked the
state complete in its sweep.json, else 24 months back (a first sweep: the last full check is not
assumed complete). Exit status 3 (NOT RUN) when no pass store is configured. Internal tooling:
nothing here enters the published data or the build.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import shutil
import sys
import tempfile
from collections.abc import Callable
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from string import Template
from typing import Any

try:
    from . import research_pass, verify_excerpts
except ImportError:
    import research_pass  # type: ignore[no-redef]  # Direct script entry point.
    import verify_excerpts  # type: ignore[no-redef]

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data" / "aisi.json"
QUERIES = ROOT / "data" / "reference" / "sweep_queries.csv"
BRIEFS = {"state": ROOT / "scripts" / "sweep_brief_state.md", "global": ROOT / "scripts" / "sweep_brief_global.md"}
SWEEP_FILE = "sweep.json"
NOT_RUN = 3
FIRST_WINDOW_YEARS = 2  # 24 months: a first sweep does not trust the last full check to be complete
GLOBAL_WINDOW_DAYS = 60
# Fields of a jurisdiction record that hold no claim to re-check.
SKIP_KEYS = {"population", "gdp", "memberships", "m49", "wikidata", "iso3", "identifier_source_ids", "region"}

# The global sweep's fixed queries (calibration 14 Nov 2026): three topics, five languages.
GLOBAL_QUERIES = {
    "institute": {
        "en": "national AI safety institute or AI security institute established",
        "es": "instituto nacional de seguridad de la inteligencia artificial creado",
        "fr": "institut national pour la sécurité de l'intelligence artificielle créé",
        "pt": "instituto nacional de segurança da inteligência artificial criado",
        "ar": "إنشاء معهد وطني لسلامة الذكاء الاصطناعي",
    },
    "authority": {
        "en": "government designates national artificial intelligence authority or agency",
        "es": "gobierno designa autoridad o agencia nacional de inteligencia artificial",
        "fr": "le gouvernement désigne l'autorité nationale de l'intelligence artificielle",
        "pt": "governo designa autoridade ou agência nacional de inteligência artificial",
        "ar": "الحكومة تعين هيئة وطنية للذكاء الاصطناعي",
    },
    "strategy_law": {
        "en": "national artificial intelligence strategy or law adopted by cabinet or parliament",
        "es": "estrategia o ley nacional de inteligencia artificial aprobada",
        "fr": "stratégie ou loi nationale sur l'intelligence artificielle adoptée",
        "pt": "estratégia ou lei nacional de inteligência artificial aprovada",
        "ar": "اعتماد الاستراتيجية الوطنية أو قانون الذكاء الاصطناعي",
    },
}


def source_ids_in(value: Any) -> set[str]:
    """Every source ID a record cites, in any `*source_ids` list at any depth."""
    found: set[str] = set()
    if isinstance(value, dict):
        for key, item in value.items():
            if key.endswith("source_ids") and isinstance(item, list):
                found.update(x for x in item if isinstance(x, str))
            else:
                found |= source_ids_in(item)
    elif isinstance(value, list):
        for item in value:
            found |= source_ids_in(item)
    return found


def retrieved_at(value: str | None) -> datetime:
    """A manifest's retrieval time as an aware datetime, so offsets compare correctly; unknown sorts first."""
    try:
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return datetime.min.replace(tzinfo=timezone.utc)
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def copy_text(source_id: str, snapshots: Path) -> tuple[str, str | None]:
    """Literal text of the newest private copy (the quotation verifier's view) and its retrieval time."""
    manifest = snapshots / source_id / "manifest.json"
    if not manifest.is_file():
        return "", None
    entries = [e for e in json.loads(manifest.read_text(encoding="utf-8")) if e.get("path")]
    if not entries:
        return "", None
    entry = max(entries, key=lambda e: (retrieved_at(e.get("retrieved")), e["path"]))
    try:
        text = verify_excerpts.extract_text((snapshots / entry["path"]).read_bytes(), entry.get("mime", ""))[0]
    except Exception as exc:  # noqa: BLE001 - an unreadable copy is reported in the packet, not fatal
        text = f"[text extraction failed: {type(exc).__name__}]"
    return text, entry.get("retrieved")


def entity_seeds(record: dict, institutes: list[dict]) -> list[str]:
    """Names the record states; the agent adds acts, bills and strategies named in its text."""
    names = [b.get("name") for b in record.get("related_bodies", [])]
    for inst in institutes:
        names += [inst.get("name"), inst.get("acronym"), inst.get("name_local")]
    return sorted({n for n in names if n})


def topic_queries(state: str, path: Path = QUERIES) -> list[dict]:
    with path.open(encoding="utf-8", newline="") as fh:
        return [{k: row[k] for k in ("query_id", "lang", "query", "translation_verified")}
                for row in csv.DictReader(fh) if row["id"] == state]


def completed_sweeps(store: Path, state: str) -> list[date]:
    """Start dates of closed sweep passes whose sweep.json marks the state complete."""
    starts = []
    if not store.is_dir():  # a new store has no history yet
        return starts
    for folder in sorted(p for p in store.iterdir() if (p / SWEEP_FILE).is_file() and (p / research_pass.PASS_FILE).is_file()):
        record = json.loads((folder / research_pass.PASS_FILE).read_text(encoding="utf-8"))
        status = json.loads((folder / SWEEP_FILE).read_text(encoding="utf-8")).get("states", {})
        if record.get("status") == "closed" and status.get(state) == "complete":
            starts.append(date.fromisoformat(record["started"][:10]))
    return starts


def window_start(store: Path, state: str, today: date, since: date | None = None) -> date:
    if since is not None:
        return since
    done = completed_sweeps(store, state)
    return max(done) if done else years_before(today, FIRST_WINDOW_YEARS)


def years_before(day: date, years: int) -> date:
    """The same calendar day `years` earlier; 29 February becomes 28 February."""
    try:
        return day.replace(year=day.year - years)
    except ValueError:
        return day.replace(year=day.year - years, day=28)


def packet(state: str, data: dict, snapshots: Path, folder: Path, since: date) -> dict:
    juris = {j["id"]: j for j in data["jurisdictions"]}
    record = {k: v for k, v in juris[state].items() if k not in SKIP_KEYS}
    institutes = [i for i in data["institutes"] if i.get("jurisdiction_id") == state]
    sources = {s["id"]: s for s in data["sources"]}
    cited = sorted(source_ids_in(record) | set().union(*(source_ids_in(i) for i in institutes)))
    (folder / "sources").mkdir(parents=True, exist_ok=True)
    rows = []
    for sid in cited:
        source = sources.get(sid, {})
        text, retrieved = copy_text(sid, snapshots)
        if text:  # whole, never cut: the passage a claim rests on can sit anywhere
            (folder / "sources" / f"{sid}.txt").write_text(text, encoding="utf-8")
        rows.append({
            **{k: source.get(k) for k in ("id", "url", "title", "publisher", "type", "published", "language",
                                          "archive_url", "excerpts")},
            "copy": {"file": f"sources/{sid}.txt", "retrieved": retrieved, "chars": len(text)} if text else None,
        })
    return {
        "state": state,
        "name": juris[state]["name"],
        "dataset": {k: data["dataset"][k] for k in ("version", "as_of")},
        "window_from": since.isoformat(),
        "record": record,
        "institutes": institutes,
        "sources": rows,
        "topic_queries": topic_queries(state),
        "entity_seeds": entity_seeds(record, institutes),
    }


def pass_file(pass_id: str, who: str, kind: str, states: list[str]) -> dict:
    return {
        "pass_id": pass_id,
        "who": who,
        "harness": {"name": "claude_code", "version": "set at close"},
        "models": [],
        "sessions": [],
        "started": pass_id[:10],
        "ended": None,
        "type": "targeted" if kind == "state" else "news-sweep",
        "scope": {"states": states, "institutes": [], "fields": []},
        "search_tools": [{"tool": "mcp__exa__web_search_exa", "engine": "exa"}],
        "stopping_reason": None,
        "status": "open",
    }


def render(kind: str, values: dict[str, str]) -> str:
    return Template(BRIEFS[kind].read_text(encoding="utf-8")).substitute(values)


def check_pass_id(pass_id: str) -> date:
    """The pass ID's start date; refuses an ID that is not a real date and a short slug."""
    try:
        if re.match(research_pass.PASS_ID, pass_id):
            return date.fromisoformat(pass_id[:10])
    except ValueError:
        pass
    raise SystemExit(f"pass ID {pass_id!r}: a real start date and a short slug, e.g. 2026-11-23-sweep-w48")


def build_folder(store: Path, pass_id: str, fill: Callable[[Path], None]) -> Path:
    """Fill a hidden temporary sibling, then rename it to the pass folder: a failure leaves nothing behind."""
    check_pass_id(pass_id)
    folder = store / pass_id
    if folder.exists():
        raise SystemExit(f"{folder} exists; a pass folder is never overwritten")
    store.mkdir(parents=True, exist_ok=True)
    partial = Path(tempfile.mkdtemp(prefix=f".{pass_id}-", dir=store))
    try:
        fill(partial)
        partial.rename(folder)
    except BaseException:
        shutil.rmtree(partial, ignore_errors=True)
        raise
    return folder


def write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


def prepare_state(store: Path, snapshots: Path, data: dict, pass_id: str, who: str, states: list[str],
                  since: date | None) -> Path:
    known = {j["id"] for j in data["jurisdictions"]}
    unknown = [s for s in states if s not in known]
    if unknown or not states:
        raise SystemExit(f"unknown or missing states: {unknown or 'none given'}")
    if len(set(states)) != len(states):
        raise SystemExit(f"a state is listed twice: {states}")
    today = check_pass_id(pass_id)
    windows = {s: window_start(store, s, today, since) for s in states}

    def fill(folder: Path) -> None:
        for state in states:
            target = folder / "packets" / state
            target.mkdir(parents=True)
            write_json(target / "packet.json", packet(state, data, snapshots, target, windows[state]))
        write_json(folder / research_pass.PASS_FILE, pass_file(pass_id, who, "state", states))
        write_json(folder / SWEEP_FILE, {"kind": "state", "windows": {s: d.isoformat() for s, d in windows.items()},
                                         "states": {s: "pending" for s in states}})
        (folder / research_pass.FINDINGS_FILE).touch()
        lines = "\n".join(f"- {s} ({juris_name(data, s)}): search window from {windows[s].isoformat()}" for s in states)
        (folder / "brief.md").write_text(render("state", {"pass_id": pass_id, "states": lines, "as_of": data["dataset"]["as_of"],
                                                           "version": data["dataset"]["version"]}), encoding="utf-8")

    return build_folder(store, pass_id, fill)


def juris_name(data: dict, state: str) -> str:
    return next(j["name"] for j in data["jurisdictions"] if j["id"] == state)


def prepare_global(store: Path, data: dict, pass_id: str, who: str, month: str) -> Path:
    check_pass_id(pass_id)
    first = date.fromisoformat(f"{month}-01")
    end = (first.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
    since = end - timedelta(days=GLOBAL_WINDOW_DAYS - 1)  # 60 calendar days, both ends included
    queries = "\n".join(f"- `{topic}:{lang}`: {query}" for topic, langs in GLOBAL_QUERIES.items()
                        for lang, query in langs.items())

    def fill(folder: Path) -> None:
        write_json(folder / research_pass.PASS_FILE, pass_file(pass_id, who, "global", ["all"]))
        write_json(folder / SWEEP_FILE, {"kind": "global", "month": month, "window": [since.isoformat(), end.isoformat()]})
        (folder / research_pass.FINDINGS_FILE).touch()
        (folder / "brief.md").write_text(render("global", {"pass_id": pass_id, "since": since.isoformat(), "until": end.isoformat(),
                                                            "queries": queries, "as_of": data["dataset"]["as_of"],
                                                            "version": data["dataset"]["version"]}), encoding="utf-8")

    return build_folder(store, pass_id, fill)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--store", type=Path, help="pass store (default: AISI_PASSES, else passes/ beside AISI_SNAPSHOTS)")
    parser.add_argument("--data", type=Path, default=DATA)
    commands = parser.add_subparsers(dest="command", required=True)
    state = commands.add_parser("state", help="prepare a per-state stale-fact re-check pass")
    state.add_argument("pass_id")
    state.add_argument("--who", required=True)
    state.add_argument("--states", required=True, help="comma-separated jurisdiction IDs")
    state.add_argument("--since", type=date.fromisoformat, help="search window start for every state")
    glob = commands.add_parser("global", help="prepare the monthly global topic sweep pass")
    glob.add_argument("pass_id")
    glob.add_argument("--who", required=True)
    glob.add_argument("--month", required=True, help="YYYY-MM; the window is the 60 days to its last day")
    args = parser.parse_args(argv)

    store = research_pass.store_directory(args.store)
    if store is None:
        print("NOT RUN: no pass store; set AISI_PASSES or AISI_SNAPSHOTS, or pass --store", file=sys.stderr)
        return NOT_RUN
    data = json.loads(args.data.read_text(encoding="utf-8"))
    if args.command == "state":
        snapshots = Path(os.environ.get("AISI_SNAPSHOTS", store.parent / "sources")).expanduser()
        folder = prepare_state(store, snapshots, data, args.pass_id, args.who,
                               [s.strip() for s in args.states.split(",") if s.strip()], args.since)
    else:
        folder = prepare_global(store, data, args.pass_id, args.who, args.month)
    print(folder)
    return 0


if __name__ == "__main__":
    sys.exit(main())
