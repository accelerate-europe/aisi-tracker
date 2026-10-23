"""Offline verifier checks with hand-written excerpts and generated binary fixtures."""

import hashlib
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from zipfile import ZipFile

import pytest
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from scripts import verify_excerpts as verify


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Unexpected network request")

    monkeypatch.setattr(verify, "urlopen", forbidden)


def cached(
    fetcher,
    url,
    body,
    *,
    mime="text/html",
    status="ok",
    version=0,
    title="",
    text=None,
    method="live",
):
    folder = fetcher.entry(url)
    folder.mkdir(parents=True)
    (folder / "response.bin").write_bytes(body)
    if text is not None:
        (folder / "text.txt").write_text(text)
    meta = {
        "url": url,
        "final_url": url,
        "method": method,
        "status": status,
        "extraction_version": version,
        "sha256": hashlib.sha256(body).hexdigest(),
        "content_type": mime,
        "retrieved": "2026-10-17T15:00:00Z",
        "bytes": len(body),
        "title": title,
    }
    verify.atomic_json(folder / "metadata.json", meta)
    return meta


def test_normalisation_is_literal():
    assert verify.normalise("  Cafe\u0301\n “AI”\tteam’s ") == 'Café "AI" team\'s'
    assert verify.normalise("AI—safety") != verify.normalise("AI-safety")
    assert verify.normalise("AI safety") != verify.normalise("AIsafety")
    assert verify.normalise("AI") != verify.normalise("ai")
    assert verify.normalise("３１") != verify.normalise("31")
    assert verify.normalise("31 人") != verify.normalise("31人")
    assert verify.normalise("a … c") != verify.normalise("a b c")


@pytest.mark.parametrize(
    ("joined", "wrapped"),
    [
        # jp-aisi-deck-jp: the saved PDF splits these two partner names.
        ("国家サイバー統括室", "国 家サイバー統括室"),
        ("科学技術・イノベーション推進事務局", "科学技術・イノベーション 推進事務局"),
        ("安全について", "安全に\nついて"),
        ("人工智能安全", "人工智\n能安 全"),
    ],
)
def test_literal_match_ignores_cjk_layout_spaces(joined, wrapped):
    assert verify.literal_match(joined, wrapped)
    assert verify.literal_match(wrapped, joined)
    # Snapshot text hashes must retain their existing whitespace semantics.
    assert verify.normalise(joined) != verify.normalise(wrapped)


@pytest.mark.parametrize(
    ("quote", "text"),
    [
        ("国家サイバー統括室", "国家安全保障局"),
        ("AI Safety", "AISafety"),
        ("AI安全", "AI 安全"),
        ("31人", "31 人"),
        ("31", "31 000人"),
        ("인공지능 안전", "인공지능안전"),
        ("安全・評価", "安全 ・評価"),
    ],
)
def test_cjk_matching_preserves_wording_and_other_boundaries(quote, text):
    assert not verify.literal_match(quote, text)


def test_html_inline_text_no_abbreviation_expansion_or_hidden_scripts():
    body = b'<html><head><title>Official report</title></head><body><p>AI <abbr title="Artificial Intelligence Security Institute">AISI</abbr> (<span>AISI</span>), <a href="/report">available here</a>, reports.</p><p>Next paragraph.</p><script>False quotation</script><style>False style</style><template>False template</template></body></html>'
    text, title = verify.extract_text(body, "text/html")
    assert title == "Official report"
    assert (
        "AI AISI (AISI), available here, reports. Next paragraph."
        in verify.normalise(text)
    )
    assert "Artificial Intelligence Security Institute" not in text
    assert "False" not in text
    assert "reports.Next" not in text


def test_docx_preserves_run_adjacency_and_explicit_separators():
    output = BytesIO()
    xml = b'<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>AI-</w:t></w:r><w:r><w:t>veiligheid</w:t><w:tab/><w:t>institute</w:t><w:br/><w:t>report</w:t></w:r></w:p><w:p><w:r><w:t>Next paragraph.</w:t></w:r></w:p></w:body></w:document>'
    with ZipFile(output, "w") as archive:
        archive.writestr("word/document.xml", xml)
    text, title = verify.extract_text(output.getvalue(), "application/octet-stream")
    assert verify.normalise(text) == "AI-veiligheid institute report Next paragraph."
    assert title == ""
    other = BytesIO()
    with ZipFile(other, "w") as archive:
        archive.writestr("other.txt", "not a Word document")
    with pytest.raises(ValueError, match="not a Word"):
        verify.extract_text(other.getvalue(), "application/octet-stream")


def test_pdf_extracts_actual_text():
    writer = PdfWriter()
    page = writer.add_blank_page(300, 300)
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    page[NameObject("/Resources")] = DictionaryObject(
        {NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})}
    )
    stream = DecodedStreamObject()
    stream.set_data(b"BT /F1 12 Tf 20 200 Td (Institute staff: 31) Tj ET")
    page[NameObject("/Contents")] = stream
    output = BytesIO()
    writer.write(output)
    text, title = verify.extract_text(output.getvalue(), "application/pdf")
    assert "Institute staff: 31" in text
    assert title == ""


@pytest.mark.parametrize(
    "body,text,title,url,expected",
    [
        (b"", "", "", "https://example.org/", "no_extractable_text"),
        (
            b"denied",
            "denied",
            "Access denied",
            "https://example.org/",
            "error_or_challenge",
        ),
        (b"login", "login", "", "https://example.org/signin", "login_or_challenge"),
        (
            b"wrapper",
            "Wayback Machine 1 capture",
            "Wayback Machine",
            "https://web.archive.org/web/20261017000000/https://example.org/a.pdf",
            "archive_wrapper",
        ),
        (
            b"empty",
            "Title: \nURL Source: https://example.org\nMarkdown Content:\n",
            "",
            "https://r.jina.ai/https://example.org",
            "empty_render",
        ),
        (
            b"captcha",
            "Checking your browser before accessing",
            "",
            "https://example.org",
            "error_or_challenge",
        ),
        (
            b'<div id="root"></div>',
            "App",
            "",
            "https://example.org",
            "javascript_shell",
        ),
        (
            b"warning",
            "Warning: Target URL returned error 403",
            "",
            "https://r.jina.ai/https://example.org",
            "error_or_challenge",
        ),
        # Site error page served with HTTP 200 (def.finanze.it, 19 Oct 2026).
        (
            b"<title>Documentazione Economica e Finanziaria - Errore</title>",
            "Siamo spiacenti Si è verificato un errore: errore sconosciuto",
            "Documentazione Economica e Finanziaria - Errore",
            "https://def.finanze.it/DocTribFrontend/getAttoNormativoDetail.do",
            "error_or_challenge",
        ),
        # Web application firewall rejection (dekamer.be, 19 Oct 2026).
        (
            b"<title>Request Rejected</title>",
            "Request Rejected Your support ID is: 1",
            "Request Rejected",
            "https://www.dekamer.be/doc/CCRI/html/56/ic465x.html",
            "error_or_challenge",
        ),
        (
            b"rejected",
            "The requested URL was rejected. Please consult with your administrator. Your support ID is: 1",
            "",
            "https://r.jina.ai/https://www.dekamer.be/doc/CCRI/html/56/ic465x.html",
            "error_or_challenge",
        ),
        # Render that ends at the application's loading indicator (Eurostat, 19 Oct 2026).
        (
            b"loading",
            "Title: [tps00001] Population on 1 January\nMarkdown Content:\n* [Skip to Content](#)\n"
            + "An official website of the European Union\n" * 10
            + "Close\n\nLoading...\n",
            "",
            "https://r.jina.ai/https://ec.europa.eu/eurostat/databrowser/view/tps00001/default/table",
            "javascript_shell",
        ),
    ],
)
def test_rejects_unusable_responses(body, text, title, url, expected):
    assert verify.content_problem(body, text, title, url) == expected


