"""Offline behaviour checks using complete, error and archive fixtures."""

import copy
import hashlib
import json
import shutil
import sys

import pytest

from scripts import snapshot_sources as snap


def test_missing_store_fails_before_reading_data(monkeypatch, capsys):
    monkeypatch.setattr(snap, "PRIVATE_SNAPSHOTS", None)
    monkeypatch.setattr(sys, "argv", ["snapshot_sources.py", "--data", "does-not-exist.json"])
    with pytest.raises(SystemExit) as error:
        snap.main()
    assert error.value.code == 2
    assert "set AISI_SNAPSHOTS or pass --directory DIR" in capsys.readouterr().err


def test_directory_overrides_environment_default(tmp_path, monkeypatch):
    monkeypatch.setattr(snap, "PRIVATE_SNAPSHOTS", tmp_path / "environment-store")
    data = tmp_path / "data.json"
    data.write_text(json.dumps({"sources": [{"id": "s"}]}))
    selected = []
    monkeypatch.setattr(snap, "rehash_source", lambda source, directory, fallback: selected.append(directory) or {"status": "no_snapshot", "source_id": "s"})
    monkeypatch.setattr(sys, "argv", ["snapshot_sources.py", "--rehash", "--data", str(data), "--directory", str(tmp_path / "cli-store"), "--report", str(tmp_path / "report.json")])
    snap.main()
    assert selected == [tmp_path / "cli-store"]


class FakeFetcher:
    def __init__(self, directory):
        self.directory = directory
        self.rows = {}
        self.calls = []

    def entry(self, url):
        return self.directory / hashlib.sha256(url.encode()).hexdigest()

    def add(
        self,
        url,
        body,
        *,
        text=None,
        final_url=None,
        status="ok",
        mime="text/html",
        title="Institute annual report on frontier safety evaluations 2026",
        retrieved="2026-10-17T14:00:00Z",
    ):
        folder = self.entry(url)
        folder.mkdir(parents=True)
        (folder / "response.bin").write_bytes(body)
        (folder / "text.txt").write_text(text if text is not None else body.decode())
        self.rows[url] = {
            "url": url,
            "final_url": final_url or url,
            "status": status,
            "content_type": mime,
            "title": title,
            "retrieved": retrieved,
            "sha256": hashlib.sha256(body).hexdigest(),
            "bytes": len(body),
        }

    def fetch(self, url, method="live"):
        self.calls.append((url, method))
        return {
            **self.rows.get(url, {"url": url, "status": "not_cached"}),
            "method": method,
        }

    def text(self, meta):
        return (self.entry(meta["url"]) / "text.txt").read_text()


@pytest.fixture
def source():
    return {
        "id": "test-report",
        "url": "https://example.gov/report",
        "title": "Institute annual report on frontier safety evaluations 2026",
        "title_en": "Annual report",
        "publisher": "Example ministry",
        "origin": {"kind": "primary"},
        "excerpts": [
            {
                "text": "Our institute published its first safety evaluation.",
                "supports": ["/institutes/0/status"],
            }
        ],
    }


def run(source, fetcher, tmp_path, lookup=False):
    return snap.process_source(
        source, fetcher, tmp_path / "private", tmp_path / "fallback", lookup
    )


def test_exact_hash_public_metadata_and_idempotence(source, tmp_path):
    fetcher = FakeFetcher(tmp_path / "cache")
    body = b"<html><title>Institute annual report on frontier safety evaluations 2026</title><p>Our institute published its first safety evaluation.</p></html>"
    fetcher.add(source["url"], body)
    original = copy.deepcopy(source)
    first = run(source, fetcher, tmp_path)
    snap.apply_result(source, first)
    assert source["snapshot"] == {
        "retrieved": "2026-10-17T14:00:00Z",
        "sha256": hashlib.sha256(body).hexdigest(),
        "text_sha256": hashlib.sha256(
            b"Our institute published its first safety evaluation."
        ).hexdigest(),
        "text_scheme": snap.TEXT_SCHEME,
    }
    assert {k: v for k, v in source.items() if k != "snapshot"} == original
    assert str(tmp_path) not in json.dumps(source)
    files_before = sorted(
        p.relative_to(tmp_path) for p in (tmp_path / "private").rglob("*")
    )
    # Same bytes retrieved at a later time retain the earlier public snapshot and file.
    fetcher.rows[source["url"]]["retrieved"] = "2026-10-18T14:00:00Z"
    second = run(source, fetcher, tmp_path)
    assert second["status"] == "retained"
    assert second["snapshot"] == first["snapshot"]
    assert files_before == sorted(
        p.relative_to(tmp_path) for p in (tmp_path / "private").rglob("*")
    )
    manifest = json.loads((tmp_path / "private/test-report/manifest.json").read_text())
    assert len(manifest) == 1
    assert (tmp_path / "private" / manifest[0]["path"]).read_bytes() == body


@pytest.mark.parametrize(
    "fault",
    [
        "http_error",
        "incomplete_response",
        "error_or_challenge",
        "bad_checksum",
        "bad_length",
        "ok_challenge",
    ],
)
def test_errors_never_create_or_replace_snapshot(source, tmp_path, fault):
    source["snapshot"] = {"retrieved": "2026-10-16T12:00:00Z", "sha256": "a" * 64}
    previous = copy.deepcopy(source)
    fetcher = FakeFetcher(tmp_path / "cache")
    fetcher.add(
        source["url"],
        b"<html>Access denied</html>",
        status=fault
        if fault in {"http_error", "incomplete_response", "error_or_challenge"}
        else "ok",
        title="Access denied"
        if fault == "ok_challenge"
        else "Institute annual report on frontier safety evaluations 2026",
    )
    if fault == "bad_checksum":
        fetcher.rows[source["url"]]["sha256"] = "b" * 64
    if fault == "bad_length":
        fetcher.rows[source["url"]]["bytes"] = 999
    result = run(source, fetcher, tmp_path)
    snap.apply_result(source, result)
    assert result["status"] == "failed"
    assert source == previous
    assert not (tmp_path / "private").exists()


def test_lookup_only_accepts_verified_capture_and_uses_get_fetcher(source, tmp_path):
    fetcher = FakeFetcher(tmp_path / "cache")
    lookup = "https://archive.ph/newest/" + source["url"]
    final = "https://archive.ph/20261017000000/https://example.gov/report"
    fetcher.add(
        lookup,
        b"<html>Institute annual report on frontier safety evaluations 2026</html>",
        final_url=final,
    )
    result = run(source, fetcher, tmp_path, lookup=True)
    assert result["proposed_archive_url"] == final
    assert result["method"] == "archive_lookup"
    assert fetcher.calls.count((lookup, "archive_lookup")) == 1
    assert all("submit" not in url for url, _ in fetcher.calls)
    snap.apply_result(source, result)
    assert source["archive_url"] == final


@pytest.mark.parametrize(
    "case,final,title,text,status",
    [
        (
            "newest",
            "https://archive.ph/newest/https://example.gov/report",
            "Institute annual report on frontier safety evaluations 2026",
            "Institute annual report on frontier safety evaluations 2026",
            "no_capture",
        ),
        (
            "wrong",
            "https://archive.ph/20261017000000/https://example.gov/report",
            "Other document",
            "Unrelated document content",
            "identity_not_verified",
        ),
        (
            "challenge",
            "https://archive.ph/20261017000000/https://example.gov/report",
            "Verify you are human",
            "Verify you are human",
            "error_or_challenge",
        ),
        (
            "wrapper",
            "https://web.archive.org/web/20261017000000/https://example.gov/report",
            "Wayback Machine",
            "Wayback Machine 1 capture",
            "not_checked",
        ),
    ],
)
def test_archive_failure_is_not_published(
    source, tmp_path, case, final, title, text, status
):
    fetcher = FakeFetcher(tmp_path / "cache")
    lookup = "https://archive.ph/newest/" + source["url"]
    fetcher.add(lookup, text.encode(), text=text, title=title, final_url=final)
    result = run(source, fetcher, tmp_path, lookup=True)
    assert result["archives"][0]["status"] == status
    assert "proposed_archive_url" not in result
    assert "snapshot" not in result


