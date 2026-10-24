"""Check source quotations against cached live or archived documents (online, opt-in)."""

import argparse
import gzip
import hashlib
import json
import posixpath
import re
import shutil
import ssl
import subprocess
import threading
import time
import unicodedata
import uuid
import zlib
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from http.client import HTTPException, IncompleteRead
from io import BytesIO
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen
from xml.etree import ElementTree
from zipfile import ZipFile

import truststore
from bs4 import BeautifulSoup
from pypdf import PdfReader

try:
    from references import excerpt_identity
    from published import publish
    from method_checks import required_excerpt_pointers
except ImportError:
    from scripts.references import excerpt_identity
    from scripts.published import publish
    from scripts.method_checks import required_excerpt_pointers

ROOT = Path(__file__).resolve().parents[1]
EXTRACTION_VERSION = 20
MAX_BYTES = 128 * 1024 * 1024
QUOTES = str.maketrans(
    {c: "'" for c in "‘’‚‛"}
    | {c: '"' for c in "“”„‟"}
    # Latin typographic ligatures, which NFC keeps (PDF text layers often use them).
    | {"\ufb00": "ff", "\ufb01": "fi", "\ufb02": "fl", "\ufb03": "ffi", "\ufb04": "ffl", "\ufb05": "st", "\ufb06": "st"}
)
# Marks an HTML element boundary in extracted text. It is whitespace to str.split(), so
# normalise() and the snapshot text hash treat it as a space; literal_match() treats it
# as a hard token boundary, so a number never continues across two elements.
BOUNDARY = "\u2029"


def source_text(text: str) -> str:
    """Extracted text with any U+2029 in the source itself made a line break, so only
    html_text() can put a BOUNDARY into text."""
    return text.replace(BOUNDARY, "\n")
BLOCKED_TITLE = re.compile(
    r"^(?:just a moment|access denied|attention required|verify (?:you are|you're) human|"
    r"page not found|404(?:\b.*)?|403(?:\b.*)?|error(?:\s+[45]\d{2})?|sign in|log in|login|"
    r"robot check|security check|service unavailable|request rejected)(?:\s*[|:!.\-].*)?$",
    re.IGNORECASE,
)
# A site-name prefix followed by a bare error label, e.g. "Documentazione Economica e Finanziaria - Errore".
ERROR_TITLE_SUFFIX = re.compile(
    r"(?:[|:\u2013\u2014]|\s-)\s*(?:error|errore|erreur|fehler|erro|fout|b\u0142\u0105d)(?:\s+[45]\d{2})?\s*$",
    re.IGNORECASE,
)
# The final rendered line of an application that had not loaded its content.
LOADING_LINE = re.compile(
    r"(?:loading|chargement|cargando|caricamento|carregando|wird geladen|laden|please wait)\s*(?:\.\.\.|\u2026)",
    re.IGNORECASE,
)
BAD_PATH = re.compile(
    r"/(?:login|log-in|signin|sign-in|captcha|signon)(?:[/?#._-]|$)", re.IGNORECASE
)


def well_formed(text: str) -> str:
    """Replace lone surrogates, which some PDF text layers yield, with U+FFFD."""
    return re.sub("[\ud800-\udfff]", "\ufffd", text)


def normalise(text: str) -> str:
    """Fold canonical Unicode, whitespace, quotation marks and Latin ligatures, not wording or omissions."""
    return " ".join(unicodedata.normalize("NFC", text).translate(QUOTES).split())


def literal_match(quote: str, text: str) -> bool:
    """Match one normalised view without truncating a numeric token at either edge.

    text is extractor output, where a BOUNDARY can only be an HTML element boundary.
    """
    # PDF line wraps can split Japanese or Chinese text inside a word. Keep this
    # comparison rule separate from normalise(), which also defines snapshot hashes.
    cjk = r"\u3041-\u3096\u30a1-\u30fa\u30fc\u3400-\u4dbf\u4e00-\u9fff"
    gap = rf"(?<=[{cjk}])\s+(?=[{cjk}])"
    needle = re.sub(gap, "", normalise(quote))
    if not needle:
        return False
    # Normalise each element's text separately and join them with one marker, so the
    # marked view has the same length as the plain view the quotation is found in.
    marked = re.sub(
        gap,
        "",
        BOUNDARY.join(
            filter(None, (normalise(part) for part in text.split(BOUNDARY)))
        ),
    )
    haystack = marked.replace(BOUNDARY, " ")
    start = haystack.find(needle)
    while start >= 0:
        end = start + len(needle)
        separators = ",.\u066b\u066c '，．"
        # The marked view: an element boundary is not a number separator.
        left_numeric = start > 0 and (
            marked[start - 1].isdigit()
            or (
                start > 1
                and marked[start - 1] in separators
                and marked[start - 2].isdigit()
            )
        )
        right_numeric = end < len(marked) and (
            marked[end].isdigit()
            or (
                end + 1 < len(marked)
                and marked[end] in separators
                and marked[end + 1].isdigit()
            )
        )
        left_ok = not (needle[0].isdigit() and left_numeric)
        right_ok = not (needle[-1].isdigit() and right_numeric)
        if left_ok and right_ok:
            return True
        start = haystack.find(needle, start + 1)
    return False


