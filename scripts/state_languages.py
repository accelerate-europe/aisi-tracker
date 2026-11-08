"""The internal per-jurisdiction language reference and the national-language flag derived from it.

data/internal/state_languages.json (export-ignored) holds, per jurisdiction in data/aisi.json, the language
statuses seeded from Unicode CLDR territory data, plus authored exceptions that cite a retrieved source where
CLDR does not match the tracker's rule. A search's flag is derived from its query language, jurisdiction and
date; an unresolved case is None (unknown), never guessed. A jurisdiction CLDR does not cover (the EU) is listed
in `exception_only` and counts only the languages its exceptions list. `cldr_checks` records where a CLDR-only
status was checked against primary sources that support it or are silent; it documents and does not change the
derivation (a contradiction is an exception).

    uv run scripts/state_languages.py seed --cldr work/languages/cldr-49.0.0/territoryInfo.json \\
        --version 49.0.0 --released 2026-10-22 --url <download URL>
    uv run scripts/state_languages.py report --frontload <searches.json>

`seed` rewrites only the `cldr` block and keeps `policy`, `aliases` and `exceptions`. `report` prints the
frontload flag contradictions and the English-only searched states resolved against the reference.
"""

import argparse
import hashlib
import json
import sys
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data" / "aisi.json"
REF = ROOT / "data" / "internal" / "state_languages.json"

# Strongest first; a base language with several script variants takes the strongest status.
CLDR_STATUSES = ("official", "de_facto_official", "official_regional")
STATUSES = set(CLDR_STATUSES) | {"national", "working", "link", "official_use", "unresolved", "not_official"}


def base_code(lang: str, aliases: dict) -> str:
    """Primary language subtag, lower case, with deprecated codes mapped by the reference's aliases."""
    code = lang.strip().replace("_", "-").split("-")[0].casefold()
    return aliases.get(code, code)


def cldr_block(territory_info: dict, jurisdictions: list[dict], meta: dict, aliases: dict) -> dict:
    """The generated block: per jurisdiction, each base language CLDR gives an official status."""
    out = {}
    for j in sorted(jurisdictions, key=lambda j: j["id"]):
        entry = territory_info.get(j["id"])
        if entry is None:
            continue
        langs: dict[str, dict] = {}
        for code, info in entry.get("languagePopulation", {}).items():
            status = info.get("_officialStatus")
            if status is None:
                continue
            if status not in CLDR_STATUSES:
                raise ValueError(f"{j['id']} {code}: unknown CLDR status {status}")
            base = base_code(code, aliases)
            slot = langs.setdefault(base, {"status": status, "cldr_codes": []})
            slot["cldr_codes"].append(code)
            if CLDR_STATUSES.index(status) < CLDR_STATUSES.index(slot["status"]):
                slot["status"] = status
        out[j["id"]] = {
            "name": j["name"],
            "languages": {k: langs[k] | {"cldr_codes": sorted(langs[k]["cldr_codes"])} for k in sorted(langs)},
        }
    return meta | {"jurisdictions": out}


def is_date(value) -> bool:
    """A canonical calendar date, so string comparison orders dates correctly."""
    try:
        return isinstance(value, str) and date.fromisoformat(value).isoformat() == value
    except ValueError:
        return False


def load(path: Path = REF) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def language_status(ref: dict, jurisdiction: str, lang: str, on: str) -> tuple[str | None, str]:
    """(status, basis) for one query language in one jurisdiction on a date (YYYY-MM-DD).

    An exception in force on the date overrides CLDR; a language CLDR lists without official status, or
    does not list, is not_official. A jurisdiction absent from both CLDR and the exceptions is (None, ...).
    """
    code = base_code(lang, ref.get("aliases", {}))
    for exc in ref.get("exceptions", []):
        if exc["jurisdiction"] != jurisdiction or code not in exc["languages"]:
            continue
        if exc.get("from") and on < exc["from"]:
            continue
        if exc.get("until") and on >= exc["until"]:
            continue
        return exc["languages"][code], f"exception {exc['id']}"
    entry = ref["cldr"]["jurisdictions"].get(jurisdiction)
    if entry is None:
        if jurisdiction in ref.get("exception_only", []):
            return "not_official", "not listed in the exceptions for a jurisdiction CLDR does not cover"
        return None, "jurisdiction not in the reference"
    if code in entry["languages"]:
        return entry["languages"][code]["status"], f"CLDR {ref['cldr']['version']}"
    return "not_official", f"no official status in CLDR {ref['cldr']['version']}"


def national_or_official(ref: dict, jurisdiction: str, lang: str | None, on: str) -> tuple[bool | None, str]:
    """The derived flag and its reason; None where the language, status or policy is unresolved."""
    if not lang or not lang.strip():
        return None, "no query language recorded"
    status, basis = language_status(ref, jurisdiction, lang, on)
    if status is None:
        return None, basis
    counts = ref["policy"]["counts"][status]
    return counts, f"{status} ({basis})" + ("" if counts is not None else "; policy open")