def test_existing_wayback_capture_prevents_archive_ph_lookup(source, tmp_path):
    source["archive_url"] = (
        "https://web.archive.org/web/20261017000000/https://example.gov/report"
    )
    fetcher = FakeFetcher(tmp_path / "cache")
    fetcher.add(
        source["archive_url"],
        b"<html>Institute annual report on frontier safety evaluations 2026</html>",
    )
    result = run(source, fetcher, tmp_path, lookup=True)
    assert result["archives"][0]["status"] == "verified"
    assert not any("archive.ph/newest" in url for url, _ in fetcher.calls)


def test_permission_error_falls_back_and_logs(source, tmp_path, monkeypatch):
    fetcher = FakeFetcher(tmp_path / "cache")
    fetcher.add(
        source["url"],
        b"<html>Institute annual report on frontier safety evaluations 2026</html>",
    )
    real = snap.save_snapshot

    def restricted(s, meta, body, directory, fields=None):
        if directory.name == "private":
            raise PermissionError("fixture denial")
        return real(s, meta, body, directory, fields)

    monkeypatch.setattr(snap, "save_snapshot", restricted)
    result = run(source, fetcher, tmp_path)
    assert result["fallback_used"] is True
    assert (tmp_path / "fallback/test-report/manifest.json").is_file()
    assert not (tmp_path / "private").exists()


def test_rendered_text_copy_keeps_its_origin(source, tmp_path):
    fetcher = FakeFetcher(tmp_path / "cache")
    url = "https://r.jina.ai/" + source["url"]
    body = b"Title: Institute annual report on frontier safety evaluations 2026\nMarkdown Content:\nOur institute published its first safety evaluation."
    fetcher.add(url, body, mime="text/plain")
    result = run(source, fetcher, tmp_path)
    assert result["method"] == "render"
    manifest = json.loads((tmp_path / "private/test-report/manifest.json").read_text())[
        0
    ]
    assert manifest["requested_url"] == url
    assert manifest["path"].endswith(".txt")


def test_main_reports_source_failure_and_keeps_other_metadata(
    source, tmp_path, monkeypatch
):
    other = {
        **copy.deepcopy(source),
        "id": "other-report",
        "url": "https://example.gov/other",
    }
    data = {"sources": [source, other]}
    data_path = tmp_path / "data.json"
    data_path.write_text(json.dumps(data))

    def processing(s, *args):
        if s["id"] == "test-report":
            raise ValueError("fixture checksum mismatch")
        return {
            "source_id": s["id"],
            "status": "saved",
            "snapshot": {"retrieved": "2026-10-17T14:00:00Z", "sha256": "a" * 64},
        }

    monkeypatch.setattr(snap, "process_source", processing)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "snapshot_sources.py",
            "--data",
            str(data_path),
            "--cache",
            str(tmp_path / "cache"),
            "--directory",
            str(tmp_path / "private"),
            "--report",
            str(tmp_path / "report.json"),
            "--offline",
            "--apply",
        ],
    )
    snap.main()
    written = json.loads(data_path.read_text())
    report = json.loads((tmp_path / "report.json").read_text())
    assert report["partial"] is False
    assert sorted(r["status"] for r in report["sources"]) == ["failed", "saved"]
    assert written["sources"][0] == source
    assert written["sources"][1]["title_en"] == other["title_en"]
    assert written["sources"][1]["origin"] == other["origin"]
    assert set(written["sources"][1]["snapshot"]) == {"retrieved", "sha256"}


@pytest.mark.parametrize(
    "archive_status,title,remove",
    [
        ("ok", "Wayback Machine", False),
        ("ok", "Access denied", True),
        ("unreachable", "", False),
        ("not_cached", "", False),
        ("http_error", "", False),
        ("ok", "Unrelated article", False),
    ],
)
def test_declared_error_archive_removed_but_access_failure_preserved(
    source, tmp_path, archive_status, title, remove
):
    source["archive_url"] = (
        "https://web.archive.org/web/20261017000000/https://example.gov/report"
    )
    original = source["archive_url"]
    fetcher = FakeFetcher(tmp_path / "cache")
    fetcher.add(
        source["url"],
        b"<html>Institute annual report on frontier safety evaluations 2026</html>",
    )
    fetcher.add(
        original, b"<html>Response body</html>", title=title, status=archive_status
    )
    result = run(source, fetcher, tmp_path)
    assert ("proposed_archive_removal" in result) is remove
    snap.apply_result(source, result)
    assert source.get("archive_url") == (None if remove else original)


def test_unchecked_viewer_retained_when_alternative_capture_verifies(source, tmp_path):
    source["archive_url"] = (
        "https://web.archive.org/web/20261017000000/https://example.gov/report"
    )
    fetcher = FakeFetcher(tmp_path / "cache")
    fetcher.add(
        source["archive_url"], b"<html>Wayback Machine</html>", title="Wayback Machine"
    )
    lookup = "https://archive.ph/newest/" + source["url"]
    fetcher.add(
        lookup,
        b"<html>Institute annual report on frontier safety evaluations 2026</html>",
        final_url="https://archive.ph/20261017000000/https://example.gov/report",
    )
    result = run(source, fetcher, tmp_path, lookup=True)
    assert result["archives"][0]["status"] == "not_checked"
    assert "proposed_archive_url" not in result
    assert (
        result["verified_alternative_archive_url"]
        == "https://archive.ph/20261017000000/https://example.gov/report"
    )
    declared = source["archive_url"]
    snap.apply_result(source, result)
    assert source["archive_url"] == declared


def test_image_pdf_is_retained_but_cannot_establish_archive_identity(source, tmp_path):
    from io import BytesIO

    from pypdf import PdfWriter

    stream = BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    writer.write(stream)
    body = stream.getvalue()
    fetcher = FakeFetcher(tmp_path / "cache")
    fetcher.add(
        source["url"],
        body,
        text="",
        title="",
        mime="application/pdf",
        status="no_extractable_text",
    )
    result = run(source, fetcher, tmp_path)
    assert result["status"] == "saved"
    assert result["snapshot"]["sha256"] == hashlib.sha256(body).hexdigest()
    archive = "https://archive.ph/20261017000000/https://example.gov/report"
    fetcher.add(
        archive,
        body,
        text="",
        title="",
        mime="application/pdf",
        status="no_extractable_text",
    )
    meta, outcome = snap.check_archive(source, fetcher, archive)
    assert meta is None
    assert outcome["status"] == "identity_not_verified"


def test_empty_html_is_not_retained(source, tmp_path):
    fetcher = FakeFetcher(tmp_path / "cache")
    fetcher.add(
        source["url"], b"<html></html>", text="", title="", status="no_extractable_text"
    )
    assert run(source, fetcher, tmp_path)["status"] == "failed"


@pytest.mark.parametrize("error", [TypeError("bad metadata"), KeyError("retrieved")])
def test_malformed_source_metadata_is_reported_without_stopping_other_sources(
    source, tmp_path, error
):
    class BrokenFetcher:
        def fetch(self, *args):
            raise error

    result = snap.process_source_safe(
        source, BrokenFetcher(), tmp_path / "private", tmp_path / "fallback"
    )
    assert result["status"] == "failed"
    assert type(error).__name__ in result["reason"]
    fetcher = FakeFetcher(tmp_path / "cache")
    fetcher.add(
        source["url"], b"Institute annual report on frontier safety evaluations 2026"
    )
    assert (
        snap.process_source_safe(
            source, fetcher, tmp_path / "private", tmp_path / "fallback"
        )["status"]
        == "saved"
    )