def is_xlsx(body: bytes) -> bool:
    """Recognise a ZIP with a bounded, valid SpreadsheetML workbook part."""
    if not body.startswith(b"PK\x03\x04"):
        return False
    try:
        with ZipFile(BytesIO(body)) as archive:
            entry = archive.getinfo("xl/workbook.xml")
            if entry.file_size > MAX_BYTES:
                return False
            root = ElementTree.fromstring(archive.read(entry))
            return root.tag in {
                "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}workbook",
                "{http://purl.oclc.org/ooxml/spreadsheetml/main}workbook",
            }
    except Exception:  # noqa: BLE001 - reject malformed or unsupported archive parsers
        return False


def is_data_zip(body: bytes, content_type: str) -> bool:
    """Validate a declared ZIP data archive without writing or executing members."""
    if content_type.split(";", 1)[0].strip().lower() != "application/zip":
        return False
    try:
        with ZipFile(BytesIO(body)) as archive:
            entries = archive.infolist()
            if not entries or sum(entry.file_size for entry in entries) > MAX_BYTES:
                return False
            return archive.testzip() is None
    except Exception:  # noqa: BLE001 - reject malformed or unsupported archive parsers
        return False


def decoded_response(body: bytes) -> bytes:
    """Decode a gzip response for inspection while callers retain the original bytes."""
    if not body.startswith(b"\x1f\x8b"):
        return body
    try:
        with gzip.GzipFile(fileobj=BytesIO(body)) as stream:
            decoded = stream.read(MAX_BYTES + 1)
    except (OSError, EOFError, zlib.error) as exc:
        raise ValueError("Invalid or incomplete gzip response") from exc
    if len(decoded) > MAX_BYTES:
        raise ValueError("Decoded response exceeds size limit")
    return decoded


def archive_text(body: bytes, *, workbook: bool) -> str:
    """Read bounded literal string cells or README text without extracting files.

    XLSX numeric cells, formula results, phonetic annotations and hidden sheets
    are excluded; quotations relying on those cells need a separate visual check.
    """
    with ZipFile(BytesIO(body)) as archive:
        entries = archive.infolist()
        if sum(entry.file_size for entry in entries) > MAX_BYTES:
            raise ValueError("Archive expanded text exceeds size limit")
        if len({entry.filename for entry in entries}) != len(entries):
            raise ValueError("Archive contains duplicate member names")
        if not workbook:
            return "\n".join(
                archive.read(entry).decode("utf-8-sig")
                for entry in entries
                if Path(entry.filename).name.lower() in {"readme.md", "readme.txt"}
            )
        workbook_root = ElementTree.fromstring(archive.read("xl/workbook.xml"))
        if not workbook_root.tag.startswith("{"):
            raise ValueError("Workbook namespace is required")
        ns = workbook_root.tag.split("}")[0] + "}"

        def literal_string(item):
            # Phonetic annotations (rPh) are readings, not cell content.
            pieces = []
            for child in item:
                if child.tag == ns + "t":
                    pieces.append(child.text or "")
                elif child.tag == ns + "r":
                    pieces.extend(node.text or "" for node in child.findall(ns + "t"))
            return "".join(pieces)

        strings = []
        if "xl/sharedStrings.xml" in archive.namelist():
            root = ElementTree.fromstring(archive.read("xl/sharedStrings.xml"))
            strings = [literal_string(item) for item in root.findall(ns + "si")]
        visible = [sheet for sheet in workbook_root.findall(ns + "sheets/" + ns + "sheet") if sheet.get("state", "visible") == "visible"]
        if not visible:
            return ""
        relations = ElementTree.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
        targets = {node.get("Id"): node.get("Target", "") for node in relations if node.get("TargetMode") != "External"}
        sheets = []
        for sheet in visible:
            rid = next((value for key, value in sheet.attrib.items() if key.endswith("}id")), None)
            target = targets.get(rid, "")
            member = posixpath.normpath(target.lstrip("/") if target.startswith("/") else "xl/" + target)
            if not re.fullmatch(r"xl/worksheets/[^/]+\.xml", member):
                raise ValueError("Invalid worksheet relationship")
            sheets.append(member)
        cells = []
        for member in sheets:
            root = ElementTree.fromstring(archive.read(member))
            for cell in root.iter(ns + "c"):
                # Formula results and numbers are not evaluated or reformatted.
                if cell.find(ns + "f") is not None:
                    continue
                if cell.get("t") == "s":
                    value = cell.findtext(ns + "v", "")
                    if not re.fullmatch(r"0|[1-9][0-9]*", value) or int(value) >= len(strings):
                        raise ValueError("Invalid shared string cell reference")
                    cells.append(strings[int(value)])
                elif cell.get("t") == "inlineStr":
                    inline = cell.find(ns + "is")
                    cells.append(literal_string(inline) if inline is not None else "")
        return "\n".join(cells)


