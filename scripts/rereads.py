"""Per-quote re-read states from the source monitor's check log (docs/design/claims-1.1.md, section 4).

`export` replays the monitor's private check rows into `data/rereads.json` (keyed by source id, then quote hash:
`state`, `last_found`, `since`, `last_attempt`); `monitor_sources.py --rereads` is its only writer. The file holds
observed states only. `quote_states` adds what depends on `dataset.as_of` (`not_attempted`, deadlines), so the build
is deterministic for a given file; `join` publishes `sources[].excerpts[].reread` and `claims[].reread`/`expired`;
`alerts` lists the maintenance alerts.

States, per quote. A failure state starts at the first attempt that shows it (`since`) and holds until an attempt
finds the quote again; a later access failure never resets it. A confirmed loss replaces an access failure and keeps
its own start. `last_found` moves only when the quote is found. A Wayback copy is not a re-read.
"""

from __future__ import annotations

import calendar
import hashlib
import json
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REREADS = ROOT / "data/rereads.json"
LIVE_ROUTES = {"direct", "headless", "jina", "headless+jina"}
LOSSES = ("quote_gone", "source_gone")
# Worse states rank higher; a leaf takes the best of its excerpts, a claim the worst of its required leaves.
RANK = {"confirmed": 0, "not_attempted": 1, "blocked_or_failed": 2, "quote_gone": 3, "source_gone": 3}
NOT_ATTEMPTED_DAYS = 14
TRIAGE_DAYS = 7
STALE_FILE_DAYS = 8
DEADLINE_MONTHS = {"quote_gone": 1, "source_gone": 1, "blocked_or_failed": 6}
BLOCKED_ALERT_MONTHS = 5
DEVELOPMENT_YEARS = 3
CLAIM_LEAVES = {"development": ("text", "act", "date")}


def quote_id(text: str) -> str:
    """The monitor's quote identity, which is also the excerpt identity (references.excerpt_identity)."""
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def bound(source: dict, excerpt: dict) -> bool:
    """The monitor's binding rule (snapshot_sources.bound_quotes): the excerpt's copy is the source itself."""
    return excerpt.get("url") in {None, source["url"], source.get("archive_url")}


def add_months(day: date, months: int) -> date:
    month = day.month - 1 + months
    year, month = day.year + month // 12, month % 12 + 1
    return date(year, month, min(day.day, calendar.monthrange(year, month)[1]))


def evidence(row: dict, qid: str, own_archive: bool = False) -> str | None:
    """What one check row says about one quote: a state, or None when it says nothing about it.

    own_archive: the source's own URL is an archive capture, so re-reading that capture is a re-read; otherwise an
    archive observation (a Wayback route, or a request that ended at an archive) never confirms.
    """
    klass = row["class"]
    if klass == "skipped":
        return None
    if klass == "gone":
        return "source_gone"
    quote = next((q for q in row.get("quotes", []) if q["id"] == qid), None)
    if quote and not quote["in_cited_copy"] and not quote["in_checked_copy"]:
        # Carried by neither copy: the monitor cannot compare this quote, whatever the row's class.
        return None
    lost = bool(quote and quote["in_cited_copy"] and not quote["in_checked_copy"])
    # Only a live re-read confirms or loses a quote; an archive observation is at most an access failure.
    live = row["route"] in LIVE_ROUTES and (row.get("observation") == "live" or own_archive)
    if live and qid in row.get("confirmed_quote_ids", ()):
        return "quote_gone"
    if live and klass == "substantive" and lost and "confirmed_quote_ids" not in row:
        return "quote_gone"
    if live and klass not in {"blocked", "archived"} and quote and quote["in_checked_copy"]:
        return "confirmed"
    if klass in {"blocked", "archived", "failed"} or lost or not live:
        # An access failure, a fallback loss only one route saw, or a copy that is not a live re-read.
        return "blocked_or_failed"
    return None


def step(entry: dict, found: str, day: str) -> dict:
    entry = {**entry, "last_attempt": day}
    if found == "confirmed":
        return {**entry, "state": "confirmed", "last_found": day, "since": None}
    if entry["state"] in LOSSES or (found == "blocked_or_failed" and entry["state"] == "blocked_or_failed"):
        return entry
    return {**entry, "state": found, "since": day}


