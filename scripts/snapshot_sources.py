"""Save private source copies from verified cache responses; never submit pages to archives."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import unicodedata
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup

try:
    from .verify_excerpts import (
        Fetcher,
        atomic_json,
        content_problem,
        decoded_response,
        extract_text,
        html_text,
        is_data_zip,
        is_xlsx,
        literal_match,
        normalise,
        pdf_text_views,
    )
except ImportError:
    from verify_excerpts import (  # type: ignore[no-redef]  # Direct script entry point.
        Fetcher,
        atomic_json,
        content_problem,
        decoded_response,
        extract_text,
        html_text,
        is_data_zip,
        is_xlsx,
        literal_match,
        normalise,
        pdf_text_views,
    )

ROOT = Path(__file__).resolve().parents[1]
ARCHIVE_HOSTS = {
    "archive.ph",
    "archive.today",
    "archive.is",
    "archive.li",
    "archive.vn",
    "archive.fo",
    "archive.md",
}


# Default private snapshot directory; check.py --snapshots has the same default (tested).
_snapshot_directory = os.environ.get("AISI_SNAPSHOTS")
PRIVATE_SNAPSHOTS = Path(_snapshot_directory).expanduser() if _snapshot_directory else None

# Version of the text hash rule (schema: sources[].snapshot.text_sha256). Any change to
# main_text() or to the extraction it reuses is a new scheme and one deliberate re-hash
# of every stored copy (--rehash).
TEXT_SCHEME = 1
# Page furniture dropped before hashing; header and footer only outside main/article.
FURNITURE = (
    "head, title, script, style, template, noscript, nav, aside, "
    "[role=navigation], [role=banner], [role=contentinfo], [role=complementary], "
    "[role=search], #wm-ipp-base, #wm-ipp-print"
)


def outermost(nodes: list) -> list:
    ids = {id(node) for node in nodes}
    return [n for n in nodes if not any(id(parent) in ids for parent in n.parents)]


def main_text(body: bytes, mime: str) -> str:
    """Normalised main text of a copy, the input of the text hash.

    Implements text_scheme TEXT_SCHEME as the schema defines it (sources[].snapshot.text_sha256).
    """
    body = decoded_response(body)
    binary = body.startswith((b"%PDF-", b"PK\x03\x04"))
    if not binary and (
        "html" in mime
        or body.lstrip().lower().startswith((b"<!doctype html", b"<html"))
    ):
        soup = BeautifulSoup(body, "html.parser")
        for node in soup.select(FURNITURE):
            node.decompose()
        for node in soup.select("header, footer"):
            if not any(
                parent.name in {"main", "article"} or parent.get("role") == "main"
                for parent in node.parents
                if parent.attrs is not None
            ):
                node.decompose()
        candidates = [
            outermost(soup.select("main, [role=main]")),
            outermost(soup.select("article")),
            [soup.body or soup],
        ]
        text = ""
        for roots in candidates:
            if len(roots) == 1:
                # Serialising rewrites any <meta charset> to UTF-8, so re-reading is safe.
                text = html_text(str(roots[0]).encode(), visible_only=True)[0]
                if text.strip():
                    break
        if not text.strip():
            # Malformed markup can nest the whole page in furniture: keep the full view.
            text = html_text(body, visible_only=True)[0]
    else:
        text = extract_text(body, mime)[0]
        if text.startswith("Title:") and "Markdown Content:" in text:
            text = text.partition("Markdown Content:")[2]
    return " ".join(unicodedata.normalize("NFKC", text).split())


def text_fields(body: bytes, mime: str) -> dict:
    """Public text-hash fields, or none when the copy yields no text (file hash only)."""
    try:
        text = main_text(body, mime)
    except Exception:  # noqa: BLE001 - an unparseable copy falls back to its file hash
        return {}
    if not text:
        return {}
    return {
        "text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "text_scheme": TEXT_SCHEME,
    }


def archive_snapshot(url: str) -> bool:
    """A capture URL, never a search/newest/submit endpoint or archive landing page."""
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"}:
        return False
    if parts.hostname == "web.archive.org":
        return bool(re.fullmatch(r"/web/\d{14}(?:[a-z]+_)?/https?://.+", parts.path))
    if parts.hostname in ARCHIVE_HOSTS:
        return bool(
            re.fullmatch(r"/[A-Za-z0-9]{5,12}/?", parts.path)
            and parts.path.strip("/").lower()
            not in {"newest", "submit", "search", "about", "login"}
        ) or bool(re.fullmatch(r"/\d{14}/https?://.+", parts.path))
    return False


def archive_lookup_target(url: str) -> str:
    """Use the embedded original of a valid Wayback capture for archive lookup."""
    parts = urlsplit(url)
    if parts.hostname != "web.archive.org" or not archive_snapshot(url):
        return url
    original = re.sub(r"^/web/\d{14}(?:[a-z]+_)?/", "", parts.path)
    if parts.query:
        original += "?" + parts.query
    if parts.fragment:
        original += "#" + parts.fragment
    return original


def normalise_method(meta: dict, requested_role: str | None = None) -> dict:
    """Classify the copy URL without losing the cache's first requester role."""
    urls = [meta.get("final_url", ""), meta.get("url", meta.get("requested_url", ""))]
    role = requested_role or meta.get("method", "live")
    if any(urlsplit(url).hostname == "r.jina.ai" for url in urls):
        role = "render"
    elif any(archive_snapshot(url) for url in urls):
        role = role if role in {"archive_raw", "archive_lookup"} else "archive"
    result = {**meta, "method": role}
    cached_role = meta.get("cache_method", meta.get("method"))
    if cached_role is not None:
        result["cache_method"] = cached_role
    return result