def test_declared_evidence_mirror_saved_but_not_published_as_archive(source, tmp_path):
    fetcher = FakeFetcher(tmp_path / "cache")
    mirror = "https://bulletins.example.gov/release"
    source["archive_url"] = mirror
    source["excerpts"][0]["url"] = mirror
    fetcher.add(
        mirror, b"<html>Our institute published its first safety evaluation.</html>"
    )
    result = run(source, fetcher, tmp_path, lookup=True)
    assert result["status"] == "saved"
    assert result["method"] == "declared_mirror"
    assert result["archives"][0]["status"] == "verified_mirror"
    assert "proposed_archive_url" not in result
    assert result["proposed_archive_removal"]["url"] == mirror
    assert (
        "https://archive.ph/newest/" + source["url"],
        "archive_lookup",
    ) in fetcher.calls
    assert not any(url.startswith("https://r.jina.ai/") for url, _ in fetcher.calls)
    snap.apply_result(source, result)
    assert "archive_url" not in source
    second = run(source, fetcher, tmp_path, lookup=True)
    assert second["status"] == "retained"
    assert second["method"] == "declared_mirror"
    assert "proposed_archive_url" not in second


def test_arbitrary_nonarchive_url_is_not_a_declared_evidence_copy(source, tmp_path):
    fetcher = FakeFetcher(tmp_path / "cache")
    mirror = "https://unrelated.example/copy"
    source["archive_url"] = mirror
    fetcher.add(
        mirror, b"<html>Our institute published its first safety evaluation.</html>"
    )
    meta, outcome = snap.check_archive(source, fetcher, mirror)
    assert meta is None
    assert outcome["status"] == "no_capture"


def test_declared_mirror_still_requires_document_identity(source, tmp_path):
    fetcher = FakeFetcher(tmp_path / "cache")
    mirror = "https://bulletins.example.gov/release"
    source["archive_url"] = mirror
    source["excerpts"][0]["url"] = mirror
    fetcher.add(
        mirror, b"<html>Different unrelated report</html>", title="Other report"
    )
    assert snap.check_archive(source, fetcher, mirror)[0] is None


def test_cookie_settings_archive_is_removed_without_snapshot(source, tmp_path):
    fetcher = FakeFetcher(tmp_path / "cache")
    source["archive_url"] = (
        "https://archive.ph/20261017000000/https://example.gov/report"
    )
    fetcher.add(
        source["archive_url"], b"cookie settings", status="cookie_settings_shell"
    )
    result = run(source, fetcher, tmp_path)
    assert result["status"] == "failed"
    assert result["proposed_archive_removal"]["reason"] == "cookie_settings_shell"
    assert "snapshot" not in result
    snap.apply_result(source, result)
    assert "archive_url" not in source


def test_complete_workbook_is_preserved_but_arbitrary_zip_is_not(source, tmp_path):
    from io import BytesIO
    from zipfile import ZipFile

    stream = BytesIO()
    with ZipFile(stream, "w") as archive:
        archive.writestr(
            "xl/workbook.xml",
            '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"/>',
        )
    body = stream.getvalue()
    fetcher = FakeFetcher(tmp_path / "cache")
    fetcher.add(source["url"], body, text="", status="no_extractable_text")
    result = run(source, fetcher, tmp_path)
    assert result["status"] == "saved"
    assert snap.extension(body, "application/octet-stream") == "xlsx"
    assert list((tmp_path / "private" / source["id"]).glob("*.xlsx"))
    stream = BytesIO()
    with ZipFile(stream, "w") as archive:
        archive.writestr("unrelated.txt", "hello")
    fetcher = FakeFetcher(tmp_path / "other-cache")
    fetcher.add(source["url"], stream.getvalue(), text="", status="no_extractable_text")
    assert (
        snap.valid_response(fetcher, fetcher.fetch(source["url"]))[1]
        == "no_extractable_text"
    )


def test_declared_valid_data_archive_saved_as_zip(source, tmp_path):
    from io import BytesIO
    from zipfile import ZipFile

    stream = BytesIO()
    with ZipFile(stream, "w") as archive:
        archive.writestr("data.csv", "a,b\n1,2\n")
    body = stream.getvalue()
    fetcher = FakeFetcher(tmp_path / "zip-cache")
    fetcher.add(
        source["url"],
        body,
        text="",
        status="no_extractable_text",
        mime="application/zip",
    )
    assert run(source, fetcher, tmp_path)["status"] == "saved"
    assert snap.extension(body, "application/zip") == "zip"
    assert list((tmp_path / "private" / source["id"]).glob("*.zip"))


@pytest.mark.parametrize(
    "url,expected",
    [
        (
            "https://web.archive.org/web/20260208102715/https://example.gov/report.pdf?a=1&b=two%20words",
            "https://example.gov/report.pdf?a=1&b=two%20words",
        ),
        (
            "https://web.archive.org/web/20260208102715id_/https://example.gov/report.pdf?q=a%2Fb",
            "https://example.gov/report.pdf?q=a%2Fb",
        ),
        (
            "https://web.archive.org/web/*/https://example.gov/report.pdf?q=1",
            "https://web.archive.org/web/*/https://example.gov/report.pdf?q=1",
        ),
        ("https://example.gov/report.pdf?q=1", "https://example.gov/report.pdf?q=1"),
        (
            "https://archive.ph/20261017000000/https://example.gov/report",
            "https://archive.ph/20261017000000/https://example.gov/report",
        ),
    ],
)
def test_wayback_archive_lookup_target(url, expected):
    assert snap.archive_lookup_target(url) == expected


def test_archive_title_identity_casefold_does_not_change_excerpt_matching():
    source = {
        "title": "Institute Annual Report on Frontier Safety Evaluations 2026",
        "excerpts": [],
    }
    assert not snap.identity_matches(
        source, "", "INSTITUTE ANNUAL REPORT ON FRONTIER SAFETY EVALUATIONS 2026"
    )
    assert snap.identity_matches(
        source, "institute annual report on frontier safety evaluations 2026", ""
    )
    source = {
        "title": "",
        "excerpts": [{"text": "Our institute published its first safety evaluation."}],
    }
    assert snap.identity_matches(
        source, "Our institute published its first safety evaluation.", ""
    )
    assert not snap.identity_matches(
        source, "OUR INSTITUTE PUBLISHED ITS FIRST SAFETY EVALUATION.", ""
    )


def test_failed_wayback_source_looks_up_original_only(source, tmp_path):
    original = "https://example.gov/report.pdf?q=1"
    source["url"] = "https://web.archive.org/web/20260208102715/" + original
    fetcher = FakeFetcher(tmp_path / "wayback-cache")
    run(source, fetcher, tmp_path, lookup=True)
    assert ("https://archive.ph/newest/" + original, "archive_lookup") in fetcher.calls
    assert not any(
        "archive.ph/newest/https://web.archive.org" in url for url, _ in fetcher.calls
    )


def test_parser_failed_pdf_preserved_only_with_usable_independent_view(
    source, tmp_path, monkeypatch
):
    from io import BytesIO

    from pypdf import PdfWriter

    stream = BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    writer.write(stream)
    body = stream.getvalue()
    fetcher = FakeFetcher(tmp_path / "cache")
    fetcher.add(
        source["url"],
        body,
        text="",
        title="",
        mime="application/pdf",
        status="extraction_error",
    )
    meta = fetcher.fetch(source["url"])
    monkeypatch.setattr(
        fetcher,
        "pdf_views",
        lambda meta: [("pdftotext", source["excerpts"][0]["text"])],
        raising=False,
    )
    assert snap.valid_response(fetcher, meta) == (body, None)
    result = run(source, fetcher, tmp_path)
    assert result["status"] == "saved"
    assert result["snapshot"]["sha256"] == hashlib.sha256(body).hexdigest()
    assert meta["status"] == "extraction_error"
    monkeypatch.setattr(fetcher, "pdf_views", lambda meta: [("pdftotext", "")])
    assert snap.valid_response(fetcher, meta) == (None, "extraction_error")
    monkeypatch.setattr(
        fetcher,
        "pdf_views",
        lambda meta: [("pdftotext", "Checking your browser before accessing")],
    )
    assert snap.valid_response(fetcher, meta) == (None, "extraction_error")


