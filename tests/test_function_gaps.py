"""Output judgements and both directions of the informational function-gap report."""

import copy
import json
import sys
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from conftest import write_json
from scripts import build, check, claim_checks, function_gaps as gaps
from scripts.references import authored_schema, materialise_references

ROOT = Path(__file__).resolve().parents[1]
CLAIM = "claims-" + "1" * 32
VOCABULARY = {
    code: {"label": code, "short": code, "definition": "Synthetic test.",
           "summary": "Explains the function.", "group": "Testing and science"}
    for code in ("research", "model_testing", "evaluation_methods", "international_cooperation", "standards_guidance")
}


def output(**changes):
    return {
        "type": "report", "date": "2024-01", "title": "Synthetic research report", "lineage": "research-report",
        "credit": {"institute_id": "test", "date": "2024-02"}, "source_ids": ["bound"],
        "attribution": "institutional_author", "qualifies_as": "published_work", "qualification_note": "Synthetic research.",
        **changes,
    }


def claim(**changes):
    return {
        "id": CLAIM, "kind": "function", "basis": "activity", "code": "research",
        "text": "It researches AI risks.", "source_ids": ["bound", "listed-only"], **changes,
    }


@pytest.fixture
def fragment():
    return {
        "dataset": {"vocabularies": {"function": copy.deepcopy(VOCABULARY)}},
        "jurisdictions": [],
        "institutes": [{"id": "test", "outputs": [output()], "claims": [claim()]}],
        "sources": [
            {"id": "bound", "excerpts": [{"text": "Research on AI risks.", "supports": [
                ["institutes", {"id": "test"}, "claims", {"id": CLAIM}, "text"],
            ]}]},
            {"id": "listed-only", "excerpts": []},
        ],
    }


@pytest.mark.parametrize("kind, expected", [
    ("joint_testing", {"model_testing", "international_cooperation"}),
    ("evaluation", {"model_testing"}),
    ("tool", {"evaluation_methods"}),
    ("dataset", {"evaluation_methods"}),
    ("guidance", {"standards_guidance"}),
    ("report", {"research"}),
    ("other", set()),
])
def test_unreviewed_type_fallback(fragment, kind, expected):
    institute, = fragment["institutes"]
    institute["outputs"] = [output(type=kind)]
    institute["claims"] = []
    fragment["sources"] = []
    report = gaps.function_gaps(fragment)
    assert {row["code"] for row in report["shown_without_entry"]} == expected
    assert report["counts"] == {
        "credited_outputs": 1, "reviewed": 0, "unreviewed": 1,
        "shown_without_entry": len(expected), "entry_not_shown": 0,
    }
    assert all(detail["review"] == "unreviewed" for row in report["shown_without_entry"] for detail in row["outputs"])


@pytest.mark.parametrize("shows", [[], ["evaluation_methods"], ["evaluation_methods", "model_testing"]])
def test_reviewed_judgement_replaces_type_including_empty(fragment, shows):
    institute, = fragment["institutes"]
    institute["outputs"] = [output(shows=shows)]
    report = gaps.function_gaps(fragment)
    assert {row["code"] for row in report["shown_without_entry"]} == set(shows)
    assert report["counts"]["reviewed"] == 1
    assert report["counts"]["unreviewed"] == 0
    assert report["counts"]["entry_not_shown"] == 1


@pytest.mark.parametrize("entry, expected", [
    (None, 1), (claim(), 0), (claim(expired=True), 1), (claim(basis="mandate"), 1),
    ({"id": CLAIM, "kind": "fact", "text": "A fact.", "source_ids": ["bound"]}, 1),
])
def test_only_live_activity_suppresses_direction_one(fragment, entry, expected):
    institute, = fragment["institutes"]
    institute["claims"] = [entry] if entry else []
    if entry is None:
        fragment["sources"] = []
    assert gaps.function_gaps(fragment)["counts"]["shown_without_entry"] == expected


def test_groups_outputs_once_per_institute_and_code(fragment):
    institute, = fragment["institutes"]
    institute["claims"][0]["basis"] = "mandate"
    institute["outputs"] = [output(date="2025", lineage="later", shows=["research"]), output()]
    report = gaps.function_gaps(fragment)
    row, = report["shown_without_entry"]
    assert row["institute_id"] == "test"
    assert row["code"] == "research"
    assert [(o["date"], o["review"]) for o in row["outputs"]] == [("2024-01", "unreviewed"), ("2025", "reviewed")]
    assert report["counts"]["shown_without_entry"] == 1
    assert report["counts"]["credited_outputs"] == 2


