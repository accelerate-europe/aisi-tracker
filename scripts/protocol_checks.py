"""Cheap checks of the search protocol (pilot, warn-only): do the logged passes follow the door lists and field cards?

Reads data/internal/doors.json and data/internal/field_searches.json (internal, export-ignored) and data/aisi.json.
Every finding is a warning; the exit code is 0 unless an input file is malformed. Run for the piloted institutes:

    uv run --group evidence python scripts/protocol_checks.py [--as-of YYYY-MM-DD] [--fetch-doors] [--snapshots DIR]

--fetch-doors fetches every door once (honest User-Agent, one request a second) and checks its marker phrase.
--snapshots (default AISI_SNAPSHOTS) enables the saved-copy scan of cited sources for empty slots.
The checks cannot judge whether a door was read well or a candidate judged correctly; review covers that.
"""

import argparse
import json
import os
import re
import ssl
import sys
import time
from datetime import date, timedelta
from pathlib import Path
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data" / "aisi.json"
DOORS = ROOT / "data" / "internal" / "doors.json"
LOG = ROOT / "data" / "internal" / "field_searches.json"
UA = {"User-Agent": "aisi-tracker/1.0 (contact@accelerate-europe.org)"}
try:  # the system store on some hosts lacks intermediates that curl fetches itself (www8.cao.go.jp)
    import certifi

    SSL = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    SSL = ssl.create_default_context()
READ = {"found", "none_found", "confirms_existing", "inconclusive"}
COVERED = READ | {"blocked", "absent"}
QUERY_TOOLS = {"exa", "web_search", "brave"}
INTERVAL_MONTHS = {"institute": 6, "planned": 3}


def empty_slots(inst: dict) -> dict[str, bool | None]:
    """Card -> whether any field the card covers shows no value (None: the card does not apply)."""
    no_institute = (inst.get("head") or {}).get("state") == "not_applicable"
    own_funding = [f for f in inst.get("funding") or [] if f.get("amount") is not None and f.get("coverage") == "whole_institute"]
    staff = inst.get("staff") or []
    testing = inst.get("testing_capacity") or {}
    access = inst.get("model_access") or {}
    return {
        "funding": not own_funding,
        "staff": not any(s.get("total") is not None for s in staff) or not any(s.get("technical") is not None for s in staff)
        or (inst.get("staffing_model") or {}).get("code") in (None, "not_published"),
        "head": None if no_institute else (inst.get("head") or {}).get("state") != "stated",
        "outputs": not inst.get("outputs"),
        "agreements": not inst.get("agreements"),
        "testing_capacity": (testing.get("state") in (None, "not_published") and not testing.get("modes"))
        or not access.get("access_events"),
        "national_partners": None if no_institute else (inst.get("national_partners") or {}).get("state") != "stated",
        "legal_basis": not inst.get("legal_basis") or not inst.get("host")
        or (inst.get("reports_to") or {}).get("state") not in ("stated", "not_applicable"),
        "wikidata": inst.get("wikidata") is None,
    }


def cited_sources(value) -> set[str]:
    """Every source ID cited anywhere in a record (top-level and field citations)."""
    found = set()
    if isinstance(value, dict):
        for key, item in value.items():
            if key in ("source_ids", "identifier_source_ids") and isinstance(item, list):
                found.update(x for x in item if isinstance(x, str))
            else:
                found |= cited_sources(item)
    elif isinstance(value, list):
        for item in value:
            found |= cited_sources(item)
    return found


def latest_pass(entries: list[dict]) -> str | None:
    return max((e["pass"] for e in entries), default=None)