def extract_text(body: bytes, content_type: str) -> tuple[str, str]:
    """Return primary literal text and title; abbreviation attributes are ignored."""
    body = decoded_response(body)
    if is_xlsx(body):
        return source_text(archive_text(body, workbook=True)), ""
    if is_data_zip(body, content_type):
        return source_text(archive_text(body, workbook=False)), ""
    if body.startswith(b"PK\x03\x04"):
        with ZipFile(BytesIO(body)) as archive:
            if "word/document.xml" not in archive.namelist():
                raise ValueError("ZIP response is not a Word document")
            entry = archive.getinfo("word/document.xml")
            if entry.file_size > MAX_BYTES:
                raise ValueError("Word document XML exceeds size limit")
            document = ElementTree.fromstring(archive.read(entry))
        ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
        paragraphs = []
        for paragraph in document.findall(".//w:p", ns):
            parts = []
            for node in paragraph.iter():
                if node.tag == "{" + ns["w"] + "}t":
                    parts.append(node.text or "")
                elif node.tag in {
                    "{" + ns["w"] + "}tab",
                    "{" + ns["w"] + "}br",
                    "{" + ns["w"] + "}cr",
                }:
                    parts.append(" ")
            paragraphs.append("".join(parts))
        return source_text("\n".join(paragraphs)), ""
    if body.startswith(b"%PDF-"):
        reader = PdfReader(BytesIO(body))
        return (
            source_text(
                well_formed("\n".join(page.extract_text() or "" for page in reader.pages))
            ),
            "",
        )
    if "html" in content_type or body.lstrip().lower().startswith(
        (b"<!doctype html", b"<html")
    ):
        return html_text(body)
    # Jina responds as UTF-8 plain text. JSON/XML sources remain literal text.
    return source_text(body.decode("utf-8-sig", errors="replace")), ""


def html_text(body: bytes, visible_only: bool = False) -> tuple[str, str]:
    """Literal HTML view; optional visible-only filtering never expands attributes."""
    soup = BeautifulSoup(decoded_response(body), "html.parser")
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    for string in list(soup.find_all(string=lambda value: BOUNDARY in value)):
        # Same node type: a comment or doctype stays excluded from the text.
        string.replace_with(type(string)(source_text(str(string))))
    if visible_only:
        for node in list(soup.find_all(True)):
            if node.attrs is None:
                continue
            classes = set(node.get("class", []))
            style = re.sub(r"\s+", "", node.get("style", "").casefold())
            if (
                node.has_attr("hidden")
                or classes.intersection(
                    {
                        "sr-only",
                        "visually-hidden",
                        "visuallyhidden",
                        "screen-reader-only",
                        "screen-reader-text",
                    }
                )
                or re.search(
                    r"(?:^|;)(?:display:none|visibility:hidden)(?:!important)?(?:;|$)",
                    style,
                )
            ):
                node.decompose()
        # aria-hidden hides text from accessibility APIs, not from the printed view.

    for node in soup(["script", "style", "template"]):
        node.decompose()
    # Preserve adjacency across inline markup, including links followed by punctuation.
    # Block boundaries still need separators so independent paragraphs cannot become one word.
    for node in soup.find_all(
        (
            "address",
            "article",
            "aside",
            "blockquote",
            "br",
            "dd",
            "div",
            "dl",
            "dt",
            "footer",
            "h1",
            "h2",
            "h3",
            "h4",
            "h5",
            "h6",
            "header",
            "hr",
            "li",
            "main",
            "nav",
            "ol",
            "p",
            "section",
            "table",
            "td",
            "th",
            "tr",
            "ul",
        )
    ):
        node.insert_before("\n" + BOUNDARY)
        node.insert_after(BOUNDARY + "\n")
    return soup.get_text(), title