def identity_matches(source: dict, text: str, title: str = "") -> bool:
    """Require a substantive quotation or informative title in document body text."""
    for excerpt in source.get("excerpts", []):
        if len(normalise(excerpt["text"])) >= 25 and literal_match(
            excerpt["text"], text
        ):
            return True
    source_title = normalise(source.get("title", "")).casefold()
    return bool(
        len(source_title) >= 40
        and len(source_title.split()) >= 5
        and source_title in normalise(text).casefold()
    )


def html_document_text(body: bytes) -> str:
    soup = BeautifulSoup(decoded_response(body), "html.parser")
    for node in soup.select("head, title, nav, #HEADER, #wm-ipp-base, #wm-ipp-print"):
        node.decompose()
    return html_text(str(soup).encode(), visible_only=True)[0]


def identity_views(fetcher, meta: dict, body: bytes) -> list[tuple[str, str]]:
    if body.startswith(b"%PDF-"):
        views = (
            []
            if meta.get("status") == "extraction_error"
            else [("default", fetcher.text(meta))]
        )
        if hasattr(fetcher, "pdf_views"):
            views.extend(recovered_pdf_views(fetcher, meta, body))
        return views
    if "html" in meta.get("content_type", "") or body.lstrip().lower().startswith(
        (b"<html", b"<!doctype html")
    ):
        return [("body", html_document_text(body))]
    return [("default", fetcher.text(meta))]


def normalised_url(url: str) -> str:
    parts = urlsplit(url)
    return urlunsplit(
        (parts.scheme.lower(), parts.netloc.lower(), parts.path or "/", parts.query, "")
    )


def capture_target(url: str, body: bytes = b"") -> str | None:
    parts = urlsplit(url)
    if parts.hostname == "web.archive.org" and archive_snapshot(url):
        return archive_lookup_target(url)
    if parts.hostname in ARCHIVE_HOSTS:
        match = re.match(
            r"/(?:\d{14}|\d{4}\.\d{2}\.\d{2}-\d{6})/(https?://.*)", parts.path
        )
        if match:
            return match.group(1) + ("?" + parts.query if parts.query else "")
        soup = BeautifulSoup(decoded_response(body), "html.parser")
        # Archive-generated longlink is stronger evidence than an arbitrary page link.
        longlink = soup.select_one("input#SHARE_LONGLINK[value]")
        if longlink:
            return capture_target(longlink["value"])
    return None


def allowed_targets(source: dict, fetcher) -> set[str]:
    targets = {normalised_url(archive_lookup_target(source["url"]))}
    path = fetcher.entry(source["url"]) / "metadata.json"
    if path.is_file():
        meta = json.loads(path.read_text(encoding="utf-8"))
        if meta.get("status") in {"ok", "no_extractable_text", "extraction_error"}:
            targets.add(
                normalised_url(
                    archive_lookup_target(meta.get("final_url", source["url"]))
                )
            )
    return targets