def test_offline_missing_cache_and_cached_failures_do_not_request(tmp_path):
    fetcher = verify.Fetcher(tmp_path, offline=True)
    assert fetcher.fetch("https://example.org/missing")["status"] == "not_cached"
    url = "https://example.org/failed"
    verify.atomic_json(
        fetcher.entry(url) / "metadata.json",
        {"url": url, "method": "live", "status": "http_error", "error": "HTTP 403"},
    )
    assert fetcher.fetch(url)["status"] == "http_error"


def test_cache_reextracts_old_or_corrupt_text_without_refetch(tmp_path):
    fetcher = verify.Fetcher(tmp_path, offline=True)
    url = "https://example.org/report"
    raw = b"<html><p>Correct source wording.</p></html>"
    cached(fetcher, url, raw, text="Invented quotation")
    meta = fetcher.fetch(url)
    assert meta["extraction_version"] == verify.EXTRACTION_VERSION
    assert "Correct source wording." in fetcher.text(meta)
    assert "Invented" not in fetcher.text(meta)
    (fetcher.entry(url) / "text.txt").write_text("Tampered text")
    meta = fetcher.fetch(url)
    assert "Tampered" not in fetcher.text(meta)
    assert meta["sha256"] == hashlib.sha256(raw).hexdigest()


def test_seed_cache_is_read_but_never_written(tmp_path):
    seed = tmp_path / "seed"
    url = "https://example.org/report"
    cached(verify.Fetcher(seed), url, b"<html><p>Correct source wording.</p></html>", text="Stale")
    before = {p: p.read_bytes() for p in seed.rglob("*") if p.is_file()}
    fetcher = verify.Fetcher(tmp_path / "scratch", offline=True, seed=seed)
    meta = fetcher.fetch(url)
    assert "Correct source wording." in fetcher.text(meta)
    assert {p: p.read_bytes() for p in seed.rglob("*") if p.is_file()} == before
    assert fetcher.fetch("https://example.org/absent")["status"] == "not_cached"


def test_cache_never_promotes_incomplete_transport_response(tmp_path):
    fetcher = verify.Fetcher(tmp_path, offline=True)
    url = "https://example.org/report"
    cached(
        fetcher,
        url,
        b"<p>Partial but plausible source</p>",
        status="incomplete_response",
        text="Partial but plausible source",
    )
    assert fetcher.fetch(url)["status"] == "incomplete_response"


def test_corrupted_raw_cache_is_rejected(tmp_path):
    fetcher = verify.Fetcher(tmp_path, offline=True)
    url = "https://example.org/report"
    cached(fetcher, url, b"<p>Real source</p>")
    (fetcher.entry(url) / "response.bin").write_bytes(b"<p>Edited source</p>")
    with pytest.raises(ValueError, match="checksum"):
        fetcher.fetch(url)


def test_fallback_order_stops_at_first_literal_match(tmp_path):
    fetcher = verify.Fetcher(tmp_path, offline=True)
    source = {
        "id": "sample",
        "url": "https://example.org/report",
        "archive_url": "https://web.archive.org/web/20261017000000/https://example.org/report",
    }
    cached(fetcher, source["url"], b"<p>Other text.</p>")
    cached(
        fetcher,
        source["archive_url"],
        b"<p>The institute has 31 staff.</p>",
        method="archive",
    )
    excerpt = {
        "text": "The institute has 31 staff.",
        "supports": ["/institutes/0/staff/0/value"],
    }
    result = verify.verify_source(source, [(0, excerpt)], fetcher)
    assert result["excerpts"][0]["status"] == "matched"
    assert result["excerpts"][0]["evidence_origin"] == "archive"
    assert [a["url"] for a in result["attempts"]] == [
        source["url"],
        source["archive_url"],
    ]
    assert len(list(verify.candidates(source, source["url"]))) == 3


def test_verifier_labels_excerpt_by_quote_identity_after_reordering(tmp_path):
    fetcher = verify.Fetcher(tmp_path, offline=True)
    source = {"id": "sample", "url": "https://example.org/report"}
    cached(fetcher, source["url"], b"<p>The institute has 31 staff.</p>")
    quote = {"text": "The institute has 31 staff.", "supports": ["/institutes/0/staff/0/total"]}
    before = verify.verify_source(source, [(0, quote)], fetcher)["excerpts"][0]
    after = verify.verify_source(source, [(9, quote)], fetcher)["excerpts"][0]
    expected = "sha256:" + hashlib.sha256(quote["text"].encode()).hexdigest()
    assert before["excerpt_id"] == after["excerpt_id"] == expected
    assert before["excerpt_index"] != after["excerpt_index"]


def test_archive_wrapper_does_not_match_its_url_text(tmp_path):
    fetcher = verify.Fetcher(tmp_path, offline=True)
    source = {
        "id": "sample",
        "url": "https://example.org/report",
        "archive_url": "https://web.archive.org/web/20261017000000/https://example.org/report",
    }
    cached(
        fetcher,
        source["archive_url"],
        b"<html><title>Wayback Machine</title><p>Institute has 31 staff.</p></html>",
    )
    result = verify.verify_source(
        source, [(0, {"text": "Institute has 31 staff.", "supports": []})], fetcher
    )
    assert result["excerpts"][0]["status"] == "rejected_content"


class Response:
    def __init__(self, body, length=None):
        self.body = body
        self.headers = {
            "Content-Type": "text/html",
            "Content-Length": str(len(body) if length is None else length),
        }

    def geturl(self):
        return "https://example.org/report"

    def read(self, size):
        return self.body[:size]

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


def test_download_detects_partial_body_and_retries_once_per_fetcher_instance(
    tmp_path, monkeypatch
):
    calls = []

    def opening(request, timeout):
        calls.append(request.full_url)
        assert request.get_header("Accept-encoding") == "identity"
        return Response(b"partial", length=100)

    monkeypatch.setattr(verify, "urlopen", opening)
    fetcher = verify.Fetcher(tmp_path, delay=0)
    url = "https://example.org/report"
    assert fetcher.fetch(url)["status"] == "incomplete_response"
    assert fetcher.fetch(url)["status"] == "incomplete_response"
    assert calls == [url]
    assert verify.Fetcher(tmp_path, delay=0).fetch(url)["status"] == "incomplete_response"
    assert calls == [url, url]
    assert not (fetcher.entry(url) / "response.bin").exists()


@pytest.mark.parametrize(
    "failure", ["429", "500", "503", "599", "unreachable", "incomplete_response"]
)
def test_cached_transient_failure_recovers_online_and_reuses_success(
    tmp_path, monkeypatch, failure
):
    url = "https://example.org/report"
    calls = []

    def opening(request, timeout):
        calls.append(request.full_url)
        if len(calls) == 1:
            if failure.isdigit():
                raise verify.HTTPError(url, int(failure), "fixture", None, None)
            if failure == "unreachable":
                raise verify.URLError("temporary outage")
            return Response(b"partial", length=100)
        return Response(b"<html><p>Recovered research report.</p></html>")

    monkeypatch.setattr(verify, "urlopen", opening)
    fetcher = verify.Fetcher(tmp_path, delay=0)
    failed = fetcher.fetch(url)
    assert failed["status"] == ("http_error" if failure.isdigit() else failure)
    assert fetcher.fetch(url) == failed
    assert calls == [url]
    meta_path = fetcher.entry(url) / "metadata.json"
    diagnostic = meta_path.read_bytes()
    assert verify.Fetcher(tmp_path, offline=True).fetch(url) == failed
    assert meta_path.read_bytes() == diagnostic
    assert calls == [url]
    # A later process must recover too; a failed request is not an immutable response.
    online = verify.Fetcher(tmp_path, delay=0)
    recovered = online.fetch(url)
    assert recovered["status"] == "ok"
    assert "Recovered research report." in online.text(recovered)
    assert online.fetch(url) == recovered
    assert calls == [url, url]