@pytest.mark.parametrize(
    "body", [b"<html>Parser failed</html>", b"PK\x03\x04bad zip", b"not a PDF"]
)
def test_parser_failed_non_pdf_never_uses_recovery_view(
    source, tmp_path, monkeypatch, body
):
    fetcher = FakeFetcher(tmp_path / "cache")
    fetcher.add(source["url"], body, status="extraction_error")

    def forbidden(meta):
        raise AssertionError("Only verified PDF bytes may use recovery")

    monkeypatch.setattr(fetcher, "pdf_views", forbidden, raising=False)
    assert snap.valid_response(fetcher, fetcher.fetch(source["url"])) == (
        None,
        "extraction_error",
    )


def test_recovered_pdf_archive_identity_uses_independent_view(
    source, tmp_path, monkeypatch
):
    fetcher = FakeFetcher(tmp_path / "cache")
    source["url"] = "https://example.gov/report.pdf"
    url = "https://web.archive.org/web/20261017140000/https://example.gov/report.pdf"
    fetcher.add(
        url,
        b"%PDF-fixture\n%%EOF\n",
        text="",
        title="",
        status="extraction_error",
        mime="application/pdf",
    )
    monkeypatch.setattr(
        fetcher,
        "pdf_views",
        lambda meta: [("pdftotext", source["excerpts"][0]["text"])],
        raising=False,
    )

    def absent_primary(meta):
        raise AssertionError("Parser-failed PDF has no usable primary text")

    monkeypatch.setattr(fetcher, "text", absent_primary)
    meta, result = snap.check_archive(source, fetcher, url)
    assert result["status"] == "verified"
    assert meta["status"] == "extraction_error"
    monkeypatch.setattr(
        fetcher,
        "pdf_views",
        lambda meta: [
            ("pdftotext", "Different source identity and substantive report")
        ],
    )
    assert (
        snap.check_archive(source, fetcher, url)[1]["status"] == "identity_not_verified"
    )


def test_shell_prefers_render_with_recorded_quote(source, tmp_path):
    fetcher = FakeFetcher(tmp_path / "cache")
    fetcher.add(
        source["url"],
        b"<html><title>Institute annual report</title><nav>Home Research Publications Contact</nav></html>",
    )
    rendered = "https://r.jina.ai/" + source["url"]
    fetcher.add(
        rendered,
        b"Title: Institute annual report\nMarkdown Content:\nOur institute published its first safety evaluation.",
        mime="text/plain",
    )
    result = run(source, fetcher, tmp_path)
    assert result["evidence_url"] == rendered
    assert result["method"] == "render"
    assert fetcher.calls.count((rendered, "render")) == 1


def test_original_quote_avoids_render_even_if_other_quote_missing(source, tmp_path):
    fetcher = FakeFetcher(tmp_path / "cache")
    fetcher.add(
        source["url"],
        b"<html><p>Our institute published its first safety evaluation.</p></html>",
    )
    source["excerpts"].append(
        {"text": "Another separately recorded quotation is absent here."}
    )
    assert run(source, fetcher, tmp_path)["method"] == "live"
    assert fetcher.calls == [(source["url"], "live")]


@pytest.mark.parametrize(
    "render_text",
    [
        None,
        "Title: Institute annual report\nMarkdown Content:\nHome Research Publications Contact details.",
    ],
)
def test_shell_render_failure_or_title_only_retains_original(
    source, tmp_path, render_text
):
    fetcher = FakeFetcher(tmp_path / "cache")
    fetcher.add(
        source["url"],
        b"<html><title>Institute annual report</title><nav>Home Research Publications Contact</nav></html>",
    )
    if render_text:
        fetcher.add(
            "https://r.jina.ai/" + source["url"],
            render_text.encode(),
            mime="text/plain",
        )
    assert run(source, fetcher, tmp_path)["method"] == "live"
    assert sum(method == "render" for _, method in fetcher.calls) == 1


def test_pdf_without_quote_does_not_try_render(source, tmp_path):
    fetcher = FakeFetcher(tmp_path / "cache")
    fetcher.add(
        source["url"],
        b"%PDF-1.7 complete-test-fixture\n%%EOF\n",
        text="A valid report with other substantive text.",
        mime="application/pdf",
    )
    assert run(source, fetcher, tmp_path)["method"] == "live"
    assert fetcher.calls == [(source["url"], "live")]


def cache_metadata(fetcher, url):
    (fetcher.entry(url) / "metadata.json").write_text(json.dumps(fetcher.rows[url]))


def test_archive_wrong_target_rejected_despite_exact_quote(source, tmp_path):
    fetcher = FakeFetcher(tmp_path / "cache")
    url = "https://web.archive.org/web/20261017000000/https://other.gov/unrelated"
    fetcher.add(url, source["excerpts"][0]["text"].encode(), mime="text/plain")
    assert (
        snap.check_archive(source, fetcher, url)[1]["status"] == "target_not_verified"
    )


def test_short_archive_url_uses_recorded_longlink_target(source, tmp_path):
    fetcher = FakeFetcher(tmp_path / "cache")
    url = "https://archive.ph/Ab123"
    body = (
        '<html><input id="SHARE_LONGLINK" value="https://archive.ph/2026.10.17-000000/'
        + source["url"]
        + '"><main>'
        + source["excerpts"][0]["text"]
        + "</main></html>"
    ).encode()
    fetcher.add(url, body)
    assert snap.check_archive(source, fetcher, url)[1]["status"] == "verified"


def test_archive_target_allows_recorded_original_redirect(source, tmp_path):
    fetcher = FakeFetcher(tmp_path / "cache")
    target = "https://example.gov/report-renamed"
    fetcher.add(
        source["url"],
        source["excerpts"][0]["text"].encode(),
        final_url=target,
        mime="text/plain",
    )
    cache_metadata(fetcher, source["url"])
    url = "https://web.archive.org/web/20261017000000/" + target
    fetcher.add(url, source["excerpts"][0]["text"].encode(), mime="text/plain")
    assert snap.check_archive(source, fetcher, url)[1]["status"] == "verified"


@pytest.mark.parametrize(
    "body,expected",
    [
        (
            b"<html><head><title>Institute annual report on frontier safety evaluations 2026</title></head><body><div id='root'></div></body></html>",
            "javascript_shell",
        ),
        (
            b"<html><body><nav>Institute annual report on frontier safety evaluations 2026</nav></body></html>",
            "identity_not_verified",
        ),
    ],
)
def test_title_and_navigation_only_do_not_identify_archive(
    source, tmp_path, body, expected
):
    fetcher = FakeFetcher(tmp_path / "cache")
    url = "https://archive.ph/20261017000000/" + source["url"]
    fetcher.add(url, body)
    assert snap.check_archive(source, fetcher, url)[1]["status"] == expected


def test_generic_body_title_does_not_identify_archive(source, tmp_path):
    source["title"] = "Artificial intelligence systems"
    source["excerpts"] = []
    fetcher = FakeFetcher(tmp_path / "cache")
    url = "https://web.archive.org/web/20261017000000/" + source["url"]
    fetcher.add(
        url,
        b"<html><body>Artificial intelligence systems are interesting.</body></html>",
    )
    assert (
        snap.check_archive(source, fetcher, url)[1]["status"] == "identity_not_verified"
    )