def raw_wayback_url(meta: dict, fetcher) -> str | None:
    url = meta.get("final_url", meta["url"])
    if urlsplit(url).hostname != "web.archive.org" or not archive_snapshot(url):
        return None
    path = fetcher.entry(meta["url"]) / "response.bin"
    if path.is_file():
        body = path.read_bytes()
        if hashlib.sha256(body).hexdigest() != meta.get("sha256"):
            raise ValueError("Archive wrapper checksum mismatch")
        soup = BeautifulSoup(decoded_response(body), "html.parser")
        for node in soup.select("iframe[src], embed[src], object[data]"):
            candidate = urljoin(url, node.get("src", node.get("data", "")))
            if (
                archive_snapshot(candidate)
                and capture_target(candidate) == capture_target(url)
                and re.search(r"/web/\d{14}(?:id|if)_/", candidate)
                and (candidate_stamp := re.search(r"/web/(\d{14})", candidate))
                and (viewer_stamp := re.search(r"/web/(\d{14})", url))
                and candidate_stamp[1] == viewer_stamp[1]
            ):
                return candidate
    candidate = re.sub(r"(/web/\d{14})(?:[a-z]+_)?/", r"\1id_/", url)
    return candidate if candidate != url else None


def recovered_pdf_views(fetcher, meta: dict, body: bytes) -> list[tuple[str, str]]:
    """Reuse independently extracted text without relabelling the primary parser result."""
    if not body.startswith(b"%PDF-"):
        return []
    return [
        (name, text)
        for name, text in fetcher.pdf_views(meta)
        if normalise(text)
        and content_problem(
            body, text, meta.get("title", ""), meta.get("final_url", meta["url"])
        )
        is None
    ]


def missing_pdf_eof(body: bytes) -> bool:
    """Require a trailing PDF marker within 1 KiB; this is not full structural validation."""
    body = decoded_response(body)
    return body.startswith(b"%PDF-") and not body[-1024:].rstrip(
        b"\x00\t\n\f\r "
    ).endswith(b"%%EOF")


def valid_response(fetcher, meta: dict) -> tuple[bytes | None, str | None]:
    if meta.get("status") not in {"ok", "no_extractable_text", "extraction_error"}:
        return None, meta.get("status", "unreachable")
    folder = fetcher.entry(meta["url"])
    raw = folder / "response.bin"
    if not raw.is_file():
        return None, "missing_response"
    body = raw.read_bytes()
    if hashlib.sha256(body).hexdigest() != meta.get("sha256"):
        return None, "checksum_mismatch"
    if meta.get("bytes") is not None and len(body) != meta["bytes"]:
        return None, "incomplete_response"
    if not body:
        return None, "empty_response"
    if missing_pdf_eof(body):
        return None, "incomplete_pdf"
    if meta.get("status") == "extraction_error":
        return (
            (body, None)
            if recovered_pdf_views(fetcher, meta, body)
            else (None, "extraction_error")
        )
    problem = content_problem(
        body,
        fetcher.text(meta),
        meta.get("title", ""),
        meta.get("final_url", meta["url"]),
    )
    # This status follows a successful parse, not a transport or parser failure.
    # Keep complete scan PDFs privately even though literal quote matching is impossible.
    if (
        problem == "no_extractable_text"
        and (
            body.startswith(b"%PDF-")
            or is_xlsx(body)
            or is_data_zip(body, meta.get("content_type", ""))
        )
        and meta.get("status") == "no_extractable_text"
    ):
        problem = None
    return (None, problem) if problem else (body, None)