@pytest.mark.parametrize(
    "status,error",
    [
        ("http_error", "HTTP 404"),
        ("http_error", "HTTP 403"),
        ("http_error", "HTTP 400"),
        ("http_error", None),
        ("too_large", ""),
        ("error_or_challenge", ""),
        ("javascript_shell", ""),
        ("extraction_error", "ValueError: invalid document"),
        ("unreachable", "ValueError: ZIP response is not a Word document"),
    ],
)
def test_online_cache_does_not_retry_permanent_failures(tmp_path, status, error):
    fetcher = verify.Fetcher(tmp_path, delay=0)
    url = "https://example.org/failed"
    meta = {"url": url, "method": "live", "status": status, "error": error}
    verify.atomic_json(fetcher.entry(url) / "metadata.json", meta)
    assert fetcher.fetch(url) == meta


def test_failed_cache_write_does_not_repeat_download_in_same_instance(
    tmp_path, monkeypatch
):
    calls = []

    def opening(request, timeout):
        calls.append(request.full_url)
        raise verify.URLError("temporary outage")

    def unwritable(*args, **kwargs):
        raise OSError("cache is read-only")

    monkeypatch.setattr(verify, "urlopen", opening)
    monkeypatch.setattr(verify, "atomic_json", unwritable)
    fetcher = verify.Fetcher(tmp_path, delay=0)
    url = "https://example.org/report"
    failed = fetcher.fetch(url)
    assert failed["status"] == "storage_error"
    assert fetcher.fetch(url) == failed
    assert calls == [url]


def test_same_host_requests_are_paced_and_same_url_is_downloaded_once(
    tmp_path, monkeypatch
):
    clock = [100.0]
    sleeps, starts = [], []
    monkeypatch.setattr(verify.time, "monotonic", lambda: clock[0])

    def sleep(seconds):
        sleeps.append(seconds)
        clock[0] += seconds

    monkeypatch.setattr(verify.time, "sleep", sleep)
    fetcher = verify.Fetcher(tmp_path, delay=2)

    def download(url, method, folder):
        starts.append((url, clock[0]))
        return {"url": url, "method": method, "status": "http_error"}

    monkeypatch.setattr(fetcher, "_download", download)
    with ThreadPoolExecutor(max_workers=3) as pool:
        list(
            pool.map(
                fetcher.fetch,
                [
                    "https://example.org/a",
                    "https://example.org/a",
                    "https://example.org/b",
                ],
            )
        )
    assert len(starts) == 2
    assert starts[1][1] - starts[0][1] >= 2


def test_report_records_input_hash_and_mismatches(tmp_path):
    data = tmp_path / "data.json"
    data.write_text('{"sources": []}')
    output = tmp_path / "report.json"
    rows = [
        {
            "source_id": "sample",
            "excerpts": [
                {"source_id": "sample", "excerpt_id": "quote-sample", "excerpt_index": 0, "status": "mismatch"}
            ],
            "attempts": [],
        }
    ]
    counts = verify.write_report(output, rows, data)
    assert counts == {"mismatch": 1}
    assert (
        json.loads(output.read_text())["data_sha256"]
        == hashlib.sha256(data.read_bytes()).hexdigest()
    )
    assert "mismatch" in output.with_suffix(".md").read_text()


def test_same_host_response_wait_does_not_hold_start_lock(tmp_path, monkeypatch):
    from threading import Event

    first_started, second_started = Event(), Event()
    fetcher = verify.Fetcher(tmp_path, delay=0)

    def download(url, method, folder):
        if url.endswith("/first"):
            first_started.set()
            assert second_started.wait(2), (
                "Second request blocked by first response lifetime"
            )
        else:
            second_started.set()
        return {"url": url, "method": method, "status": "http_error"}

    monkeypatch.setattr(fetcher, "_download", download)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(fetcher.fetch, "https://example.org/first")
        assert first_started.wait(2)
        second = pool.submit(fetcher.fetch, "https://example.org/second")
        assert first.result()["status"] == "http_error"
        assert second.result()["status"] == "http_error"


@pytest.mark.parametrize(
    "text,title",
    [
        ("", "Browser Validation Page"),
        ("Browser Validation Page", ""),
        ("The service requires full cookie support to view the website.", ""),
        (
            "using security service for protection against online attacks. Enable cookies.",
            "",
        ),
        (
            "using security service for protection against online attacks. Redirecting now.",
            "",
        ),
    ],
)
def test_sgdsn_browser_validation_is_not_evidence(text, title):
    assert (
        verify.content_problem(
            b"<html>challenge</html>", text, title, "https://www.sgdsn.gouv.fr/page"
        )
        == "error_or_challenge"
    )


def test_security_service_discussion_alone_is_not_a_challenge():
    text = "Our report discusses using security service for protection against online attacks."
    assert (
        verify.content_problem(
            text.encode(), text, "Security report", "https://example.gov/report"
        )
        is None
    )


def test_cached_sgdsn_challenge_is_reclassified_offline(tmp_path):
    fetcher = verify.Fetcher(tmp_path, offline=True)
    url = "https://www.sgdsn.gouv.fr/page"
    cached(
        fetcher,
        url,
        b"<html><title>Browser Validation Page</title><p>The service requires full cookie support to view the website.</p></html>",
        version=3,
        text="Browser Validation Page",
    )
    assert fetcher.fetch(url)["status"] == "error_or_challenge"


def test_valid_textless_pdf_still_cannot_verify_a_quote(tmp_path):
    stream = BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    writer.write(stream)
    url = "https://example.gov/scanned.pdf"
    fetcher = verify.Fetcher(tmp_path, offline=True)
    cached(fetcher, url, stream.getvalue(), mime="application/pdf")
    excerpt = {"text": "Our institute published this report.", "supports": ["/test"]}
    result = verify.verify_source({"id": "scan", "url": url}, [(0, excerpt)], fetcher)
    assert result["excerpts"][0]["status"] == "unextractable"
    assert result["attempts"][0]["status"] == "no_extractable_text"


def test_pdf_alternative_view_recovers_visual_order_without_changing_quote(tmp_path):
    writer = PdfWriter()
    page = writer.add_blank_page(300, 300)
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    page[NameObject("/Resources")] = DictionaryObject(
        {NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})}
    )
    stream = DecodedStreamObject()
    stream.set_data(
        b"BT /F1 12 Tf 20 180 Td (Second sentence.) Tj ET BT /F1 12 Tf 20 200 Td (First sentence.) Tj ET"
    )
    page[NameObject("/Contents")] = stream
    output = BytesIO()
    writer.write(output)
    url = "https://example.org/authority.pdf"
    fetcher = verify.Fetcher(tmp_path, offline=True)
    cached(fetcher, url, output.getvalue(), mime="application/pdf")
    excerpt = {"text": "First sentence. Second sentence.", "supports": ["/test"]}
    result = verify.verify_source({"id": "law", "url": url}, [(0, excerpt)], fetcher)
    assert result["excerpts"][0]["status"] == "matched"
    assert result["excerpts"][0]["extraction_view"] in {"pdftotext", "pypdf-layout"}
    assert fetcher.text(fetcher.fetch(url)).index("Second") < fetcher.text(
        fetcher.fetch(url)
    ).index("First")