def content_problem(body: bytes, text: str, title: str, final_url: str) -> str | None:
    """Reject common error, authentication and challenge responses, including HTTP 200s."""
    body = decoded_response(body)
    if urlsplit(final_url).path.endswith("/mp/wappoc_appmsgcaptcha"):
        return "login_or_challenge"
    if BAD_PATH.search(urlsplit(final_url).path):
        return "login_or_challenge"
    if title.strip().casefold() in {"browser validation page", "challenge validation"}:
        return "error_or_challenge"
    if BLOCKED_TITLE.match(title.strip()) or ERROR_TITLE_SUFFIX.search(title.strip()):
        return "error_or_challenge"
    if title.strip().casefold() == "wayback machine" and len(text) < 1500:
        return "archive_wrapper"
    if urlsplit(final_url).hostname == "web.archive.org" and b"wm-ipp-base" in body:
        archived = BeautifulSoup(body, "html.parser")
        toolbar = archived.select_one("#wm-ipp-base")
        if toolbar is not None:
            for node in archived.select("#wm-ipp-base, #wm-ipp-print"):
                node.decompose()
            for node in archived(["script", "style", "template"]):
                node.decompose()
            if not normalise((archived.body or archived).get_text()):
                return "archive_wrapper"
    if "r.jina.ai" in urlsplit(final_url).netloc:
        rendered = text.partition("Markdown Content:")[2].strip()
        if "Markdown Content:" in text and not rendered:
            return "empty_render"
    # A complete settings panel can be long enough to look like an article.
    # Require both panel boundaries and repeated cookie-table fields; ordinary
    # articles with a banner or a settings link do not satisfy this conjunction.
    visible = (
        text.partition("Markdown Content:")[2] if "Markdown Content:" in text else text
    )
    panel = normalise(visible).casefold().strip(" *#-\n")
    banner_text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", panel)
    if banner_text == (
        "this website uses cookies to ensure you get the best experience on the website. "
        "if you continue to browse, then you agree to our cookie policy and privacy policy."
    ):
        return "cookie_settings_shell"
    if (
        len(panel) < 500
        and panel.startswith("环境异常")
        and "当前环境异常，完成验证后即可继续访问。" in panel
        and "去验证" in panel
    ):
        return "error_or_challenge"
    if (
        panel.startswith("advanced cookie settings")
        and panel.endswith("setting cookies in the browser")
        and panel.count("due date") >= 3
        and panel.count("domain") >= 3
        and panel.count("company") >= 3
        and "strictly necessary cookies" in panel
        and "performance cookies" in panel
    ):
        return "cookie_settings_shell"
    start = text[:2500].casefold()
    if "using security service for protection against online attacks." in start and any(
        word in start for word in ("cookie", "redirect")
    ):
        return "error_or_challenge"
    if any(
        marker in start
        for marker in (
            "browser validation page",
            "the service requires full cookie support to view the website.",
            "credentials_cookie_auth",
            "require_login",
            "checking your browser before accessing",
            "enable javascript and cookies to continue",
            "verify you are human",
            "verify you're human",
            "please complete the security check",
            "automated access to this page is forbidden",
            "warning: target url returned error",
            # F5 web application firewall rejection page.
            "the requested url was rejected. please consult with your administrator.",
            "warning: this page maybe requiring captcha",
        )
    ):
        return "error_or_challenge"
    if "r.jina.ai" in urlsplit(final_url).netloc and any(
        marker in start
        for marker in (
            '"status":401',
            '"status":403',
            '"status":404',
            '"status":422',
            "securitycompromiseerror",
            "assertionfailureerror",
            "couldn't resolve host name",
        )
    ):
        return "error_or_challenge"
    if re.search(rb'<app-root\b|id\s*=\s*["\'](?:root|app|__next)["\']', body, re.I):
        app = BeautifulSoup(body, "html.parser")
        for node in app.select("head, script, style, template"):
            node.decompose()
        if not normalise(app.get_text()):
            return "javascript_shell"
    if b"_df_book" in body and b"source=" in body:
        viewer = BeautifulSoup(
            re.sub(rb"<%--.*?--%>", b"", body, flags=re.S), "html.parser"
        )
        if viewer.select_one("._df_book[source]"):
            for node in viewer.select("head, script, style, template"):
                node.decompose()
            if not normalise(viewer.get_text()):
                return "document_viewer"
    if not text.strip():
        return "no_extractable_text"
    lines = [line.strip(" *#_>") for line in visible.splitlines() if line.strip(" *#_>")]
    if (
        "r.jina.ai" in urlsplit(final_url).netloc
        and lines
        and LOADING_LINE.fullmatch(lines[-1])
    ):
        # The render stopped at the application's loading indicator, before its content.
        # Live HTML is exempt: a spinner hidden by a stylesheet still extracts as text.
        return "javascript_shell"
    if len(normalise(text)) < 200 and any(
        marker in normalise(text).casefold()
        for marker in (
            "enable javascript",
            "javascript is required",
            "включите javascript",
        )
    ):
        return "javascript_shell"
    return None


def atomic_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    temp.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    temp.replace(path)


def snapshot_index(
    directory: Path, cited: dict[str, str] | None = None
) -> dict[str, tuple[dict, Path]]:
    """Current private snapshot per requested URL, keyed like cache entries.

    A copy whose hash the dataset cites for its source (cited: source ID to snapshot.sha256)
    is preferred, so a later failed or rejected retrieval cannot displace it. Otherwise the
    most recent retrieval of a URL is current, compared as instants; on a tie the row read
    later wins (manifests append rows; manifests are read in source ID order).
    """
    cited = cited or {}
    current: dict[str, tuple[dict, Path, tuple[bool, datetime]]] = {}
    root = directory.resolve()
    for manifest in sorted(directory.glob("*/manifest.json")):
        for row in json.loads(manifest.read_text(encoding="utf-8")):
            path = (directory / row["path"]).resolve()
            if not path.is_relative_to(root) or not path.is_file():
                continue
            key = hashlib.sha256(row["requested_url"].encode()).hexdigest()
            rank = (
                cited.get(row.get("source_id")) == row["sha256"],
                datetime.fromisoformat(row["retrieved"]),
            )
            if key not in current or rank >= current[key][2]:
                current[key] = (row, path, rank)
    return {key: (row, path) for key, (row, path, _) in current.items()}


def seed_from_snapshot(folder: Path, row: dict, path: Path) -> None:
    """A cache entry from a snapshot copy; the first fetch re-extracts and re-checks it."""
    body = path.read_bytes()
    # Built beside the entry and renamed into place, so an interrupted seed leaves no half entry.
    temp = folder.with_name(folder.name + "." + uuid.uuid4().hex + ".tmp")
    temp.mkdir(parents=True)
    (temp / "response.bin").write_bytes(body)
    # No extraction_version: fetch() then extracts text and checks the bytes against sha256.
    atomic_json(
        temp / "metadata.json",
        {
            "cache_version": 1,
            "url": row["requested_url"],
            "final_url": row["final_url"],
            "method": row.get("cache_method", row["method"]),
            "retrieved": row["retrieved"],
            "content_type": row["mime"],
            "sha256": row["sha256"],
            "bytes": len(body),
            "status": "ok",
            "seeded_from": "snapshot",
        },
    )
    temp.rename(folder)


