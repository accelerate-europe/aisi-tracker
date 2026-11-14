"""Change monitor for cited sources: re-fetch, compare with the cited copy, triage.

Read-only for the dataset and cited copies. Checks are journalled separately. Direct
requests are followed by headless Chromium, jina and Wayback when necessary:

- unchanged: same file hash or same normalised text hash (text_scheme of snapshot_sources).
- trivial: the text hash differs, but every bound quotation the cited copy carries is
  still present or, without such quotations, a long run of the cited text survives.
- substantive: a bound quotation the cited copy carries is missing from the new copy
  (``quote_lost``), or, for a like-for-like copy without such quotations, no long run of
  the cited text survives (``text_changed``).
- gone: HTTP 404 or 410, or the host no longer resolves.
- blocked: the site refuses automated readers (401, 403, 429, 451, challenge, login,
  error, JavaScript or cookie shells, or a near-empty response carrying no quotation).
- failed: transient or tool failures (timeouts, 5xx, too large, unparseable, no cited copy).
- archived: a Wayback capture reached after the live routes failed.
- skipped: no snapshot hash to compare.

Quotation presence uses the verifier's own views (copy_views, literal_match), so a
quotation counts as lost only when the cited copy carried it and no view of the new copy
does. The report also lists jurisdictions whose ``next_check`` is due and a discovery
queue of recorded searches for states without an institute (queries only; it does not
search). A seen-leads ledger (--lead) remembers pages discovery read but did not cite,
so a later search skips them until their text hash changes.

    uv run --locked scripts/monitor_sources.py --report work/monitor

--rereads fetches nothing: it replays the check log into the tracked data/rereads.json, the per-quote re-read
states and dates the build publishes (scripts/rereads.py).
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
import uuid
from collections import Counter, defaultdict
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from urllib.parse import quote, urlsplit

try:
    from .snapshot_sources import (
        PRIVATE_SNAPSHOTS,
        ROOT,
        TEXT_SCHEME,
        archive_lookup_target,
        archive_snapshot,
        copy_views,
        extension,
        main_text,
        stored_copy,
        text_fields,
    )
    from .verify_excerpts import EXTRACTION_VERSION, Fetcher, content_problem, extract_text, literal_match
    from . import rereads
except ImportError:
    from snapshot_sources import (  # type: ignore[no-redef]  # Direct script entry point.
        PRIVATE_SNAPSHOTS,
        ROOT,
        TEXT_SCHEME,
        archive_lookup_target,
        archive_snapshot,
        copy_views,
        extension,
        main_text,
        stored_copy,
        text_fields,
    )
    from verify_excerpts import EXTRACTION_VERSION, Fetcher, content_problem, extract_text, literal_match  # type: ignore[no-redef]
    import rereads  # type: ignore[no-redef]

MONITOR_VERSION = 1
CLASSES = ("substantive", "gone", "trivial", "unchanged", "archived", "blocked", "failed", "skipped")
ROUTES = ("direct", "headless", "jina", "wayback")
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


def text_change(old: str, new: str, width: int = SHINGLE) -> dict:
    """Shingle shares removed and added, and the longest run of cited words still present."""
    a_words, b_words = old.lower().split(), new.lower().split()
    a, b = shingles(old), shingles(new)
    positions: dict[tuple[str, ...], list[int]] = defaultdict(list)
    for j in range(max(0, len(b_words) - width + 1)):
        positions[tuple(b_words[j : j + width])].append(j)
    # Longest run of consecutive cited shingles found at consecutive new positions.
    previous: dict[int, int] = {}
    longest = 0
    for i in range(max(0, len(a_words) - width + 1)):
        current = {
            j: previous.get(j - 1, 0) + 1 for j in positions.get(tuple(a_words[i : i + width]), ())
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
        "kept_run": longest + width - 1 if longest else 0,
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
    if not error and isinstance(meta.get("http_status"), int):
        error = f"HTTP {meta['http_status']}"
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


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def markdown_view(text: str) -> str:
    """A literal content view of jina's Markdown, alongside the original view."""
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)
    for marker in ("**", "__", "*", "_", "`"):
        text = re.sub(re.escape(marker) + r"(.+?)" + re.escape(marker), r"\1", text)
    return re.sub(r"(?m)^\s*(?:(?:#{1,6}|>|[-+*]|\d+[.)])\s+)+", "", text)


def observation(source: dict, final_url: str | None, reached: bool) -> dict:
    for url in (final_url, source["url"]):
        if url and archive_snapshot(url):
            match = re.search(r"/(?:web/)?(\d{14})(?:[a-z]+_)?/", urlsplit(url).path)
            return {"observation": "archive", "capture": match.group(1) if match else None}
    return {"observation": "live" if reached else "none"}