def test_pdf_views_cannot_be_concatenated_into_a_false_quote(tmp_path, monkeypatch):
    url = "https://example.org/report"
    fetcher = verify.Fetcher(tmp_path, offline=True)
    cached(fetcher, url, b"<html>First half</html>")
    monkeypatch.setattr(fetcher, "pdf_views", lambda meta: [("layout", "second half")])
    excerpt = {"text": "First half second half", "supports": ["/test"]}
    result = verify.verify_source({"id": "report", "url": url}, [(0, excerpt)], fetcher)
    assert result["excerpts"][0]["status"] == "mismatch"


def cookie_panel():
    return (
        "Advanced cookie settings\nStrictly necessary cookies\n"
        + "Cookies\nexample\nDue date\n365 days\nDomain\nexample.gov\nCompany\nGov\n"
        * 4
        + "Performance cookies\nSetting cookies in the browser\n* * *\n"
    )


def test_cookie_settings_only_render_is_rejected():
    text = (
        "Title: Ministry\nURL Source: https://example.gov/speech\nMarkdown Content:\n"
        + cookie_panel()
    )
    assert (
        verify.content_problem(
            text.encode(),
            text,
            "Ministry",
            "https://r.jina.ai/https://example.gov/speech",
        )
        == "cookie_settings_shell"
    )


@pytest.mark.parametrize(
    "text",
    [
        "Minister speech: We established a national institute.\n" + cookie_panel(),
        cookie_panel() + "\nMinister speech: We established a national institute.",
        "Advanced cookie settings. Accept or reject cookies.\nMinister speech: We established a national institute.\nSetting cookies in the browser",
    ],
)
def test_article_with_cookie_banner_or_settings_link_is_retained(text):
    assert (
        verify.content_problem(
            text.encode(), text, "Minister speech", "https://example.gov/speech"
        )
        is None
    )


def test_cookie_shell_cache_reclassified_on_version_change(tmp_path):
    fetcher = verify.Fetcher(tmp_path, offline=True)
    url = "https://r.jina.ai/https://example.gov/speech"
    text = "Markdown Content:\n" + cookie_panel()
    cached(fetcher, url, text.encode(), mime="text/plain", version=4, text=text)
    assert fetcher.fetch(url)["status"] == "cookie_settings_shell"


def workbook_bytes(
    part="xl/workbook.xml",
    xml=b'<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"/>',
):
    stream = BytesIO()
    with ZipFile(stream, "w") as archive:
        archive.writestr(part, xml)
    return stream.getvalue()


def test_xlsx_recognition_rejects_arbitrary_zip_and_wrong_xml():
    body = workbook_bytes()
    assert verify.is_xlsx(body)
    assert verify.extract_text(body, "application/octet-stream") == ("", "")
    assert not verify.is_xlsx(workbook_bytes("unrelated.txt"))
    assert not verify.is_xlsx(workbook_bytes(xml=b"<html/>"))
    with pytest.raises(ValueError):
        verify.extract_text(workbook_bytes("unrelated.txt"), "application/octet-stream")


@pytest.mark.parametrize("offline", [False, True])
def test_legacy_workbook_parser_failure_reextracts_without_network(tmp_path, offline):
    fetcher = verify.Fetcher(tmp_path, offline=offline)
    url = "https://example.gov/report.xlsx"
    old = cached(fetcher, url, workbook_bytes(), status="unreachable", version=5)
    old["error"] = "ValueError: ZIP response is not a Word document"
    verify.atomic_json(fetcher.entry(url) / "metadata.json", old)
    result = fetcher.fetch(url)
    assert result["status"] == "no_extractable_text"
    assert result["retrieved"] == old["retrieved"]
    assert result["sha256"] == old["sha256"]
    assert result["extraction_version"] == verify.EXTRACTION_VERSION


def test_declared_data_zip_requires_valid_crc_mime_and_bounded_size(monkeypatch):
    body = workbook_bytes("data.csv", b"a,b\n1,2\n")
    assert verify.is_data_zip(body, "application/zip; charset=binary")
    assert verify.extract_text(body, "application/zip") == ("", "")
    assert not verify.is_data_zip(body, "application/octet-stream")
    assert not verify.is_data_zip(body[:-10], "application/zip")
    corrupted = body.replace(b"a,b\n1,2\n", b"a,b\n9,2\n")
    assert not verify.is_data_zip(corrupted, "application/zip")
    monkeypatch.setattr(verify, "MAX_BYTES", 3)
    assert not verify.is_data_zip(body, "application/zip")


def test_wayback_toolbar_without_source_content_is_not_an_evidence_copy():
    url = "https://web.archive.org/web/20261017010712/https://example.org/source"
    shell = b'<html><body><div id="wm-ipp-base">1 capture About this capture TIMESTAMPS<script>x</script></div><div id="wm-ipp-print">The Wayback Machine</div><div id="app"></div><script>renderApp()</script></body></html>'
    text, title = verify.extract_text(shell, "text/html")
    assert verify.content_problem(shell, text, title, url) == "archive_wrapper"
    article = shell.replace(
        b'<div id="app"></div>',
        b"<article>A real short source document remains readable.</article>",
    )
    text, title = verify.extract_text(article, "text/html")
    assert verify.content_problem(article, text, title, url) is None
    assert (
        verify.content_problem(
            shell,
            *verify.extract_text(shell, "text/html"),
            "https://example.org/source",
        )
        != "archive_wrapper"
    )


def test_html_visible_view_preserves_printed_law_not_spoken_expansion(tmp_path):
    url = "https://example.gov/law"
    body = '<html><p>gemäß <span class="sr-only">Artikel 51, </span><span aria-hidden="true">Art. 51</span> DSGVO; <span class="visually-hidden">Paragraph 36, Absatz eins,</span><span aria-hidden="true">§ 36 Abs. 1</span>.</p><span hidden>Hidden assertion</span><span style="display: none">Other hidden assertion</span></html>'.encode()
    fetcher = verify.Fetcher(tmp_path, offline=True)
    cached(fetcher, url, body)
    excerpt = {"text": "gemäß Art. 51 DSGVO; § 36 Abs. 1.", "supports": ["/law"]}
    result = verify.verify_source({"id": "law", "url": url}, [(0, excerpt)], fetcher)
    assert result["excerpts"][0]["status"] == "matched"
    assert result["excerpts"][0]["extraction_view"] == "html-visible"
    visible = fetcher.html_views(fetcher.fetch(url))[0][1]
    assert "Hidden assertion" not in visible and "Other hidden assertion" not in visible
    assert "Artikel" not in visible
    assert "Artikel" in fetcher.text(fetcher.fetch(url))


def test_html_views_are_not_stitched(tmp_path, monkeypatch):
    url = "https://example.org/report"
    fetcher = verify.Fetcher(tmp_path, offline=True)
    cached(fetcher, url, b"<html>First half</html>")
    monkeypatch.setattr(
        fetcher, "html_views", lambda meta: [("html-visible", "second half")]
    )
    excerpt = {"text": "First half second half", "supports": []}
    assert (
        verify.verify_source({"id": "report", "url": url}, [(0, excerpt)], fetcher)[
            "excerpts"
        ][0]["status"]
        == "mismatch"
    )


def test_declared_excerpt_url_fallback_is_flagged(tmp_path):
    source = {"id": "report", "url": "https://example.org/list"}
    fetcher = verify.Fetcher(tmp_path, offline=True)
    item = "https://example.org/item"
    cached(fetcher, item, b"<p>Different publication</p>")
    cached(fetcher, source["url"], b"<p>Report published 15 April 2026</p>")
    excerpt = {"text": "Report published 15 April 2026", "url": item, "supports": []}
    row = verify.verify_source(source, [(0, excerpt)], fetcher)["excerpts"][0]
    assert row["status"] == "matched_other_copy"
    assert row["declared_excerpt_url"] == item
    assert row["evidence_url"] == source["url"]
    excerpt["url"] = source["url"]
    assert (
        verify.verify_source(source, [(0, excerpt)], fetcher)["excerpts"][0]["status"]
        == "matched"
    )