def citation_errors(where: str, sources: list) -> list[str]:
    """Each source needs a non-empty url, title, excerpt and a YYYY-MM-DD retrieval date."""
    errors = []
    for source in sources or [{}]:
        for key in ("url", "title", "retrieved", "excerpt"):
            value = source.get(key)
            if not isinstance(value, str) or not value.strip():
                errors.append(f"{where}: source {key} missing")
        if isinstance(source.get("retrieved"), str) and not is_date(source["retrieved"]):
            errors.append(f"{where}: source retrieved is not a YYYY-MM-DD date")
    return errors


def validate(ref: dict, data: dict) -> list[str]:
    """Structural checks: full coverage of the dataset's jurisdictions, known statuses, cited exceptions."""
    errors = []
    exception_only = set(ref.get("exception_only", []))
    covered = set(ref["cldr"]["jurisdictions"]) | exception_only
    errors += [f"{i}: exception_only but in CLDR" for i in sorted(exception_only & set(ref["cldr"]["jurisdictions"]))]
    errors += [f"{i}: exception_only without an exception" for i in sorted(
        exception_only - {e["jurisdiction"] for e in ref.get("exceptions", [])})]
    aliases = ref.get("aliases", {})
    ids = {j["id"] for j in data["jurisdictions"]}
    errors += [f"{i}: not in the reference" for i in sorted(ids - covered)]
    errors += [f"{i}: in the reference but not in the dataset" for i in sorted(covered - ids)]
    policy = ref["policy"]["counts"]
    errors += [f"policy: status {s} has no rule" for s in sorted(STATUSES - set(policy))]
    errors += [f"policy: unknown status {s}" for s in sorted(set(policy) - STATUSES)]
    for jid, entry in ref["cldr"]["jurisdictions"].items():
        for code, info in entry["languages"].items():
            if info["status"] not in CLDR_STATUSES:
                errors.append(f"{jid} {code}: unknown CLDR status {info['status']}")
            if base_code(code, aliases) != code:
                errors.append(f"{jid} {code}: stored key is not canonical")
    seen = Counter(e["id"] for e in ref.get("exceptions", []))
    errors += [f"exception {i}: duplicate id" for i, n in seen.items() if n > 1]
    for exc in ref.get("exceptions", []):
        where = f"exception {exc['id']}"
        for code, status in exc["languages"].items():
            if status not in STATUSES:
                errors.append(f"{where} {code}: unknown status {status}")
            if base_code(code, aliases) != code:
                errors.append(f"{where} {code}: stored key is not canonical")
        errors += citation_errors(where, exc.get("sources", []))
        bad_bounds = [k for k in ("from", "until") if exc.get(k) is not None and not is_date(exc[k])]
        errors += [f"{where}: {k} is not a YYYY-MM-DD date" for k in bad_bounds]
        if not bad_bounds and exc.get("from") and exc.get("until") and exc["from"] >= exc["until"]:
            errors.append(f"{where}: from is not before until")
        if not exc.get("basis", "").strip():
            errors.append(f"{where}: basis missing (why CLDR does not match the rule)")
    for chk in ref.get("cldr_checks", []):
        where = f"cldr_check {chk.get('jurisdiction')} {chk.get('language')}"
        entry = ref["cldr"]["jurisdictions"].get(chk.get("jurisdiction"), {"languages": {}})
        if chk.get("language") not in entry["languages"]:
            errors.append(f"{where}: CLDR gives no status to check")
        if chk.get("outcome") not in ("supports", "silent"):
            errors.append(f"{where}: outcome must be supports or silent; a contradiction needs an exception")
        errors += citation_errors(where, chk.get("sources", []))
    by_pair = defaultdict(list)
    for exc in ref.get("exceptions", []):
        if any(exc.get(k) is not None and not is_date(exc[k]) for k in ("from", "until")):
            continue  # reported above; spans compare only valid dates
        for code in exc["languages"]:
            by_pair[(exc["jurisdiction"], code)].append(exc)
    for (jid, code), excs in by_pair.items():
        spans = sorted((e.get("from") or "", e.get("until") or "9999") for e in excs)
        if any(a[1] > b[0] for a, b in zip(spans, spans[1:])):
            errors.append(f"{jid} {code}: overlapping exceptions")
    return errors