def compare_copy(
    source: dict, cited: tuple[dict, Path], body: bytes, mime: str, provenance: dict
) -> dict:
    """Compare bytes with a cited copy; provenance names route, final_url and http.

    path is optional in provenance, for the verifier's extra PDF extraction views.
    A fallback's missing quotations require confirmation by the route coordinator.
    """
    manifest, old_path = cited
    route = provenance["route"]
    new_path = provenance.get("path", old_path.parent / "unchecked-response")
    row = {
        "source_id": source["id"], "url": source["url"], "route": route,
        "checked": provenance.get("checked", utc_now()), "http": provenance.get("http"),
        "final_url": provenance.get("final_url"), "cited_method": manifest["method"],
        "file_sha256": hashlib.sha256(body).hexdigest(),
        **observation(source, provenance.get("final_url"), True),
        "_body": body, "_mime": mime,
    }
    old_body = old_path.read_bytes()
    previous = source["snapshot"]
    old_views = copy_views(old_body, manifest.get("mime", ""), old_path)
    new_views = copy_views(body, mime, new_path)
    if route == "jina":
        new_views += [markdown_view(view) for view in new_views]
    quotes = []
    for excerpt in source.get("excerpts", []):
        if bound(source, excerpt):
            now = present(excerpt["text"], new_views)
            quotes.append({
                "id": "sha256:" + hashlib.sha256(excerpt["text"].encode()).hexdigest(),
                "text": excerpt["text"], "supports": [label(p) for p in excerpt["supports"]],
                "in_cited_copy": present(excerpt["text"], old_views),
                "in_checked_copy": now, "in_new_copy": now,
            })
    row["quotes"] = quotes
    fields = text_fields(body, mime)
    row.update(fields)
    if route == "direct" and row["file_sha256"] == previous["sha256"]:
        return {**row, "class": "unchanged", "reason": "same_file"}
    if not any(view.strip() for view in new_views):
        return {**row, "class": "failed", "reason": "new copy has no readable text"}
    lost = [q for q in quotes if q["in_cited_copy"] and not q["in_checked_copy"]]
    # The text hash leaves out furniture, so quotation loss is checked first.
    if route == "direct" and not lost and fields and fields.get("text_sha256") == previous.get("text_sha256"):
        return {**row, "class": "unchanged", "reason": "same_text"}
    like_for_like = route == "direct" and manifest["method"] == "live" and manifest["requested_url"] == source["url"]
    try:
        new_text = main_text(body, mime)
        if route == "jina":
            new_text = markdown_view(new_text)
        old_text = main_text(old_body, manifest.get("mime", ""))
        # Short fallback copies can meet the half-text rule with fewer than five words.
        width = SHINGLE if route == "direct" else min(SHINGLE, max(1, len(old_text.split()) // 2))
        change = text_change(old_text, new_text, width)
    except Exception as exc:  # noqa: BLE001 - quotations can still decide a parser failure
        change = {"error": f"{type(exc).__name__}: {exc}"}
    row.update(text_change=change, like_for_like=like_for_like)
    carried = [q for q in quotes if q["in_cited_copy"]]
    kept_any = any(q["in_checked_copy"] for q in (quotes if route == "direct" else carried))
    if "new_chars" in change and not kept_any and (
        change["new_chars"] < SHELL_CHARS
        or (like_for_like and change["new_chars"] < change["old_chars"] / 10)
    ):
        return {**row, "class": "blocked", "reason": "near_empty_response"}
    if route == "wayback":
        return {**row, "class": "archived", "reason": "archive_quote_missing" if lost else "quotes_kept"}
    if route != "direct":
        if carried:
            if not kept_any:
                return {**row, "class": "failed", "reason": "not_comparable"}
            if lost:
                return {**row, "class": "failed", "reason": "fallback_quote_unconfirmed"}
            return {**row, "class": "trivial", "reason": "quotes_kept"}
        if change.get("old_words", 0) and change["kept_run"] >= min(KEPT_RUN, change["old_words"] / 2):
            return {**row, "class": "trivial", "reason": "small_change"}
        return {**row, "class": "failed", "reason": "not_comparable"}
    if lost:
        return {**row, "class": "substantive", "reason": "quote_lost"}
    if carried:
        reason = "quotes_kept"
    elif "error" in change:
        return {**row, "class": "failed", "reason": f"text comparison: {change['error']}"}
    elif like_for_like and change["kept_run"] < min(KEPT_RUN, change["old_words"] / 2):
        return {**row, "class": "substantive", "reason": "text_changed"}
    else:
        reason = "small_change"
    if not like_for_like:
        reason += "_cited_copy_other_format"
    return {**row, "class": "trivial", "reason": reason}


def checked_result(source: dict, cited: tuple[dict, Path], meta: dict, path: Path, route: str) -> dict:
    row = {
        "source_id": source["id"], "url": source["url"], "route": route,
        "checked": meta.get("retrieved", utc_now()), "http": meta.get("http_status"),
        "final_url": meta.get("final_url"), "fetch_status": meta.get("status"),
        **observation(source, meta.get("final_url"), False),
    }
    klass, reason = fetch_class(meta)
    if klass:
        return {**row, "class": klass, "reason": reason}
    try:
        body = path.read_bytes()
    except OSError as exc:
        return {**row, "class": "failed", "reason": f"response unavailable: {exc}"}
    if hashlib.sha256(body).hexdigest() != meta.get("sha256"):
        return {**row, "class": "failed", "reason": "response checksum mismatch"}
    if route != "direct":
        try:
            text, title = extract_text(body, meta.get("content_type", ""))
            problem = content_problem(body, text, title, meta.get("final_url") or source["url"])
        except Exception:  # noqa: BLE001 - comparison retains the verifier's other views
            problem = None
        if problem and problem != "no_extractable_text":
            return {**row, "class": "blocked", "reason": problem}
    return {
        **row,
        **compare_copy(source, cited, body, meta.get("content_type", ""), {**row, "path": path}),
    }


def compare(source: dict, fetcher: Fetcher, directory: Path, fallback: Path) -> dict:
    """Compatibility entry point for the direct route."""
    row = {"source_id": source["id"], "url": source["url"], "route": "none", "observation": "none"}
    if not source.get("snapshot", {}).get("sha256"):
        return {**row, "class": "skipped", "reason": "no_snapshot_hash"}
    try:
        found = stored_copy(source, directory, fallback)
    except ValueError as exc:
        return {**row, "class": "failed", "reason": f"cited copy: {exc}"}
    if found is None:
        return {**row, "class": "failed", "reason": "no cited copy held privately"}
    meta = fetcher.fetch(source["url"], "live")
    return checked_result(source, found, meta, fetcher.entry(source["url"]) / "response.bin", "direct")


def run_browser(urls: list[str], out: Path, *, wait: float, deadline: float, delay: float, timeout: float) -> tuple[bool, int | None]:
    """One browser batch, with ownership and process cleanup provided by slice B."""
    if __package__:
        from .browser_lock import run_in_slot
    else:
        from browser_lock import run_in_slot
    out.mkdir(parents=True, exist_ok=True)
    urls_file = out / "urls.txt"
    urls_file.write_text("\n".join(urls) + "\n", encoding="utf-8")
    return run_in_slot(
        ["uv", "run", "--locked", str(ROOT / "scripts/monitor_browser.py"),
         "--urls", str(urls_file), "--out", str(out), "--delay", str(delay), "--timeout", str(timeout)],
        holder="source monitor", wait=wait, deadline=deadline,
    )


def send_digest(summary: dict) -> None:
    """Lazy adapter so a monitor test never needs the notification implementation."""
    if __package__:
        from .monitor_digest import build, send
    else:
        from monitor_digest import build, send
    send(*build(summary))


class StopRun(KeyboardInterrupt):
    """An interrupted run remains resumable."""


class RunControl:
    def __init__(self, hours: float):
        self.deadline = time.monotonic() + hours * 3600
        self.stopped = threading.Event()
        self.reason = "interrupted"

    def check(self) -> None:
        if time.monotonic() >= self.deadline and not self.stopped.is_set():
            self.reason = "deadline"
            self.stopped.set()
        if self.stopped.is_set():
            raise StopRun(self.reason)

    def signal(self, signum, frame) -> None:
        if not self.stopped.is_set():
            self.reason = signal.Signals(signum).name
            self.stopped.set()
            raise StopRun(self.reason)


class MonitorFetcher(Fetcher):
    """Check the deadline after host pacing too, immediately before opening a URL."""
    def __init__(self, *args, control: RunControl, **kwargs):
        super().__init__(*args, **kwargs)
        self.control = control

    def fetch(self, url: str, method: str = "live", headers: dict | None = None) -> dict:
        self.control.check()
        return super().fetch(url, method, headers=headers)

    def _download(self, url: str, method: str, folder: Path, headers: dict | None = None) -> dict:
        self.control.check()
        return super()._download(url, method, folder, headers=headers)


@contextmanager
def file_lock(path: Path, *, blocking: bool = True):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        except BlockingIOError as exc:
            raise RuntimeError(f"run already active: {path.stem}") from exc
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def repair_tail(handle) -> None:
    """Discard an incomplete last record, never turn it into an interior bad line."""
    handle.seek(0, os.SEEK_END)
    end = handle.tell()
    if not end:
        return
    handle.seek(end - 1)
    if handle.read(1) == b"\n":
        return
    position = end
    while position:
        start = max(0, position - 8192)
        handle.seek(start)
        chunk = handle.read(position - start)
        newline = chunk.rfind(b"\n")
        if newline >= 0:
            handle.truncate(start + newline + 1)
            break
        position = start
    else:
        handle.truncate(0)
    handle.flush()
    os.fsync(handle.fileno())


def append_line(path: Path, row: dict) -> None:
    """Caller owns the journal lock."""
    with path.open("a+b") as handle:
        repair_tail(handle)
        handle.seek(0, os.SEEK_END)
        handle.write((json.dumps(row, ensure_ascii=False) + "\n").encode())
        handle.flush()
        os.fsync(handle.fileno())


def sync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def read_lines(path: Path) -> list[dict]:
    """Read complete journal rows; caller holds the journal lock."""
    if not path.exists():
        return []
    with path.open("r+b") as handle:
        repair_tail(handle)
        handle.seek(0)
        return [json.loads(line) for line in handle if line.strip()]


def validate_write_paths(paths, protected: tuple[Path, ...]) -> None:
    """Resolve destinations, including symlinks, before any write or cleanup."""
    stores = [path.resolve() for path in protected]
    for path in paths:
        if path is not None and any(path.resolve().is_relative_to(store) for store in stores):
            raise ValueError(f"write destination must be outside both snapshot stores: {path}")


def log_paths(directory: Path) -> list[Path]:
    return [directory, *(directory / name for name in (
        "responses", "checks.lock", "checks.jsonl", "runs.lock", "runs.jsonl", "runs",
    ))]


class CheckLog:
    """Append-only journals and content-addressed copies, separate from snapshots."""
    def __init__(self, directory: Path, *, protected: tuple[Path, ...] = ()):
        validate_write_paths(log_paths(directory), protected)
        self.directory = directory
        self.responses = directory / "responses"
        self.responses.mkdir(parents=True, exist_ok=True)
        with file_lock(directory / "checks.lock"):
            for path in self.responses.glob("*.tmp"):
                path.unlink()

    def read(self, name: str) -> list[dict]:
        with file_lock(self.directory / f"{name}.lock"):
            return read_lines(self.directory / f"{name}.jsonl")

    def event(self, run_id: str, event: str, **fields) -> None:
        with file_lock(self.directory / "runs.lock"):
            append_line(self.directory / "runs.jsonl", {
                "run_id": run_id, "time": utc_now(), "event": event, **fields,
            })

    def save_response(self, body: bytes, mime: str) -> str:
        """Caller holds checks.lock, also excluding startup temp cleanup."""
        digest = hashlib.sha256(body).hexdigest()
        path = self.responses / f"{digest}.{extension(body, mime)}"
        temp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
        with temp.open("xb") as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        temp.replace(path)
        sync_directory(self.responses)
        return str(path.relative_to(self.directory))

    def record(self, run_id: str, revision: dict, source: dict, result: dict) -> dict:
        row = {
            "run_id": run_id, "dataset_revision": revision,
            "source_id": source["id"], "url": source["url"],
            "checked": result.get("checked"), "route": result["route"],
            "observation": result["observation"], "attempts": result.get("attempts", []),
            "http": result.get("http"), "final_url": result.get("final_url"),
            "file_sha256": result.get("file_sha256"), "text_sha256": result.get("text_sha256"),
            "text_scheme": result.get("text_scheme"), "response": None,
            "quotes": [{k: q[k] for k in ("id", "in_cited_copy", "in_checked_copy")}
                       for q in result.get("quotes", [])],
            "class": result["class"], "reason": result["reason"],
            "verifier": {"monitor": MONITOR_VERSION, "extraction": EXTRACTION_VERSION, "text_scheme": TEXT_SCHEME},
        }
        if row["observation"] == "archive":
            row["capture"] = result.get("capture")
        if "confirmed_quote_ids" in result:
            row["confirmed_quote_ids"] = result["confirmed_quote_ids"]
        with file_lock(self.directory / "checks.lock"):
            if any(r["run_id"] == run_id and r["source_id"] == source["id"]
                   for r in read_lines(self.directory / "checks.jsonl")):
                raise ValueError(f"duplicate check row for run {run_id}, source {source['id']}")
            if "_body" in result:
                row["response"] = (
                    "cited" if row["file_sha256"] == source.get("snapshot", {}).get("sha256")
                    else self.save_response(result["_body"], result["_mime"])
                )
            append_line(self.directory / "checks.jsonl", row)
        return row

    def rows(self, run_id: str, sources: dict, directory: Path, fallback: Path) -> list[dict]:
        rows = {}
        for row in self.read("checks"):
            if row["run_id"] != run_id or row["source_id"] not in sources:
                continue
            sid = row["source_id"]
            if sid in rows:
                raise ValueError(f"duplicate check row for run {run_id}, source {sid}")
            if row.get("file_sha256"):
                try:
                    if row["response"] == "cited":
                        cited = stored_copy(sources[sid], directory, fallback)
                        path = cited[1] if cited else None
                    else:
                        path = (self.directory / row["response"]).resolve()
                        if not path.is_relative_to(self.responses.resolve()):
                            raise ValueError("response path is outside responses")
                    if path is None:
                        raise ValueError("cited response is missing")
                    if hashlib.sha256(path.read_bytes()).hexdigest() != row["file_sha256"]:
                        raise ValueError("response checksum mismatch")
                except (OSError, ValueError, TypeError) as exc:
                    raise ValueError(f"invalid saved response for source {sid}: {exc}") from exc
            rows[sid] = row
        return list(rows.values())


def attempt(result: dict) -> dict:
    return {"route": result["route"], "http": result.get("http"),
            "fetch_status": result.get("fetch_status"), "class": result["class"], "reason": result["reason"]}


def lost_quotes(result: dict) -> set[str]:
    if result["reason"] != "fallback_quote_unconfirmed":
        return set()
    return {q["id"] for q in result.get("quotes", []) if q["in_cited_copy"] and not q["in_checked_copy"]}


def source_failure(source: dict, route: str, exc: Exception, attempts: list[dict] | None = None) -> dict:
    """An individual source error is terminal; interrupts and journal writes stay outside."""
    result = {
        "source_id": source["id"], "url": source["url"], "route": route,
        **observation(source, None, False), "class": "failed", "fetch_status": "tool_error",
        "reason": f"tool error: {type(exc).__name__}: {exc}", "_terminal": True,
    }
    result["attempts"] = [*(attempts or []), attempt(result)]
    return result


def wayback(source: dict, cited: tuple[dict, Path], fetcher: Fetcher) -> dict:
    url = archive_lookup_target(source["url"])
    lookup = "https://archive.org/wayback/available?url=" + quote(url, safe="")
    meta = fetcher.fetch(lookup, "archive_lookup")
    empty = {"route": "wayback", "http": meta.get("http_status"),
             "fetch_status": meta.get("status"), "class": "failed", "reason": "no_capture"}
    if meta.get("status") != "ok":
        return {**empty, "reason": fetch_class(meta)[1]}
    try:
        payload = json.loads((fetcher.entry(lookup) / "response.bin").read_bytes())
        capture = payload.get("archived_snapshots", {}).get("closest", {})
        stamp = capture.get("timestamp", "")
        if not capture.get("available") or not re.fullmatch(r"\d{14}", stamp):
            return empty
    except (OSError, ValueError, TypeError):
        return {**empty, "reason": "invalid availability response"}
    raw = f"https://web.archive.org/web/{stamp}id_/{url}"
    meta = fetcher.fetch(raw, "archive_raw")
    result = checked_result(source, cited, meta, fetcher.entry(raw) / "response.bin", "wayback")
    final = meta.get("final_url", "")
    if result["class"] == "archived" and (
        urlsplit(final).hostname != "web.archive.org" or not archive_snapshot(final)
    ):
        return {**empty, "http": meta.get("http_status"), "reason": "not a capture"}
    return result


def finish_routes(source: dict, cited: tuple[dict, Path], direct: dict, headless: dict, jina: Fetcher, archive: Fetcher) -> dict:
    attempts = [attempt(direct), attempt(headless)]
    if headless["class"] == "trivial":
        return {**headless, "attempts": attempts}
    url = "https://r.jina.ai/" + source["url"]
    try:
        meta = jina.fetch(url, "render", headers={"X-No-Cache": "true"})
        rendered = checked_result(source, cited, meta, jina.entry(url) / "response.bin", "jina")
    except Exception as exc:  # noqa: BLE001 - source-specific failures do not stop other sources
        return source_failure(source, "jina", exc, attempts)
    attempts.append(attempt(rendered))
    if rendered["class"] == "trivial" and not lost_quotes(headless):
        return {**rendered, "attempts": attempts}
    confirmed = lost_quotes(headless) & lost_quotes(rendered)
    # Every quotation either comparable fallback missed stays in the row: confirmed when both missed it,
    # otherwise reported as unconfirmed.
    missing = lost_quotes(headless) | lost_quotes(rendered)

    def with_missing(result: dict) -> dict:
        quotes = [{**q, "in_checked_copy": q["in_checked_copy"] and q["id"] not in missing,
                   "in_new_copy": q.get("in_new_copy", q["in_checked_copy"]) and q["id"] not in missing}
                  for q in result.get("quotes", [])]
        return {**result, "quotes": quotes}

    if confirmed:
        return {**with_missing(rendered), "class": "substantive", "reason": "quote_lost",
                "route": "headless+jina", "attempts": attempts,
                "confirmed_quote_ids": sorted(confirmed)}
    try:
        archived = wayback(source, cited, archive)
    except Exception as exc:  # noqa: BLE001 - keep the failed route and continue with the next source
        return source_failure(source, "wayback", exc, attempts)
    attempts.append(attempt(archived))
    # A live page missing a quotation outranks an older capture that still has it: the loss stays visible,
    # with the capture among the attempts.
    for result in (rendered, headless):
        if lost_quotes(result):
            return {**with_missing(result), "class": "failed", "reason": "fallback_quote_unconfirmed",
                    "attempts": attempts}
    if archived["class"] == "archived":
        return {**archived, "attempts": attempts}
    return {**direct, "attempts": attempts}


def direct_results(selected: list[dict], fetcher: Fetcher, directory: Path, fallback: Path, workers: int, control: RunControl):
    """Bound queued work; cancellation never starts the remaining source list."""
    pool = ThreadPoolExecutor(workers)
    jobs = {}
    remaining = iter(selected)
    def work(source):
        control.check()
        try:
            return compare(source, fetcher, directory, fallback)
        except Exception as exc:  # noqa: BLE001 - interruption is a BaseException, not a source failure
            return source_failure(source, "direct", exc)

    def submit():
        source = next(remaining, None)
        if source is not None:
            control.check()
            jobs[pool.submit(work, source)] = source
    try:
        for _ in range(workers):
            submit()
        while jobs:
            control.check()
            done, _ = wait(jobs, timeout=0.2, return_when=FIRST_COMPLETED)
            for job in done:
                source = jobs.pop(job)
                yield source, job.result()
                submit()
    finally:
        for job in jobs:
            job.cancel()
        # In-flight requests use --timeout; the unit leaves an hour between the 4 h deadline and 5 h ceiling.
        pool.shutdown(wait=True, cancel_futures=True)


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
    for confirmed, heading in ((True, "Quotations no longer present"), (False, "Unconfirmed quotation losses")):
        out += [f"## {heading}", ""]
        found = False
        for r in rows:
            if r["reason"] not in {"quote_lost", "fallback_quote_unconfirmed"}:
                continue
            quotes = [q for q in r["quotes"] if q["in_cited_copy"] and not q["in_checked_copy"] and (
                (r["route"] == "direct" or q["id"] in r.get("confirmed_quote_ids", [])) == confirmed
            )]
            if not quotes:
                continue
            found = True
            src = sources[r["source_id"]]
            out.append(f"- [{src['title']}]({r['url']}) (`{r['source_id']}`, {r['route']})")
            excerpts = {"sha256:" + hashlib.sha256(e["text"].encode()).hexdigest(): e for e in src.get("excerpts", [])}
            for q in quotes:
                excerpt = excerpts[q["id"]]
                out.append(f"    - \"{excerpt['text'][:200]}\" supports {'; '.join(label(p) for p in excerpt['supports'])}")
        if not found:
            out.append("None.")
        out.append("")
    out += ["", "## Page text changed (no quotation lost)", ""]
    changed = [r for r in rows if r.get("reason") == "text_changed"]
    if not changed:
        out.append("None.")
    for r in changed:
        src = sources[r["source_id"]]
        where = "; ".join(cites.get(r["source_id"], [])[:4]) or "excerpt-supported fields"
        out.append(
            f"- [{src['title']}]({r['url']}) (`{r['source_id']}`): "
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
    out += ["", "## Routes", "", f"Live-unchecked: {report['live_unchecked']}.", ""]
    out += [f"- {route}: {count}" for route, count in sorted(report["by_route"].items())]
    out += ["", "## Unreached live sources", ""]
    for r in report["unreached"]:
        tried = "; ".join(f"{a['route']}: {a['http'] or a['fetch_status']} / {a['reason']}" for a in r["attempts"])
        capture = f"; capture {r.get('capture')}" if r["observation"] == "archive" else ""
        out.append(f"- {r['source_id']}: {r['class']} / {r['reason']} ({r['route']}){capture}. {tried}")
    if not report["unreached"]:
        out.append("None.")
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
    cache = ROOT / "work/leads-cache" / stamp
    validate_write_paths([cache, args.leads], (args.directory, ROOT / "work/snapshots"))
    fetcher = Fetcher(cache, timeout=args.timeout)
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


def dataset_revision(path: Path, body: bytes, data: dict) -> dict:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=path.resolve().parent,
        capture_output=True, text=True, check=False,
    )
    return {"commit": result.stdout.strip() if result.returncode == 0 else None,
            "data_sha256": hashlib.sha256(body).hexdigest(), "version": data["dataset"].get("version")}


def statistics(rows: list[dict]) -> dict:
    return {
        "counts": dict(Counter(r["class"] for r in rows)),
        "live_unchecked": sum(r["class"] != "skipped" and (
            r["observation"] != "live" or r["class"] in {"blocked", "failed"}
        ) for r in rows),
        "by_route": dict(Counter(r["route"] for r in rows if r["route"] != "direct")),
    }


def build_report(args, data: dict, start: dict, rows: list[dict], seconds: float, error: str | None, interrupted: bool) -> dict:
    rows = sorted(rows, key=lambda r: (CLASSES.index(r["class"]), r["source_id"]))
    return {
        "run_id": start["run_id"], "started": start["time"], "seconds": seconds,
        "dataset_revision": start["dataset_revision"], "dataset_version": start["dataset_revision"]["version"],
        "thresholds": {"shingle": SHINGLE, "kept_run": KEPT_RUN, "shell_chars": SHELL_CHARS},
        **statistics(rows),
        "reasons": dict(Counter(f"{r['class']}/{r['reason']}" for r in rows)),
        "sources": rows, "unreached": [r for r in rows if r["class"] != "skipped" and (
            r["observation"] != "live" or r["class"] in {"blocked", "failed"}
        )],
        "next_checks": due_checks(data, args.today, args.horizon), "discovery": discovery_queue(data),
        "error": error, "interrupted": interrupted, "complete": len(rows) == len(start["sources"]),
    }


def digest_summary(report: dict, path: Path) -> dict:
    def entries(predicate):
        return [{k: r[k] for k in ("source_id", "reason", "route")} for r in report["sources"] if predicate(r)]
    return {
        **{k: report[k] for k in ("run_id", "dataset_revision", "seconds", "counts", "live_unchecked", "by_route", "error", "interrupted", "complete")},
        "baselines": [["8 Nov", 188], ["10 Nov", 157]], "target": 100,
        "substantive": entries(lambda r: r["class"] == "substantive"),
        "gone": entries(lambda r: r["class"] == "gone"),
        "unconfirmed": entries(lambda r: r["reason"] in {"fallback_quote_unconfirmed", "archive_quote_missing"}),
        "next_checks_due": [r["id"] for r in report["next_checks"] if r["due"]], "report": str(path),
    }


def check_sources(args, selected: list[dict], log: CheckLog, start: dict, control: RunControl, caches: dict[str, Path]) -> None:
    for path in caches.values():
        path.mkdir(parents=True, exist_ok=True)
    direct = MonitorFetcher(caches["direct"], delay=args.delay, timeout=args.timeout, control=control)
    jina = MonitorFetcher(caches["jina"], delay=max(3, args.delay), timeout=args.timeout, control=control)
    archive = MonitorFetcher(caches["wayback"], delay=max(5, args.delay), timeout=args.timeout, control=control)
    fallback = ROOT / "work/snapshots"
    pending = []
    for source, result in direct_results(selected, direct, args.directory, fallback, args.workers, control):
        if result["class"] in {"blocked", "failed"} and result["route"] == "direct" and not result.get("_terminal"):
            pending.append((source, result))
        else:
            result["attempts"] = [attempt(result)] if result["route"] == "direct" else []
            log.record(start["run_id"], start["dataset_revision"], source, result)
    if not pending:
        return
    control.check()
    acquired, code = run_browser(
        list(dict.fromkeys(source["url"] for source, _ in pending)), caches["headless"],
        wait=min(args.browser_wait, max(0, control.deadline - time.monotonic())),
        deadline=control.deadline, delay=args.delay, timeout=args.timeout,
    )
    control.check()
    for source, plain in pending:
        control.check()
        try:
            cited = stored_copy(source, args.directory, fallback)
            if cited is None:
                raise ValueError(f"cited copy disappeared: {source['id']}")
            folder = caches["headless"] / hashlib.sha256(source["url"].encode()).hexdigest()
            meta_path = folder / "metadata.json"
            if acquired and meta_path.is_file():
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                rendered = checked_result(source, cited, meta, folder / "response.bin", "headless")
            else:
                reason = "browser busy" if not acquired else f"browser helper exit {code}"
                rendered = {"route": "headless", "class": "failed", "reason": reason, "fetch_status": "not_run", "http": None}
            result = finish_routes(source, cited, plain, rendered, jina, archive)
        except Exception as exc:  # noqa: BLE001 - journal writes deliberately remain outside this handler
            result = source_failure(source, "headless", exc, [attempt(plain)])
        log.record(start["run_id"], start["dataset_revision"], source, result)


def run_monitor(args) -> int:
    body = args.data.read_bytes()
    data = json.loads(body)
    revision = dataset_revision(args.data, body, data)
    sources = {s["id"]: s for s in data["sources"]}
    if len(sources) != len(data["sources"]):
        raise ValueError("duplicate source IDs")
    selected = [s for s in data["sources"] if not args.only or s["id"] in args.only]
    run_id = args.resume or datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ-") + uuid.uuid4().hex[:6]
    if not re.fullmatch(r"\d{8}T\d{6}Z-[0-9a-f]{6}", run_id):
        raise ValueError("invalid run ID")
    # Resolve every destination before CheckLog can create directories or clean temp files.
    protected = (args.directory, ROOT / "work/snapshots")
    cache_root = args.report / f"cache-{run_id}"
    invocation = uuid.uuid4().hex[:6]
    caches = {route: cache_root / route / invocation for route in ROUTES}
    if args.cache:
        caches["direct"] = args.cache
    destinations = [*log_paths(args.checks), args.checks / "runs" / f"{run_id}.lock",
                    args.report, args.report / "monitor.json", args.report / "monitor.md",
                    cache_root, *(cache_root / route for route in ROUTES), *caches.values(),
                    caches["headless"] / "urls.txt"]
    for source in selected:
        for route in ("direct", "headless", "jina"):
            url = ("https://r.jina.ai/" if route == "jina" else "") + source["url"]
            folder = caches[route] / hashlib.sha256(url.encode()).hexdigest()
            destinations.extend([folder, *(folder / name for name in ("response.bin", "metadata.json", "text.txt"))])
    validate_write_paths(destinations, protected)
    log = CheckLog(args.checks, protected=protected)
    with file_lock(args.checks / "runs" / f"{run_id}.lock", blocking=False):
        events = [e for e in log.read("runs") if e["run_id"] == run_id]
        if args.resume:
            starts = [e for e in events if e["event"] == "start"]
            if len(starts) != 1:
                raise ValueError(f"run has no unique start: {run_id}")
            start, = starts
            if start["dataset_revision"]["data_sha256"] != revision["data_sha256"]:
                raise ValueError("cannot resume: data_sha256 changed")
            if args.only and set(args.only) != set(start["sources"]):
                raise ValueError("cannot change --only on resume")
            selected = [sources[sid] for sid in start["sources"]]
        else:
            start = {"run_id": run_id, "time": utc_now(), "dataset_revision": revision,
                     "sources": [s["id"] for s in selected], "routes": list(ROUTES)}
            log.event(run_id, "start", **{k: start[k] for k in ("dataset_revision", "sources", "routes")})
        control = RunControl(args.deadline_hours)
        previous = {sig: signal.signal(sig, control.signal) for sig in (signal.SIGINT, signal.SIGTERM)}
        clock = time.monotonic()
        error, interrupted = None, False
        rows = []
        report_path = args.report / "monitor.md"
        try:
            rows = log.rows(run_id, sources, args.directory, ROOT / "work/snapshots")
            try:
                finished = {r["source_id"] for r in rows}
                remaining = [s for s in selected if s["id"] not in finished]
                if remaining:
                    check_sources(args, remaining, log, start, control, caches)
                rows = log.rows(run_id, sources, args.directory, ROOT / "work/snapshots")
                if len(rows) != len(selected):
                    raise RuntimeError("not every source has a durable row")
                if not any(e["event"] == "lines_done" for e in events):
                    log.event(run_id, "lines_done", **statistics(rows))
            except (Exception, KeyboardInterrupt) as exc:
                control.stopped.set()
                interrupted = isinstance(exc, KeyboardInterrupt)
                error = str(exc) or type(exc).__name__
                log.event(run_id, "interrupted", error=error)
                rows = log.rows(run_id, sources, args.directory, ROOT / "work/snapshots")
            report = build_report(args, data, start, rows, time.monotonic() - clock, error, interrupted)
            try:
                args.report.mkdir(parents=True, exist_ok=True)
                (args.report / "monitor.json").write_text(json.dumps(report, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
                report_path.write_text(markdown(report, sources, citing_records(data)), encoding="utf-8")
                log.event(run_id, "report_written", report=str(report_path))
            except (Exception, KeyboardInterrupt) as exc:
                error = str(exc) or type(exc).__name__
                interrupted = interrupted or isinstance(exc, KeyboardInterrupt)
                report.update(error=error, interrupted=interrupted)
                log.event(run_id, "interrupted", error=error)
            if args.digest:
                try:
                    send_digest(digest_summary(report, report_path))
                except Exception as exc:  # noqa: BLE001 - failed delivery must fail the command
                    error = str(exc) or type(exc).__name__
                    log.event(run_id, "digest_failed", error=error)
                else:
                    log.event(run_id, "digest_sent")
            print(run_id, json.dumps(report["counts"]), report_path)
            if error:
                print(error, file=sys.stderr)
            return 1 if error else 0
        except (Exception, KeyboardInterrupt) as exc:
            # Even failures in report construction must reach the run journal and digest.
            control.stopped.set()
            error = str(exc) or type(exc).__name__
            interrupted = isinstance(exc, KeyboardInterrupt)
            log.event(run_id, "interrupted", error=error)
            if args.digest:
                report = {
                    "run_id": run_id, "dataset_revision": start["dataset_revision"],
                    "seconds": time.monotonic() - clock, **statistics(rows), "sources": rows,
                    "next_checks": [], "error": error, "interrupted": interrupted,
                    "complete": len(rows) == len(selected),
                }
                try:
                    send_digest(digest_summary(report, report_path))
                except Exception as delivery_error:  # noqa: BLE001 - preserve both failures
                    log.event(run_id, "digest_failed", error=str(delivery_error))
                else:
                    log.event(run_id, "digest_sent")
            print(error, file=sys.stderr)
            return 1
        finally:
            for sig, handler in previous.items():
                signal.signal(sig, handler)


def write_rereads(args) -> int:
    """The only writer of data/rereads.json: states and dates, never the private log's paths or texts."""
    data = json.loads(args.data.read_text(encoding="utf-8"))
    with file_lock(args.checks / "checks.lock"):
        rows = read_lines(args.checks / "checks.jsonl")
    result = rereads.export(data, rows, is_archive=archive_snapshot)
    args.rereads_out.write_text(json.dumps(result, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    states = Counter(e["state"] for quotes in result["sources"].values() for e in quotes.values())
    print(f"{args.rereads_out}: runs {', '.join(result['runs']) or 'none'}, checked to {result['checked_to']}; "
          + ", ".join(f"{k} {v}" for k, v in sorted(states.items())))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--data", type=Path, default=ROOT / "data/aisi.json")
    parser.add_argument("--directory", type=Path, default=PRIVATE_SNAPSHOTS)
    parser.add_argument("--report", type=Path, default=ROOT / "work/monitor")
    parser.add_argument(
        "--cache",
        type=Path,
        help="direct route only: reuse usable responses and retry transient failures; default: fresh cache per run",
    )
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument("--delay", type=float, default=2.0, help="seconds between requests per host")
    parser.add_argument("--timeout", type=float, default=25)
    parser.add_argument("--only", nargs="*", help="source IDs")
    parser.add_argument("--checks", type=Path, help="default: checks beside the snapshot directory")
    parser.add_argument("--resume", metavar="RUN_ID")
    parser.add_argument("--browser-wait", type=float, default=5400, help="whole-run browser lock wait budget in seconds")
    parser.add_argument("--deadline-hours", type=float, default=4)
    parser.add_argument("--digest", action="store_true")
    parser.add_argument("--today", type=date.fromisoformat, default=date.today())
    parser.add_argument("--horizon", type=int, default=14, help="days ahead for next checks")
    parser.add_argument("--rereads", action="store_true", help="write the re-read states from the check log and stop; fetches nothing")
    parser.add_argument("--rereads-out", type=Path, default=rereads.REREADS, help="default: data/rereads.json")
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
    args = parser.parse_args(argv)
    if args.directory is None:
        parser.error("Set AISI_SNAPSHOTS or pass --directory")
    args.checks = args.checks or args.directory.parent / "checks"
    if args.workers < 1 or args.browser_wait < 0 or args.deadline_hours < 0:
        parser.error("workers must be positive and time budgets non-negative")
    try:
        validate_write_paths(
            [args.report, args.cache, args.leads, args.rereads_out, *log_paths(args.checks),
             args.report / "monitor.json", args.report / "monitor.md"],
            (args.directory, ROOT / "work/snapshots"),
        )
    except ValueError as exc:
        parser.error(str(exc))
    if args.rereads:
        try:
            return write_rereads(args)
        except (OSError, ValueError, KeyError) as exc:
            print(str(exc), file=sys.stderr)
            return 1
    if args.lead:
        # Private like the snapshots: rejected leads are working notes, not public data.
        args.leads = args.leads or args.directory.parent / "discovery-leads.jsonl"
        lead(args)
        return 0
    try:
        return run_monitor(args)
    except (OSError, ValueError, RuntimeError) as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