@pytest.mark.parametrize(
    ("quote", "text"),
    [
        ("うち専門家は90", "うち専門家は900人"),
        ("30名から陣容", "130名から陣容"),
        ("２００", "１２００人"),
        ("２００", "２０００人"),
        ("٣٠", "١٣٠"),
    ],
)
def test_numeric_quote_edges_cannot_truncate_unicode_digits(quote, text):
    assert not verify.literal_match(quote, text)
    assert verify.literal_match(quote, f"{text}; ({quote})")


def test_download_parser_error_is_not_transport_failure(tmp_path, monkeypatch):
    body = workbook_bytes("not-word.txt", b"other archive")
    response = Response(body)
    response.headers["Content-Type"] = "application/octet-stream"
    monkeypatch.setattr(verify, "urlopen", lambda *args, **kwargs: response)
    fetcher = verify.Fetcher(tmp_path, delay=0)
    meta = fetcher._download(
        "https://example.org/report", "live", tmp_path / "download"
    )
    assert meta["status"] == "extraction_error"
    assert "not a Word document" in meta["error"]
    assert (tmp_path / "download/response.bin").read_bytes() == body


def test_download_local_storage_error_is_not_transport_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(
        verify, "urlopen", lambda *args, **kwargs: Response(b"<html>Report</html>")
    )
    blocked = tmp_path / "not-a-directory"
    blocked.write_text("existing")
    meta = verify.Fetcher(tmp_path)._download(
        "https://example.org/report", "live", blocked
    )
    assert meta["status"] == "storage_error"
    assert "sha256" in meta


def test_download_transport_error_stays_unreachable(tmp_path, monkeypatch):
    def unavailable(*args, **kwargs):
        raise verify.URLError("offline fixture")

    monkeypatch.setattr(verify, "urlopen", unavailable)
    meta = verify.Fetcher(tmp_path)._download(
        "https://example.org/report", "live", tmp_path / "download"
    )
    assert meta["status"] == "unreachable"
    assert "sha256" not in meta


def test_report_exposes_extraction_environment_and_attempt_failure(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(verify.shutil, "which", lambda name: None)
    data = tmp_path / "data.json"
    data.write_text("{}")
    report = tmp_path / "report.json"
    row = {
        "source_id": "scan",
        "excerpt_id": "quote-scan",
        "excerpt_index": 0,
        "status": "unextractable",
        "attempts": [
            {"url": "https://example.org/scan", "status": "no_extractable_text"}
        ],
    }
    verify.write_report(
        report, [{"source_id": "scan", "excerpts": [row], "attempts": []}], data
    )
    saved = json.loads(report.read_text())
    assert saved["extraction_version"] == verify.EXTRACTION_VERSION
    assert saved["pdftotext_available"] is False
    assert "no_extractable_text" in report.with_suffix(".md").read_text()
    assert "https://example.org/scan" in report.with_suffix(".md").read_text()


@pytest.mark.parametrize(
    "error, expected",
    [
        (verify.IncompleteRead(b"partial", 20), "incomplete_response"),
        (verify.HTTPException("bad HTTP response"), "unreachable"),
    ],
)
def test_http_protocol_failures_are_cached_without_aborting(
    tmp_path, monkeypatch, error, expected
):
    class BrokenResponse(Response):
        def read(self, size):
            raise error

    monkeypatch.setattr(verify, "urlopen", lambda *args, **kwargs: BrokenResponse(b""))
    fetcher = verify.Fetcher(tmp_path, delay=0)
    url = "https://example.org/broken"
    assert fetcher.fetch(url)["status"] == expected
    assert fetcher.fetch(url)["status"] == expected
    assert not (fetcher.entry(url) / "response.bin").exists()


@pytest.mark.parametrize("other_copy", [False, True])
def test_hidden_only_match_is_flagged_even_on_another_copy(tmp_path, other_copy):
    url = "https://example.org/law"
    fetcher = verify.Fetcher(tmp_path, offline=True)
    cached(
        fetcher,
        url,
        b'<html><p><span class="sr-only">Article 51 spoken text</span><span aria-hidden="true">Art. 51</span></p></html>',
    )
    excerpt = {
        "text": "Article 51 spoken text",
        "supports": [],
        "url": "https://example.org/missing" if other_copy else url,
    }
    row = verify.verify_source({"id": "law", "url": url}, [(0, excerpt)], fetcher)[
        "excerpts"
    ][0]
    assert row["status"] == "matched_hidden_text"
    assert "matched_hidden_text" in row["match_flags"]
    assert ("matched_other_copy" in row["match_flags"]) is other_copy
    assert row["extraction_view"] == "default"
    excerpt["text"] = "Art. 51"
    excerpt["url"] = url
    row = verify.verify_source({"id": "law", "url": url}, [(0, excerpt)], fetcher)[
        "excerpts"
    ][0]
    assert row["status"] == "matched"
    assert row["extraction_view"] == "html-visible"
    assert row["match_flags"] == []


@pytest.mark.parametrize(
    "quote,text",
    [
        ("200", "1,200"),
        ("60", "60.5"),
        ("000", "66,000,000"),
        ("٢٠٠", "١٬٢٠٠"),
        ("٦٠", "٦٠٫٥"),
        ("5", "60.5"),
    ],
)
def test_numeric_token_boundaries_include_decimal_and_group_separators(quote, text):
    assert not verify.literal_match(quote, text)
    assert verify.literal_match(quote, f"{text}; {quote} people")
    assert verify.literal_match(text, f"Total: {text}.")


def test_render_only_match_requires_explicit_excerpt_url_for_plain_match(tmp_path):
    url = "https://example.gov/report"
    rendered = "https://r.jina.ai/" + url
    fetcher = verify.Fetcher(tmp_path, offline=True)
    cached(
        fetcher,
        rendered,
        b"Published Time: 2026-07-07",
        mime="text/plain",
        method="render",
    )
    excerpt = {"text": "Published Time: 2026-07-07", "supports": []}
    row = verify.verify_source({"id": "report", "url": url}, [(0, excerpt)], fetcher)[
        "excerpts"
    ][0]
    assert row["status"] == "matched_render_only"
    assert "matched_render_only" in row["match_flags"]
    excerpt["url"] = rendered
    assert (
        verify.verify_source({"id": "report", "url": url}, [(0, excerpt)], fetcher)[
            "excerpts"
        ][0]["status"]
        == "matched"
    )


@pytest.mark.parametrize(
    ("quote", "html", "found"),
    [
        # sgaisi.sg date line: the date and the reading time are separate elements.
        ("June 26, 2025", "<div>June 26, 2025</div><div>0 min read</div>", True),
        ("January 19, 2026", "<div>January 19, 2026</div><div>18 min read</div>", True),
        ("January 19, 2026 18 min read", "<div>January 19, 2026</div><div>18 min read</div>", True),
        ("500", "<td>1</td><td>500</td>", True),
        ("June 26, 2025", "<div>June 26, 2025</div>0 min read", True),
        ("500", "1<p>500</p>", True),
        # Within one element numbers keep their boundaries.
        ("26", "<p>260 staff</p>", False),
        ("26", "<p>1</p><p>260</p>", False),
        ("500", "<p>2 500 000</p>", False),
        ("June 26, 2025", "<div>June 26, 2025 0 min read</div>", False),
        ("June 26, 2025", "<div>June 26, 2025<span>0</span> min read</div>", False),
    ],
)
def test_element_boundaries_end_numeric_tokens(quote, html, found):
    text, _ = verify.extract_text(f"<html><body>{html}</body></html>".encode(), "text/html")
    assert verify.literal_match(quote, text) is found


def test_line_breaks_outside_html_still_continue_numbers():
    # PDF and plain text carry no element boundaries: a wrapped grouped number stays one number.
    assert not verify.literal_match("June 26, 2025", "June 26, 2025\n0 min read")
    assert not verify.literal_match("500", "2 500\n000 EUR")


def test_paragraph_separators_in_the_source_are_not_element_boundaries():
    # Only html_text() marks element boundaries; a U+2029 in the source text is a line break.
    html = "<html><body><p>2\u2029500\u2029000 EUR</p></body></html>".encode()
    assert not verify.literal_match("500", verify.extract_text(html, "text/html")[0])
    assert not verify.literal_match("500", verify.html_text(html, visible_only=True)[0])
    # Comments stay out of every view, and of the snapshot text hash.
    commented = "<html><body><p>Public text.</p><!--Hidden quotation\u2029500--></body></html>".encode()
    assert not verify.literal_match("Hidden quotation", verify.extract_text(commented, "text/html")[0])
    assert not verify.literal_match("Hidden quotation", verify.html_text(commented, visible_only=True)[0])
    from scripts import snapshot_sources

    assert snapshot_sources.main_text(commented, "text/html") == "Public text."
    plain = "2\u2029500\u2029000 EUR".encode()
    assert not verify.literal_match("500", verify.extract_text(plain, "text/plain")[0])


def test_latin_ligatures_fold_on_both_sides():
    """catalogue-jp-11-0: the PDF title spells Officer with the single ffi ligature."""
    assert verify.literal_match("Chief AI Officer", "Chief AI O\ufb03cer guidebook")
    assert verify.literal_match("Chief AI O\ufb03cer", "Chief AI Officer guidebook")
    assert verify.literal_match("\ufb01nal \ufb02ow", "final flow")
    assert not verify.literal_match("Officer", "Offcer")
    assert verify.normalise("O\ufb03cer") == "Officer"


@pytest.mark.parametrize("separator", [" ", "\u00a0", "\u202f", "'", "’", "，", "．"])
def test_numeric_grouping_separators_fail_closed(separator):
    assert not verify.literal_match(
        f"500{separator}000 EUR", f"2{separator}500{separator}000 EUR"
    )
    assert not verify.literal_match("500", f"500{separator}000")
    assert verify.literal_match("500", "There are 500 people.")


@pytest.mark.parametrize(
    "status,row_status",
    [
        ("not_cached", "not_cached"),
        ("error_or_challenge", "rejected_content"),
        ("http_error", "unreachable"),
    ],
)
def test_unavailable_rows_distinguish_cache_rejection_and_transport(
    tmp_path, status, row_status
):
    url = "https://example.org/report"
    fetcher = verify.Fetcher(tmp_path, offline=True)
    if status != "not_cached":
        verify.atomic_json(
            fetcher.entry(url) / "metadata.json",
            {"url": url, "method": "live", "status": status},
        )
    row = verify.verify_source(
        {"id": "report", "url": url},
        [(0, {"text": "Actual quotation", "supports": []})],
        fetcher,
    )["excerpts"][0]
    assert row["status"] == row_status
    assert row["attempts"][0]["status"] == status


@pytest.mark.parametrize("parser_error", [False, True])
def test_textless_or_failed_primary_pdf_tries_independent_views_before_classification(
    tmp_path, monkeypatch, parser_error
):
    stream = BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    writer.write(stream)
    url = "https://example.org/report.pdf"
    fetcher = verify.Fetcher(tmp_path, offline=True)
    cached(fetcher, url, stream.getvalue(), mime="application/pdf")
    if parser_error:

        def broken_primary(*args, **kwargs):
            raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "font encoding")

        monkeypatch.setattr(verify, "extract_text", broken_primary)
    monkeypatch.setattr(verify.shutil, "which", lambda name: "/fixture/pdftotext")
    monkeypatch.setattr(
        verify.subprocess,
        "run",
        lambda *args, **kwargs: verify.subprocess.CompletedProcess(
            args, 0, stdout=b"Recovered literal report text"
        ),
    )
    source = {"id": "report", "url": url}
    excerpt = {"text": "Recovered literal report text", "supports": []}
    row = verify.verify_source(source, [(0, excerpt)], fetcher)["excerpts"][0]
    assert row["status"] == "matched" and row["extraction_view"] == "pdftotext"
    excerpt["text"] = "Absent quotation"
    assert (
        verify.verify_source(source, [(0, excerpt)], fetcher)["excerpts"][0]["status"]
        == "mismatch"
    )


