"""Change monitor for cited sources: re-fetch, compare with the cited copy, triage.

Read-only for the dataset and the private store. For each source with a cited snapshot it
fetches the live URL once (honest User-Agent, per-host pacing, no reader proxies or
archives), then classifies the result:

- unchanged: same file hash or same normalised text hash (text_scheme of snapshot_sources).
- trivial: the text hash differs, but every bound quotation the cited copy carries is
  still present or, without such quotations, a long run of the cited text survives.
- substantive: a bound quotation the cited copy carries is missing from the new copy
  (``quote_lost``), or, for a like-for-like copy without such quotations, no long run of
  the cited text survives (``text_changed``).
- gone: HTTP 404 or 410, or the host no longer resolves.
- blocked: the site refuses automated readers (401, 403, 429, 451, challenge, login,
  error, JavaScript or cookie shells, or a near-empty response carrying no quotation).
  Reported as a class; never worked around.
- failed: transient or tool failures (timeouts, 5xx, too large, unparseable, no cited copy).

Quotation presence uses the verifier's own views (copy_views, literal_match), so a
quotation counts as lost only when the cited copy carried it and no view of the new copy
does. The report also lists jurisdictions whose ``next_check`` is due and a discovery
queue of recorded searches for states without an institute (queries only; it does not
search). A seen-leads ledger (--lead) remembers pages discovery read but did not cite,
so a later search skips them until their text hash changes.

    uv run --group evidence python scripts/monitor_sources.py --report work/monitor
"""

from __future__ import annotations

import argparse
import json
import re
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit

try:
    from .snapshot_sources import (
        PRIVATE_SNAPSHOTS,
        ROOT,
        copy_views,
        main_text,
        stored_copy,
        text_fields,
    )
    from .verify_excerpts import Fetcher, literal_match
except ImportError:
    from snapshot_sources import (  # type: ignore[no-redef]  # Direct script entry point.
        PRIVATE_SNAPSHOTS,
        ROOT,
        copy_views,
        main_text,
        stored_copy,
        text_fields,
    )
    from verify_excerpts import Fetcher, literal_match  # type: ignore[no-redef]

CLASSES = ("substantive", "gone", "trivial", "unchanged", "blocked", "failed")
BLOCKED_STATUSES = {
    "login_or_challenge",
    "error_or_challenge",
    "javascript_shell",
    "cookie_settings_shell",
    "empty_render",
    "document_viewer",
    "archive_wrapper",
}
BLOCKED_HTTP = re.compile(r"HTTP (?:401|403|429|451)")
GONE_HTTP = re.compile(r"HTTP (?:404|410)")
GONE_DNS = re.compile(r"nodename nor servname|Name or service not known|getaddrinfo failed")
# Page-level change, used only where no bound quotation decides. News and portal pages
# rotate sidebars and "read next" lists around an unchanged article, so the share of
# changed text says little; the longest run of the cited text that survives intact does.
SHINGLE = 5
# The page counts as rewritten when its longest surviving run is shorter than this many
# words, or than half the cited text when that is shorter.
KEPT_RUN = 50
# A response with less text than this (non-space characters, so unspaced scripts count
# fully), or than a tenth of a like-for-like cited text, that carries none of the
# quotations is a shell the verifier did not recognise (bot challenge, empty WAF
# response, restricted-access redirect), not a change.
SHELL_CHARS = 250


def shingles(text: str) -> set[tuple[str, ...]]:
    words = text.lower().split()
    return {tuple(words[i : i + SHINGLE]) for i in range(max(1, len(words) - SHINGLE + 1))}


def text_change(old: str, new: str) -> dict:
    """Shingle shares removed and added, and the longest run of cited words still present."""
    a_words, b_words = old.lower().split(), new.lower().split()
    a, b = shingles(old), shingles(new)
    positions: dict[tuple[str, ...], list[int]] = defaultdict(list)
    for j in range(max(0, len(b_words) - SHINGLE + 1)):
        positions[tuple(b_words[j : j + SHINGLE])].append(j)
    # Longest run of consecutive cited shingles found at consecutive new positions.
    previous: dict[int, int] = {}
    longest = 0
    for i in range(max(0, len(a_words) - SHINGLE + 1)):
        current = {
            j: previous.get(j - 1, 0) + 1 for j in positions.get(tuple(a_words[i : i + SHINGLE]), ())
        }
        longest = max([longest, *current.values()])
        previous = current
    return {
        "old_words": len(a_words),
        "new_words": len(b_words),
        "old_chars": len("".join(a_words)),
        "new_chars": len("".join(b_words)),
        "removed": round(len(a - b) / len(a), 3) if a else 0.0,
        "added": round(len(b - a) / len(b), 3) if b else 0.0,
        "kept_run": longest + SHINGLE - 1 if longest else 0,
    }


