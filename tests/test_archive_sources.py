"""Legacy application must not restore rejected archive candidates."""

import hashlib
import json
import sys
from io import BytesIO

import pytest

from scripts import archive_sources, verify_excerpts


@pytest.fixture(autouse=True)
def no_requests(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Unexpected network request; online tests must supply fixtures")

    monkeypatch.setattr(verify_excerpts, "urlopen", forbidden)
    monkeypatch.setattr(archive_sources, "fetch", forbidden)
    monkeypatch.setattr(archive_sources, "save", forbidden)


def source():
    return {
        "id": "report",
        "url": "https://example.gov/report",
        "title": "National Institute Annual Research Report for 2026",
    }


def cache(fetcher, url, body):
    folder = fetcher.entry(url)
    folder.mkdir(parents=True)
    (folder / "response.bin").write_bytes(body)
    verify_excerpts.atomic_json(
        folder / "metadata.json",
        {
            "url": url,
            "final_url": url,
            "status": "ok",
            "method": "archive",
            "retrieved": "2026-10-17T14:00:00Z",
            "content_type": "text/html",
            "sha256": hashlib.sha256(body).hexdigest(),
            "bytes": len(body),
            "extraction_version": 0,
        },
    )


CAPTURE = "https://web.archive.org/web/20251017140000/https://example.gov/report"


def test_verified_capture_applies(tmp_path):
    fetcher = verify_excerpts.Fetcher(tmp_path, offline=True)
    cache(
        fetcher,
        CAPTURE,
        b"<html><title>National Institute Annual Research Report for 2026</title><article><h1>National Institute Annual Research Report for 2026</h1>Annual research findings from the institute.</article></html>",
    )
    data = {"sources": [source()]}
    result = archive_sources.apply_candidates(
        data, {"report": {"archive_url": CAPTURE}}, fetcher
    )
    assert result == {"applied": ["report"], "rejected": []}
    assert data["sources"][0]["archive_url"] == CAPTURE


@pytest.mark.parametrize(
    "body,status",
    [
        (
            b'<html><title>Wayback Machine</title><div id="wm-ipp-base">National Institute Annual Research Report for 2026</div></html>',
            "not_checked",
        ),
        (
            b"<html><title>Login</title><p>National Institute Annual Research Report for 2026</p></html>",
            "error_or_challenge",
        ),
        (
            b"<html><title>Unrelated document</title><article>Different research report with no target identity.</article></html>",
            "identity_not_verified",
        ),
    ],
)
def test_cached_bad_capture_is_not_restored(tmp_path, body, status):
    fetcher = verify_excerpts.Fetcher(tmp_path, offline=True)
    cache(fetcher, CAPTURE, body)
    data = {"sources": [source()]}
    result = archive_sources.apply_candidates(
        data, {"report": {"archive_url": CAPTURE}}, fetcher
    )
    assert result["applied"] == []
    assert result["rejected"][0]["status"] == status
    assert "archive_url" not in data["sources"][0]


def test_missing_cache_fails_closed(tmp_path):
    data = {"sources": [source()]}
    result = archive_sources.apply_candidates(
        data,
        {"report": {"archive_url": CAPTURE}},
        verify_excerpts.Fetcher(tmp_path, offline=True),
    )
    assert result["rejected"][0]["status"] == "not_checked"
    assert result["rejected"][0]["reason"] == "not_cached"
    assert "archive_url" not in data["sources"][0]


def test_verified_mirror_is_not_an_archive(tmp_path):
    fetcher = verify_excerpts.Fetcher(tmp_path, offline=True)
    mirror = "https://mirror.example.org/report"
    cache(
        fetcher,
        mirror,
        b"<html><title>National Institute Annual Research Report for 2026</title><article>National Institute Annual Research Report for 2026</article></html>",
    )
    row = source()
    row["excerpts"] = [{"text": row["title"], "url": mirror}]
    data = {"sources": [row]}
    result = archive_sources.apply_candidates(
        data, {"report": {"archive_url": mirror}}, fetcher
    )
    assert result["applied"] == []
    assert "archive_url" not in row


def test_existing_canonical_archive_is_unchanged(tmp_path):
    row = source()
    row["archive_url"] = CAPTURE
    data = {"sources": [row]}
    result = archive_sources.apply_candidates(
        data,
        {"report": {"archive_url": "https://example.org/wrong"}},
        verify_excerpts.Fetcher(tmp_path, offline=True),
    )
    assert row["archive_url"] == CAPTURE
    assert result == {"applied": [], "rejected": []}


RAW_CAPTURE = CAPTURE.replace("20251017140000/", "20251017140000id_/")
REPORT_BODY = b"<html><title>National Institute Annual Research Report for 2026</title><article>National Institute Annual Research Report for 2026. Annual research findings from the institute.</article></html>"
WRAPPER_BODY = b'<html><title>Wayback Machine</title><div id="wm-ipp-base">National Institute Annual Research Report for 2026</div></html>'


def network_fixture(monkeypatch, responses):
    calls = []

    class Response(BytesIO):
        def __init__(self, body, final_url):
            super().__init__(body)
            self.final_url = final_url
            self.headers = {"Content-Type": "text/html", "Content-Length": str(len(body))}

        def geturl(self):
            return self.final_url

    def opening(request, timeout):
        calls.append(request.full_url)
        body, final_url = responses[request.full_url]
        return Response(body, final_url)

    monkeypatch.setattr(verify_excerpts, "urlopen", opening)
    return calls


@pytest.mark.parametrize("wrong_identity", [False, True])
def test_online_apply_fetches_and_caches_wrapper_and_raw_capture(
    tmp_path, monkeypatch, wrong_identity
):
    body = (
        b"<html><title>Unrelated document</title><article>Different research report.</article></html>"
        if wrong_identity
        else REPORT_BODY
    )
    calls = network_fixture(
        monkeypatch, {CAPTURE: (WRAPPER_BODY, CAPTURE), RAW_CAPTURE: (body, RAW_CAPTURE)}
    )
    fetcher = verify_excerpts.Fetcher(tmp_path, delay=0)
    data = {"sources": [source()]}
    candidates = {"report": {"archive_url": CAPTURE}}
    result = archive_sources.apply_candidates(data, candidates, fetcher)
    assert calls == [CAPTURE, RAW_CAPTURE]
    for url, expected_body in [(CAPTURE, WRAPPER_BODY), (RAW_CAPTURE, body)]:
        assert (fetcher.entry(url) / "response.bin").read_bytes() == expected_body
    if wrong_identity:
        assert result["applied"] == []
        assert result["rejected"][0]["raw_attempt"]["status"] == "identity_not_verified"
        assert "archive_url" not in data["sources"][0]
    else:
        assert result == {"applied": ["report"], "rejected": []}
        assert data["sources"][0]["archive_url"] == RAW_CAPTURE
    assert archive_sources.apply_candidates(
        {"sources": [source()]}, candidates, fetcher
    ) == result
    assert calls == [CAPTURE, RAW_CAPTURE]


@pytest.mark.parametrize("offline", [False, True])
def test_apply_cli_defaults_online_with_explicit_offline(
    tmp_path, monkeypatch, capsys, offline
):
    data_path, candidates_path = tmp_path / "data.json", tmp_path / "candidates.json"
    data_path.write_text(json.dumps({"sources": [source()]}))
    candidates_path.write_text(json.dumps({"report": {"archive_url": CAPTURE}}))
    before = data_path.read_bytes()
    monkeypatch.setattr(archive_sources, "ROOT", tmp_path)
    monkeypatch.setattr(archive_sources, "DATA", data_path)
    monkeypatch.setattr(archive_sources, "OUT", candidates_path)
    monkeypatch.setattr(
        sys, "argv", ["archive_sources.py", "--apply"] + (["--offline"] if offline else [])
    )
    calls = network_fixture(monkeypatch, {CAPTURE: (REPORT_BODY, CAPTURE)})
    archive_sources.main()
    output = capsys.readouterr().out
    if offline:
        assert calls == []
        assert data_path.read_bytes() == before
        assert "applied 0 verified archive URLs; rejected 1 candidates" in output
        assert "report not_checked" in output
    else:
        assert calls == [CAPTURE]
        assert json.loads(data_path.read_text())["sources"][0]["archive_url"] == CAPTURE
        assert "applied 1 verified archive URLs; rejected 0 candidates" in output


def test_offline_requires_apply(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["archive_sources.py", "--offline"])
    with pytest.raises(SystemExit, match="2"):
        archive_sources.main()
    assert "--offline requires --apply" in capsys.readouterr().err


@pytest.mark.parametrize("cached,offline", [(False, False), (True, False), (False, True)])
def test_apply_announces_uncached_candidates_before_network(tmp_path, monkeypatch, capsys, cached, offline):
    data_path, candidates_path = tmp_path / "data.json", tmp_path / "candidates.json"
    data_path.write_text(json.dumps({"sources": [
        source(), dict(source(), id="duplicate"),
        dict(source(), id="existing", archive_url=CAPTURE),
        dict(source(), id="no-candidate"),
    ]}))
    candidates_path.write_text(json.dumps({
        "report": {"archive_url": CAPTURE}, "duplicate": {"archive_url": CAPTURE},
        "existing": {"archive_url": "https://example.org/ignored"},
    }))
    monkeypatch.setattr(archive_sources, "ROOT", tmp_path)
    monkeypatch.setattr(archive_sources, "DATA", data_path)
    monkeypatch.setattr(archive_sources, "OUT", candidates_path)
    monkeypatch.setattr(sys, "argv", ["archive_sources.py", "--apply"] + (["--offline"] if offline else []))
    if cached:
        cache(verify_excerpts.Fetcher(tmp_path / "work/source-cache"), CAPTURE, REPORT_BODY)
    announcements = []

    def observe_apply(data, done, fetcher):
        # apply_candidates is the first code that can fetch, including fallbacks.
        announcements.append(capsys.readouterr().out)
        return original_apply(data, done, fetcher)

    original_apply = archive_sources.apply_candidates
    monkeypatch.setattr(archive_sources, "apply_candidates", observe_apply)
    calls = network_fixture(monkeypatch, {CAPTURE: (REPORT_BODY, CAPTURE)})
    archive_sources.main()
    assert announcements == ([""] if offline else [f"retrieving {0 if cached else 1} uncached captures\n"])
    assert calls == ([] if offline or cached else [CAPTURE])
    assert "retrieving" not in capsys.readouterr().out
