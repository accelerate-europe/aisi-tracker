"""Development versions: release boundaries, work dates and unchanged stable behaviour."""

import copy
import datetime as dt
import json
import re
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from jsonschema import Draft202012Validator

from conftest import write_json
from scripts import agent_files, build, check, check_page, published, release_checks, release_tag, rereads, versioning, zenodo_metadata

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("version, development", [("1.1.0-dev", True), ("1.1.0", False),
    ("1.1.0-rc.1", None), ("1.1.0-dev+abc", None), ("1.1.0-next", None), ("1.1", None), ("v1.1.0", None)])
def test_version_state(version, development):
    schema = json.loads((ROOT / "schema/aisi.schema.json").read_text())["properties"]["dataset"]["properties"]["version"]
    if development is None:
        with pytest.raises(ValueError, match=re.escape(version)):
            versioning.parse_version(version)
        assert list(Draft202012Validator(schema).iter_errors(version))
    else:
        assert versioning.parse_version(version).development is development
        assert versioning.parse_version(version).number == (1, 1, 0)
        Draft202012Validator(schema).validate(version)


def test_release_refuses_dev_version(dataset, monkeypatch, capsys):
    dataset["dataset"]["version"] = "1.1.0-dev"
    build.DATA.parent.mkdir(parents=True, exist_ok=True)
    build.DATA.write_text(json.dumps(dataset))
    monkeypatch.setattr(sys, "argv", ["build.py", "--release", "--check"])
    with pytest.raises(SystemExit):
        build.main()
    assert "development version 1.1.0-dev cannot be released" in capsys.readouterr().err
    calls = []

    def committed(root, *args, **kwargs):
        calls.append(args)
        assert args == ("show", "HEAD:data/aisi.json")
        return SimpleNamespace(stdout=json.dumps(dataset))

    monkeypatch.setattr(release_tag, "git", committed)
    with pytest.raises(SystemExit):
        release_tag.main([])
    assert "1.1.0-dev" in capsys.readouterr().err
    assert calls == [("show", "HEAD:data/aisi.json")]
    assert release_checks.check_release_tag(dataset, "{}", [], None, "head")
    for args in ([], ["--apply"], ["--version", "1.0.0"]):
        assert zenodo_metadata.main(args, data_path=build.DATA, request=lambda *a: pytest.fail("development contacted Zenodo")) == 2
        assert "1.1.0-dev" in capsys.readouterr().err
    versioning.require_release("1.1.0")


@pytest.mark.parametrize("version, tag, ref, refused", [
    ("1.1.0", None, "refs/heads/next", False),
    ("1.1.0", "head", "refs/heads/next", False),
    ("1.1.0", "earlier", "refs/heads/next", True),
    ("1.1.0", "earlier", "refs/heads/page-only-fix", True),
    ("1.1.0", "earlier", "refs/pull/123/merge", True),
    ("1.1.0", "earlier", "refs/heads/main", False),
    ("1.1.0-dev", "earlier", "refs/heads/next", False),
])
def test_released_version_guard(tmp_path, monkeypatch, version, tag, ref, refused):
    errors = versioning.check_released_version(version, ref, tag, "head")
    assert bool(errors) is refused
    if refused:
        assert "set 1.1.1-dev" in errors[0]
    # The checks job's validate step uses the full checkout, never the throwaway snapshot's tags.
    (tmp_path / "data").mkdir()
    (tmp_path / "data/aisi.json").write_text(json.dumps({"dataset": {"version": version}}))
    repo = tmp_path / "checkout"
    calls = []

    def git(root, *args, **kwargs):
        assert root == repo
        calls.append(args)
        return "head" if args == ("rev-parse", "HEAD") else tag or ""

    monkeypatch.setattr(check, "git", git)
    monkeypatch.setattr(check, "command_step", lambda *a: check.Result("validate", "PASS"))
    ctx = check.Context(repo, tmp_path, tmp_path, tmp_path, None, False, tmp_path, tmp_path, version_ref=ref)
    result = check.step_validate(ctx)
    assert result.status == ("FAIL" if refused else "PASS")
    assert all("diff" not in args for args in calls)  # D4a has no unchanged-data exception.
    workflow = yaml.safe_load((ROOT / ".github/workflows/pages.yml").read_text())
    steps = workflow["jobs"]["checks"]["steps"]
    assert any('--version-ref "$REF"' in step.get("run", "") and "scripts/check.py --only fast" in step["run"] for step in steps)
    assert any(step.get("with", {}).get("fetch-depth") == 0 for step in steps)