def check_archive(
    source: dict, fetcher, url: str, method: str = "archive"
) -> tuple[dict | None, dict]:
    meta = normalise_method(fetcher.fetch(url, method), method)
    body, problem = valid_response(fetcher, meta)
    outcome: dict = {"requested_url": url, "status": problem or "ok"}
    if problem == "archive_wrapper" and method != "archive_raw":
        raw_url = raw_wayback_url(meta, fetcher)
        if raw_url:
            raw_meta, raw_outcome = check_archive(
                source, fetcher, raw_url, "archive_raw"
            )
            outcome["raw_attempt"] = raw_outcome
            if raw_meta:
                return raw_meta, {
                    **outcome,
                    "status": "verified",
                    "final_url": raw_outcome["final_url"],
                }
            if raw_outcome["status"] in {
                "login_or_challenge",
                "error_or_challenge",
                "cookie_settings_shell",
                "empty_render",
                "javascript_shell",
                "document_viewer",
                "no_capture",
            }:
                return None, {
                    **outcome,
                    "status": raw_outcome["status"],
                    "reason": "raw_capture_rejected",
                }
            # A viewer is not proof that its payload is absent. Preserve the URL
            # with an explicit unresolved status when the raw payload was not checked.
            return None, {
                **outcome,
                "status": "not_checked",
                "reason": "raw_capture_not_verified",
            }
    if body is None:
        if problem == "not_cached" or meta.get("error") == "HTTP 429":
            outcome.update(status="not_checked", reason=meta.get("error", problem))
        return None, outcome
    final = meta.get("final_url", url)
    exact_original = False
    original_meta_path = fetcher.entry(source["url"]) / "metadata.json"
    if body.startswith((b"%PDF-", b"PK\x03\x04")) and original_meta_path.is_file():
        original_meta = json.loads(original_meta_path.read_text(encoding="utf-8"))
        if original_meta.get("sha256") == meta.get("sha256"):
            original_body, original_problem = valid_response(fetcher, original_meta)
            exact_original = original_problem is None and original_body == body
    identified = exact_original or any(
        identity_matches(source, text)
        for _, text in identity_views(fetcher, meta, body)
    )
    if not archive_snapshot(final):
        declared_copy = (
            (url == source.get("archive_url") or method == "declared_copy")
            and any(e.get("url") == url for e in source.get("excerpts", []))
            and urlsplit(url).hostname == urlsplit(final).hostname
            and urlsplit(final).hostname
            not in ARCHIVE_HOSTS | {"web.archive.org", "r.jina.ai"}
        )
        if declared_copy and identified:
            return {
                **meta,
                "cache_method": meta.get("cache_method", meta.get("method")),
                "method": "declared_mirror",
            }, {**outcome, "status": "verified_mirror", "final_url": final}
        return None, {**outcome, "status": "no_capture", "final_url": final}
    target = capture_target(final, body)
    allowed = allowed_targets(source, fetcher)
    bound = target is not None and normalised_url(target) in allowed
    if target and not bound and exact_original:
        # Kenya's publisher moved between www and the bare host. Only identical
        # validated document bytes permit this otherwise unrecorded host alias.
        def without_www(value):
            parts = urlsplit(normalised_url(value))
            return parts._replace(netloc=parts.netloc.removeprefix("www.")).geturl()

        bound = without_www(target) in {without_www(value) for value in allowed}
    if not bound:
        return None, {
            **outcome,
            "status": "target_not_verified",
            "final_url": final,
            "capture_target": target,
        }
    if not identified:
        return None, {**outcome, "status": "identity_not_verified", "final_url": final}
    return meta, {
        **outcome,
        "status": "verified",
        "final_url": final,
        "capture_target": target,
    }


def change_reason(previous: dict, digest: str, fields: dict) -> str:
    """Why a new copy would replace the recorded snapshot, or "text_unchanged"."""
    if previous["sha256"] == digest:
        return "same_file"
    if previous.get("text_scheme") != TEXT_SCHEME or "text_sha256" not in previous:
        return "no_comparable_text_hash"
    if not fields:
        return "no_text"
    if fields["text_sha256"] == previous["text_sha256"]:
        return "text_unchanged"
    return "text_changed"


def bound_quotes(source: dict) -> list[str]:
    """Quotations whose declared copy is the source itself (no URL, its URL or its archive)."""
    own = {None, source["url"], source.get("archive_url")}
    return [e["text"] for e in source.get("excerpts", []) if e.get("url") in own]


def copy_views(body: bytes, mime: str, path: Path) -> list[str]:
    """The verifier's text views of one copy held at path: primary, visible HTML, PDF views.

    The cited copy and a new copy are always compared through this one function.
    """
    views = []
    try:
        views.append(extract_text(body, mime)[0])
    except Exception:  # noqa: BLE001 - the independent PDF views may still read it
        pass
    if not body.startswith((b"%PDF-", b"PK\x03\x04")) and (
        "html" in mime or body.lstrip().lower().startswith((b"<!doctype html", b"<html"))
    ):
        views.append(html_text(body, visible_only=True)[0])
    views.extend(text for _, text in pdf_text_views(body, path))
    return views


def stored_copy(source: dict, directory: Path, fallback: Path) -> tuple[dict, Path] | None:
    """The private copy whose bytes the published snapshot hash names, checksum-verified."""
    previous = source.get("snapshot")
    if not previous:
        return None
    for root in (directory, fallback):
        manifest_path = root / source["id"] / "manifest.json"
        if not manifest_path.is_file():
            continue
        for row in reversed(json.loads(manifest_path.read_text(encoding="utf-8"))):
            path = (root / row["path"]).resolve()
            if (
                row.get("sha256") != previous["sha256"]
                or not path.is_relative_to(root.resolve())
                or not path.is_file()
            ):
                continue
            if hashlib.sha256(path.read_bytes()).hexdigest() != previous["sha256"]:
                raise ValueError("Stored snapshot checksum mismatch")
            return row, path
    return None