@pytest.mark.parametrize("judgement, expected", [({}, 0), ({"shows": []}, 1), ({"shows": ["model_testing"]}, 1), ({"shows": ["research"]}, 0)])
def test_direction_two_requires_reviewed_output_omitting_code(fragment, judgement, expected):
    institute, = fragment["institutes"]
    institute["outputs"] = [output(**judgement)]
    assert gaps.function_gaps(fragment)["counts"]["entry_not_shown"] == expected


def test_direction_two_uses_only_source_bound_to_text(fragment):
    institute, = fragment["institutes"]
    institute["outputs"] = [output(shows=[], source_ids=["listed-only"])]
    # A binding to another leaf is not the text's source either.
    fragment["sources"][1]["excerpts"] = [{"text": "Other citation.", "supports": [
        ["institutes", {"id": "test"}, "claims", {"id": CLAIM}, "source_ids"],
    ]}]
    assert gaps.function_gaps(fragment)["entry_not_shown"] == []
    institute["outputs"][0]["source_ids"].append("bound")
    row, = gaps.function_gaps(fragment)["entry_not_shown"]
    assert (row["claim_id"], row["source_id"]) == (CLAIM, "bound")


@pytest.mark.parametrize("changes", [{"expired": True}, {"basis": "mandate"}])
def test_direction_two_excludes_expired_and_mandate_entries(fragment, changes):
    institute, = fragment["institutes"]
    institute["outputs"] = [output(shows=[])]
    institute["claims"] = [claim(**changes)]
    assert gaps.function_gaps(fragment)["entry_not_shown"] == []


def test_direction_two_groups_disagreements_but_keeps_each_output_judgement(fragment):
    institute, = fragment["institutes"]
    institute["outputs"] = [output(lineage="b", shows=[]), output(lineage="a", shows=["model_testing"])]
    report = gaps.function_gaps(fragment)
    row, = report["entry_not_shown"]
    assert [o["lineage"] for o in row["outputs"]] == ["a", "b"]
    assert report["counts"]["entry_not_shown"] == 1


@pytest.mark.parametrize("other", [{"shows": ["research"]}, {}])
def test_direction_two_shared_source_needs_every_citing_output_judged_against_the_code(fragment, other):
    # A source backing several outputs is not a disagreement while one of them shows the code or is unjudged.
    institute, = fragment["institutes"]
    institute["outputs"] = [output(lineage="judged", shows=[]), output(lineage="other", **other)]
    assert gaps.function_gaps(fragment)["entry_not_shown"] == []


def test_uncredited_outputs_are_excluded_from_both_directions_and_counts(fragment):
    institute, = fragment["institutes"]
    institute["outputs"] = [output(shows=["model_testing"]), output(lineage="unreviewed")]
    for item in institute["outputs"]:
        item.pop("credit")
    report = gaps.function_gaps(fragment)
    assert all(count == 0 for count in report["counts"].values())


def test_authored_and_published_bindings_match_without_mutation(fragment):
    institute, = fragment["institutes"]
    institute["outputs"] = [output(shows=["model_testing"])]
    before = copy.deepcopy(fragment)
    resolved = materialise_references(fragment)
    assert gaps.function_gaps(fragment) == gaps.function_gaps(resolved)
    assert fragment == before
    assert resolved == materialise_references(before)


@pytest.mark.parametrize("judgement", [{}, {"shows": ["research"]}])
def test_report_research_fallback_never_changes_since_rule(fragment, judgement):
    institute, = fragment["institutes"]
    institute["outputs"] = [output(**judgement)]
    assert "report" not in claim_checks.OUTPUT_FUNCTIONS
    assert claim_checks.held_work_dates(institute) == {}
    rows = claim_checks.function_rows(institute, fragment["dataset"]["vocabularies"], {CLAIM: {"published": "2025-01"}})
    assert next(row for row in rows if row["code"] == "research")["since"] == "2025-01"