def minimal(version="1.1.0-dev"):
    return {"dataset": {"version": version, "as_of": "2026-11-14"},
            "jurisdictions": [{"id": "GB", "check": {}}], "institutes": [{"id": "gb", "check": {}}],
            "sources": [{"id": "s", "excerpts": [{}]}]}


def test_effective_date_stable(dataset):
    raw = copy.deepcopy(dataset)
    raw["sources"][0]["accessed"] = "2099-01-01"
    states = {"sources": {"s": {"q": {"last_attempt": "2099-02-01"}}}}
    assert versioning.effective_date(raw, states) == "2026-10-17"
    # The shared publisher has exactly the same result when handed the stable authored date explicitly.
    assert published.publish(dataset, strict=False, rereads={}) == published.publish(
        dataset, strict=False, rereads={}, as_of="2026-10-17")


WORK_FIELDS = [
    *(f"{collection}/check/{field}" for collection in ("jurisdictions", "institutes") for field in ("last_checked", "partial", "searches")),
    *(f"institutes/{field}" for field in ("model_access", "testing_capacity", "staffing_model")),
    "sources/accessed", "sources/excerpts", "sources/snapshot", "sources/instrument_status_checked",
    "rereads/last_found", "rereads/since", "rereads/last_attempt",
]


def set_work_date(data, states, field, day):
    collection, *parts = field.split("/")
    if collection == "rereads":
        states["sources"] = {"s": {"q": {parts[0]: day}}}
        return
    record = data[collection][0]
    if parts[0] == "check":
        record["check"][parts[1]] = day if parts[1] == "last_checked" else [{"date": day}]
    elif parts[0] in ("model_access", "testing_capacity", "staffing_model"):
        record[parts[0]] = {"searches": [{"date": day}]}
    elif parts[0] == "snapshot":
        record["snapshot"] = {"retrieved": f"{day}T23:30:00-02:00"}
    elif parts[0] == "excerpts":
        record["excerpts"][0]["accessed"] = day
    else:
        record[parts[0]] = day


@pytest.mark.parametrize("field", WORK_FIELDS)
def test_effective_date_development(field):
    data, states = minimal(), {}
    today = dt.date(2026, 11, 30)
    data["institutes"][0]["funding"] = [{"annual_period": {"start": "2098-01-01", "end": "2099-12-31"}}]
    data["jurisdictions"][0]["timeline"] = [{"date": "2099-01-01"}]
    data["sources"][0]["published"] = "2099-02-01"
    assert versioning.effective_date(data, states, today=today) == "2026-11-14"
    set_work_date(data, states, field, "2026-11-16")
    expected = "2026-11-17" if field == "sources/snapshot" else "2026-11-16"
    assert versioning.effective_date(data, states, today=today) == expected
    assert data["dataset"]["as_of"] == "2026-11-14"
    set_work_date(data, states, field, "2026-12-01")
    with pytest.raises(ValueError, match="after today"):
        versioning.effective_date(data, states, today=today)


@pytest.mark.parametrize("version, expected", [("1.1.0-dev", "2026-11-17"), ("1.1.0", "2026-11-14")])
def test_effective_date_checked_to_alone(version, expected):
    data = minimal(version)
    states = {"checked_to": "2026-11-17", "sources": {}}
    assert versioning.effective_date(data, states, today=dt.date(2026, 11, 17)) == expected
    assert data["dataset"]["as_of"] == "2026-11-14"