def bound(source: dict, excerpt: dict) -> bool:
    """Same binding rule as snapshot_sources.bound_quotes."""
    return excerpt.get("url") in {None, source["url"], source.get("archive_url")}


def present(quote: str, views: list[str]) -> bool:
    return any(literal_match(quote, view) for view in views)


def label(path: list) -> str:
    """A supports path or citing location as record › key › id › field."""
    parts = []
    for step in path:
        if isinstance(step, dict):
            parts.append(next(iter(step.values()), "?"))
        elif not isinstance(step, int):
            parts.append(str(step))
    return " › ".join(parts)


def citing_records(data: dict) -> dict[str, list[str]]:
    """source ID -> locations that cite it through source_ids or source_id."""
    found: dict[str, list[str]] = defaultdict(list)

    def walk(node, path):
        if isinstance(node, dict):
            for k, v in node.items():
                if k == "source_ids" and isinstance(v, list):
                    for sid in v:
                        found[sid].append(label(path))
                elif k == "source_id" and isinstance(v, str):
                    found[v].append(label(path))
                else:
                    walk(v, path + [k])
        elif isinstance(node, list):
            for item in node:
                has_id = isinstance(item, dict) and isinstance(item.get("id"), str)
                walk(item, path + [{"id": item["id"]}] if has_id else path)

    for collection in ("jurisdictions", "institutes"):
        walk(data[collection], [collection])
    return {sid: sorted(set(paths)) for sid, paths in found.items()}


def fetch_class(meta: dict) -> tuple[str | None, str]:
    """Class decided by the fetch alone (gone, blocked, failed), or None to compare."""
    status, error = meta.get("status", ""), meta.get("error") or ""
    if status == "ok" or status in {"no_extractable_text", "extraction_error"}:
        return None, status
    if status == "http_error" and GONE_HTTP.fullmatch(error):
        return "gone", error
    if status == "unreachable" and GONE_DNS.search(error):
        return "gone", "host does not resolve"
    if status in BLOCKED_STATUSES or (
        status == "http_error" and BLOCKED_HTTP.fullmatch(error)
    ):
        return "blocked", error or status
    return "failed", f"{status}: {error}" if error else status