class Fetcher:
    """Cache responses by URL; retry transient failures online with per-host pacing."""

    def __init__(
        self,
        cache: Path,
        delay: float = 1.0,
        timeout: float = 25,
        offline: bool = False,
        seed: Path | None = None,
        snapshots: Path | None = None,
        cited: dict[str, str] | None = None,
    ):
        self.cache, self.delay, self.timeout, self.offline = (
            cache,
            delay,
            timeout,
            offline,
        )
        # Read-only source of cache entries: each is copied into the cache before first use.
        self.seed = seed
        # Read-only private snapshot copies (docs/source-snapshots.md), keyed like the cache.
        self.snapshots = snapshot_index(snapshots, cited) if snapshots else {}
        self.lock = threading.Lock()
        self.url_locks: dict[str, threading.Lock] = {}
        self.host_locks: dict[str, threading.Lock] = {}
        self.last_request: dict[str, float] = {}
        # One download per URL per instance, even if persisting its diagnostic fails.
        self.attempts: dict[str, dict] = {}
        self.alternative_views: dict[tuple[str, str], list[tuple[str, str]]] = {}
        # The OS trust store completes chains that servers send without their intermediate
        # certificate, as browsers do; Python's bundled CA file alone rejects about 60 sources.
        self.ssl_context = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)

    def entry(self, url: str) -> Path:
        key = hashlib.sha256(url.encode()).hexdigest()
        folder = self.cache / key
        if self.seed and not folder.exists() and (self.seed / key).is_dir():
            shutil.copytree(self.seed / key, folder)
        if not folder.exists() and key in self.snapshots:
            seed_from_snapshot(folder, *self.snapshots[key])
        return folder

    def fetch(self, url: str, method: str = "live") -> dict:
        with self.lock:
            lock = self.url_locks.setdefault(url, threading.Lock())
        with lock:
            folder = self.entry(url)
            meta_path = folder / "metadata.json"
            if meta_path.exists():
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                if meta.get("url") != url:
                    raise ValueError("Cache URL mismatch")
                raw = folder / "response.bin"
                text_path = folder / "text.txt"
                text_valid = text_path.exists() and hashlib.sha256(
                    text_path.read_bytes()
                ).hexdigest() == meta.get("text_sha256")
                transport_failure = meta.get("status") in {
                    "http_error",
                    "incomplete_response",
                    "too_large",
                    "unreachable",
                }
                # Older versions labelled this parser rejection as unreachable.
                if (
                    meta.get("error")
                    == "ValueError: ZIP response is not a Word document"
                ):
                    transport_failure = False
                if (
                    raw.exists()
                    and not transport_failure
                    and (
                        meta.get("extraction_version") != EXTRACTION_VERSION
                        or not text_valid
                    )
                ):
                    body = raw.read_bytes()
                    if hashlib.sha256(body).hexdigest() != meta.get("sha256"):
                        raise ValueError(f"Cache checksum mismatch for {url}")
                    try:
                        text, title = extract_text(body, meta.get("content_type", ""))
                        problem = content_problem(
                            body, text, title, meta.get("final_url", url)
                        )
                    except Exception as exc:  # noqa: BLE001 - parser failures remain source-specific
                        meta.update(
                            status="extraction_error",
                            error=f"{type(exc).__name__}: {exc}",
                        )
                    else:
                        try:
                            text_path.write_text(text, encoding="utf-8")
                        except OSError as exc:
                            meta.update(
                                status="storage_error",
                                error=f"{type(exc).__name__}: {exc}",
                            )
                        else:
                            meta.update(
                                title=title,
                                text_sha256=hashlib.sha256(
                                    text.encode("utf-8")
                                ).hexdigest(),
                                status=problem or "ok",
                            )
                            meta.pop("error", None)
                    meta["extraction_version"] = EXTRACTION_VERSION
                    try:
                        atomic_json(meta_path, meta)
                    except OSError as exc:
                        meta.update(
                            status="storage_error", error=f"{type(exc).__name__}: {exc}"
                        )
                if meta.get("status") == "ok" and (
                    not raw.exists()
                    or hashlib.sha256(raw.read_bytes()).hexdigest() != meta["sha256"]
                ):
                    raise ValueError(f"Cache checksum mismatch for {url}")
                retryable = transport_failure and (
                    meta.get("status") in {"unreachable", "incomplete_response"}
                    or (
                        meta.get("status") == "http_error"
                        and re.fullmatch(r"HTTP (?:429|5\d{2})", meta.get("error") or "")
                        is not None
                    )
                )
                if self.offline or not retryable:
                    return meta
            if self.offline:
                return {"url": url, "method": method, "status": "not_cached"}
            if url in self.attempts:
                return dict(self.attempts[url])
            scheme = urlsplit(url).scheme
            if scheme not in {"http", "https"}:
                return {"url": url, "method": method, "status": "unsupported_url"}
            host = urlsplit(url).netloc
            with self.lock:
                host_lock = self.host_locks.setdefault(host, threading.Lock())
            # Reserve the request start, not the entire response lifetime. Global workers
            # bound in-flight requests while this lock spaces starts for each host.
            with host_lock:
                time.sleep(
                    max(
                        0,
                        self.delay
                        - (time.monotonic() - self.last_request.get(host, 0)),
                    )
                )
                self.last_request[host] = time.monotonic()
            meta = self._download(url, method, folder)
            try:
                atomic_json(meta_path, meta)
            except OSError as exc:
                meta.update(
                    status="storage_error", error=f"{type(exc).__name__}: {exc}"
                )
            self.attempts[url] = dict(meta)
            return meta

    def _download(self, url: str, method: str, folder: Path) -> dict:
        meta: dict = {
            "cache_version": 1,
            "extraction_version": EXTRACTION_VERSION,
            "url": url,
            "method": method,
            "retrieved": datetime.now(UTC)
            .isoformat(timespec="seconds")
            .replace("+00:00", "Z"),
        }
        try:
            req = Request(
                url,
                headers={
                    "User-Agent": "AISI-tracker-evidence/1.0",
                    "Accept-Encoding": "identity",
                },
            )
            with urlopen(
                req, timeout=self.timeout, context=self.ssl_context
            ) as response:
                meta.update(
                    final_url=response.geturl(),
                    content_type=response.headers.get("Content-Type", ""),
                )
                raw_length = response.headers.get("Content-Length", "")
                expected = int(raw_length) if raw_length.isdigit() else None
                if expected is not None and expected > MAX_BYTES:
                    return {**meta, "status": "too_large"}
                body = response.read(MAX_BYTES + 1)
                if len(body) > MAX_BYTES:
                    return {**meta, "status": "too_large"}
                if expected is not None and len(body) != expected:
                    return {**meta, "status": "incomplete_response"}
        except HTTPError as exc:
            return {**meta, "status": "http_error", "error": f"HTTP {exc.code}"}
        except IncompleteRead as exc:
            return {
                **meta,
                "status": "incomplete_response",
                "error": f"{type(exc).__name__}: {exc}",
            }
        except (URLError, TimeoutError, OSError, ValueError, HTTPException) as exc:
            return {
                **meta,
                "status": "unreachable",
                "error": f"{type(exc).__name__}: {exc}",
            }
        meta.update(sha256=hashlib.sha256(body).hexdigest(), bytes=len(body))
        try:
            folder.mkdir(parents=True, exist_ok=True)
            (folder / "response.bin").write_bytes(body)
        except OSError as exc:
            return {
                **meta,
                "status": "storage_error",
                "error": f"{type(exc).__name__}: {exc}",
            }
        try:
            text, title = extract_text(body, meta["content_type"])
            problem = content_problem(body, text, title, meta["final_url"])
        except Exception as exc:  # noqa: BLE001 - parser failures remain source-specific
            return {
                **meta,
                "status": "extraction_error",
                "error": f"{type(exc).__name__}: {exc}",
            }
        try:
            (folder / "text.txt").write_text(text, encoding="utf-8")
        except OSError as exc:
            return {
                **meta,
                "status": "storage_error",
                "error": f"{type(exc).__name__}: {exc}",
            }
        meta.update(
            title=title,
            text_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
            status=problem or "ok",
        )
        return meta

    def text(self, meta: dict) -> str:
        return (self.entry(meta["url"]) / "text.txt").read_text(encoding="utf-8")

    def html_views(self, meta: dict) -> list[tuple[str, str]]:
        """Independent view excluding explicit visually hidden accessibility content."""
        if meta.get("status") != "ok":
            return []
        body = (self.entry(meta["url"]) / "response.bin").read_bytes()
        if "html" not in meta.get(
            "content_type", ""
        ) and not body.lstrip().lower().startswith((b"<!doctype html", b"<html")):
            return []
        if hashlib.sha256(body).hexdigest() != meta["sha256"]:
            raise ValueError("HTML view checksum mismatch")
        return [("html-visible", html_text(body, visible_only=True)[0])]

    def pdf_views(self, meta: dict) -> list[tuple[str, str]]:
        """Independent literal extraction views of verified PDF bytes, never joined."""
        if meta.get("status") not in {"ok", "no_extractable_text", "extraction_error"}:
            return []
        key = (meta["url"], meta["sha256"])
        if key in self.alternative_views:
            return self.alternative_views[key]
        raw = self.entry(meta["url"]) / "response.bin"
        body = raw.read_bytes()
        if not body.startswith(b"%PDF-"):
            return []
        if hashlib.sha256(body).hexdigest() != meta["sha256"]:
            raise ValueError("PDF view checksum mismatch")
        views = pdf_text_views(body, raw)
        self.alternative_views[key] = views
        return views