def test_wrapper_follows_observed_payload_with_exact_valid_original_hash(
    source, tmp_path
):
    fetcher = FakeFetcher(tmp_path / "cache")
    source["url"] = "https://example.gov/report.pdf"
    wrapper = "https://web.archive.org/web/20261017000000/" + source["url"]
    raw = "https://web.archive.org/web/20261017000000if_/" + source["url"]
    fetcher.add(
        wrapper,
        ('<html><iframe src="' + raw + '"></iframe></html>').encode(),
        status="archive_wrapper",
    )
    body = b"%PDF-1.7 scan fixture\n%%EOF\n"
    fetcher.add(
        source["url"],
        body,
        text="",
        status="no_extractable_text",
        mime="application/pdf",
    )
    cache_metadata(fetcher, source["url"])
    fetcher.add(
        raw, body, text="", status="no_extractable_text", mime="application/pdf"
    )
    meta, result = snap.check_archive(source, fetcher, wrapper)
    assert result["status"] == "verified"
    assert meta["url"] == raw
    assert fetcher.calls == [(wrapper, "archive"), (raw, "archive_raw")]


def test_wrapper_ignores_unrelated_iframe_and_missing_raw_is_unchecked(
    source, tmp_path
):
    fetcher = FakeFetcher(tmp_path / "cache")
    wrapper = "https://web.archive.org/web/20261017000000/" + source["url"]
    fetcher.add(
        wrapper,
        b'<html><iframe src="https://attacker.example/report.pdf"></iframe></html>',
        status="archive_wrapper",
    )
    _, result = snap.check_archive(source, fetcher, wrapper)
    assert result["status"] == "not_checked"
    assert all("attacker" not in url for url, _ in fetcher.calls)
    assert len(fetcher.calls) == 2


def test_redirect_to_homepage_requires_identity(source, tmp_path):
    fetcher = FakeFetcher(tmp_path / "cache")
    fetcher.add(
        source["url"],
        b"<html><body>Welcome to our website. News and events.</body></html>",
        final_url="https://example.gov/",
    )
    result = run(source, fetcher, tmp_path)
    assert result["status"] == "failed"
    assert result["attempts"][0]["status"] == "redirect_identity_not_verified"


def test_rate_limit_is_not_checked(source, tmp_path):
    fetcher = FakeFetcher(tmp_path / "cache")
    url = "https://archive.ph/newest/" + source["url"]
    fetcher.add(url, b"", status="http_error")
    fetcher.rows[url]["error"] = "HTTP 429"
    _, result = snap.check_archive(source, fetcher, url, "archive_lookup")
    assert result["status"] == "not_checked"
    assert result["reason"] == "HTTP 429"


def test_valid_preserved_original_not_replaced_by_later_render(source, tmp_path):
    fetcher = FakeFetcher(tmp_path / "cache")
    body = ("<html><body>" + source["excerpts"][0]["text"] + "</body></html>").encode()
    fetcher.add(source["url"], body)
    first = run(source, fetcher, tmp_path)
    snap.apply_result(source, first)
    fetcher.rows[source["url"]]["status"] = "unreachable"
    render = "https://r.jina.ai/" + source["url"]
    fetcher.add(render, source["excerpts"][0]["text"].encode(), mime="text/plain")
    second = run(source, fetcher, tmp_path)
    assert second["status"] == "retained"
    assert second["snapshot"] == first["snapshot"]
    assert second["method"] == "live"


def test_preserved_js_shell_can_be_replaced_by_matching_render(source, tmp_path):
    fetcher = FakeFetcher(tmp_path / "cache")
    fetcher.add(
        source["url"], b"<html><body>Home News Publications Contact</body></html>"
    )
    first = run(source, fetcher, tmp_path)
    snap.apply_result(source, first)
    render = "https://r.jina.ai/" + source["url"]
    fetcher.add(render, source["excerpts"][0]["text"].encode(), mime="text/plain")
    second = run(source, fetcher, tmp_path)
    assert second["snapshot"] != first["snapshot"]
    assert second["method"] == "render"


@pytest.mark.parametrize("same_bytes", [True, False])
def test_www_alias_requires_identical_valid_document_bytes(
    source, tmp_path, same_bytes
):
    source["url"] = "https://example.gov/report.pdf"
    fetcher = FakeFetcher(tmp_path / "cache")
    body = b"%PDF-1.7 scan fixture\n%%EOF\n"
    fetcher.add(
        source["url"],
        body,
        text="",
        status="no_extractable_text",
        mime="application/pdf",
    )
    cache_metadata(fetcher, source["url"])
    url = "https://web.archive.org/web/20261017000000if_/https://www.example.gov/report.pdf"
    fetcher.add(
        url,
        body
        if same_bytes
        else body.replace(b"scan fixture", b"different scan fixture"),
        text="",
        status="no_extractable_text",
        mime="application/pdf",
    )
    _, outcome = snap.check_archive(source, fetcher, url)
    assert outcome["status"] == ("verified" if same_bytes else "target_not_verified")


def test_identical_html_shell_is_not_document_hash_identity(source, tmp_path):
    fetcher = FakeFetcher(tmp_path / "cache")
    body = b"<html><head><title>Institute annual report on frontier safety evaluations 2026</title></head><body><div id='root'></div></body></html>"
    fetcher.add(source["url"], body)
    cache_metadata(fetcher, source["url"])
    url = "https://web.archive.org/web/20261017000000/" + source["url"]
    fetcher.add(url, body)
    assert snap.check_archive(source, fetcher, url)[1]["status"] == "javascript_shell"


@pytest.mark.parametrize(
    "status",
    [
        "login_or_challenge",
        "error_or_challenge",
        "cookie_settings_shell",
        "empty_render",
        "javascript_shell",
        "document_viewer",
    ],
)
def test_rejected_raw_capture_proposes_removal(source, tmp_path, status):
    fetcher = FakeFetcher(tmp_path / "cache")
    wrapper = "https://web.archive.org/web/20261017000000/" + source["url"]
    raw = "https://web.archive.org/web/20261017000000if_/" + source["url"]
    source["archive_url"] = wrapper
    fetcher.add(source["url"], b"Usable original document content")
    fetcher.add(
        wrapper,
        ('<html><iframe src="' + raw + '"></iframe></html>').encode(),
        status="archive_wrapper",
    )
    fetcher.add(raw, b"Rejected raw response", status=status)
    result = run(source, fetcher, tmp_path)
    assert result["proposed_archive_removal"] == {"url": wrapper, "reason": status}
    assert result["archives"][0]["raw_attempt"]["status"] == status
    snap.apply_result(source, result)
    assert "archive_url" not in source


def test_gzip_archive_identity_uses_decoded_body(source, tmp_path):
    import gzip

    fetcher = FakeFetcher(tmp_path / "cache")
    url = "https://web.archive.org/web/20261017000000/" + source["url"]
    text = source["excerpts"][0]["text"]
    body = gzip.compress(
        ("<html><body><article>" + text + "</article></body></html>").encode()
    )
    fetcher.add(url, body, text=text)
    meta, outcome = snap.check_archive(source, fetcher, url)
    assert outcome["status"] == "verified"
    assert meta["sha256"] == hashlib.sha256(body).hexdigest()


def test_truncated_gzip_does_not_abort_source_processing(source, tmp_path):
    import gzip

    fetcher = FakeFetcher(tmp_path / "cache")
    body = gzip.compress(b"<html><p>Original document text.</p></html>")[:-5]
    fetcher.add(source["url"], body, text="stale cached text")
    result = snap.process_source_safe(
        source, fetcher, tmp_path / "private", tmp_path / "fallback"
    )
    assert result["status"] == "failed"
    assert "Invalid or incomplete gzip" in result["reason"]
    assert "snapshot" not in result


@pytest.mark.parametrize("end_marker", [b"", b"\n%%EOF\n"])
def test_compressed_pdf_also_requires_end_marker(source, tmp_path, end_marker):
    import gzip

    fetcher = FakeFetcher(tmp_path / "cache")
    body = gzip.compress(b"%PDF-1.7 document fixture" + end_marker)
    fetcher.add(source["url"], body, text="Article text", mime="application/pdf")
    meta = fetcher.fetch(source["url"])
    accepted, problem = snap.valid_response(fetcher, meta)
    assert (accepted, problem) == (
        (body, None) if end_marker else (None, "incomplete_pdf")
    )