def test_error_analysis_title_is_not_a_failure_page():
    assert (
        verify.content_problem(
            b"report",
            "Actual report",
            "Error analysis of model evaluations",
            "https://example.org/report",
        )
        is None
    )
    assert (
        verify.content_problem(
            b"error", "Unavailable", "Error 403", "https://example.org/report"
        )
        == "error_or_challenge"
    )


def test_cli_decisive_selection_source_filter_and_all_excerpts(tmp_path, monkeypatch):
    # This fixture tests legacy verification. Patch the module the verifier imported:
    # it is `published` or `scripts.published` depending on sys.path at import time.
    monkeypatch.setattr(sys.modules[verify.publish.__module__], "AUTHORING_STRICT", False)
    url = "https://example.org/report"
    cache = tmp_path / "cache"
    cached(verify.Fetcher(cache, offline=True), url, b"<html>Decisive quotation</html>")
    data = {
        "dataset": {"as_of": "2026-10-18"},
        "jurisdictions": [{"id": "GB", "status": "established", "timeline": [
            {"id": "act", "event": "designated", "date": "2024", "source_ids": []}
        ]}],
        "institutes": [],
        "other": "optional claim",
        "sources": [
            {
                "id": "selected",
                "url": url,
                "excerpts": [
                    {
                        "text": "Decisive quotation",
                        "supports": ["/jurisdictions/0/status"],
                    },
                    {"text": "Absent optional text", "supports": ["/other"]},
                ],
            },
            {
                "id": "excluded",
                "url": "https://example.org/other",
                "excerpts": [
                    {"text": "Absent text", "supports": ["/jurisdictions/0/status"]}
                ],
            },
        ],
    }
    data_path, report = tmp_path / "data.json", tmp_path / "report.json"
    data_path.write_text(json.dumps(data))
    args = [
        "verify_excerpts.py",
        "--data",
        str(data_path),
        "--cache",
        str(cache),
        "--report",
        str(report),
        "--offline",
        "--source",
        "selected",
    ]
    monkeypatch.setattr(sys, "argv", args)
    verify.main()
    assert json.loads(report.read_text())["counts"] == {"matched": 1}
    monkeypatch.setattr(sys, "argv", args + ["--all-excerpts"])
    with pytest.raises(SystemExit) as exc:
        verify.main()
    assert exc.value.code == 1
    assert json.loads(report.read_text())["counts"] == {"matched": 1, "mismatch": 1}
    monkeypatch.setattr(sys, "argv", args[:-1] + ["unknown"])
    with pytest.raises(SystemExit) as exc:
        verify.main()
    assert exc.value.code == 2


def test_wechat_challenge_redirect_is_rejected():
    assert (
        verify.content_problem(
            b"<html></html>",
            "",
            "",
            "https://mp.weixin.qq.com/mp/wappoc_appmsgcaptcha?target_url=x",
        )
        == "login_or_challenge"
    )


def test_short_chinese_verification_panel_is_rejected():
    text = "环境异常\n当前环境异常，完成验证后即可继续访问。\n去验证"
    assert (
        verify.content_problem(
            text.encode(),
            text,
            "",
            "https://r.jina.ai/https://mp.weixin.qq.com/s/example",
        )
        == "error_or_challenge"
    )