def export(data: dict, rows: list[dict], *, is_archive) -> dict:
    """Replay every check row, oldest first, for each bound quote of each source in the dataset.

    The source's own copy (`snapshot.retrieved`) counts as a finding of each bound quote: the copies step checks
    that the cited copy carries it. is_archive(url) says whether a URL is an archive capture (the monitor passes
    snapshot_sources.archive_snapshot).
    """
    by_source: dict[str, list[dict]] = {}
    for row in rows:
        by_source.setdefault(row["source_id"], []).append(row)
    result: dict[str, dict] = {}
    for source in data["sources"]:
        retrieved = (source.get("snapshot") or {}).get("retrieved")
        events = [(retrieved, None)] if retrieved else []
        events += [(row["checked"], row) for row in by_source.get(source["id"], []) if row.get("checked")]
        events.sort(key=lambda event: (event[0], event[1] is not None))
        quotes, own_archive = {}, is_archive(source["url"])
        for excerpt in source.get("excerpts", []):
            if not bound(source, excerpt):
                continue
            qid = quote_id(excerpt["text"])
            entry = {"state": "confirmed", "last_found": None, "since": None, "last_attempt": None}
            for stamp, row in events:
                found = "confirmed" if row is None else evidence(row, qid, own_archive)
                if found:
                    entry = step(entry, found, stamp[:10])
            if entry["last_attempt"]:
                quotes[qid] = entry
        if quotes:
            result[source["id"]] = dict(sorted(quotes.items()))
    stamps = [row["checked"] for row in rows if row.get("checked")]
    return {"checked_to": max(stamps)[:10] if stamps else None,
            "runs": sorted({row["run_id"] for row in rows}), "sources": result}


def load(path: Path = REREADS) -> dict:
    if not path.is_file():
        return {"checked_to": None, "runs": [], "sources": {}}
    return json.loads(path.read_text(encoding="utf-8"))


def effective(entry: dict, as_of: str) -> dict:
    """The quote's state as of `as_of`: a confirmed quote without an attempt for 14 days is not_attempted."""
    state = entry["state"]
    if state == "confirmed" and (not entry["last_attempt"] or
                                 date.fromisoformat(entry["last_attempt"]) <= date.fromisoformat(as_of) - timedelta(days=NOT_ATTEMPTED_DAYS)):
        state = "not_attempted"
    return {"state": state, "last_found": entry["last_found"], "since": entry["since"]}


def quote_states(data: dict, rereads: dict) -> dict[tuple[str, str], dict]:
    """(source id, quote id) -> full entry, for every excerpt; a quote without a row takes `snapshot.retrieved`."""
    known = rereads.get("sources", {})
    states = {}
    for source in data["sources"]:
        retrieved = (source.get("snapshot") or {}).get("retrieved")
        day = retrieved[:10] if retrieved else None
        for excerpt in source.get("excerpts", []):
            qid = quote_id(excerpt["text"])
            entry = known.get(source["id"], {}).get(qid)
            if entry is None and day is None:
                continue
            states[source["id"], qid] = entry or {"state": "confirmed", "last_found": day, "since": None, "last_attempt": day}
    return states


def deadline(reread: dict) -> date | None:
    months = DEADLINE_MONTHS.get(reread["state"])
    return add_months(date.fromisoformat(reread["since"]), months) if months else None


def ordinal(day: str | None) -> int:
    return date.fromisoformat(day).toordinal() if day else 0


def best(rereads: list[dict]) -> dict:
    """Lowest rank; among equals the latest `since` (the later deadline), then the latest `last_found`, then the state."""
    return min(rereads, key=lambda r: (RANK[r["state"]], -ordinal(r["since"]), -ordinal(r["last_found"]), r["state"]))


def worst(rereads: list[dict]) -> dict:
    """Highest rank; among equals the earliest `since` (the earlier deadline), then the earliest `last_found`, then the state."""
    return max(rereads, key=lambda r: (RANK[r["state"]], -ordinal(r["since"]), -ordinal(r["last_found"]), r["state"]))


def latest_day(day: str) -> date:
    """The last day a partial date (YYYY or YYYY-MM) can mean, so a partial date never expires early."""
    if len(day) == 4:
        return date(int(day), 12, 31)
    if len(day) == 7:
        year, month = int(day[:4]), int(day[5:])
        return date(year, month, calendar.monthrange(year, month)[1])
    return date.fromisoformat(day)


def claim_expiry(claim: dict, reread: dict | None, as_of: str) -> date | None:
    """The first day the claim is out of the summary, when that day is on or before `as_of`; else None.

    It is the day after its deadline: a loss more than a month old, an access failure more than six months old, or a
    development dated more than three years before `as_of`.
    """
    ends = []
    if reread and (end := deadline(reread)):
        ends.append(end)
    if claim.get("kind") == "development" and claim.get("date"):
        ends.append(add_months(latest_day(claim["date"]), 12 * DEVELOPMENT_YEARS))
    passed = [end + timedelta(days=1) for end in ends if end < date.fromisoformat(as_of)]
    return min(passed) if passed else None