@pytest.mark.parametrize("status", ["ok", "no_extractable_text", "extraction_error"])
def test_truncated_pdf_rejected_before_recovery_or_archive_identity(
    source, tmp_path, monkeypatch, status
):
    from io import BytesIO

    from pypdf import PdfWriter

    stream = BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    writer.write(stream)
    prefix = stream.getvalue().rsplit(b"%%EOF", 1)[0]
    fetcher = FakeFetcher(tmp_path / "cache")
    archive = "https://web.archive.org/web/20261017000000/" + source["url"]
    fetcher.add(
        archive,
        prefix,
        text=source["excerpts"][0]["text"],
        mime="application/pdf",
        status=status,
    )

    def forbidden(meta):
        raise AssertionError("A PDF prefix must not reach identity or recovery")

    monkeypatch.setattr(fetcher, "pdf_views", forbidden, raising=False)
    meta = fetcher.fetch(archive)
    assert snap.valid_response(fetcher, meta) == (None, "incomplete_pdf")
    assert snap.check_archive(source, fetcher, archive)[1]["status"] == "incomplete_pdf"
    assert not (tmp_path / "private").exists()


def test_empty_response_with_matching_hash_is_not_cache_corruption(source, tmp_path):
    fetcher = FakeFetcher(tmp_path / "cache")
    fetcher.add(source["url"], b"", text="")
    meta = fetcher.fetch(source["url"])
    assert meta["bytes"] == 0
    assert meta["sha256"] == hashlib.sha256(b"").hexdigest()
    assert snap.valid_response(fetcher, meta) == (None, "empty_response")


@pytest.mark.parametrize(
    "status,body,replace",
    [
        ("unreachable", b"", False),
        ("not_cached", b"", False),
        (
            "ok",
            b"<html><p>Different substantive article about a separate event.</p></html>",
            False,
        ),
        ("javascript_shell", b"<html><div id='app'></div></html>", True),
    ],
)
def test_verified_lookup_does_not_replace_uncertain_declared_capture(
    source, tmp_path, status, body, replace
):
    declared = "https://web.archive.org/web/20261017000000/" + source["url"]
    source["archive_url"] = declared
    fetcher = FakeFetcher(tmp_path / "cache")
    fetcher.add(declared, body, status=status)
    alternative = "https://archive.ph/20261017000000/" + source["url"]
    lookup = "https://archive.ph/newest/" + source["url"]
    fetcher.add(
        lookup,
        b"<html><p>Our institute published its first safety evaluation.</p></html>",
        final_url=alternative,
    )
    result = run(source, fetcher, tmp_path, lookup=True)
    assert result["status"] == "saved"
    assert result["evidence_url"] == lookup
    if replace:
        assert result["proposed_archive_url"] == alternative
        assert "verified_alternative_archive_url" not in result
    else:
        assert "proposed_archive_url" not in result
        assert "proposed_archive_removal" not in result
        assert result["verified_alternative_archive_url"] == alternative
    snap.apply_result(source, result)
    assert source["archive_url"] == (alternative if replace else declared)


def test_truncated_previous_pdf_is_not_preserved_over_valid_render(source, tmp_path):
    fetcher = FakeFetcher(tmp_path / "cache")
    body = b"%PDF-1.7 incomplete prefix"
    meta = {
        "url": source["url"],
        "method": "live",
        "retrieved": "2026-10-17T14:00:00Z",
        "content_type": "application/pdf",
    }
    source["snapshot"], _ = snap.save_snapshot(source, meta, body, tmp_path / "private")
    rendered = "https://r.jina.ai/" + source["url"]
    fetcher.add(
        rendered,
        b"Our institute published its first safety evaluation.",
        mime="text/plain",
    )
    result = run(source, fetcher, tmp_path)
    assert result["status"] == "saved"
    assert result["method"] == "render"


def test_pdf_with_appended_server_error_rejected(source, tmp_path):
    fetcher = FakeFetcher(tmp_path / "cache")
    body = b"%PDF-1.7 fixture\n%%EOF\nError 500: javax.servlet.ServletException"
    fetcher.add(source["url"], body, text="Recovered PDF text", mime="application/pdf")
    assert snap.valid_response(fetcher, fetcher.fetch(source["url"])) == (
        None,
        "incomplete_pdf",
    )


def test_empty_file_with_wrong_recorded_hash_is_still_corruption(source, tmp_path):
    fetcher = FakeFetcher(tmp_path / "cache")
    fetcher.add(source["url"], b"", text="")
    meta = fetcher.fetch(source["url"])
    meta["sha256"] = hashlib.sha256(b"lost original bytes").hexdigest()
    assert snap.valid_response(fetcher, meta) == (None, "checksum_mismatch")


@pytest.fixture
def real_offline_cache(tmp_path, monkeypatch):
    """Exercise the real Fetcher's immutable first-caller method behaviour."""
    from scripts import verify_excerpts as verify

    def forbidden(*args, **kwargs):
        raise AssertionError("Offline fixture must not request the network")

    monkeypatch.setattr(verify, "urlopen", forbidden)
    fetcher = verify.Fetcher(tmp_path / "real-cache", offline=True)

    def store(url, body, *, method="live", final_url=None):
        folder = fetcher.entry(url)
        folder.mkdir(parents=True)
        text, title = verify.extract_text(body, "text/html")
        meta = {
            "url": url,
            "final_url": final_url or url,
            "method": method,
            "retrieved": "2026-10-17T14:00:00Z",
            "content_type": "text/html",
            "sha256": hashlib.sha256(body).hexdigest(),
            "bytes": len(body),
            "title": title,
            "status": "ok",
            "extraction_version": verify.EXTRACTION_VERSION,
            "text_sha256": hashlib.sha256(text.encode()).hexdigest(),
        }
        (folder / "response.bin").write_bytes(body)
        (folder / "text.txt").write_text(text, encoding="utf-8")
        (folder / "metadata.json").write_text(json.dumps(meta), encoding="utf-8")
        return meta

    return fetcher, store


@pytest.mark.parametrize(
    "prefix,expected",
    [
        ("https://r.jina.ai/", "render"),
        ("https://web.archive.org/web/20261017000000/", "archive"),
    ],
)
def test_real_cached_live_copy_gets_actual_method(
    source, tmp_path, real_offline_cache, prefix, expected
):
    fetcher, store = real_offline_cache
    source["url"] = prefix + source["url"]
    body = b"<html><p>Our institute published its first safety evaluation.</p></html>"
    store(source["url"], body)
    assert fetcher.fetch(source["url"], expected)["method"] == "live"
    result = run(source, fetcher, tmp_path)
    assert result["method"] == expected
    manifest = json.loads(
        (tmp_path / "private" / source["id"] / "manifest.json").read_text()
    )
    assert manifest[-1]["method"] == expected
    assert manifest[-1]["cache_method"] == "live"
    assert fetcher.fetch(source["url"])["method"] == "live"


@pytest.mark.parametrize("role", ["archive_raw", "archive_lookup"])
def test_archive_requested_role_survives_cached_live_method(
    source, real_offline_cache, role
):
    fetcher, store = real_offline_cache
    archive = "https://web.archive.org/web/20261017000000/" + source["url"]
    store(
        archive,
        b"<html><p>Our institute published its first safety evaluation.</p></html>",
    )
    meta, outcome = snap.check_archive(source, fetcher, archive, role)
    assert outcome["status"] == "verified"
    assert meta["method"] == role
    assert meta["cache_method"] == "live"