@pytest.mark.parametrize("code", ["unknown", "retired"])
def test_validation_rejects_unknown_and_retired_output_codes(fragment, code):
    fragment["dataset"]["vocabularies"]["function"]["retired"] = {"deprecated": True, "replaced_by": ["research"]}
    institute, = fragment["institutes"]
    institute["outputs"] = [output(shows=[code])]
    assert claim_checks.check_claims(materialise_references(fragment)) == [
        f"test output research-report: shows code '{code}' is not a current function code"
    ]


@pytest.mark.parametrize("judgement, valid", [({}, True), ({"shows": []}, True), ({"shows": ["research"]}, True),
                                             ({"shows": ["research", "research"]}, False), ({"shows": "research"}, False),
                                             ({"shows": [1]}, False), ({"shows": None}, False)])
def test_shows_schema_allows_optional_unique_strings_in_authored_and_download(judgement, valid):
    download = json.loads((ROOT / "schema/aisi.schema.json").read_text())
    for schema in (download, authored_schema(download, strict=True)):
        node = schema["properties"]["institutes"]["items"]["properties"]["outputs"]["items"]
        validator = Draft202012Validator({**node, "$defs": schema["$defs"]})
        assert validator.is_valid(output(**judgement)) is valid


def test_provisional_path_and_published_judgement(dataset):
    real = json.loads((ROOT / "data/aisi.json").read_text())
    assert "institutes[].outputs[].shows" in real["dataset"]["provisional"]
    assert claim_checks.schema_node("institutes[].outputs[].shows")
    institute, = dataset["institutes"]
    institute["outputs"][0]["shows"] = ["research"]
    dataset["dataset"]["vocabularies"]["function"] = copy.deepcopy(VOCABULARY)
    assert claim_checks.check_claims(dataset) == []
    published = build.export_dataset(dataset)
    assert published["institutes"][0]["outputs"][0]["shows"] == ["research"]


@pytest.mark.parametrize("as_json", [False, True])
def test_cli_publishes_before_reporting(fragment, monkeypatch, tmp_path, capsys, as_json):
    path = write_json(tmp_path / "aisi.json", fragment)
    monkeypatch.setattr(gaps, "DATA", path)
    published = materialise_references(fragment)
    published["institutes"][0]["claims"][0]["expired"] = True

    def publish(data):
        assert data == fragment
        return published

    monkeypatch.setattr(gaps, "publish", publish)
    monkeypatch.setattr(sys, "argv", ["function_gaps.py", *(["--json"] if as_json else [])])
    gaps.main()
    captured = capsys.readouterr()
    report = gaps.function_gaps(published)
    assert report["counts"]["shown_without_entry"] == 1
    assert captured.err == ""
    assert (json.loads(captured.out) == report) if as_json else (captured.out.splitlines() == gaps.report_lines(report))


def test_warning_lines_never_fail_validation_or_check_report(fragment, dataset, monkeypatch, tmp_path, capsys):
    fragment["institutes"][0]["outputs"] = [output(shows=["model_testing"])]
    report = gaps.function_gaps(fragment)
    lines = gaps.report_lines(report)
    assert report["counts"]["shown_without_entry"] == report["counts"]["entry_not_shown"] == 1
    assert all("\u2014" not in line for line in lines)

    def report_for_published(data):
        assert "functions" in data["institutes"][0]
        return report

    dataset["dataset"]["vocabularies"]["function"] = copy.deepcopy(VOCABULARY)
    write_json(build.DATA, dataset)
    monkeypatch.setattr(build, "function_gaps", report_for_published)
    report_dir = tmp_path / "warnings"
    monkeypatch.setattr(sys, "argv", ["build.py", "--check", "--report-dir", str(report_dir)])
    build.main()
    captured = capsys.readouterr()
    assert "is valid" in captured.out
    warning_text = (report_dir / build.RELEASE_WARNINGS).read_text()
    assert all(f"warning: {line}" in captured.err and f"- {line}" in warning_text for line in lines)

    def validated(ctx, step, command):
        directory = Path(command[command.index("--report-dir") + 1])
        build.write_release_warnings(lines, directory)
        return check.Result(step, "PASS")

    monkeypatch.setattr(check, "command_step", validated)
    ctx = check.Context(tmp_path, tmp_path, tmp_path, tmp_path / "check-report", "HEAD", False, tmp_path / "dist", tmp_path / "cache")
    assert check.step_validate(ctx).status == "PASS"
    assert all(line in ctx.sections["warnings"] for line in lines)