def claim_owners(published: dict):
    for collection in ("jurisdictions", "institutes"):
        for index, record in enumerate(published.get(collection, [])):
            for number, claim in enumerate(record.get("claims", [])):
                yield f"/{collection}/{index}/claims/{number}", claim


def previous_release(published: dict) -> str | None:
    """The date of the latest changelog entry other than the current version's."""
    dates = [c["date"] for c in published.get("changelog", []) if c["version"] != published["dataset"]["version"]]
    return max(dates) if dates else None


def listed(ids: list[str], shown: int = 8) -> str:
    return ", ".join(ids[:shown]) + (f" and {len(ids) - shown} more" if len(ids) > shown else "")


def alerts(data: dict, rereads: dict, as_of: str) -> list[str]:
    """Maintenance alerts as of `as_of`: overdue triage, long access failures, quotes not attempted, a stale file
    and, when `data` is published (claims carry `reread`), claims that expired since the last release."""
    day = date.fromisoformat(as_of)
    lines = []
    if not rereads.get("checked_to"):
        lines.append("data/rereads.json has no monitor results; run scripts/monitor_sources.py --rereads")
    elif date.fromisoformat(rereads["checked_to"]) < day - timedelta(days=STALE_FILE_DAYS):
        lines.append(f"data/rereads.json reflects checks up to {rereads['checked_to']}, more than {STALE_FILE_DAYS} days before {as_of}; "
                     "run scripts/monitor_sources.py --rereads")
    overdue: dict[tuple[str, str, str], int] = {}
    unattempted: dict[str, int] = {}
    for (sid, _), entry in sorted(quote_states(data, rereads).items()):
        state = entry["state"]
        since = date.fromisoformat(entry["since"]) if entry["since"] else None
        if (state in LOSSES and since < day - timedelta(days=TRIAGE_DAYS)) or (
                state == "blocked_or_failed" and add_months(since, BLOCKED_ALERT_MONTHS) < day):
            key = (sid, state, entry["since"])
            overdue[key] = overdue.get(key, 0) + 1
        if not entry["last_attempt"] or date.fromisoformat(entry["last_attempt"]) <= day - timedelta(days=NOT_ATTEMPTED_DAYS):
            unattempted[sid] = unattempted.get(sid, 0) + 1
    words = {"quote_gone": "quote not found in the source", "source_gone": "offline", "blocked_or_failed": "could not be re-read"}
    for (sid, state, since), count in sorted(overdue.items(), key=lambda item: (item[0][0], -RANK[item[0][1]], item[0][2])):
        start = date.fromisoformat(since)
        due = f"triage was due {start + timedelta(days=TRIAGE_DAYS)}" if state in LOSSES \
            else f"its claims leave summaries after {add_months(start, DEADLINE_MONTHS[state])}"
        lines.append(f"source {sid}: {words[state]} since {since} ({count} {'quote' if count == 1 else 'quotes'}); {due}")
    if unattempted:
        total = sum(unattempted.values())
        lines.append(f"{total} {'quote' if total == 1 else 'quotes'} in {len(unattempted)} {'source' if len(unattempted) == 1 else 'sources'} "
                     f"had no re-read attempt for {NOT_ATTEMPTED_DAYS} days: {listed(sorted(unattempted))}")
    last = previous_release(data)
    for pointer, claim in claim_owners(data):
        if "reread" not in claim and claim.get("kind") != "development":
            continue
        end = claim_expiry(claim, claim.get("reread"), as_of)
        if end and (last is None or end > date.fromisoformat(last)):
            lines.append(f"claim {claim['id']} ({pointer}) left the summary on {end}")
    return lines


def join(published: dict, rereads: dict) -> dict:
    """Publish per-quote and per-claim re-read states (excerpt supports are RFC 6901 pointers here)."""
    as_of = published["dataset"]["as_of"]
    states = quote_states(published, rereads)
    leaves: dict[str, list[dict]] = {}
    for source in published["sources"]:
        for excerpt in source.get("excerpts", []):
            entry = states.get((source["id"], quote_id(excerpt["text"])))
            if entry is None:
                continue
            excerpt["reread"] = effective(entry, as_of)
            for pointer in excerpt["supports"]:
                leaves.setdefault(pointer, []).append(excerpt["reread"])
    for pointer, claim in claim_owners(published):
        found = [best(leaves[f"{pointer}/{leaf}"]) for leaf in CLAIM_LEAVES.get(claim["kind"], ("text",)) if f"{pointer}/{leaf}" in leaves]
        reread = worst(found) if found else None
        if reread:
            claim["reread"] = dict(reread)
        claim["expired"] = claim_expiry(claim, reread, as_of) is not None
    return published