def test_cached_live_render_cannot_downgrade_existing_original(
    source, tmp_path, real_offline_cache
):
    fetcher, store = real_offline_cache
    body = b"<html><p>Our institute published its first safety evaluation.</p></html>"
    meta = {
        "url": source["url"],
        "method": "live",
        "retrieved": "2026-10-17T14:00:00Z",
        "content_type": "text/html",
    }
    source["snapshot"], _ = snap.save_snapshot(source, meta, body, tmp_path / "private")
    store("https://r.jina.ai/" + source["url"], body + b"\n")
    result = run(source, fetcher, tmp_path)
    assert result["status"] == "retained"
    assert result["reason"] == "existing_original_preserved"
    assert result["method"] == "live"


@pytest.mark.parametrize(
    "prefix,expected",
    [
        ("https://r.jina.ai/", "render"),
        ("https://web.archive.org/web/20261017000000/", "archive"),
    ],
)
def test_manifest_method_correction_appends_once_without_changing_bytes(
    source, tmp_path, prefix, expected
):
    directory = tmp_path / "private"
    body = b"Our institute published its first safety evaluation."
    meta = {
        "url": prefix + source["url"],
        "method": "live",
        "retrieved": "2026-10-17T14:00:00Z",
        "content_type": "text/plain",
    }
    snapshot, manifest = snap.save_snapshot(source, meta, body, directory)
    path = directory / source["id"] / "manifest.json"
    legacy = {**manifest, "method": "live"}
    legacy.pop("cache_method", None)
    path.write_text(json.dumps([legacy]))
    source["snapshot"] = snapshot
    assert snap.save_snapshot(source, meta, body, directory)[0] == snapshot
    corrected = json.loads(path.read_text())
    assert corrected[0] == legacy
    assert len(corrected) == 2
    assert corrected[-1]["method"] == expected
    assert corrected[-1]["cache_method"] == "live"
    assert (directory / manifest["path"]).read_bytes() == body
    snap.save_snapshot(source, meta, body, directory)
    assert json.loads(path.read_text()) == corrected
    fetcher = FakeFetcher(tmp_path / "cache")
    previous = snap.preserved_original(
        source, fetcher, directory, tmp_path / "fallback"
    )
    if expected == "render":
        assert previous is None
        path.write_text(json.dumps([legacy]))
        assert (
            snap.preserved_original(source, fetcher, directory, tmp_path / "fallback")
            is None
        )
    else:
        assert previous == corrected[-1]


def test_final_render_host_overrides_requested_live_role():
    meta = {
        "url": "https://example.gov/report",
        "final_url": "https://r.jina.ai/https://example.gov/report",
        "method": "live",
    }
    result = snap.normalise_method(meta, "live")
    assert result["method"] == "render"
    assert result["cache_method"] == "live"
    assert meta["method"] == "live"
    assert snap.normalise_method(result, "archive")["cache_method"] == "live"


def test_declared_mirror_keeps_original_cached_role(source, real_offline_cache):
    fetcher, store = real_offline_cache
    mirror = "https://official.example.gov/bulletin"
    source["excerpts"][0]["url"] = mirror
    store(
        mirror,
        b"<html><p>Our institute published its first safety evaluation.</p></html>",
    )
    meta, outcome = snap.check_archive(source, fetcher, mirror, "declared_copy")
    assert outcome["status"] == "verified_mirror"
    assert meta["method"] == "declared_mirror"
    assert meta["cache_method"] == "live"


ARTICLE = "<main><h1>Report</h1><p>Our institute published its first safety evaluation.</p></main>"


def page(main=ARTICLE, nav="Home", footer="Updated 17 October", aside="Most read: A"):
    return (
        f"<html><head><title>T</title><script>var t={footer!r}</script></head><body>"
        f"<header>Ministry</header><nav>{nav}</nav>{main}<aside>{aside}</aside>"
        f"<footer>{footer}</footer></body></html>"
    ).encode()


def text_hash(body, mime="text/html"):
    return snap.text_fields(body, mime).get("text_sha256")


def test_text_hash_ignores_page_furniture_but_not_main_text():
    base = text_hash(page())
    assert base == hashlib.sha256(
        b"Report Our institute published its first safety evaluation."
    ).hexdigest()
    assert text_hash(page(nav="Start", footer="Updated 18 October", aside="Most read: B")) == base
    assert text_hash(page(main=ARTICLE.replace("first", "second"))) != base
    # Without a main element the single article is used; several articles fall back to the body.
    article = page(main=ARTICLE.replace("main>", "article>"), aside="x")
    assert text_hash(article) == base
    two = b"<html><body><article>A</article><article>B</article><p>C</p></body></html>"
    assert text_hash(two) == hashlib.sha256(b"A B C").hexdigest()
    # A header inside a role=main container is content.
    assert text_hash(b"<body><div role=main><header>Title</header><p>Text</p></div></body>") == (
        hashlib.sha256(b"Title Text").hexdigest()
    )
    # A charset declaration inside main does not re-decode the extracted text.
    cyrillic = (
        "<head><meta charset=utf-8></head><main><meta charset=windows-1251><p>Закон</p></main>"
    ).encode()
    assert text_hash(cyrillic) == hashlib.sha256("Закон".encode()).hexdigest()
    # Unmarked blocks outside main do not count; furniture is dropped even without main.
    assert text_hash(page(main=ARTICLE + "<div>Related: A</div>")) == base
    assert text_hash(b"<body><nav>Menu 1</nav><p>Text</p></body>") == text_hash(
        b"<body><nav>Menu 2</nav><p>Text</p></body>"
    )


def test_text_hash_folds_compatibility_forms_and_whitespace_only():
    plain = text_hash(b"<p>Office fi 2026</p>")
    assert text_hash(b"<p>Of\xef\xac\x81ce  \n fi\xc2\xa0\xef\xbc\x92\xef\xbc\x90\xef\xbc\x92\xef\xbc\x96</p>") == plain
    # No ASCII or case folding: accents and case stay significant.
    assert text_hash("<p>Société</p>".encode()) != text_hash(b"<p>Societe</p>")
    assert text_hash(b"<p>AI Office</p>") != text_hash(b"<p>ai office</p>")


def test_text_hash_of_render_ignores_header_and_empty_copy_has_none():
    render = "Title: {t}\nURL Source: https://example.gov/report\nPublished Time: {p}\nMarkdown Content:\nBody text."
    first = text_hash(render.format(t="A", p="1").encode(), "text/plain")
    assert first == text_hash(render.format(t="B", p="2").encode(), "text/plain")
    assert first == hashlib.sha256(b"Body text.").hexdigest()
    assert snap.text_fields(b"<html><body> <script>x()</script></body></html>", "text/html") == {}
    # When furniture removal leaves nothing, the whole visible document is hashed.
    assert text_hash(b"<html><nav>Only menu</nav></html>") == hashlib.sha256(b"Only menu").hexdigest()
    # An empty main element falls through to the article or body; ASP.NET forms are content.
    assert text_hash(b"<body><nav>Menu</nav><main></main><form><p>Release</p></form></body>") == (
        hashlib.sha256(b"Release").hexdigest()
    )
    assert snap.text_fields(b"%PDF-1.7 broken", "application/pdf") == {}


def snapshotted(source, fetcher, tmp_path, body):
    fetcher.add(source["url"], body)
    first = run(source, fetcher, tmp_path)
    snap.apply_result(source, first)
    return first


def refetched(source, fetcher, tmp_path, body):
    fetcher.rows.pop(source["url"])
    shutil.rmtree(fetcher.entry(source["url"]))
    fetcher.add(source["url"], body, retrieved="2026-10-19T00:00:00Z")
    return run(source, fetcher, tmp_path)