def rehash_source(source: dict, directory: Path, fallback: Path) -> dict:
    """Text hash of the copy the published snapshot names; never fetches."""
    previous = source.get("snapshot")
    if not previous:
        return {"source_id": source["id"], "status": "no_snapshot"}
    try:
        found = stored_copy(source, directory, fallback)
    except ValueError as exc:
        found, reason = None, f"{type(exc).__name__}: {exc}"
    else:
        reason = "previous_copy_missing"
    if found is None:
        return {
            "source_id": source["id"],
            "status": "failed",
            "reason": reason,
            "previous_snapshot_retained": True,
        }
    row, path = found
    fields = text_fields(path.read_bytes(), row.get("mime", ""))
    return {
        "source_id": source["id"],
        "status": "rehashed",
        "snapshot": {
            "retrieved": previous["retrieved"],
            "sha256": previous["sha256"],
            **fields,
        },
        "text_hash": bool(fields),
    }


def extension(body: bytes, mime: str) -> str:
    if body.startswith(b"%PDF-"):
        return "pdf"
    if is_xlsx(body):
        return "xlsx"
    if is_data_zip(body, mime):
        return "zip"
    if body.startswith(b"PK\x03\x04"):
        return "docx"  # Fetcher has already rejected ZIPs without word/document.xml.
    if "html" in mime or body.lstrip().lower().startswith(
        (b"<!doctype html", b"<html")
    ):
        return "html"
    if "json" in mime:
        return "json"
    return "txt"


def stamp(value: str) -> str:
    if not re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})", value
    ):
        raise ValueError(
            "Snapshot timestamp must be the recorded UTC/offset retrieval time"
        )
    return value.replace(":", "-").replace("+", "plus")


