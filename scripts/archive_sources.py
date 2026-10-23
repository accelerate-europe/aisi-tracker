"""Legacy manual Wayback lookup/request command, separate from private-copy preservation.

Record candidates in data/reference/archive_urls.json; --apply verifies candidate captures before applying them.

  uv run --group evidence python scripts/archive_sources.py  # look up / request snapshots
  uv run --group evidence python scripts/archive_sources.py --apply  # fetch and apply verified captures
  uv run --group evidence python scripts/archive_sources.py --apply --offline  # use cached captures only

Sources that already have an archive_url are skipped. Failures are recorded and retried on the next run.
"""

import argparse
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data" / "aisi.json"
OUT = ROOT / "data" / "reference" / "archive_urls.json"
UA = {"User-Agent": "aisi-tracker/1.0 (contact@accelerate-europe.org)"}


def fetch(url: str, timeout: int = 60):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.geturl(), r.read()


# Snapshots of login walls or captchas are useless as archive copies; match path segments and markers, not substrings
# of ordinary words ("designing" contains "signin").
BAD_SNAPSHOT = re.compile(
    r"/(log-?in|sign-?in|signon|captcha)([/?#._-]|$)|credentials_cookie_auth|require_login",
    re.IGNORECASE,
)


def usable(snapshot: str | None) -> str | None:
    return snapshot if snapshot and not BAD_SNAPSHOT.search(snapshot) else None


def existing_snapshot(url: str) -> str | None:
    q = "https://archive.org/wayback/available?" + urllib.parse.urlencode({"url": url})
    _, body = fetch(q)
    snap = json.loads(body).get("archived_snapshots", {}).get("closest")
    return (
        usable(snap["url"].replace("http://", "https://", 1))
        if snap and snap.get("available")
        else None
    )


def save(url: str) -> str | None:
    final, _ = fetch("https://web.archive.org/save/" + url, timeout=180)
    return usable(final) if "/web/" in final else None


def with_backoff(fn, url: str):
    """Retry on HTTP 429 with growing pauses; Save Page Now throttles anonymous use hard."""
    for wait in (60, 180, 600):
        try:
            return fn(url)
        except urllib.error.HTTPError as e:
            if e.code != 429:
                raise
            time.sleep(wait)
    return fn(url)


def apply_candidates(data: dict, done: dict, fetcher) -> dict:
    """Apply verified captures, retrieving missing responses unless explicitly offline."""
    if __package__:
        from .snapshot_sources import check_archive
    else:
        from snapshot_sources import check_archive  # type: ignore[no-redef]  # Direct script entry point.

    applied, rejected = [], []
    for source in data["sources"]:
        candidate = done.get(source["id"], {}).get("archive_url")
        if not candidate or source.get("archive_url"):
            continue
        try:
            meta, outcome = check_archive(source, fetcher, candidate)
        except (OSError, ValueError) as exc:
            rejected.append(
                {"source_id": source["id"], "status": "cache_error", "error": str(exc)}
            )
            continue
        if meta is not None and outcome["status"] == "verified":
            source["archive_url"] = outcome["final_url"]
            applied.append(source["id"])
        else:
            rejected.append({"source_id": source["id"], **outcome})
    return {"applied": applied, "rejected": rejected}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument(
        "--offline", action="store_true", help="with --apply, use cached captures only"
    )
    args = ap.parse_args()
    if args.offline and not args.apply:
        ap.error("--offline requires --apply")
    data = json.loads(DATA.read_text(encoding="utf-8"))
    done = json.loads(OUT.read_text(encoding="utf-8")) if OUT.exists() else {}
    if args.apply:
        if __package__:
            from .verify_excerpts import Fetcher
        else:
            from verify_excerpts import Fetcher  # type: ignore[no-redef]  # Direct script entry point.
        fetcher = Fetcher(ROOT / "work/source-cache", offline=args.offline)
        if not args.offline:
            candidates = {
                done[source["id"]]["archive_url"]
                for source in data["sources"]
                if not source.get("archive_url")
                and done.get(source["id"], {}).get("archive_url")
            }
            # Count initially uncached candidates, not retries or raw-view fallbacks.
            uncached = sum(not (fetcher.entry(url) / "metadata.json").is_file() for url in candidates)
            print(f"retrieving {uncached} uncached captures", flush=True)
        outcome = apply_candidates(data, done, fetcher)
        if outcome["applied"]:
            DATA.write_text(
                json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
            )
        print(
            f"applied {len(outcome['applied'])} verified archive URLs; rejected {len(outcome['rejected'])} candidates"
        )
        for rejection in outcome["rejected"]:
            print(rejection["source_id"], rejection["status"])
        return
    for s in data["sources"]:
        if s.get("archive_url") or usable(done.get(s["id"], {}).get("archive_url")):
            continue
        rec = {"url": s["url"]}
        try:
            rec["archive_url"] = with_backoff(existing_snapshot, s["url"])
            if not rec["archive_url"]:
                time.sleep(20)  # Save Page Now rate limits anonymous captures
                rec["archive_url"] = with_backoff(save, s["url"])
        except Exception as e:  # noqa: BLE001 - retain legacy lookup retry behaviour
            rec["error"] = f"{type(e).__name__}: {e}"[:200]
        done[s["id"]] = rec
        OUT.write_text(
            json.dumps(done, indent=1, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        print(s["id"], rec.get("archive_url") or rec.get("error"), flush=True)
    ok = sum(1 for r in done.values() if usable(r.get("archive_url")))
    print(f"done: {ok}/{len(done)} sources have a snapshot")


if __name__ == "__main__":
    main()