def compare(source: dict, fetcher: Fetcher, directory: Path, fallback: Path) -> dict:
    row = {"source_id": source["id"], "url": source["url"]}
    try:
        found = stored_copy(source, directory, fallback)
    except ValueError as exc:
        return {**row, "class": "failed", "reason": f"cited copy: {exc}"}
    if found is None:
        return {**row, "class": "failed", "reason": "no cited copy held privately"}
    manifest, old_path = found
    row["cited_method"] = manifest["method"]
    meta = fetcher.fetch(source["url"], "live")
    row["fetch_status"] = meta.get("status")
    if meta.get("final_url") and meta["final_url"] != source["url"]:
        row["final_url"] = meta["final_url"]
    klass, reason = fetch_class(meta)
    if klass:
        return {**row, "class": klass, "reason": reason}
    new_path = fetcher.entry(source["url"]) / "response.bin"
    new_body, old_body = new_path.read_bytes(), old_path.read_bytes()
    previous = source["snapshot"]
    if meta["sha256"] == previous["sha256"]:
        return {**row, "class": "unchanged", "reason": "same_file"}
    mime = meta.get("content_type", "")
    old_views = copy_views(old_body, manifest.get("mime", ""), old_path)
    new_views = copy_views(new_body, mime, new_path)
    if not any(view.strip() for view in new_views):
        # An unreadable copy cannot show that a quotation or the text has gone.
        return {**row, "class": "failed", "reason": "new copy has no readable text"}
    quotes = []
    for excerpt in source.get("excerpts", []):
        if not bound(source, excerpt):
            continue
        was = present(excerpt["text"], old_views)
        now = present(excerpt["text"], new_views)
        quotes.append(
            {
                "text": excerpt["text"],
                "supports": [label(p) for p in excerpt["supports"]],
                "in_cited_copy": was,
                "in_new_copy": now,
            }
        )
    row["quotes"] = quotes
    lost = [q for q in quotes if q["in_cited_copy"] and not q["in_new_copy"]]
    fields = text_fields(new_body, mime)
    # The text hash leaves out page furniture, so a quotation there is checked first.
    if not lost and fields and fields.get("text_sha256") == previous.get("text_sha256"):
        return {**row, "class": "unchanged", "reason": "same_text"}
    like_for_like = manifest["method"] == "live" and manifest["requested_url"] == source["url"]
    try:
        change = text_change(
            main_text(old_body, manifest.get("mime", "")), main_text(new_body, mime)
        )
    except Exception as exc:  # noqa: BLE001 - an unparseable copy is compared by quotations only
        change = {"error": f"{type(exc).__name__}: {exc}"}
    row["text_change"] = change
    row["like_for_like"] = like_for_like
    kept_any = any(q["in_new_copy"] for q in quotes)
    if (
        "new_chars" in change
        and not kept_any
        and (
            change["new_chars"] < SHELL_CHARS
            # A rendered or archived cited copy carries its own furniture: compare sizes
            # only like for like.
            or (like_for_like and change["new_chars"] < change["old_chars"] / 10)
        )
    ):
        return {**row, "class": "blocked", "reason": "near_empty_response"}
    if lost:
        return {**row, "class": "substantive", "reason": "quote_lost"}
    carried = [q for q in quotes if q["in_cited_copy"]]
    if carried:
        reason = "quotes_kept"
    elif "error" in change:
        return {**row, "class": "failed", "reason": f"text comparison: {change['error']}"}
    elif (
        like_for_like
        and "kept_run" in change
        and change["kept_run"] < min(KEPT_RUN, change["old_words"] / 2)
    ):
        return {**row, "class": "substantive", "reason": "text_changed"}
    else:
        reason = "small_change"
    if not like_for_like:
        reason += "_cited_copy_other_format"
    return {**row, "class": "trivial", "reason": reason}


def due_checks(data: dict, today: date, horizon: int) -> list[dict]:
    rows = []
    for record in data["jurisdictions"]:
        nc = record.get("next_check")
        if not nc:
            continue
        when = date.fromisoformat(nc["date"])
        if when <= today + timedelta(days=horizon):
            rows.append(
                {
                    "id": record["id"],
                    "name": record["name"],
                    "date": nc["date"],
                    "due": when <= today,
                    "reason": nc["reason"],
                }
            )
    return sorted(rows, key=lambda r: (r["date"], r["id"]))


def discovery_queue(data: dict) -> list[dict]:
    """States without an institute, oldest recorded search first, with their queries."""
    rows = []
    for record in data["jurisdictions"]:
        if record.get("institute_id") or record.get("kind") != "state":
            continue
        searches = record.get("check", {}).get("searches", [])
        last = max((s["date"] for s in searches), default="")
        rows.append(
            {
                "id": record["id"],
                "name": record["name"],
                "last_search": last or None,
                "queries": [
                    {"language": s.get("language"), "query": s["query"]} for s in searches
                ],
            }
        )
    return sorted(rows, key=lambda r: (r["last_search"] or "", r["id"]))