def test_effective_date_refuses_future_checked_to():
    states = {"checked_to": "2026-11-18", "sources": {}}
    with pytest.raises(ValueError, match=r"rereads/checked_to: 2026-11-18 is after today \(2026-11-17\)"):
        versioning.effective_date(minimal(), states, today=dt.date(2026, 11, 17))


def test_effective_date_year_boundary(dataset, monkeypatch):
    monkeypatch.setattr(release_checks, "today", lambda: dt.date(2027, 1, 5))
    dataset["dataset"].update(version="1.1.0-dev", as_of="2026-12-20")
    dataset["sources"][0]["accessed"] = "2027-01-05"
    funding = dataset["institutes"][0]["funding"]
    funding[0].update(annual_amount=100, annual_basis="Stated annual budget.")
    funding.append({**copy.deepcopy(funding[0]), "id": "next-budget", "annual_period": {"start": "2027-01-01", "end": "2027-12-31"}})
    facts = published.publish(dataset, strict=False, rereads={})
    assert facts["dataset"]["as_of"] == "2027-01-05"
    assert published.comparable_funding(facts["institutes"][0]["funding"], facts["dataset"]["as_of"]) == 1
    assert any("period must be calendar 2026" in error for error in build.check_consistency(dataset, strict=False))
    for rate in dataset["dataset"]["exchange_rates"]:
        rate["period"] = {"start": "2026-01-01", "end": "2026-12-31"}
    assert not any("exchange_rates:" in error for error in build.check_consistency(dataset, strict=False))
    stable = copy.deepcopy(dataset)
    stable["dataset"]["version"] = "1.1.0"
    stable_facts = published.publish(stable, strict=False, rereads={})
    assert published.comparable_funding(stable_facts["institutes"][0]["funding"], "2026-12-20") == 0
    assert any("period must be calendar 2025" in error for error in build.check_consistency(stable, strict=False))


def test_effective_date_freshness_boundary(dataset, monkeypatch):
    monkeypatch.setattr(release_checks, "today", lambda: dt.date(2026, 11, 2))
    dataset["dataset"].update(version="1.1.0-dev", as_of="2026-11-01")
    claim = {"id": "test-claim", "kind": "fact", "text": "Synthetic institute", "source_ids": ["gb-source"]}
    dataset["jurisdictions"][0]["claims"] = [claim]
    dataset["sources"][0]["excerpts"][0]["supports"].append("/jurisdictions/0/claims/0/text")
    qid = rereads.quote_id("Synthetic institute")
    states = {"sources": {"gb-source": {qid: {"state": "quote_gone", "last_found": "2026-09-30", "since": "2026-10-01", "last_attempt": "2026-11-01"}}}}
    before = published.publish(dataset, strict=False, rereads=states)
    assert before["dataset"]["as_of"] == "2026-11-01"
    assert before["jurisdictions"][0]["claims"][0]["expired"] is False
    states["sources"]["gb-source"][qid]["last_attempt"] = "2026-11-02"
    after = published.publish(dataset, strict=False, rereads=states)
    assert after["dataset"]["as_of"] == "2026-11-02"
    assert after["jurisdictions"][0]["claims"][0]["expired"] is True
    assert dataset["dataset"]["as_of"] == "2026-11-01"


