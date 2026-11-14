"""Sweep passes: packets, windows and briefs, from a synthetic dataset and store, without network access."""

import csv
import json
from datetime import date

import pytest

from scripts import research_pass, sweep

DATA = {
    "dataset": {"version": "1.0.9", "as_of": "2026-11-14"},
    "jurisdictions": [
        {"id": "MV", "name": "Maldives", "population": 1, "summary": "No qualifying institute identified.",
         "source_ids": ["mv-page"], "check": {"last_checked": "2026-10-16"},
         "related_bodies": [{"name": "National Centre for Information Technology", "role": "It leads the plan."}]},
        {"id": "GB", "name": "United Kingdom", "source_ids": [], "institute_id": "gb-aisi"},
    ],
    "institutes": [{"id": "gb-aisi", "name": "AI Security Institute", "acronym": "AISI", "jurisdiction_id": "GB",
                    "head": {"name": "Director", "source_ids": ["gb-page"]}}],
    "sources": [{"id": "mv-page", "url": "https://example.gov.mv/a", "title": "A", "type": "primary",
                 "excerpts": [{"text": "plan", "supports": []}]},
                {"id": "gb-page", "url": "https://example.gov.uk/b", "title": "B", "type": "primary"}],
}


@pytest.fixture
def store(tmp_path):
    folder = tmp_path / "passes"
    folder.mkdir()
    return folder


@pytest.fixture
def snapshots(tmp_path):
    folder = tmp_path / "sources" / "mv-page"
    folder.mkdir(parents=True)
    (folder / "copy.html").write_bytes("<html><body><main><p>The plan, ２０２６.</p></main></body></html>".encode())
    (folder / "manifest.json").write_text(json.dumps([
        {"path": "mv-page/old.html", "retrieved": "2026-10-01T00:00:00Z", "mime": "text/html"},
        {"path": "mv-page/copy.html", "retrieved": "2026-10-17T00:00:00Z", "mime": "text/html"},
    ]))
    return tmp_path / "sources"


def prepare(store, snapshots, pass_id="2026-11-23-sweep-w48", states=("MV", "GB"), since=None):
    return sweep.prepare_state(store, snapshots, DATA, pass_id, "Maintainer", list(states), since)


def test_packet_holds_record_institutes_sources_and_literal_copy(store, snapshots):
    folder = prepare(store, snapshots)
    mv = json.loads((folder / "packets" / "MV" / "packet.json").read_text())
    assert "population" not in mv["record"] and mv["record"]["summary"].startswith("No qualifying")
    assert mv["entity_seeds"] == ["National Centre for Information Technology"]
    assert [s["id"] for s in mv["sources"]] == ["mv-page"]
    copy = (folder / "packets" / "MV" / mv["sources"][0]["copy"]["file"]).read_text()
    assert "２０２６" in copy  # the verifier's literal view, not NFKC-folded
    assert {q["query_id"] for q in mv["topic_queries"]} == {"topic-en", "topic-local"}
    gb = json.loads((folder / "packets" / "GB" / "packet.json").read_text())
    assert [i["id"] for i in gb["institutes"]] == ["gb-aisi"]
    assert gb["entity_seeds"] == ["AI Security Institute", "AISI"]
    assert gb["sources"][0]["id"] == "gb-page" and gb["sources"][0]["copy"] is None  # cited by the institute, no copy


def test_pass_folder_is_an_open_targeted_pass(store, snapshots):
    folder = prepare(store, snapshots)
    record = json.loads((folder / "pass.json").read_text())
    assert record["type"] == "targeted" and record["status"] == "open" and record["scope"]["states"] == ["MV", "GB"]
    assert (folder / "findings.jsonl").read_text() == ""
    assert json.loads((folder / "sweep.json").read_text())["states"] == {"MV": "pending", "GB": "pending"}
    brief = (folder / "brief.md").read_text()
    assert "Pass 2026-11-23-sweep-w48" in brief and "- MV (Maldives): search window from 2024-11-23" in brief
    assert "$" not in brief.replace("$$", "")


def test_window_is_24_months_then_the_last_complete_sweep(store, snapshots):
    first = prepare(store, snapshots)
    status = json.loads((first / "sweep.json").read_text())
    status["states"] = {"MV": "complete", "GB": "incomplete: blocked"}
    (first / "sweep.json").write_text(json.dumps(status))
    assert sweep.window_start(store, "MV", date(2027, 2, 1)) == date(2025, 2, 1)  # the pass is still open
    record = json.loads((first / "pass.json").read_text())
    (first / "pass.json").write_text(json.dumps({**record, "status": "closed"}))
    assert sweep.window_start(store, "MV", date(2027, 2, 1)) == date(2026, 11, 23)
    assert sweep.window_start(store, "GB", date(2027, 2, 1)) == date(2025, 2, 1)  # incomplete moves nothing
    assert sweep.window_start(store, "GB", date(2027, 2, 1), date(2026, 12, 1)) == date(2026, 12, 1)