def test_article_mentioning_verification_is_retained():
    text = "Research on browser verification. 环境异常 当前环境异常，完成验证后即可继续访问。 去验证. This article explains access checks and their limitations."
    assert (
        verify.content_problem(
            text.encode(),
            text,
            "Research on browser verification",
            "https://example.org/article",
        )
        is None
    )


def test_environment_term_alone_is_not_a_challenge():
    text = "环境异常 can refer to abnormal operating environments in scientific experiments."
    assert (
        verify.content_problem(
            text.encode(), text, "Operating environments", "https://example.org/article"
        )
        is None
    )


@pytest.mark.parametrize(
    "root",
    [b'<div id="root"></div>', b'<div id="app"></div>', b"<app-root></app-root>"],
)
def test_empty_application_root_is_not_a_source(root):
    body = (
        b"<html><head><title>Agency title</title><script>SEO_BODY</script></head><body>"
        + root
        + b"</body></html>"
    )
    text, title = verify.extract_text(body, "text/html")
    assert (
        verify.content_problem(body, text, title, "https://example.org/article")
        == "javascript_shell"
    )


def test_gzip_shell_and_article_are_inspected_without_replacing_bytes():
    import gzip

    shell = gzip.compress(
        b'<html><head><title>Agency</title></head><body><div id="root"></div></body></html>'
    )
    digest = hashlib.sha256(shell).hexdigest()
    text, title = verify.extract_text(shell, "text/html")
    assert (
        verify.content_problem(shell, text, title, "https://example.org/article")
        == "javascript_shell"
    )
    assert hashlib.sha256(shell).hexdigest() == digest
    article = gzip.compress(
        b'<html><body><div id="root"></div><article>The institute has published its original research report. Methods describe controlled evaluation, limitations and evidence. Results compare performance across the published test conditions, with detailed findings and conclusions for readers to inspect.</article></body></html>'
    )
    text, title = verify.extract_text(article, "text/html")
    assert "original research report" in text
    # An empty app mount cannot erase substantive separately rendered article text.
    assert (
        verify.content_problem(article, text, title, "https://example.org/article")
        is None
    )


def test_gzip_decoding_is_bounded(monkeypatch):
    import gzip

    monkeypatch.setattr(verify, "MAX_BYTES", 8)
    with pytest.raises(ValueError, match="Decoded response exceeds"):
        verify.extract_text(gzip.compress(b"x" * 9), "text/html")


@pytest.mark.parametrize("damage", ["truncated", "corrupt"])
def test_invalid_gzip_is_a_source_specific_extraction_failure(tmp_path, damage):
    import gzip

    body = gzip.compress(b"<html><p>Original document text.</p></html>")
    body = body[:-5] if damage == "truncated" else body[:10] + b"\xff" + body[11:]
    with pytest.raises(ValueError, match="Invalid or incomplete gzip"):
        verify.decoded_response(body)
    fetcher = verify.Fetcher(tmp_path / "cache", offline=True)
    url = "https://example.org/damaged"
    cached(fetcher, url, body)
    result = fetcher.fetch(url)
    assert result["status"] == "extraction_error"
    assert "Invalid or incomplete gzip" in result["error"]


@pytest.mark.parametrize("mount", ["root", "app", "__next"])
def test_noscript_article_is_retained_with_empty_application_mount(mount):
    body = (
        f'<html><body><div id="{mount}"></div><noscript><article>'
        "The institute published its safety evaluation report."
        "</article></noscript></body></html>"
    ).encode()
    text, title = verify.extract_text(body, "text/html")
    assert "published its safety evaluation report" in text
    assert verify.content_problem(body, text, title, "https://example.org/report") is None


def test_noscript_javascript_instruction_is_not_an_article():
    body = (
        '<html><body><noscript><h1>Әділет</h1>'
        '<p>Для работы с поиском и текстами актов включите JavaScript.</p>'
        '</noscript><div id="root"></div></body></html>'
    ).encode()
    text, title = verify.extract_text(body, "text/html")
    assert (
        verify.content_problem(body, text, title, "https://example.org/law")
        == "javascript_shell"
    )


def test_empty_flipbook_is_not_the_linked_document():
    body = b'<html><head><title>Official action plan</title></head><body><div class="_df_book" source="/plan.pdf"></div><%-- viewer setup --%><script>DFLIP.defaults = {};</script></body></html>'
    text, title = verify.extract_text(body, "text/html")
    assert (
        verify.content_problem(body, text, title, "https://example.org/viewer")
        == "document_viewer"
    )
    substantive = body.replace(
        b"</body>",
        b"<article>This page includes an explanation of the plan alongside the viewer.</article></body>",
    )
    text, title = verify.extract_text(substantive, "text/html")
    assert (
        verify.content_problem(substantive, text, title, "https://example.org/viewer")
        is None
    )


def test_challenge_validation_title_is_rejected():
    body = b'<html><head><title>Challenge Validation</title></head><body><iframe title="Challenge Content"></iframe></body></html>'
    text, title = verify.extract_text(body, "text/html")
    assert (
        verify.content_problem(body, text, title, "https://example.gov/page")
        == "error_or_challenge"
    )


def test_banner_only_render_is_rejected_but_article_with_banner_is_retained():
    banner = "This website uses cookies to ensure you get the best experience on the website. If you continue to browse, then you agree to our [Cookie Policy](https://example.org/cookies) and [Privacy Policy](https://example.org/privacy)."
    text = "Title: Agency\nMarkdown Content:\n" + banner
    assert (
        verify.content_problem(
            text.encode(), text, "", "https://r.jina.ai/https://example.org/article"
        )
        == "cookie_settings_shell"
    )
    article = (
        text + "\nThe institute published a research report with methods and results."
    )
    assert (
        verify.content_problem(
            article.encode(),
            article,
            "",
            "https://r.jina.ai/https://example.org/article",
        )
        is None
    )


def test_archive_quotes_use_readme_and_referenced_literal_cells(monkeypatch):
    stream = BytesIO()
    ns = 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'
    with ZipFile(stream, 'w') as archive:
        archive.writestr('xl/workbook.xml', f'<workbook xmlns="{ns}" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="Visible" sheetId="1" r:id="rId1"/><sheet name="Hidden" sheetId="2" state="hidden" r:id="rId2"/></sheets></workbook>')
        archive.writestr('xl/_rels/workbook.xml.rels', '<Relationships><Relationship Id="rId1" Target="worksheets/sheet1.xml"/><Relationship Id="rId2" Target="worksheets/sheet2.xml"/></Relationships>')
        archive.writestr('xl/worksheets/sheet2.xml', f'<worksheet xmlns="{ns}"><c t="inlineStr"><is><t>Hidden false quote</t></is></c></worksheet>')
        archive.writestr('xl/sharedStrings.xml', f'<sst xmlns="{ns}"><si><r><t>AI-</t></r><r><t>Institute</t></r><rPh><t>Pronunciation</t></rPh></si><si><t>Unused false quote</t></si></sst>')
        archive.writestr('xl/worksheets/sheet1.xml', f'<worksheet xmlns="{ns}"><sheetData><row><c t="s"><v>0</v></c><c t="inlineStr"><is><t>Published 2026</t><rPh><t>Reading</t></rPh></is></c><c t="s"><f>FORMULA()</f><v>1</v></c></row></sheetData></worksheet>')
    body = stream.getvalue()
    assert verify.extract_text(body, 'application/octet-stream') == ('AI-Institute\nPublished 2026', '')
    monkeypatch.setattr(verify, 'MAX_BYTES', 500)
    with pytest.raises(ValueError, match='size limit'):
        verify.extract_text(body, 'application/octet-stream')


