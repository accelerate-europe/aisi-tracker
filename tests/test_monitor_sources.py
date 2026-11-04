"""Change monitor: classification against a cited copy, without network access."""

import hashlib
import json
from datetime import date

import pytest

from scripts import monitor_sources as monitor
from scripts import snapshot_sources, verify_excerpts


@pytest.fixture(autouse=True)
def no_requests(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Unexpected network request")

    monkeypatch.setattr(verify_excerpts, "urlopen", forbidden)


URL = "https://example.gov/news"


def page(body: str) -> bytes:
    return f"<!doctype html><html><body><nav>Menu</nav><main>{body}</main></body></html>".encode()


OLD = page(
    "<p>The institute was founded in 2024 with a budget of 10 million euro.</p>"
    "<p>It evaluates frontier models for national security risks and publishes reports.</p>"
    "<p>Its staff work with partners across government, industry and academia on testing "
    "methods, shared benchmarks and joint evaluations of advanced systems.</p>"
    "<p>Annual reports describe completed evaluations, published research papers and the "
    "international agreements the institute has signed with partner organisations.</p>"
)


def setup(
    tmp_path, new_body: bytes | None, status="ok", error=None, cited=OLD, method="live", excerpts=True
):
    store = tmp_path / "store"
    digest = hashlib.sha256(cited).hexdigest()
    rel = f"src/2026-10-17T00-00-00Z-{digest[:16]}.html"
    (store / "src").mkdir(parents=True)
    (store / rel).write_bytes(cited)
    (store / "src/manifest.json").write_text(
        json.dumps(
            [
                {
                    "source_id": "src",
                    "requested_url": URL if method == "live" else "https://r.jina.ai/" + URL,
                    "final_url": URL,
                    "method": method,
                    "retrieved": "2026-10-17T00:00:00Z",
                    "mime": "text/html",
                    "sha256": digest,
                    "path": rel,
                }
            ]
        )
    )
    source = {
        "id": "src",
        "url": URL,
        "title": "News",
        "excerpts": [] if not excerpts else [
            {"text": "founded in 2024", "supports": [["jurisdictions", {"id": "XX"}, "timeline", {"id": "xx-1"}, "description"]]},
            {"text": "not on the page", "supports": [["institutes", {"id": "xx-aisi"}, "name"]]},
        ],
        "snapshot": {
            "retrieved": "2026-10-17T00:00:00Z",
            "sha256": digest,
            **snapshot_sources.text_fields(cited, "text/html"),
        },
    }
    fetcher = verify_excerpts.Fetcher(tmp_path / "cache")
    folder = fetcher.entry(URL)
    folder.mkdir(parents=True)
    meta = {"url": URL, "final_url": URL, "status": status, "content_type": "text/html"}
    if error:
        meta["error"] = error
    if new_body is not None:
        (folder / "response.bin").write_bytes(new_body)
        try:
            text, title = verify_excerpts.extract_text(new_body, "text/html")
        except Exception:  # noqa: BLE001 - an unreadable fixture keeps no text
            text, title = "", ""
        (folder / "text.txt").write_text(text)
        meta.update(
            sha256=hashlib.sha256(new_body).hexdigest(),
            text_sha256=hashlib.sha256(text.encode()).hexdigest(),
            extraction_version=verify_excerpts.EXTRACTION_VERSION,
            title=title,
        )
    (folder / "metadata.json").write_text(json.dumps(meta))
    fetcher.offline = True
    return monitor.compare(source, fetcher, store, tmp_path / "fallback")


def test_same_file_is_unchanged(tmp_path):
    assert setup(tmp_path, OLD)["class"] == "unchanged"


def test_furniture_change_is_unchanged(tmp_path):
    new = OLD.replace(b"<nav>Menu</nav>", b"<nav>Other menu, cookie token 8f3a</nav>")
    row = setup(tmp_path, new)
    assert (row["class"], row["reason"]) == ("unchanged", "same_text")


def test_small_edit_keeping_quotes_is_trivial(tmp_path):
    new = OLD.replace(b"publishes reports.", b"publishes reports. Updated 23 October.")
    row = setup(tmp_path, new)
    assert (row["class"], row["reason"]) == ("trivial", "quotes_kept")
    found = {q["text"]: (q["in_cited_copy"], q["in_new_copy"]) for q in row["quotes"]}
    # A quotation the cited copy never carried decides nothing.
    assert found == {"founded in 2024": (True, True), "not on the page": (False, False)}


def test_lost_quote_is_substantive_and_names_claim(tmp_path):
    row = setup(tmp_path, OLD.replace(b"founded in 2024", b"founded in 2025"))
    assert (row["class"], row["reason"]) == ("substantive", "quote_lost")
    lost = [q for q in row["quotes"] if q["in_cited_copy"] and not q["in_new_copy"]]
    assert lost[0]["supports"] == ["jurisdictions › XX › timeline › xx-1 › description"]


ARTICLE = " ".join(f"word{i}" for i in range(120))


def test_rotating_sidebar_without_quotes_is_trivial(tmp_path):
    cited = page(f"<p>{ARTICLE}</p><div>Read next: {' '.join(['old'] * 3 + [f'a{i}' for i in range(200)])}</div>")
    new = page(f"<p>{ARTICLE}</p><div>Read next: {' '.join(f'b{i}' for i in range(200))}</div>")
    row = setup(tmp_path, new, cited=cited, excerpts=False)
    assert (row["class"], row["reason"]) == ("trivial", "small_change")
    assert row["text_change"]["removed"] > 0.5 and row["text_change"]["kept_run"] >= 120


def test_rewritten_page_without_quotes_is_substantive(tmp_path):
    cited = page(f"<p>{ARTICLE}</p>")
    new = page("<p>" + " ".join(f"word{i}" if i % 20 else "changed" for i in range(120)) + "</p>")
    row = setup(tmp_path, new, cited=cited, excerpts=False)
    assert (row["class"], row["reason"]) == ("substantive", "text_changed")


def test_quotes_kept_outweigh_page_rewrite(tmp_path):
    new = page("<p>The institute was founded in 2024.</p><p>Its director announced a merger with the cyber agency.</p>")
    assert setup(tmp_path, new)["reason"] == "quotes_kept"


def test_unrecognised_shell_is_blocked(tmp_path):
    row = setup(tmp_path, page("<p>Bot Detection</p>"))
    assert (row["class"], row["reason"]) == ("blocked", "near_empty_response")


def test_other_format_cited_copy_needs_a_lost_quote(tmp_path):
    cited = page(f"<p>{ARTICLE}</p>")
    new = page("<p>" + " ".join(f"other{i}" for i in range(120)) + "</p>")
    row = setup(tmp_path, new, cited=cited, method="render", excerpts=False)
    assert row["class"] == "trivial" and not row["like_for_like"]


@pytest.mark.parametrize(
    "status,error,expected",
    [
        ("http_error", "HTTP 404", "gone"),
        ("http_error", "HTTP 410", "gone"),
        ("unreachable", "URLError: <urlopen error [Errno 8] nodename nor servname provided, or not known>", "gone"),
        ("http_error", "HTTP 403", "blocked"),
        ("http_error", "HTTP 429", "blocked"),
        ("login_or_challenge", None, "blocked"),
        ("javascript_shell", None, "blocked"),
        ("http_error", "HTTP 503", "failed"),
        ("unreachable", "TimeoutError: timed out", "failed"),
    ],
)
def test_fetch_failures(tmp_path, status, error, expected):
    assert setup(tmp_path, None, status=status, error=error)["class"] == expected


def test_missing_cited_copy_fails(tmp_path):
    fetcher = verify_excerpts.Fetcher(tmp_path / "cache", offline=True)
    source = {"id": "src", "url": URL, "snapshot": {"sha256": "0" * 64, "retrieved": "x"}}
    row = monitor.compare(source, fetcher, tmp_path / "store", tmp_path / "fallback")
    assert row["class"] == "failed"


def test_due_checks_and_discovery():
    data = {
        "jurisdictions": [
            {"id": "AA", "name": "A", "kind": "state", "institute_id": "aa", "next_check": {"date": "2026-10-01", "reason": "Vote."}},
            {"id": "BB", "name": "B", "kind": "state", "next_check": {"date": "2026-11-01", "reason": "Budget."},
             "check": {"searches": [{"date": "2026-10-17", "query": "b ai institute", "language": "en"}]}},
            {"id": "CC", "name": "C", "kind": "state", "next_check": {"date": "2027-03-01", "reason": "Later."},
             "check": {"searches": [{"date": "2026-09-01", "query": "c ai institute", "language": "en"}]}},
        ]
    }
    due = monitor.due_checks(data, date(2026, 10, 24), horizon=14)
    assert [(r["id"], r["due"]) for r in due] == [("AA", True), ("BB", False)]
    assert [r["id"] for r in monitor.discovery_queue(data)] == ["CC", "BB"]


def test_citing_records():
    data = {
        "jurisdictions": [{"id": "XX", "timeline": [{"id": "xx-1", "source_ids": ["s1"]}], "gdp": {"source_ids": ["s2"]}}],
        "institutes": [{"id": "xx-aisi", "source_ids": ["s1"]}],
    }
    assert monitor.citing_records(data) == {
        "s1": ["institutes › xx-aisi", "jurisdictions › XX › timeline › xx-1"],
        "s2": ["jurisdictions › XX › gdp"],
    }


def test_seen_leads_ledger(tmp_path):
    path = tmp_path / "leads.jsonl"
    url = "https://example.gov/lead"
    assert monitor.lead_status(monitor.ledger_rows(path), url, "h1") == {"status": "new"}
    monitor.record_lead(path, {"url": url, "text_sha256": "h1", "date": "2026-10-24", "verdict": "irrelevant", "reason": "Sports news."})
    rows = monitor.ledger_rows(path)
    assert monitor.lead_status(rows, url, "h1")["status"] == "skip"
    assert monitor.lead_status(rows, url, "h2")["status"] == "changed"
    # An unreadable page has no hash and is never skipped.
    assert monitor.lead_status(rows, url, None)["status"] == "changed"
    with pytest.raises(ValueError):
        monitor.record_lead(path, {"url": url, "verdict": "duplicate", "reason": ""})
    with pytest.raises(ValueError):
        monitor.record_lead(path, {"url": url, "verdict": "maybe", "reason": ""})


def test_quote_lost_from_furniture_is_not_unchanged(tmp_path):
    cited = OLD.replace(b"<nav>Menu</nav>", b"<aside>founded in 2024</aside>").replace(b"founded in 2024 with", b"set up with")
    new = cited.replace(b"<aside>founded in 2024</aside>", b"<aside>Other</aside>")
    assert snapshot_sources.text_fields(cited, "text/html") == snapshot_sources.text_fields(new, "text/html")
    row = setup(tmp_path, new, cited=cited)
    assert (row["class"], row["reason"]) == ("substantive", "quote_lost")


def test_unreadable_new_copy_fails(tmp_path):
    row = setup(tmp_path, b"%PDF-1.7 broken", status="extraction_error")
    assert row["class"] == "failed"


def test_unspaced_script_page_is_not_a_shell(tmp_path):
    cited = page("<p>" + "人工智能安全研究所负责评估前沿模型的风险" * 20 + "</p>")
    new = page("<p>" + "国家网络安全中心发布新的人工智能评估报告" * 20 + "</p>")
    row = setup(tmp_path, new, cited=cited, excerpts=False)
    assert row["class"] != "blocked"


def test_kept_run_needs_contiguity():
    old = " ".join(f"w{i}" for i in range(100))
    chunks = [old.split()[i : i + 5] for i in range(0, 100, 5)]
    shuffled = " ".join(" ".join(c + ["x"]) for c in reversed(chunks))
    assert monitor.text_change(old, shuffled)["kept_run"] == 5
    assert monitor.text_change(old, "intro " + old + " outro")["kept_run"] == 100


def test_failed_text_comparison_without_quotes_fails(tmp_path, monkeypatch):
    def broken(*args, **kwargs):
        raise ValueError("parser")

    monkeypatch.setattr(monitor, "main_text", broken)
    row = setup(tmp_path, OLD.replace(b"reports.", b"reports today."), excerpts=False)
    assert row["class"] == "failed"