def pdf_text_views(body: bytes, path: Path) -> list[tuple[str, str]]:
    """Independent PDF text views (pdftotext, pypdf layout) of bytes held at path."""
    views: list[tuple[str, str]] = []
    if not body.startswith(b"%PDF-"):
        return views
    if shutil.which("pdftotext"):
        try:
            result = subprocess.run(
                ["pdftotext", str(path), "-"],
                capture_output=True,
                timeout=30,
                check=True,
            )
            views.append(("pdftotext", source_text(result.stdout.decode("utf-8"))))
        except (OSError, subprocess.SubprocessError, UnicodeError):
            pass  # Optional extractor failure does not invalidate the primary view.
    try:
        reader = PdfReader(BytesIO(body))
        views.append(
            (
                "pypdf-layout",
                source_text(
                    well_formed(
                        "\n".join(
                            page.extract_text(extraction_mode="layout") or ""
                            for page in reader.pages
                        )
                    )
                ),
            )
        )
    except Exception:  # noqa: BLE001, S110 - optional layout view may fail independently
        pass  # Parser-specific layout failures retain the existing extraction result.
    return views


def candidates(source: dict, excerpt_url: str | None = None):
    """Each declared copy once, then the owner's single rendered-text fallback."""
    seen = set()
    for url, method in (
        (
            excerpt_url,
            "archive"
            if excerpt_url
            and any(
                h in excerpt_url
                for h in ("web.archive.org/", "archive.ph/", "archive.today/")
            )
            else "live",
        ),
        (source["url"], "live"),
        (source.get("archive_url"), "archive"),
        ("https://r.jina.ai/" + source["url"], "render"),
    ):
        if url and url not in seen:
            seen.add(url)
            yield url, method


