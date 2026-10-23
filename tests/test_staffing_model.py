"""Staffing model: shape, evidence gates (attribution, the 2023 relevance cutoff, the
federation-only member code), exports and the all-or-none switch, each gate with its
mutation."""

import copy
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from scripts import agent_files, build, flag_checks
from scripts.display import record_sources
from scripts.method_checks import required_excerpt_pointers
from scripts.references import authored_schema

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = json.loads(build.SCHEMA.read_text())
AUTHORED = authored_schema(SCHEMA)
REAL = json.loads((ROOT / "data/aisi.json").read_text())
SM = "/institutes/0/staffing_model"
SEARCH = {
    "date": "2026-10-21",
    "query": "Synthetic institute staff",
    "language": "en",
    "sources_read": ["https://example.org/institute"],
}


def schema_errors(d, schema=AUTHORED):
    validator = Draft202012Validator(
        schema, format_checker=Draft202012Validator.FORMAT_CHECKER
    )
    return [e.message for e in validator.iter_errors(d)]


def errors(d):
    return build.check_consistency(d)


def unbind(d, prefix):
    for s in d["sources"]:
        for excerpt in s.get("excerpts", []):
            excerpt["supports"] = [
                p for p in excerpt["supports"] if not p.startswith(prefix)
            ]
        s["excerpts"] = [e for e in s.get("excerpts", []) if e["supports"]]


@pytest.fixture
def staffed(dataset):
    """Own staff, bound to an excerpt from a 2026 source that names the institute."""
    d = copy.deepcopy(dataset)
    d["sources"].append(
        {
            "id": "gb-staff",
            "origin": "GB",
            "url": "https://example.org/gb-staff",
            "title": "Synthetic careers",
            "publisher": "Test ministry",
            "type": "primary",
            "language": "en",
            "accessed": "2026-10-17",
            "published": "2026-05",
            "excerpts": [
                {
                    "text": "The Synthetic institute recruits technical staff to its own teams.",
                    "supports": [f"{SM}/code"],
                }
            ],
        }
    )
    d["institutes"][0]["staffing_model"] = {
        "code": "own_staff",
        "source_ids": ["gb-staff"],
    }
    assert schema_errors(d) == []
    assert build.check_consistency(d, require_roster=True, complete=True) == []
    return d


def excerpt(d):
    return d["sources"][-1]["excerpts"][0]


def test_value_needs_a_bound_excerpt_and_a_parent_binding_does_not_count(staffed):
    assert f"{SM}/code" in required_excerpt_pointers(staffed)
    excerpt(staffed)["supports"] = [SM]
    assert f"required claim has no excerpt: {SM}/code" in errors(staffed)


def test_host_body_attribution_is_rejected(staffed):
    excerpt(staffed)["text"] = "The Test ministry recruits technical staff."
    assert any(
        e.startswith(
            "gb-test: staffing_model own_staff: no bound excerpt names the institute"
        )
        for e in errors(staffed)
    )
    staffed["institutes"][0]["staffing_model"]["named_as"] = "the Test ministry"
    assert any(
        "is the host, the reporting-line body or a partner" in e
        for e in errors(staffed)
    )


@pytest.mark.parametrize(
    ("published", "ok"),
    [("2022-12-31", False), ("2022", False), ("2023", True), ("2023-01", True)],
)
def test_relevance_cutoff_2023(staffed, published, ok):
    staffed["sources"][-1]["published"] = published
    message = (
        f"gb-test: staffing_model own_staff: gb-staff was published in {published[:4]}; "
        "only a source dated 2023 or later supports a staffing model (older ones go in the notes)"
    )
    assert (message in errors(staffed)) is not ok
    if ok:
        assert errors(staffed) == []


@pytest.mark.parametrize(
    ("accessed", "ok"), [("2022-12-31", False), ("2023-01-01", True)]
)
def test_undated_page_counts_as_of_the_day_it_was_read(staffed, accessed, ok):
    del staffed["sources"][-1]["published"]
    staffed["sources"][-1]["accessed"] = accessed
    message = (
        "gb-test: staffing_model own_staff: gb-staff was undated and read in 2022; "
        "only a source dated 2023 or later supports a staffing model (older ones go in the notes)"
    )
    assert (message in errors(staffed)) is not ok


def test_an_old_source_beside_a_new_one_still_fails(staffed):
    old = copy.deepcopy(staffed["sources"][-1])
    old.update(id="gb-old", url="https://example.org/gb-old", published="2021")
    staffed["sources"].append(old)
    staffed["institutes"][0]["staffing_model"]["source_ids"].append("gb-old")
    assert any("gb-old was published in 2021" in e for e in errors(staffed))


def test_member_bodies_only_for_a_federation(staffed):
    staffed["institutes"][0]["staffing_model"]["code"] = "member_bodies"
    assert (
        "gb-test: staffing_model member_bodies needs legal_form federation_of_existing_bodies"
        in errors(staffed)
    )
    staffed["institutes"][0]["legal_form"] = "federation_of_existing_bodies"
    assert not any("needs legal_form" in e for e in errors(staffed))