def markdown(report: dict, sources: dict[str, dict], cites: dict[str, list[str]]) -> str:
    rows = report["sources"]
    counts = report["counts"]
    out = [
        "# Source change monitor",
        "",
        f"Run {report['started']}, {report['seconds']:.0f} s, {len(rows)} sources, "
        f"dataset {report['dataset_version']}.",
        "",
        "| Class | Sources |",
        "| --- | --- |",
        *(f"| {k} | {counts.get(k, 0)} |" for k in CLASSES),
        "",
    ]
    lost = [r for r in rows if r.get("reason") == "quote_lost"]
    out += ["## Quotations no longer present", ""]
    if not lost:
        out.append("None.")
    for r in lost:
        src = sources[r["source_id"]]
        out.append(f"- [{src['title']}]({r['url']}) (`{r['source_id']}`)")
        for q in r["quotes"]:
            if q["in_cited_copy"] and not q["in_new_copy"]:
                out.append(f"    - \"{q['text'][:200]}\" supports {'; '.join(q['supports'])}")
    out += ["", "## Page text changed (no quotation lost)", ""]
    changed = [r for r in rows if r.get("reason") == "text_changed"]
    if not changed:
        out.append("None.")
    for r in changed:
        src = sources[r["source_id"]]
        tc = r["text_change"]
        where = "; ".join(cites.get(r["source_id"], [])[:4]) or "excerpt-supported fields"
        out.append(
            f"- [{src['title']}]({r['url']}) (`{r['source_id']}`): "
            f"longest surviving run {tc['kept_run']} of {tc['old_words']} words; "
            f"cited by {where}"
        )
    out += ["", "## Gone", ""]
    gone = [r for r in rows if r["class"] == "gone"]
    if not gone:
        out.append("None.")
    for r in gone:
        src = sources[r["source_id"]]
        archive = f", [archive]({src['archive_url']})" if src.get("archive_url") else ""
        n = len(src.get("excerpts", []))
        out.append(f"- [{src['title']}]({r['url']}) (`{r['source_id']}`): {r['reason']}; {n} excerpts{archive}")
    blocked = [r for r in rows if r["class"] == "blocked"]
    hosts = Counter(urlsplit(r["url"]).netloc for r in blocked)
    reasons = Counter(r["reason"] for r in blocked)
    out += [
        "",
        "## Blocked (reported as a class)",
        "",
        f"{len(blocked)} sources. Reasons: "
        + ", ".join(f"{k} {v}" for k, v in reasons.most_common())
        + ".",
        "",
        "Hosts with most blocked sources: "
        + ", ".join(f"{h} ({n})" for h, n in hosts.most_common(12))
        + ".",
        "",
        "## Failed",
        "",
    ]
    failed = Counter(r["reason"].split(":")[0] for r in rows if r["class"] == "failed")
    out.append(", ".join(f"{k} {v}" for k, v in failed.most_common()) or "None.")
    out += ["", "## Next checks due", "", "| Jurisdiction | Date | Reason |", "| --- | --- | --- |"]
    for r in report["next_checks"]:
        flag = "" if r["due"] else " (upcoming)"
        out.append(f"| {r['name']} ({r['id']}) | {r['date']}{flag} | {r['reason']} |")
    q = report["discovery"]
    out += [
        "",
        "## Discovery queue",
        "",
        f"{len(q)} states without an institute; oldest recorded search first. "
        "Rerun their queries by hand or with a search tool; this script does not search.",
        "",
    ]
    for r in q[:10]:
        first = r["queries"][0]["query"] if r["queries"] else "(no recorded search)"
        out.append(f"- {r['name']} ({r['id']}), last {r['last_search']}: {first}")
    return "\n".join(out) + "\n"


VERDICTS = ("irrelevant", "not_primary", "duplicate", "candidate")