def atomic_bytes(path: Path, body: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() != body:
            raise ValueError(f"Existing snapshot differs: {path.name}")
        return
    temp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    temp.write_bytes(body)
    temp.replace(path)


def save_snapshot(
    source: dict, meta: dict, body: bytes, directory: Path, fields: dict | None = None
) -> tuple[dict, dict]:
    meta = normalise_method(meta)
    sid = source["id"]
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", sid) or sid in {".", ".."}:
        raise ValueError("Unsafe source ID")
    digest = hashlib.sha256(body).hexdigest()
    previous = source.get("snapshot", {})
    retrieved = (
        previous["retrieved"] if previous.get("sha256") == digest else meta["retrieved"]
    )
    snapshot = {
        "retrieved": retrieved,
        "sha256": digest,
        **(text_fields(body, meta.get("content_type", "")) if fields is None else fields),
    }
    relative = (
        Path(sid)
        / f"{stamp(retrieved)}-{digest[:16]}.{extension(body, meta.get('content_type', ''))}"
    )
    atomic_bytes(directory / relative, body)
    manifest = {
        "source_id": sid,
        "requested_url": meta["url"],
        "final_url": meta.get("final_url", meta["url"]),
        "method": meta["method"],
        "retrieved": retrieved,
        "mime": meta.get("content_type", ""),
        "sha256": digest,
        "path": str(relative),
    }
    if meta.get("cache_method"):
        manifest["cache_method"] = meta["cache_method"]
    manifest_path = directory / sid / "manifest.json"
    old = (
        json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest_path.exists()
        else []
    )
    if not any(
        row["sha256"] == digest
        and row["retrieved"] == retrieved
        and row.get("method") == manifest["method"]
        and row.get("requested_url") == manifest["requested_url"]
        and row.get("final_url") == manifest["final_url"]
        for row in old
    ):
        atomic_json(manifest_path, [*old, manifest])
    return snapshot, manifest


def prefer_rendered_html(
    source: dict, fetcher, live: dict, body: bytes, attempts: list
) -> dict:
    """Replace a usable HTML shell only on positive literal rendered evidence."""
    live = normalise_method(live)
    if (
        body.startswith(b"%PDF-")
        or is_xlsx(body)
        or is_data_zip(body, live.get("content_type", ""))
    ):
        return live
    if "html" not in live.get(
        "content_type", ""
    ) and not body.lstrip().lower().startswith((b"<!doctype html", b"<html")):
        return live
    quotes = [
        e["text"] for e in source.get("excerpts", []) if len(normalise(e["text"])) >= 25
    ]
    if not quotes:
        return live
    original_views = [fetcher.text(live), html_text(body, visible_only=True)[0]]
    if any(literal_match(quote, text) for quote in quotes for text in original_views):
        return live
    render_url = "https://r.jina.ai/" + source["url"]
    rendered = normalise_method(fetcher.fetch(render_url, "render"), "render")
    _, problem = valid_response(fetcher, rendered)
    matched = problem is None and any(
        literal_match(quote, fetcher.text(rendered)) for quote in quotes
    )
    attempts.append(
        {
            "url": render_url,
            "status": problem
            or ("matched_excerpt" if matched else "no_matching_excerpt"),
        }
    )
    return rendered if matched else live


def preserved_original(
    source: dict, fetcher, directory: Path, fallback: Path
) -> dict | None:
    """Find and validate the currently published original bytes before a render downgrade."""
    previous = source.get("snapshot")
    if not previous:
        return None
    for root in (directory, fallback):
        manifest_path = root / source["id"] / "manifest.json"
        if not manifest_path.is_file():
            continue
        for stored_row in reversed(
            json.loads(manifest_path.read_text(encoding="utf-8"))
        ):
            row = normalise_method(stored_row)
            if (
                row.get("sha256") != previous["sha256"]
                or row.get("retrieved") != previous["retrieved"]
                or row.get("method") == "render"
            ):
                continue
            path = (root / row["path"]).resolve()
            if not path.is_relative_to(root.resolve()) or not path.is_file():
                continue
            body = path.read_bytes()
            if hashlib.sha256(body).hexdigest() != previous["sha256"]:
                raise ValueError("Previous snapshot checksum mismatch")
            if missing_pdf_eof(body):
                continue
            try:
                text, title = extract_text(body, row.get("mime", ""))
            except Exception:  # noqa: BLE001 - reuse shared independent PDF validation if available
                cached = fetcher.entry(row["requested_url"]) / "metadata.json"
                if body.startswith(b"%PDF-") and cached.is_file():
                    meta = json.loads(cached.read_text(encoding="utf-8"))
                    if (
                        meta.get("sha256") == previous["sha256"]
                        and valid_response(fetcher, meta)[1] is None
                    ):
                        return row
                continue
            problem = content_problem(
                body, text, title, row.get("final_url", row["requested_url"])
            )
            if problem and not (
                problem == "no_extractable_text"
                and (
                    body.startswith(b"%PDF-")
                    or is_xlsx(body)
                    or is_data_zip(body, row.get("mime", ""))
                )
            ):
                continue
            is_html = "html" in row.get("mime", "") or body.lstrip().lower().startswith(
                (b"<html", b"<!doctype html")
            )
            quotes = [
                e["text"]
                for e in source.get("excerpts", [])
                if len(normalise(e["text"])) >= 25
            ]
            if (
                is_html
                and quotes
                and not any(literal_match(q, html_document_text(body)) for q in quotes)
            ):
                # This may be the original JS shell that the rendering resolves.
                continue
            return row
    return None


def process_source(
    source: dict,
    fetcher,
    directory: Path,
    fallback: Path,
    lookup_archives: bool = False,
) -> dict:
    """Return only proposed public metadata; keep private paths in the private manifest."""
    attempts, archives = [], []
    chosen = None
    live = normalise_method(fetcher.fetch(source["url"], "live"), "live")
    live_body, problem = valid_response(fetcher, live)
    if (
        problem is None
        and live_body is not None
        and normalised_url(live.get("final_url", source["url"]))
        != normalised_url(source["url"])
    ):
        if not any(
            identity_matches(source, text)
            for _, text in identity_views(fetcher, live, live_body)
        ):
            problem = "redirect_identity_not_verified"
    attempts.append({"url": source["url"], "status": problem or "ok"})
    if problem is None and live_body is not None:
        chosen = prefer_rendered_html(source, fetcher, live, live_body, attempts)
    verified_archive = None
    declared_mirror = None
    rejected_archive = None
    declared_archive_verified = False
    if source.get("archive_url"):
        verified_archive, outcome = check_archive(
            source, fetcher, source["archive_url"]
        )
        archives.append(outcome)
        declared_archive_verified = outcome["status"] == "verified"
        if outcome["status"] == "verified_mirror":
            declared_mirror, verified_archive = verified_archive, None
            rejected_archive = {
                "url": source["archive_url"],
                "reason": "declared_copy_is_not_archive_capture",
            }
        if outcome["status"] in {
            "archive_wrapper",
            "login_or_challenge",
            "error_or_challenge",
            "cookie_settings_shell",
            "empty_render",
            "javascript_shell",
            "document_viewer",
            "no_capture",
        }:
            rejected_archive = {
                "url": source["archive_url"],
                "reason": outcome["status"],
            }
    if lookup_archives and verified_archive is None:
        # GET only. Fetcher follows the lookup redirect but never submits a page for capture.
        verified_archive, outcome = check_archive(
            source,
            fetcher,
            "https://archive.ph/newest/" + archive_lookup_target(source["url"]),
            "archive_lookup",
        )
        archives.append(outcome)
    if chosen is None:
        chosen = verified_archive or declared_mirror
    if chosen is None:
        # After correcting archive_url, a cited publisher copy remains discoverable.
        copy_urls = dict.fromkeys(
            e["url"]
            for e in source.get("excerpts", [])
            if e.get("url") and e["url"] != source["url"]
        )
        for copy_url in copy_urls:
            if urlsplit(copy_url).hostname in ARCHIVE_HOSTS | {
                "web.archive.org",
                "r.jina.ai",
            }:
                continue
            candidate, outcome = check_archive(
                source, fetcher, copy_url, "declared_copy"
            )
            attempts.append({"url": copy_url, "status": outcome["status"]})
            if outcome["status"] == "verified_mirror":
                chosen = candidate
                break
    if chosen is None:
        render_url = "https://r.jina.ai/" + source["url"]
        rendered = normalise_method(fetcher.fetch(render_url, "render"), "render")
        _, problem = valid_response(fetcher, rendered)
        attempts.append({"url": render_url, "status": problem or "ok"})
        if problem is None:
            chosen = rendered
    row = {"source_id": source["id"], "attempts": attempts, "archives": archives}
    if rejected_archive:
        row["proposed_archive_removal"] = rejected_archive
    if verified_archive:
        final = verified_archive.get("final_url", verified_archive["url"])
        if final != source.get("archive_url"):
            if (
                not source.get("archive_url")
                or rejected_archive
                or declared_archive_verified
            ):
                row["proposed_archive_url"] = final
            else:
                # A verified alternative does not disprove the declared capture.
                row["verified_alternative_archive_url"] = final
    if chosen is None:
        return {
            **row,
            "status": "failed",
            "previous_snapshot_retained": bool(source.get("snapshot")),
        }
    if chosen.get("method") == "render":
        previous = preserved_original(source, fetcher, directory, fallback)
        if previous:
            return {
                **row,
                "status": "retained",
                "snapshot": source["snapshot"],
                "evidence_url": previous["requested_url"],
                "method": previous["method"],
                "reason": "existing_original_preserved",
                "fallback_used": False,
            }
    body, problem = valid_response(fetcher, chosen)
    if body is None:
        return {
            **row,
            "status": "failed",
            "reason": problem,
            "previous_snapshot_retained": bool(source.get("snapshot")),
        }
    previous = source.get("snapshot")
    fields = text_fields(body, chosen.get("content_type", ""))
    change = None
    if previous:
        change = change_reason(previous, hashlib.sha256(body).hexdigest(), fields)
        keep = change == "text_unchanged"
        quotes = bound_quotes(source)
        # Read the cited copy only where it is used, so a corrupt copy is still
        # repaired by a new save everywhere else.
        held = (
            stored_copy(source, directory, fallback)
            if keep or (quotes and change != "same_file")
            else None
        )
        if change not in {"same_file", "text_unchanged"} and quotes and not held:
            change = "previous_copy_missing"
        elif change not in {"same_file", "text_unchanged"} and quotes:
            # Bound quotations decide (dataset.method, source copies): replace the
            # cited copy only when a quotation it carries is missing from the new
            # copy, or the new copy carries one the cited copy lacks (a repair).
            new_views = copy_views(
                body,
                chosen.get("content_type", ""),
                fetcher.entry(chosen["url"]) / "response.bin",
            )
            old_views = copy_views(held[1].read_bytes(), held[0].get("mime", ""), held[1])
            in_new = [any(literal_match(q, v) for v in new_views) for q in quotes]
            in_old = [any(literal_match(q, v) for v in old_views) for q in quotes]
            lost = sum(old and not new for old, new in zip(in_old, in_new))
            gained = sum(new and not old for old, new in zip(in_old, in_new))
            row["bound_quotes"] = {
                "total": len(quotes),
                "lost_in_new_copy": lost,
                "gained_in_new_copy": gained,
                "in_neither": sum(not (old or new) for old, new in zip(in_old, in_new)),
            }
            if lost:
                change = "bound_quote_missing"
            elif gained:
                change = "cited_copy_lacks_bound_quote"
            else:
                keep = True
        if keep:
            # A byte-only, furniture or quotation-neutral change keeps the cited
            # copy, provided that copy is still held privately.
            if held:
                held_row = normalise_method(held[0])
                return {
                    **row,
                    "status": "retained",
                    "snapshot": previous,
                    "evidence_url": held_row["requested_url"],
                    "method": held_row["method"],
                    "candidate_url": chosen["url"],
                    "change": change,
                    "fallback_used": False,
                }
            change = "previous_copy_missing"
    used_fallback = False
    try:
        snapshot, _ = save_snapshot(source, chosen, body, directory, fields)
    except PermissionError:
        snapshot, _ = save_snapshot(source, chosen, body, fallback, fields)
        used_fallback = True
    if change == "same_file":
        # Identical bytes: the recorded metadata stands; text hashes are added only
        # by the deliberate --rehash from the cited copy.
        snapshot = previous
    return {
        **row,
        "status": "retained" if change == "same_file" else "saved",
        "snapshot": snapshot,
        "evidence_url": chosen["url"],
        "method": chosen["method"],
        **({"change": change or "new_snapshot"} if change != "same_file" else {}),
        "fallback_used": used_fallback,
    }


def process_source_safe(
    source: dict,
    fetcher,
    directory: Path,
    fallback: Path,
    lookup_archives: bool = False,
) -> dict:
    """Isolate malformed source/cache metadata without interrupting other results."""
    try:
        return process_source(source, fetcher, directory, fallback, lookup_archives)
    except (OSError, ValueError, TypeError, KeyError) as exc:
        return {
            "source_id": source.get("id", "unknown"),
            "status": "failed",
            "reason": f"{type(exc).__name__}: {exc}",
            "previous_snapshot_retained": bool(source.get("snapshot")),
        }


def apply_result(source: dict, result: dict) -> None:
    if result.get("snapshot"):
        source["snapshot"] = result["snapshot"]
    rejected = result.get("proposed_archive_removal")
    if rejected and source.get("archive_url") == rejected["url"]:
        source.pop("archive_url")
    if result.get("proposed_archive_url"):
        source["archive_url"] = result["proposed_archive_url"]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=ROOT / "data/aisi.json")
    parser.add_argument("--cache", type=Path, default=ROOT / "work/source-cache")
    parser.add_argument(
        "--directory",
        type=Path,
        default=PRIVATE_SNAPSHOTS,
        help="private source store (default: AISI_SNAPSHOTS; required if unset)",
    )
    parser.add_argument(
        "--report", type=Path, default=ROOT / "work/reports/source-snapshots.json"
    )
    parser.add_argument(
        "--offline",
        action="store_true",
        help="read cache only; never access the network",
    )
    parser.add_argument(
        "--lookup-archives",
        action="store_true",
        help="GET archive.ph newest once when no declared archive validates; never submit a capture",
    )
    parser.add_argument(
        "--rehash",
        action="store_true",
        help="recompute text hashes from the stored copies the published hashes name; never fetches",
    )
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--timeout", type=float, default=18)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="apply snapshot hashes/timestamps and verified archive URLs to the dataset",
    )
    args = parser.parse_args()
    if args.directory is None:
        parser.error("set AISI_SNAPSHOTS or pass --directory DIR for the private source store")
    args.directory = args.directory.expanduser().resolve()
    data = json.loads(args.data.read_text(encoding="utf-8"))
    if args.rehash:
        results = [
            rehash_source(source, args.directory, ROOT / "work/snapshots")
            for source in data["sources"]
        ]
        if args.apply:
            for source, result in zip(data["sources"], results):
                apply_result(source, result)
            atomic_json(args.data, data)
        atomic_json(args.report, {"partial": False, "sources": results})
        counts: dict[str, int] = {}
        for result in results:
            key = result["status"]
            if key == "rehashed" and not result["text_hash"]:
                key = "rehashed_without_text"
            counts[key] = counts.get(key, 0) + 1
        print(json.dumps(counts, sort_keys=True))
        return
    fetcher = Fetcher(args.cache, offline=args.offline, timeout=args.timeout)
    results = []

    def process(source):
        return process_source_safe(
            source,
            fetcher,
            args.directory,
            ROOT / "work/snapshots",
            args.lookup_archives,
        )

    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        pending = {pool.submit(process, source): source for source in data["sources"]}
        for future in as_completed(pending):
            source = pending[future]
            result = future.result()
            results.append(result)
            if args.apply:
                apply_result(source, result)
            print(
                f"{len(results)}/{len(data['sources'])} {source['id']}: {result['status']}"
                + (f" ({result['change']})" if result.get("change") else "")
                + (
                    " (permission fallback: work/snapshots)"
                    if result.get("fallback_used")
                    else ""
                ),
                flush=True,
            )
            atomic_json(args.report, {"partial": True, "sources": results})
    if args.apply:
        atomic_json(args.data, data)
    atomic_json(args.report, {"partial": False, "sources": results})


if __name__ == "__main__":
    main()