def verify_source(
    source: dict,
    excerpts: list[tuple[int, dict]],
    fetcher: Fetcher,
    fetch_unused: bool = False,
) -> dict:
    rows, attempts = [], {}
    for index, excerpt in excerpts:
        available = []
        excerpt_attempts = []
        match = None
        matched_view = "default"
        hidden_match = False
        for url, method in candidates(source, excerpt.get("url")):
            meta = fetcher.fetch(url, method)
            attempts[url] = meta
            excerpt_attempts.append(
                {
                    "url": url,
                    "status": meta["status"],
                    **({"error": meta["error"]} if meta.get("error") else {}),
                }
            )
            if meta["status"] in {"no_extractable_text", "extraction_error"}:
                views = fetcher.pdf_views(meta)
                if any(normalise(text) for _, text in views):
                    available.append(meta)
                for view_name, view_text in views:
                    if literal_match(excerpt["text"], view_text):
                        match, matched_view = meta, view_name
                        break
                if match:
                    break
                continue
            if meta["status"] != "ok":
                continue
            available.append(meta)
            html_views = fetcher.html_views(meta)
            for view_name, view_text in html_views:
                if literal_match(excerpt["text"], view_text):
                    match, matched_view = meta, view_name
                    break
            if match:
                break
            if literal_match(excerpt["text"], fetcher.text(meta)):
                match = meta
                hidden_match = bool(html_views)
                break
            for view_name, view_text in fetcher.pdf_views(meta):
                if literal_match(excerpt["text"], view_text):
                    match, matched_view = meta, view_name
                    break
            if match:
                break
        chosen = match or (available[0] if available else None)
        failed_statuses = {a["status"] for a in excerpt_attempts}
        if "storage_error" in failed_statuses:
            failure = "storage_error"
        elif "extraction_error" in failed_statuses:
            failure = "extraction_error"
        elif "no_extractable_text" in failed_statuses:
            failure = "unextractable"
        elif failed_statuses & {
            "login_or_challenge",
            "error_or_challenge",
            "archive_wrapper",
            "empty_render",
            "cookie_settings_shell",
            "javascript_shell",
            "document_viewer",
            "too_large",
            "unsupported_url",
        }:
            failure = "rejected_content"
        elif failed_statuses & {"http_error", "incomplete_response", "unreachable"}:
            failure = "unreachable"
        else:
            failure = "not_cached"
        render_only = bool(
            match
            and match.get("method") == "render"
            and excerpt.get("url") != match["url"]
        )
        other_copy = bool(
            match and excerpt.get("url") and match["url"] != excerpt["url"]
        )
        row = {
            "source_id": source["id"],
            "excerpt_id": excerpt_identity(excerpt),
            "excerpt_index": index,
            "text": excerpt["text"],
            "supports": excerpt["supports"],
            "status": "matched_hidden_text"
            if hidden_match
            else "matched_render_only"
            if render_only
            else "matched_other_copy"
            if other_copy
            else "matched"
            if match
            else "mismatch"
            if available
            else failure,
            "attempts": excerpt_attempts,
        }
        if excerpt.get("url"):
            row["declared_excerpt_url"] = excerpt["url"]
        if match:
            row["extraction_view"] = matched_view
            row["match_flags"] = (
                (["matched_hidden_text"] if hidden_match else [])
                + (["matched_render_only"] if render_only else [])
                + (["matched_other_copy"] if other_copy else [])
            )
        if chosen:
            row.update(
                evidence_url=chosen["url"],
                evidence_origin=chosen["method"],
                retrieved=chosen["retrieved"],
                sha256=chosen["sha256"],
            )
        rows.append(row)
    if fetch_unused or not excerpts:
        for url, method in candidates(source):
            meta = fetcher.fetch(url, method)
            attempts[url] = meta
            if meta["status"] == "ok":
                break
    return {
        "source_id": source["id"],
        "excerpts": rows,
        "attempts": list(attempts.values()),
    }