def check(data: dict, doors: dict, log: dict, as_of: date) -> list[str]:
    warn = []
    institutes = {i["id"]: i for i in data["institutes"]}
    shared = {d["id"]: d for d in doors.get("shared_doors", [])}
    entries = log["entries"]
    for iid, spec in doors["institutes"].items():
        inst = institutes.get(iid)
        if inst is None:
            warn.append(f"{iid}: door list for an institute not in the dataset")
            continue
        tier = "planned" if (inst.get("head") or {}).get("state") == "not_applicable" else "institute"
        months = INTERVAL_MONTHS[tier]
        langs = set(spec["research_languages"])
        by_kind: dict[str, list[dict]] = {}
        for door in spec["doors"] + list(shared.values()):
            for kind in door["kinds"]:
                by_kind.setdefault(kind, []).append(door)
        for door in spec["doors"]:
            if door.get("status") != "absent" and not door.get("marker") and not door.get("how"):
                warn.append(f"{iid}/{door['id']}: door has no marker phrase")
        for card, empty in empty_slots(inst).items():
            if empty is None or card not in doors["cards"]:
                continue
            mine = [e for e in entries if e["institute_id"] == iid and e["field"] == card]
            last = latest_pass(mine)
            where = f"{iid}/{card}"
            if last is None or date.fromisoformat(last) < as_of - timedelta(days=round(months * 30.44)):
                warn.append(f"{where}: no pass within {months} months ({'empty' if empty else 'filled'} slot)")
                continue
            cur = [e for e in mine if e["pass"] == last]
            # door coverage: every door of every required kind was read in this pass (for any card: a source
            # opened is read for all fields) or is recorded as blocked or absent
            pass_all = [e for e in entries if e["institute_id"] == iid and e["pass"] == last]
            for kind in doors["cards"][card]["doors"]:
                for door in by_kind.get(kind, []):
                    got = [e for e in pass_all if e.get("door") == door["id"]]
                    if not got and door.get("status") == "absent":
                        continue
                    if not got:
                        warn.append(f"{where}: door {door['id']} ({kind}) not in the {last} pass")
                    elif not any(e["result"] in COVERED for e in got):
                        warn.append(f"{where}: door {door['id']} neither read nor recorded blocked or absent")
                if not by_kind.get(kind):
                    warn.append(f"{where}: no {kind} door on the list (record one as absent)")
            # languages: queries, not door reads
            if not doors["cards"][card].get("language_exempt"):
                queried = {e["language"] for e in cur if "door" not in e and e["tool"] in QUERY_TOOLS}
                for lang in sorted(langs - queried):
                    warn.append(f"{where}: no query in research language {lang}")
            # second engine before an empty close
            # a card that still has an empty field needs a second engine, even when other fields were confirmed
            engines = {e["tool"] for e in cur if e["tool"] in QUERY_TOOLS}
            if empty and len(engines) < 2 and card != "wikidata":
                warn.append(f"{where}: a field is still empty but only one engine was queried ({', '.join(sorted(engines)) or 'none'})")
            # stop reason: the last stop in entry order is the pass's state (a resumed pass keeps its id)
            stops = [e["stop"] for e in cur if e.get("stop")]
            final = stops[-1] if stops else None
            if final is None:
                warn.append(f"{where}: {last} pass has no stop reason")
            elif empty and final == "budget_out":
                warn.append(f"{where}: empty slot rests on a budget_out pass")
            elif empty and final == "incomplete":
                warn.append(f"{where}: empty slot rests on an incomplete pass (blocked door or open lead)")
            if final == "complete":
                required = {door["id"] for kind in doors["cards"][card]["doors"] for door in by_kind.get(kind, [])}
                latest = {e["door"]: e["result"] for e in pass_all if e.get("door") in required}  # entry order: last read wins
                blocked = sorted(d for d, result in latest.items() if result == "blocked")
                if blocked:
                    warn.append(f"{where}: complete although required door(s) {', '.join(blocked)} stayed blocked")
                pending = [p for p in log.get("pending", []) if p["institute_id"] == iid and card in p.get("fields", [])
                           and p.get("kind", "lead") == "lead" and not p.get("resolved")]  # open until resolved; a watch item never holds a pass open
                if any(e.get("open_lead") for e in cur) or pending:
                    warn.append(f"{where}: complete although a lead is still open")
    for item in log.get("pending", []):
        if not item.get("resolved") and date.fromisoformat(item["next_check"]) < as_of:
            warn.append(f"{item['institute_id']}: pending item past due ({item['next_check']}): {item['item'][:80]}")
    if len(log.get("pull_list", [])) > 5:
        warn.append(f"pull list has {len(log['pull_list'])} items; the cap is 5 a month")
    return warn