@pytest.mark.parametrize("code", ["core_with_partners", "host_unit", "planned"])
def test_other_values_are_gated_the_same_way(staffed, code):
    staffed["institutes"][0]["staffing_model"]["code"] = code
    assert errors(staffed) == []
    excerpt(staffed)["text"] = "Staff are recruited."
    assert any(
        e.startswith(f"gb-test: staffing_model {code}: no bound excerpt names")
        for e in errors(staffed)
    )


def test_not_published_needs_a_dated_search_log_and_no_binding(staffed):
    staffed["institutes"][0]["staffing_model"] = {"code": "not_published"}
    unbind(staffed, SM)
    staffed["sources"].pop()  # cited by nothing else
    assert any("searches" in e for e in schema_errors(staffed))
    staffed["institutes"][0]["staffing_model"]["searches"] = [dict(SEARCH)]
    assert schema_errors(staffed) == [] and errors(staffed) == []
    assert f"{SM}/code" not in required_excerpt_pointers(staffed)
    staffed["institutes"][0]["staffing_model"]["source_ids"] = ["gb-staff"]
    assert schema_errors(staffed)  # its sources are the notes' only
    staffed["institutes"][0]["staffing_model"]["notes"] = (
        "proposed: a new unit, budget proposal, 2026."
    )
    assert schema_errors(staffed) == []
    staffed["institutes"][0]["staffing_model"]["named_as"] = "the Unit"
    assert schema_errors(staffed)


@pytest.mark.parametrize(
    "value",
    [
        {"code": "own_staff"},  # no sources
        {"code": "secondees", "source_ids": ["gb-staff"]},
        {
            "code": "own_staff",
            "source_ids": ["gb-staff"],
            "searches": [SEARCH],
        },
        {"source_ids": ["gb-staff"]},
    ],
)
def test_shape(staffed, value):
    staffed["institutes"][0]["staffing_model"] = value
    assert schema_errors(staffed)


def test_unknown_source_id(staffed):
    staffed["institutes"][0]["staffing_model"]["source_ids"] = ["missing"]
    assert any(
        "institute gb-test staffing_model: unknown source id 'missing'" in e
        for e in errors(staffed)
    )


def test_vocabulary_must_define_exactly_the_codes(staffed):
    del staffed["dataset"]["vocabularies"]["staffing_model"]["planned"]
    assert (
        "dataset.vocabularies.staffing_model must define exactly own_staff, member_bodies, core_with_partners, host_unit, planned, not_published"
        in errors(staffed)
    )
    del staffed["dataset"]["vocabularies"]["staffing_model"]
    assert schema_errors(staffed)
    assert tuple(REAL["dataset"]["vocabularies"]["staffing_model"]) == (
        flag_checks.STAFFING_CODES
    )


def test_all_or_none(staffed):
    second = copy.deepcopy(staffed["institutes"][0])
    second["id"] = "gb-second"
    del second["staffing_model"]
    staffed["institutes"].append(second)
    assert any(
        e == "gb-second: staffing_model is missing; every institute needs it"
        for e in flag_checks.check_flags(staffed)
    )


def test_published_record_csv_and_api(staffed):
    pub = build.export_dataset(staffed)
    model = pub["institutes"][0]["staffing_model"]
    assert model["citations"]["code"] == ["gb-staff"]
    assert schema_errors(pub, SCHEMA) == []
    record = next(
        v
        for k, v in agent_files.api_files(pub).items()
        if k.endswith("/institutes/gb-test.json")
    )["record"]
    assert record["staffing_model"] == model
    row = build.tables(pub)["institutes"][0]
    assert (row["staffing_model"], row["staffing_model_source_ids"]) == (
        "own_staff",
        "gb-staff",
    )
    staffed["institutes"][0]["staffing_model"] = {
        "code": "not_published",
        "notes": "proposed: a new unit, budget proposal, 2026.",
        "searches": [SEARCH],
    }
    unbind(staffed, SM)
    row = build.tables(build.export_dataset(staffed))["institutes"][0]
    assert (
        row["staffing_model"],
        row["staffing_model_notes"],
        row["staffing_model_searches"],
    ) == (
        "not_published",
        "proposed: a new unit, budget proposal, 2026.",
        "2026-10-21 (en): Synthetic institute staff",
    )
    assert all(build.FIELD_DOCS.get(c) for c in row if c.startswith("staffing_model"))


def test_staffing_only_sources_stay_off_the_page(staffed):
    institute = build.export_dataset(staffed)["institutes"][0]
    assert "gb-staff" not in record_sources(institute)
    assert "gb-staff" not in institute["display"]["source_links"]


def test_method_rule_and_reader_summary_exclusion():
    ids = [r["id"] for r in REAL["dataset"]["method"]]
    assert "staffing-model" in ids
    assert "staffing-model" in REAL["dataset"]["method_no_reader_summary"]


def test_staffing_model_is_required(dataset):
    assert flag_checks.STAFFING_REQUIRED
    assert all("staffing_model" in i for i in REAL["institutes"])
    del dataset["institutes"][0]["staffing_model"]
    assert any(
        "'staffing_model' is a required property" in e for e in schema_errors(dataset)
    )
    assert "gb-test: staffing_model is missing; every institute needs it" in errors(
        dataset
    )