def test_zip_readme_is_literal_not_arbitrary_archive_content():
    stream = BytesIO()
    with ZipFile(stream, 'w') as archive:
        archive.writestr('dataset/readme.md', 'Prepared by Institute.\n公開日：2026-04-15')
        archive.writestr('dataset/data.csv', 'Unrelated quote')
    text, title = verify.extract_text(stream.getvalue(), 'application/zip')
    assert text == 'Prepared by Institute.\n公開日：2026-04-15'
    assert title == ''


def snapshot(directory, source_id, url, body, retrieved, name):
    """One private snapshot row as snapshot_sources.save_snapshot writes it."""
    (directory / source_id).mkdir(parents=True, exist_ok=True)
    (directory / source_id / name).write_bytes(body)
    manifest = directory / source_id / "manifest.json"
    rows = json.loads(manifest.read_text()) if manifest.exists() else []
    rows.append({"source_id": source_id, "requested_url": url, "final_url": url, "method": "live",
                 "retrieved": retrieved, "mime": "text/html", "sha256": hashlib.sha256(body).hexdigest(),
                 "path": f"{source_id}/{name}"})
    manifest.write_text(json.dumps(rows))


def test_snapshots_seed_a_fresh_cache_with_the_current_copy(tmp_path):
    snapshots = tmp_path / "snapshots"
    url = "https://example.org/report"
    # Appended out of order, and an offset that sorts wrongly as a string: the newest instant is current.
    snapshot(snapshots, "s", url, b"<html><p>Current source wording.</p></html>", "2026-02-01T00:00:00Z", "new.html")
    snapshot(snapshots, "s", url, b"<html><p>Old wording.</p></html>", "2026-02-01T00:30:00+01:00", "old.html")
    before = {p: p.read_bytes() for p in snapshots.rglob("*") if p.is_file()}
    fetcher = verify.Fetcher(tmp_path / "scratch", offline=True, snapshots=snapshots)
    meta = fetcher.fetch(url)
    assert meta["status"] == "ok" and meta["retrieved"] == "2026-02-01T00:00:00Z"
    assert "Current source wording." in fetcher.text(meta)
    source = {"id": "s", "url": url}
    row = verify.verify_source(source, [(0, {"text": "Current source wording.", "supports": ["/x"]})], fetcher)["excerpts"][0]
    assert row["status"] == "matched"
    assert {p: p.read_bytes() for p in snapshots.rglob("*") if p.is_file()} == before
    assert fetcher.fetch("https://example.org/absent")["status"] == "not_cached"


def test_snapshots_seed_the_cited_copy_before_a_later_rejected_one(tmp_path):
    """be-parliament-ai-designation-oct2026: a later 246-byte "Request Rejected" page displaced the cited copy."""
    snapshots = tmp_path / "snapshots"
    url = "https://example.org/answer"
    good = b"<html><p>The minister answered on formal designation.</p></html>"
    rejected = b"<html><head><title>Request Rejected</title></head><body>The requested URL was rejected.</body></html>"
    snapshot(snapshots, "s", url, good, "2026-10-17T14:26:27Z", "good.html")
    snapshot(snapshots, "s", url, rejected, "2026-10-19T00:07:03Z", "rejected.html")
    cited = {"s": hashlib.sha256(good).hexdigest()}
    fetcher = verify.Fetcher(tmp_path / "scratch", offline=True, snapshots=snapshots, cited=cited)
    meta = fetcher.fetch(url)
    assert meta["status"] == "ok" and meta["sha256"] == cited["s"]
    # Another source's citation of the same bytes does not select them for this one.
    other = verify.Fetcher(tmp_path / "other", offline=True, snapshots=snapshots, cited={"t": cited["s"]})
    assert other.fetch(url)["status"] == "error_or_challenge"


def test_snapshot_bytes_outside_the_directory_or_off_their_hash_are_refused(tmp_path):
    snapshots = tmp_path / "snapshots"
    url = "https://example.org/report"
    snapshot(snapshots, "s", url, b"<html><p>Recorded wording.</p></html>", "2026-01-01T00:00:00Z", "copy.html")
    snapshot(snapshots, "t", "https://example.org/outside", b"<html><p>Outside.</p></html>", "2026-01-01T00:00:00Z", "copy.html")
    manifest = snapshots / "t/manifest.json"
    manifest.write_text(manifest.read_text().replace('"t/copy.html"', '"../outside.html"'))
    (tmp_path / "outside.html").write_bytes(b"<html><p>Outside.</p></html>")
    (snapshots / "s/copy.html").write_bytes(b"<html><p>Tampered wording.</p></html>")
    fetcher = verify.Fetcher(tmp_path / "scratch", offline=True, snapshots=snapshots)
    assert fetcher.fetch("https://example.org/outside")["status"] == "not_cached"
    with pytest.raises(ValueError, match="checksum mismatch"):
        fetcher.fetch(url)


@pytest.mark.parametrize(
    "text,title",
    [
        # An earlier "Loading..." line followed by the loaded content.
        ("Markdown Content:\nLoading...\nPopulation on 1 January: 450 million", ""),
        # An article about errors, and a title naming an error without a site prefix.
        ("Annual report on error rates in public registers.", "Error rates in registers"),
        ("The minister said the error was corrected.", "Statistikamt - Fehler im Register"),
    ],
)
def test_loaded_pages_and_error_topics_are_evidence(text, title):
    assert (
        verify.content_problem(text.encode(), text, title, "https://example.gov/page")
        is None
    )


def test_live_html_with_trailing_spinner_is_evidence():
    text = "Population on 1 January: 450 million.\nLoading..."
    body = b"<html><p>Population on 1 January: 450 million.</p><div class=loader>Loading...</div></html>"
    assert verify.content_problem(body, text, "Population", "https://example.gov/t") is None


@pytest.mark.parametrize("title", ["Site: Errore", "Site \u2014 Error 404", "Site|Fehler"])
def test_error_label_after_any_title_separator(title):
    assert verify.content_problem(b"x", "x", title, "https://example.gov/") == "error_or_challenge"


def test_lone_surrogates_in_pdf_text_are_stored_not_fatal(tmp_path, monkeypatch):
    """r4-il (AI Index 2026 chapter 3): pypdf yielded lone surrogates and caching crashed."""

    class Page:
        def extract_text(self, **_):
            return "Israel \ud835 AI Security Research Unit"

    class Reader:
        def __init__(self, _):
            self.pages = [Page()]

    monkeypatch.setattr(verify, "PdfReader", Reader)
    fetcher = verify.Fetcher(tmp_path, offline=True)
    url = "https://example.org/report.pdf"
    cached(fetcher, url, b"%PDF-1.7\n%%EOF\n", mime="application/pdf")
    meta = fetcher.fetch(url)
    assert meta["status"] == "ok"
    assert fetcher.text(meta) == "Israel � AI Security Research Unit"
    assert verify.literal_match("AI Security Research Unit", fetcher.text(meta))
    views = dict(fetcher.pdf_views(meta))
    assert views["pypdf-layout"] == "Israel � AI Security Research Unit"


def test_retry_diagnostics_cannot_be_changed_by_callers(tmp_path, monkeypatch):
    calls = []

    def opening(request, timeout):
        calls.append(request.full_url)
        raise verify.URLError("temporary outage")

    monkeypatch.setattr(verify, "urlopen", opening)
    fetcher = verify.Fetcher(tmp_path, delay=0)
    url = "https://example.org/report"
    first = fetcher.fetch(url)
    first["status"] = "caller changed first result"
    second = fetcher.fetch(url)
    assert second["status"] == "unreachable"
    second["status"] = "caller changed cached result"
    assert fetcher.fetch(url)["status"] == "unreachable"
    assert calls == [url]