def test_changelog_checks_development(dataset, monkeypatch, capsys):
    dataset["dataset"]["version"] = "1.1.0-dev"
    assert release_checks.check_metadata(dataset) == []
    assert release_checks.check_changelog_entry(dataset) == []
    write_json(build.DATA, dataset)
    monkeypatch.setattr(sys, "argv", ["build.py", "--release-wording", "--update-readme"])
    build.main()
    capsys.readouterr()
    broken = copy.deepcopy(dataset)
    broken["changelog"][-1]["summary"] = "Draft: unfinished."
    assert any("still a draft" in error for error in release_checks.check_changelog_entry(broken))
    broken = copy.deepcopy(dataset)
    broken["dataset"]["as_of"] = "2026-10-18"
    assert "dataset.as_of must equal the last changelog date" in release_checks.check_metadata(broken)
    broken = copy.deepcopy(dataset)
    broken["dataset"]["citation"] = broken["dataset"]["citation"].replace("1.0.0", "1.1.0-dev")
    assert any("dataset.citation does not name" in error for error in release_checks.check_metadata(broken))
    broken = copy.deepcopy(dataset)
    broken["dataset"]["version"] = "1.0.0-dev"
    assert any("less than" in error for error in release_checks.check_metadata(broken))
    broken["changelog"][-1]["version"] = "0.9.0-dev"
    assert any("changelog:" in error for error in release_checks.check_metadata(broken))
    dataset["dataset"]["version"] = "1.1.0"
    assert release_checks.check_changelog_entry(dataset) == ["changelog: no entry for dataset.version 1.1.0"]


def test_release_metadata_on_development(dataset, build_env, monkeypatch):
    cff, zenodo = agent_files.citation_cff(dataset), agent_files.zenodo_json(dataset)
    original_citation = dataset["dataset"]["citation"]
    dataset["dataset"].update(version="1.1.0-dev", definition="Current definition.")
    dataset["sources"][0]["accessed"] = "2026-10-18"
    write_json(build.DATA, dataset)
    write_json(build.DATA.with_name("rereads.json"), {"checked_to": "2026-10-19", "sources": {"gb-source": {"q": {"last_attempt": "2026-10-19"}}}})
    before = build.DATA.read_bytes()
    calls = []
    compute = build.effective_date

    def effective(data, states):
        calls.append(True)
        return compute(data, states)

    monkeypatch.setattr(build, "effective_date", effective)
    monkeypatch.setattr(published, "effective_date", lambda *a: pytest.fail("cutoff computed twice"))
    monkeypatch.setattr(sys, "argv", ["build.py", "--update-readme"])
    build.main()
    assert calls == [True]
    assert build.DATA.read_bytes() == before
    assert (build_env / "CITATION.cff").read_text() == cff
    assert (build_env / ".zenodo.json").read_text() == zenodo
    readme = (build_env / "README.md").read_text()
    citation = readme.split("<!-- citation:start -->")[1].split("<!-- citation:end -->")[0]
    assert citation.strip() == original_citation
    assert "Version 1.1.0-dev, data as of 19 October 2026: 1 jurisdictions, 1 AISIs" in readme
    assert "Current definition." in readme
    assert versioning.release_metadata(dataset)["as_of"] == "2026-10-17"


@pytest.mark.parametrize("version, expected", [("1.1.0", "2026-11-14"), ("1.1.0-dev", "2026-11-17")])
def test_page_updated_line(version, expected):
    data = {"dataset": {"version": version, "as_of": "2026-11-17"}, "changelog": [{"version": "1.0.10", "date": "2026-11-14"}]}
    assert check_page.updated_date(data) == expected
    script = (ROOT / "site/index.html").read_text()
    expression = re.search(r"updated.appendChild\(dated\((.*?)\)\);", script).group(1)
    code = f"const m = {json.dumps(data['dataset'])}, last = {json.dumps(data['changelog'][-1])}; process.stdout.write({expression});"
    assert subprocess.run(["node", "-e", code], capture_output=True, text=True, check=True).stdout == expected


def test_zenodo_reads_pinned_release(monkeypatch, capsys):
    calls = []

    def committed(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(stdout=json.dumps({"dataset": {"version": "1.1.0-dev"}}))

    monkeypatch.setattr(zenodo_metadata.subprocess, "run", committed)
    assert zenodo_metadata.main([], request=lambda *a: pytest.fail("development contacted Zenodo")) == 2
    assert calls[0][-2:] == ["show", "HEAD:data/aisi.json"]
    assert "1.1.0-dev" in capsys.readouterr().err