def test_refuses_to_overwrite_and_bad_input_before_writing(store, snapshots):
    prepare(store, snapshots)
    with pytest.raises(SystemExit, match="never overwritten"):
        prepare(store, snapshots)
    with pytest.raises(SystemExit, match="unknown or missing states"):
        prepare(store, snapshots, "2026-11-24-sweep-x", states=("XX",))
    with pytest.raises(SystemExit, match="listed twice"):
        prepare(store, snapshots, "2026-11-24-sweep-y", states=("MV", "MV"))
    for bad in ("sweep-w48", "2026-02-30-sweep"):
        with pytest.raises(SystemExit, match="pass ID"):
            prepare(store, snapshots, bad)
        with pytest.raises(SystemExit, match="pass ID"):
            sweep.prepare_global(store, DATA, bad, "M", "2026-01")
    assert sorted(p.name for p in store.iterdir()) == ["2026-11-23-sweep-w48"]


def test_a_failed_preparation_leaves_nothing_and_can_be_retried(store, snapshots):
    manifest = snapshots / "mv-page" / "manifest.json"
    good = manifest.read_text()
    manifest.write_text("{not json")
    with pytest.raises(ValueError):
        prepare(store, snapshots)
    assert list(store.iterdir()) == []  # neither the pass folder nor a partial one
    manifest.write_text(good)
    folder = prepare(store, snapshots)
    assert folder.name == "2026-11-23-sweep-w48" and (folder / "brief.md").is_file()
    assert [p.name for p in store.iterdir()] == ["2026-11-23-sweep-w48"]


def test_a_new_store_is_created_on_first_use(tmp_path, snapshots):
    folder = prepare(tmp_path / "new" / "passes", snapshots)
    assert json.loads((folder / "sweep.json").read_text())["windows"]["MV"] == "2024-11-23"


def test_first_window_is_24_calendar_months():
    assert sweep.years_before(date(2028, 3, 1), 2) == date(2026, 3, 1)
    assert sweep.years_before(date(2028, 2, 29), 2) == date(2026, 2, 28)


def test_packet_saves_the_whole_copy(store, snapshots):
    long_copy = snapshots / "mv-page" / "long.txt"
    long_copy.write_text("x " * 40_000 + "the passage at the end")
    (snapshots / "mv-page" / "manifest.json").write_text(json.dumps(
        [{"path": "mv-page/long.txt", "retrieved": "2026-11-01T00:00:00Z", "mime": "text/plain"}]))
    folder = prepare(store, snapshots, states=("MV",))
    saved = (folder / "packets" / "MV" / "sources" / "mv-page.txt").read_text()
    assert len(saved) > 60_000 and saved.endswith("the passage at the end")


def test_copy_is_whole_and_the_newest_by_time_not_by_text(tmp_path):
    folder = tmp_path / "src"
    folder.mkdir()
    long_text = "x " * 40_000 + "the passage at the end"
    (folder / "early.txt").write_text("early")
    (folder / "late.txt").write_text(long_text)
    (folder / "manifest.json").write_text(json.dumps([
        {"path": "src/late.txt", "retrieved": "2026-10-17T09:00:00Z", "mime": "text/plain"},
        {"path": "src/early.txt", "retrieved": "2026-10-17T10:00:00+02:00", "mime": "text/plain"},  # 08:00 UTC
    ]))
    text, retrieved = sweep.copy_text("src", tmp_path)
    assert retrieved == "2026-10-17T09:00:00Z" and text.endswith("the passage at the end")


def test_global_pass_window_and_fixed_queries(store):
    folder = sweep.prepare_global(store, DATA, "2026-12-01-global-sweep", "Maintainer", "2026-11")
    record = json.loads((folder / "pass.json").read_text())
    assert record["type"] == "news-sweep" and record["scope"]["states"] == ["all"]
    window = json.loads((folder / "sweep.json").read_text())["window"]
    assert window == ["2026-10-02", "2026-11-30"]  # 60 calendar days, both ends included
    assert (date.fromisoformat(window[1]) - date.fromisoformat(window[0])).days + 1 == 60
    brief = (folder / "brief.md").read_text()
    assert brief.count("\n- `") == sum(len(langs) for langs in sweep.GLOBAL_QUERIES.values()) == 15
    assert "from 2026-10-02 to 2026-11-30" in brief and "`NEW`" in brief


def test_prepared_passes_validate_as_open_passes(store, snapshots):
    for folder in (prepare(store, snapshots), sweep.prepare_global(store, DATA, "2026-12-01-global-sweep", "M", "2026-11")):
        record = json.loads((folder / "pass.json").read_text())
        problems = research_pass.validate_pass(folder, DATA)
        assert problems == [], (record["type"], problems)


def test_query_table_covers_every_jurisdiction_once_per_query():
    rows = list(csv.DictReader(sweep.QUERIES.open(encoding="utf-8", newline="")))
    data = json.loads(sweep.DATA.read_text(encoding="utf-8"))
    ids = {j["id"] for j in data["jurisdictions"]}
    assert {r["id"] for r in rows} == ids
    keys = [(r["id"], r["query_id"]) for r in rows]
    assert len(keys) == len(set(keys))
    assert all(r["query_id"] in {"topic-en", "topic-local", "topic-local-2"} and r["query"] for r in rows)


def test_cli_without_a_store_is_not_run(monkeypatch, capsys):
    monkeypatch.delenv("AISI_PASSES", raising=False)
    monkeypatch.delenv("AISI_SNAPSHOTS", raising=False)
    assert sweep.main(["state", "2026-11-23-sweep-w48", "--who", "M", "--states", "MV"]) == sweep.NOT_RUN
    assert "NOT RUN" in capsys.readouterr().err
