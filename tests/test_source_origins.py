import copy
import json
import sys

from jsonschema import Draft202012Validator

from scripts import build
from conftest import write_json

from scripts.source_origins import (
    source_origin_report,
    write_source_origin_report,
)


def fixture():
    return {
        "jurisdictions": [
            {
                "id": "CA",
                "source_ids": ["domestic"],
                "timeline": [
                    {"source_ids": ["foreign"], "description": "Foreign account."}
                ],
                "population": {"source_ids": ["foreign"]},
            }
        ],
        "institutes": [],
        "sources": [
            {"id": "domestic", "origin": "CA", "publisher": "Canada"},
            {"id": "foreign", "origin": "GB", "publisher": "UK"},
        ],
    }


def test_nested_claim_cannot_borrow_parents_domestic_citation():
    d = fixture()
    r = source_origin_report(d)
    assert [x["pointer"] for x in r["claim_gaps"]] == ["/jurisdictions/0/timeline/0"]
    assert [x["pointer"] for x in r["contextual_gaps"]] == [
        "/jurisdictions/0/population"
    ]
    d["jurisdictions"][0]["timeline"][0]["source_ids"].append("domestic")
    assert source_origin_report(d)["claim_gaps"] == []


def test_institute_inherits_jurisdiction_but_not_parent_citations():
    d = fixture()
    d["institutes"] = [
        {
            "id": "ca-test",
            "jurisdiction_id": "CA",
            "source_ids": ["domestic"],
            "outputs": [{"source_ids": ["foreign"]}],
        }
    ]
    assert (
        source_origin_report(d)["claim_gaps"][-1]["pointer"]
        == "/institutes/0/outputs/0"
    )


def test_published_metadata_and_activity_do_not_duplicate_primary_claims():
    d = fixture()
    d["jurisdictions"][0]["timeline"].append(
        {"event": "first_output", "source_ids": ["foreign"]}
    )
    d["institutes"] = [{
        "id": "ca-test", "jurisdiction_id": "CA", "source_ids": ["domestic"],
        "outputs": [{"source_ids": ["foreign"]}],
        "operating": [{"source_ids": ["foreign"]}],
        "display": {"developments": [{"source_ids": ["foreign"]}]},
        "citations": {"source_ids": ["foreign"]},
    }]
    assert [row["pointer"] for row in source_origin_report(d)["claim_gaps"]] == [
        "/jurisdictions/0/timeline/0", "/institutes/0/outputs/0",
    ]


def test_unknown_and_international_origins_remain_reviewable():
    d = fixture()
    d["sources"][1]["origin"] = None
    r = source_origin_report(d)
    assert r["unknown_origins"] == ["foreign"]
    assert r["claim_gaps"][0]["unknown_origin_ids"] == ["foreign"]
    d["sources"][1]["origin"] = "OECD"
    assert source_origin_report(d)["claim_gaps"][0]["origins"] == ["OECD"]


def test_report_is_informational_and_does_not_change_data(tmp_path):
    d = fixture()
    before = copy.deepcopy(d)
    r = write_source_origin_report(d, tmp_path)
    assert d == before
    assert r["claim_gaps"]
    assert (tmp_path / "source-origins.json").is_file()
    assert "/jurisdictions/0/timeline/0" in (tmp_path / "source-origins.md").read_text()


def test_governance_is_a_claim_but_old_independence_checks_are_context():
    d = fixture()
    d["jurisdictions"][0]["check"] = {
        "governance_source_ids": ["foreign"],
        "independent_source_ids": ["foreign"],
        "searches": [{"source_ids": ["foreign"]}],
    }
    r = source_origin_report(d)
    fields = {x["citation_field"] for x in r["claim_gaps"]}
    assert "governance_source_ids" in fields
    assert "independent_source_ids" not in fields
    assert any(x["context"] == "historical_independence" for x in r["contextual_gaps"])
    assert any(x["pointer"].endswith("/check/searches/0") for x in r["contextual_gaps"])


def test_missing_and_unknown_origins_rejected(dataset):
    dataset["sources"][0]["origin"] = "ZZ"
    assert any(
        "unknown source origin ZZ" in e for e in build.check_consistency(dataset)
    )
    dataset["sources"][0].pop("origin")
    validator = Draft202012Validator(json.loads(build.SCHEMA.read_text()))
    assert any(
        e.validator == "required" and "origin" in e.message
        for e in validator.iter_errors(dataset)
    )


def test_source_origin_csv_keeps_code_and_exports_unknown_as_blank(dataset):
    assert build.tables(dataset)["sources"][0]["origin"] == "GB"
    dataset["sources"][0]["origin"] = None
    assert build.tables(dataset)["sources"][0]["origin"] == ""


def test_eu_origin_is_valid_in_a_fragment_without_an_eu_record(dataset):
    dataset["sources"][0]["origin"] = "EU"
    assert not any("source origin" in e for e in build.check_consistency(dataset))


def test_cli_writes_report_only_to_a_given_directory(
    dataset, monkeypatch, tmp_path_factory, capsys
):
    write_json(build.DATA, dataset)
    before = sorted(p for p in build.ROOT.rglob("*"))
    monkeypatch.setattr(sys, "argv", ["build.py", "--check"])
    build.main()
    assert sorted(p for p in build.ROOT.rglob("*")) == before
    assert "pass --report-dir" in capsys.readouterr().err
    other = tmp_path_factory.mktemp("chosen-report")
    monkeypatch.setattr(
        sys, "argv", ["build.py", "--check", "--report-dir", str(other)]
    )
    build.main()
    assert json.loads(
        (other / "source-origins.json").read_text()
    ) == source_origin_report(dataset)


def test_unwritable_report_does_not_fail_valid_dataset(
    dataset, monkeypatch, tmp_path, capsys
):
    write_json(build.DATA, dataset)
    blocked = tmp_path / "not-a-directory"
    blocked.write_text("Existing file")
    monkeypatch.setattr(
        sys, "argv", ["build.py", "--check", "--report-dir", str(blocked)]
    )
    build.main()
    captured = capsys.readouterr()
    assert "warning: could not write informational" in captured.err
    assert "is valid" in captured.out
    assert blocked.read_text() == "Existing file"