def seed(args) -> int:
    raw = Path(args.cldr).read_bytes()
    territory_info = json.loads(raw)["supplemental"]["territoryInfo"]
    data = json.loads(DATA.read_text(encoding="utf-8"))
    ref = load() if REF.exists() else {"about": "", "policy": {"counts": {}}, "aliases": {}, "exception_only": [],
                                       "exceptions": []}
    meta = {
        "source": "Unicode CLDR territoryInfo (languagePopulation officialStatus)",
        "version": args.version,
        "released": args.released,
        "url": args.url,
        "sha256": hashlib.sha256(raw).hexdigest(),
    }
    ref["cldr"] = cldr_block(territory_info, data["jurisdictions"], meta, ref.get("aliases", {}))
    REF.write_text(json.dumps(ref, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    errors = validate(ref, data)
    print("\n".join(errors) or f"{REF.relative_to(ROOT)}: {len(ref['cldr']['jurisdictions'])} jurisdictions", file=sys.stderr)
    return 1 if errors else 0


def frontload_pairs(ref: dict, log: dict, institutes: dict) -> list[dict]:
    """Per institute and query language: recorded flag values against the derived flag."""
    pairs = defaultdict(list)
    for s in log["searches"]:
        pairs[(s["institute"], s["language"])].append(s)
    rows = []
    for (inst, lang), searches in sorted(pairs.items()):
        jur = institutes[inst]
        derived = {national_or_official(ref, jur, lang, s["date"]) for s in searches}
        recorded = Counter(s["national_or_official"] for s in searches)
        rows.append({"institute": inst, "jurisdiction": jur, "language": lang, "recorded": dict(recorded),
                     "derived": sorted(derived, key=str)})
    return rows


def english_only_states(ref: dict, data: dict) -> list[dict]:
    """None-identified states whose recorded searches are all in English, with the derived flag."""
    rows = []
    for j in data["jurisdictions"]:
        if j["kind"] != "state" or j.get("institute_id"):
            continue
        searches = j["check"].get("searches", [])
        if not searches or {base_code(q["language"], {}) for q in searches} != {"en"}:
            continue
        derived = {national_or_official(ref, j["id"], q["language"], q["date"]) for q in searches}
        rows.append({"id": j["id"], "name": j["name"], "confidence": j.get("status_confidence"),
                     "recorded": sorted({q["national_or_official"] for q in searches}), "derived": sorted(derived, key=str)})
    return rows


def check_search_mismatches(ref: dict, data: dict) -> list[dict]:
    """Every check.searches entry whose recorded flag differs from the derived one."""
    rows = []
    for j in data["jurisdictions"]:
        for q in j["check"].get("searches", []):
            flag, reason = national_or_official(ref, j["id"], q["language"], q["date"])
            if flag != q["national_or_official"]:
                rows.append({"id": j["id"], "language": q["language"], "date": q["date"],
                             "recorded": q["national_or_official"], "derived": flag, "reason": reason})
    return rows


def fmt(derived: list) -> str:
    return "; ".join(f"{flag} ({reason})" for flag, reason in derived)


def report(args) -> int:
    ref, data = load(), json.loads(DATA.read_text(encoding="utf-8"))
    errors = validate(ref, data)
    if errors:
        print("\n".join(errors), file=sys.stderr)
        return 1
    institutes = {i["id"]: i["jurisdiction_id"] for i in data["institutes"]}
    log = json.loads(Path(args.frontload).read_text(encoding="utf-8"))
    rows = frontload_pairs(ref, log, institutes)
    conflicts = [r for r in rows if len(r["recorded"]) > 1]
    print(f"## Frontload log: {len(conflicts)} institute-language pairs with contradictory flags\n")
    print("| Institute | Language | Recorded (true/false) | Derived |\n| --- | --- | --- | --- |")
    for r in conflicts:
        rec = f"{r['recorded'].get(True, 0)}/{r['recorded'].get(False, 0)}"
        print(f"| {r['institute']} | {r['language']} | {rec} | {fmt(r['derived'])} |")
    eng = english_only_states(ref, data)
    print(f"\n## None-identified states searched only in English: {len(eng)}\n")
    print("| State | Confidence | Recorded | Derived |\n| --- | --- | --- | --- |")
    for r in eng:
        print(f"| {r['id']} {r['name']} | {r['confidence']} | {r['recorded']} | {fmt(r['derived'])} |")
    mism = check_search_mismatches(ref, data)
    print(f"\n## check.searches entries whose recorded flag differs from the derived flag: {len(mism)}\n")
    print("| Jurisdiction | Language | Date | Recorded | Derived | Reason |\n| --- | --- | --- | --- | --- | --- |")
    for r in mism:
        print(f"| {r['id']} | {r['language']} | {r['date']} | {r['recorded']} | {r['derived']} | {r['reason']} |")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="command", required=True)
    s = sub.add_parser("seed", help="rewrite the CLDR block from a territoryInfo.json download")
    s.add_argument("--cldr", required=True, help="path to cldr-json territoryInfo.json")
    s.add_argument("--version", required=True)
    s.add_argument("--released", required=True, help="CLDR release date, YYYY-MM-DD")
    s.add_argument("--url", required=True, help="the URL the file was downloaded from")
    r = sub.add_parser("report", help="frontload contradictions and English-only states against the reference")
    r.add_argument("--frontload", required=True, help="path to the frontload searches.json")
    args = ap.parse_args(argv)
    return seed(args) if args.command == "seed" else report(args)


if __name__ == "__main__":
    sys.exit(main())