def ledger_rows(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def lead_status(rows: list[dict], url: str, text_sha256: str | None) -> dict:
    """Whether discovery needs to read a lead: new, changed since last read, or skip."""
    seen = [r for r in rows if r["url"] == url]
    if not seen:
        return {"status": "new"}
    last = seen[-1]
    if text_sha256 and last.get("text_sha256") == text_sha256:
        return {"status": "skip", **last}
    return {"status": "changed", **last}


def record_lead(path: Path, row: dict) -> None:
    """Append one verdict; the latest row for a URL is current."""
    if row["verdict"] not in VERDICTS:
        raise ValueError(f"verdict must be one of {', '.join(VERDICTS)}")
    if row["verdict"] == "duplicate" and not row.get("duplicate_of"):
        raise ValueError("a duplicate names the source ID it duplicates")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def lead(args) -> None:
    """Check a discovery lead against the ledger, or record a verdict for it."""
    # A fresh cache per call: the Fetcher would otherwise answer from an old response.
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%f")
    fetcher = Fetcher(ROOT / "work/leads-cache" / stamp, timeout=args.timeout)
    meta = fetcher.fetch(args.lead, "live")
    fields = {}
    if meta.get("status") == "ok":
        body = (fetcher.entry(args.lead) / "response.bin").read_bytes()
        fields = text_fields(body, meta.get("content_type", ""))
    digest = fields.get("text_sha256")
    rows = ledger_rows(args.leads)
    if not args.verdict:
        print(json.dumps({"fetch": meta.get("status"), **lead_status(rows, args.lead, digest)}, ensure_ascii=False))
        return
    record_lead(
        args.leads,
        {
            "url": args.lead,
            "text_sha256": digest,
            "text_scheme": fields.get("text_scheme"),
            "date": date.today().isoformat(),
            "verdict": args.verdict,
            **({"duplicate_of": args.duplicate_of} if args.duplicate_of else {}),
            "reason": args.reason or "",
        },
    )
    print(f"recorded {args.verdict} for {args.lead} in {args.leads}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--data", type=Path, default=ROOT / "data/aisi.json")
    parser.add_argument("--directory", type=Path, default=PRIVATE_SNAPSHOTS)
    parser.add_argument("--report", type=Path, default=ROOT / "work/monitor")
    parser.add_argument(
        "--cache",
        type=Path,
        help="default: a new <report>/cache-<time>; an existing cache reuses its usable "
        "responses and retries only transient failures (resume or re-classify a run)",
    )
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument("--delay", type=float, default=2.0, help="seconds between requests per host")
    parser.add_argument("--timeout", type=float, default=25)
    parser.add_argument("--only", nargs="*", help="source IDs")
    parser.add_argument("--today", type=date.fromisoformat, default=date.today())
    parser.add_argument("--horizon", type=int, default=14, help="days ahead for next checks")
    leads = parser.add_argument_group(
        "seen-leads ledger",
        "Pages discovery read but did not cite. --lead URL alone says whether to read it "
        "(new, changed, skip); with --verdict it records the reading.",
    )
    leads.add_argument("--lead", metavar="URL")
    leads.add_argument("--verdict", choices=VERDICTS)
    leads.add_argument("--duplicate-of", metavar="SOURCE_ID")
    leads.add_argument("--reason", help="one line")
    leads.add_argument("--leads", type=Path, help="default: discovery-leads.jsonl beside the snapshot directory")
    args = parser.parse_args()
    if args.directory is None:
        parser.error("Set AISI_SNAPSHOTS or pass --directory")
    store = args.directory.resolve()
    for name in ("report", "cache", "leads"):
        target = getattr(args, name)
        if target and target.resolve().is_relative_to(store):
            parser.error(f"--{name} must be outside the snapshot directory")
    if args.lead:
        # Private like the snapshots: rejected leads are working notes, not public data.
        args.leads = args.leads or args.directory.parent / "discovery-leads.jsonl"
        lead(args)
        return

    data = json.loads(args.data.read_text(encoding="utf-8"))
    sources = {s["id"]: s for s in data["sources"]}
    selected = [
        s for s in data["sources"] if s.get("snapshot") and (not args.only or s["id"] in args.only)
    ]
    started = datetime.now(UTC)
    cache = args.cache or args.report / f"cache-{started:%Y%m%dT%H%M%S}"
    fetcher = Fetcher(cache, delay=args.delay, timeout=args.timeout)
    fallback = ROOT / "work/snapshots"
    clock = time.monotonic()
    rows = []
    with ThreadPoolExecutor(args.workers) as pool:
        jobs = {pool.submit(compare, s, fetcher, args.directory, fallback): s for s in selected}
        for n, job in enumerate(as_completed(jobs), 1):
            source = jobs[job]
            try:
                rows.append(job.result())
            except Exception as exc:  # noqa: BLE001 - one source never stops the run
                rows.append(
                    {
                        "source_id": source["id"],
                        "url": source["url"],
                        "class": "failed",
                        "reason": f"tool error: {type(exc).__name__}: {exc}",
                    }
                )
            if n % 50 == 0:
                print(f"{n}/{len(selected)}", flush=True)
    rows.sort(key=lambda r: (CLASSES.index(r["class"]), r["source_id"]))
    report = {
        "started": started.isoformat(timespec="seconds"),
        "seconds": time.monotonic() - clock,
        "dataset_version": data["dataset"].get("version"),
        "thresholds": {"shingle": SHINGLE, "kept_run": KEPT_RUN, "shell_chars": SHELL_CHARS},
        "counts": dict(Counter(r["class"] for r in rows)),
        "reasons": dict(Counter(f"{r['class']}/{r['reason']}" for r in rows if r["class"] in {"substantive", "trivial", "unchanged"})),
        "sources": rows,
        "next_checks": due_checks(data, args.today, args.horizon),
        "discovery": discovery_queue(data),
    }
    args.report.mkdir(parents=True, exist_ok=True)
    (args.report / "monitor.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=1) + "\n", encoding="utf-8"
    )
    (args.report / "monitor.md").write_text(
        markdown(report, sources, citing_records(data)), encoding="utf-8"
    )
    print(json.dumps(report["counts"]), f"{report['seconds']:.0f} s", args.report / "monitor.md")


if __name__ == "__main__":
    main()