def fetch_doors(doors: dict) -> list[str]:
    warn = []
    for iid, spec in doors["institutes"].items():
        for door in spec["doors"]:
            url, marker = door.get("url"), door.get("marker")
            if not url or not marker or "apikey" in url or door.get("access"):
                continue
            try:
                with urlopen(Request(url, headers=UA), timeout=120, context=SSL) as r:
                    body = r.read(30_000_000).decode("utf-8", "replace")
                if marker not in body:
                    warn.append(f"{iid}/{door['id']}: marker {marker!r} missing (loading shell, redirect or changed page)")
            except Exception as exc:  # noqa: BLE001 - every failure is a door-health warning
                warn.append(f"{iid}/{door['id']}: fetch failed ({type(exc).__name__}: {exc})")
            time.sleep(1)
    return warn


def saved_copy_scan(data: dict, doors: dict, store: Path) -> list[str]:
    """Scan words of an empty card near the institute's names in the private copies of its cited sources."""
    sys.path.insert(0, str(ROOT / "scripts"))
    from snapshot_sources import copy_views, stored_copy  # noqa: PLC0415 - evidence group only

    warn = []
    sources = {s["id"]: s for s in data["sources"]}
    institutes = {i["id"]: i for i in data["institutes"]}
    fallback = ROOT / "work" / "snapshots"
    for iid, spec in doors["institutes"].items():
        inst = institutes[iid]
        names = [n for group in ("en", "de", "ja", "sw") for n in spec["names"].get(group, [])]
        name_re = re.compile("|".join(re.escape(n) for n in names), re.I)
        for card, empty in empty_slots(inst).items():
            words = doors["cards"][card]["scan_words"]
            if not empty or not words:
                continue
            word_re = re.compile("|".join(rf"\b{re.escape(w)}\b" if w.isascii() else re.escape(w) for w in words), re.I)
            for sid in sorted(cited_sources(inst)):
                src = sources.get(sid)
                if not src or not src.get("snapshot"):
                    continue
                try:
                    found = stored_copy(src, store, fallback)
                except ValueError:
                    continue
                if not found:
                    continue
                row, path = found
                text = " ".join(copy_views(path.read_bytes(), row.get("mime", ""), path))
                for m in name_re.finditer(text):
                    window = text[max(0, m.start() - 300): m.end() + 300]
                    hit = word_re.search(window)
                    if hit and doors["cards"][card].get("numeric"):
                        # figures only: a scan word counts when a digit stands within 30 characters of it
                        hit = next((h for h in word_re.finditer(window) if re.search(r"\d", window[max(0, h.start() - 30): h.end() + 30])), None)
                    if hit:
                        context = " ".join(window[max(0, hit.start() - 80): hit.end() + 80].split())
                        warn.append(f"{iid}/{card}: cited source {sid} has {hit.group(0)!r} near {m.group(0)!r}: …{context}…")
                        break
    return warn


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--as-of", type=date.fromisoformat, default=None, help="reference date (default: dataset as_of)")
    ap.add_argument("--fetch-doors", action="store_true", help="fetch each door and check its marker")
    ap.add_argument("--snapshots", type=Path, default=Path(os.environ["AISI_SNAPSHOTS"]) if os.environ.get("AISI_SNAPSHOTS") else None)
    args = ap.parse_args()
    data = json.loads(DATA.read_text(encoding="utf-8"))
    doors = json.loads(DOORS.read_text(encoding="utf-8"))
    log = json.loads(LOG.read_text(encoding="utf-8"))
    as_of = args.as_of or date.fromisoformat(data["dataset"]["as_of"])
    warn = check(data, doors, log, as_of)
    if args.fetch_doors:
        warn += fetch_doors(doors)
    if args.snapshots:
        warn += saved_copy_scan(data, doors, args.snapshots)
    else:
        print("saved-copy scan NOT RUN: no snapshot store (AISI_SNAPSHOTS or --snapshots)", file=sys.stderr)
    for w in warn:
        print(f"WARN {w}")
    print(f"{len(warn)} warnings (warn-only prototype)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