def test_new_copy_proposed_only_when_main_text_changes(source, tmp_path):
    fetcher = FakeFetcher(tmp_path / "cache")
    snapshotted(source, fetcher, tmp_path, page())
    previous = copy.deepcopy(source["snapshot"])
    files = sorted((tmp_path / "private").rglob("*"))
    furniture = refetched(source, fetcher, tmp_path, page(footer="Updated 19 October"))
    assert furniture["status"] == "retained"
    assert furniture["change"] == "text_unchanged"
    assert furniture["snapshot"] == previous
    assert sorted((tmp_path / "private").rglob("*")) == files
    changed = refetched(
        source,
        fetcher,
        tmp_path,
        page(main=ARTICLE.replace("</p>", " It was revised.</p>")),
    )
    # The bound quotation still matches: reported as a change, the cited copy is kept.
    assert changed["status"] == "retained"
    assert changed["change"] == "text_changed"
    assert changed["snapshot"] == previous
    assert changed["bound_quotes"] == {
        "total": 1,
        "lost_in_new_copy": 0,
        "gained_in_new_copy": 0,
        "in_neither": 0,
    }
    gone = refetched(
        source,
        fetcher,
        tmp_path,
        page(main=ARTICLE.replace("first safety evaluation", "second report")),
    )
    assert gone["status"] == "saved"
    assert gone["change"] == "bound_quote_missing"
    assert gone["snapshot"]["retrieved"] == "2026-10-19T00:00:00Z"


def test_without_bound_quotes_the_text_hash_decides(source, tmp_path):
    source["excerpts"][0]["url"] = "https://mirror.example.org/report"
    fetcher = FakeFetcher(tmp_path / "cache")
    snapshotted(source, fetcher, tmp_path, page())
    changed = refetched(
        source,
        fetcher,
        tmp_path,
        page(main=ARTICLE.replace("</p>", " It was revised.</p>")),
    )
    assert (changed["status"], changed["change"]) == ("saved", "text_changed")
    assert "bound_quotes" not in changed


def test_unchanged_text_with_missing_previous_copy_is_recorded(source, tmp_path):
    fetcher = FakeFetcher(tmp_path / "cache")
    snapshotted(source, fetcher, tmp_path, page())
    for path in (tmp_path / "private/test-report").glob("*.html"):
        path.unlink()
    result = refetched(source, fetcher, tmp_path, page(footer="Updated 19 October"))
    assert result["status"] == "saved"
    assert result["change"] == "previous_copy_missing"


def test_old_scheme_or_no_text_hash_is_not_compared(source, tmp_path):
    fetcher = FakeFetcher(tmp_path / "cache")
    snapshotted(source, fetcher, tmp_path, page())
    source["snapshot"]["text_scheme"] = snap.TEXT_SCHEME + 1
    result = refetched(source, fetcher, tmp_path, page(footer="Updated 19 October"))
    assert result["change"] == "no_comparable_text_hash"


def test_rehash_reads_only_the_cited_stored_copy(source, tmp_path):
    fetcher = FakeFetcher(tmp_path / "cache")
    snapshotted(source, fetcher, tmp_path, page())
    expected = copy.deepcopy(source["snapshot"])
    del source["snapshot"]["text_sha256"], source["snapshot"]["text_scheme"]
    result = snap.rehash_source(source, tmp_path / "private", tmp_path / "fallback")
    assert result["status"] == "rehashed"
    assert result["snapshot"] == expected
    for path in (tmp_path / "private/test-report").glob("*.html"):
        path.write_bytes(b"<p>tampered</p>")
    tampered = snap.rehash_source(source, tmp_path / "private", tmp_path / "fallback")
    assert tampered["status"] == "failed"
    assert "checksum mismatch" in tampered["reason"]
    for path in (tmp_path / "private/test-report").glob("*.html"):
        path.unlink()
    missing = snap.rehash_source(source, tmp_path / "private", tmp_path / "fallback")
    assert (missing["status"], missing["reason"]) == ("failed", "previous_copy_missing")


def test_same_bytes_keep_recorded_metadata(source, tmp_path):
    fetcher = FakeFetcher(tmp_path / "cache")
    snapshotted(source, fetcher, tmp_path, page())
    del source["snapshot"]["text_sha256"], source["snapshot"]["text_scheme"]
    recorded = copy.deepcopy(source["snapshot"])
    result = refetched(source, fetcher, tmp_path, page())
    assert result["status"] == "retained"
    assert result["snapshot"] == recorded
    assert "change" not in result


def test_rehash_cli_applies_from_stored_copies_only(source, tmp_path, monkeypatch):
    fetcher = FakeFetcher(tmp_path / "cache")
    snapshotted(source, fetcher, tmp_path, page())
    expected = copy.deepcopy(source["snapshot"])
    missing = copy.deepcopy(source)
    missing["id"] = "missing-copy"
    missing["snapshot"] = {"retrieved": "2026-10-16T00:00:00Z", "sha256": "c" * 64}
    stale = copy.deepcopy(source)
    stale["snapshot"].update(text_sha256="d" * 64, text_scheme=0)
    data = tmp_path / "aisi.json"
    data.write_text(json.dumps({"sources": [stale, missing, {"id": "none"}]}))
    report = tmp_path / "report.json"
    monkeypatch.setattr(
        sys,
        "argv",
        ["snapshot_sources.py", "--rehash", "--apply", "--data", str(data),
         "--directory", str(tmp_path / "private"), "--report", str(report),
         "--cache", str(tmp_path / "no-cache")],
    )
    snap.main()
    stale_after, missing_after, none_after = json.loads(data.read_text())["sources"]
    assert stale_after["snapshot"] == expected
    assert missing_after == missing
    assert none_after == {"id": "none"}
    statuses = [r["status"] for r in json.loads(report.read_text())["sources"]]
    assert statuses == ["rehashed", "failed", "no_snapshot"]
    assert not (tmp_path / "no-cache").exists()


def test_quote_in_neither_copy_does_not_force_replacement(source, tmp_path):
    source["excerpts"].append({"text": "A sentence found in neither copy of the page.", "supports": ["/x"]})
    fetcher = FakeFetcher(tmp_path / "cache")
    snapshotted(source, fetcher, tmp_path, page())
    changed = refetched(
        source, fetcher, tmp_path, page(main=ARTICLE.replace("</p>", " It was revised.</p>"))
    )
    assert (changed["status"], changed["change"]) == ("retained", "text_changed")
    assert changed["bound_quotes"]["in_neither"] == 1


def test_both_copies_are_read_through_the_same_views(source, tmp_path, monkeypatch):
    quote = source["excerpts"][0]["text"]
    # The quotation is only in an independent PDF-style view, for both copies alike.
    monkeypatch.setattr(snap, "pdf_text_views", lambda body, path: [("pdftotext", quote)])
    fetcher = FakeFetcher(tmp_path / "cache")
    snapshotted(source, fetcher, tmp_path, page(main="<main><p>Old wording</p></main>"))
    changed = refetched(source, fetcher, tmp_path, page(main="<main><p>New wording</p></main>"))
    assert (changed["status"], changed["change"]) == ("retained", "text_changed")


def test_quotes_with_cited_copy_not_held_report_the_missing_copy(source, tmp_path):
    fetcher = FakeFetcher(tmp_path / "cache")
    snapshotted(source, fetcher, tmp_path, page())
    for path in (tmp_path / "private/test-report").glob("*.html"):
        path.unlink()
    result = refetched(
        source, fetcher, tmp_path, page(main=ARTICLE.replace("</p>", " It was revised.</p>"))
    )
    assert (result["status"], result["change"]) == ("saved", "previous_copy_missing")


def test_corrupt_cited_copy_does_not_block_a_text_change_without_quotes(source, tmp_path):
    source["excerpts"][0]["url"] = "https://mirror.example.org/report"  # no bound quotation
    fetcher = FakeFetcher(tmp_path / "cache")
    snapshotted(source, fetcher, tmp_path, page())
    for path in (tmp_path / "private/test-report").glob("*.html"):
        path.write_bytes(b"corrupt")
    new = page(main=ARTICLE.replace("</p>", " It was revised.</p>"))
    result = refetched(source, fetcher, tmp_path, new)
    assert (result["status"], result["change"]) == ("saved", "text_changed")