def write_report(path: Path, results: list[dict], data_path: Path) -> dict:
    rows = [row for result in results for row in result["excerpts"]]
    counts = dict(Counter(row["status"] for row in rows))
    report = {
        "extraction_version": EXTRACTION_VERSION,
        "pdftotext_available": bool(shutil.which("pdftotext")),
        "data_sha256": hashlib.sha256(data_path.read_bytes()).hexdigest(),
        "counts": counts,
        "sources": results,
    }
    atomic_json(path, report)
    md = [
        "# Excerpt verification",
        "",
        f"Extraction version: {EXTRACTION_VERSION}; pdftotext available: {report['pdftotext_available']}. Counts: {json.dumps(counts, sort_keys=True)}.",
        "",
        "Unicode composition, whitespace and quotation marks are normalised. A literal match checks occurrence, not whether the quotation supports the claim. PDF extraction artefacts and editorial omissions need manual review.",
        "",
        "| Source | Excerpt | Result | Evidence / attempt reasons |",
        "| --- | --- | --- | --- |",
    ]
    for row in rows:
        if row["status"] != "matched":
            reasons = "; ".join(
                f"{a['url']}: {a['status']}"
                + (f" ({a['error']})" if a.get("error") else "")
                for a in row.get("attempts", [])
            )
            evidence = (row.get("evidence_url") or "No matching text") + (
                "; " + reasons if reasons else ""
            )
            evidence = evidence.replace("|", "&#124;").replace("\n", " ")
            md.append(
                f"| {row['source_id']} | {row['excerpt_id']} | {row['status']} | {evidence} |"
            )
    path.with_suffix(".md").write_text("\n".join(md) + "\n", encoding="utf-8")
    return counts


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--data", type=Path, default=ROOT / "data/aisi.json")
    ap.add_argument("--cache", type=Path, default=ROOT / "work/source-cache")
    ap.add_argument(
        "--report", type=Path, default=ROOT / "work/reports/excerpt-verification.json"
    )
    ap.add_argument(
        "--all-excerpts",
        action="store_true",
        help="also check excerpts outside decisive claims",
    )
    ap.add_argument(
        "--all-sources",
        action="store_true",
        help="cache sources with no selected excerpts as well",
    )
    ap.add_argument(
        "--seed-cache",
        type=Path,
        help="read-only cache: entries are copied into --cache before use, so this directory is never written",
    )
    ap.add_argument(
        "--seed-snapshots",
        type=Path,
        help="read-only private snapshot directory (docs/source-snapshots.md): a URL missing from --cache and --seed-cache is seeded from its current snapshot",
    )
    ap.add_argument("--source", action="append", help="source ID, repeatable")
    ap.add_argument(
        "--offline",
        action="store_true",
        help="read cache only; never access the network",
    )
    ap.add_argument(
        "--delay",
        type=float,
        default=1.0,
        help="minimum seconds between requests to one host",
    )
    ap.add_argument(
        "--timeout", type=float, default=25, help="timeout per HTTP request in seconds"
    )
    ap.add_argument(
        "--workers",
        type=int,
        default=4,
        help="concurrent sources, with host pacing still applied",
    )
    args = ap.parse_args()
    if args.delay < 0 or args.timeout <= 0 or args.workers < 1:
        ap.error("delay must be non-negative; timeout and workers must be positive")
    authored = json.loads(args.data.read_text(encoding="utf-8"))
    data = publish(authored)
    required = required_excerpt_pointers(data, authored=authored)
    # The copy each source cites is the one its quotations were checked against.
    cited = {
        source["id"]: source["snapshot"]["sha256"]
        for source in data["sources"]
        if source.get("snapshot", {}).get("sha256")
    }
    fetcher = Fetcher(
        args.cache,
        args.delay,
        args.timeout,
        args.offline,
        args.seed_cache,
        args.seed_snapshots,
        cited,
    )
    selected = []
    for source in data["sources"]:
        if args.source and source["id"] not in args.source:
            continue
        excerpts = [
            (n, e)
            for n, e in enumerate(source.get("excerpts", []))
            if args.all_excerpts or required.intersection(e["supports"])
        ]
        if excerpts or args.all_sources:
            selected.append((source, excerpts))
    if args.source and set(args.source) - {s["id"] for s in data["sources"]}:
        ap.error("unknown source ID")
    results = []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        jobs = {
            pool.submit(verify_source, s, ex, fetcher, args.all_sources): s["id"]
            for s, ex in selected
        }
        for future in as_completed(jobs):
            result = future.result()
            results.append(result)
            source_counts = Counter(row["status"] for row in result["excerpts"])
            print(
                f"{len(results)}/{len(selected)} {result['source_id']}: {dict(source_counts)}",
                flush=True,
            )
            # Partial reports survive interruption; the URL cache resumes successful requests.
            atomic_json(
                args.report,
                {
                    "partial": True,
                    "extraction_version": EXTRACTION_VERSION,
                    "pdftotext_available": bool(shutil.which("pdftotext")),
                    "sources": results,
                },
            )
    results.sort(key=lambda r: r["source_id"])
    counts = write_report(args.report, results, args.data)
    print(json.dumps(counts, sort_keys=True), flush=True)
    if any(count for status, count in counts.items() if status != "matched"):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
