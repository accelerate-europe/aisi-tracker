"""Testing capacity and model access: shape, evidence gates, the derived access value and
exports, each gate with its mutation."""

import ast
import copy
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from scripts import agent_files, build, flag_checks
from scripts.display import record_sources
from scripts.method_checks import required_excerpt_pointers
from scripts.published import materialise_facts
from scripts.references import IDENTITY_KEYS, authored_schema, stable_selector

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = json.loads(build.SCHEMA.read_text())
AUTHORED = authored_schema(
    SCHEMA
)  # the fixture's legacy pointers need the non-strict contract
REAL = json.loads((ROOT / "data/aisi.json").read_text())
SEARCH = {
    "date": "2026-10-21",
    "query": "Synthetic institute model access",
    "language": "en",
    "sources_read": ["https://example.org/institute"],
}
MA = "/institutes/0/model_access"
TC = "/institutes/0/testing_capacity"


def schema_errors(d, schema=AUTHORED):
    validator = Draft202012Validator(
        schema, format_checker=Draft202012Validator.FORMAT_CHECKER
    )
    return [e.message for e in validator.iter_errors(d)]


def source(sid, title, publisher, **extra):
    return {
        "id": sid,
        "origin": "GB",
        "url": f"https://example.org/{sid}",
        "title": title,
        "publisher": publisher,
        "type": "primary",
        "language": "en",
        "accessed": "2026-10-17",
        "excerpts": [],
        **extra,
    }


def bind(d, sid, text, *pointers):
    next(s for s in d["sources"] if s["id"] == sid)["excerpts"].append(
        {"text": text, "supports": list(pointers)}
    )


def unbind(d, prefix):
    """Drop bindings under a replaced value, as an editor must; a stale one fails to resolve."""
    for s in d["sources"]:
        for excerpt in s.get("excerpts", []):
            excerpt["supports"] = [
                p for p in excerpt["supports"] if not p.startswith(prefix)
            ]
        s["excerpts"] = [e for e in s.get("excerpts", []) if e["supports"]]


def errors(d):
    return build.check_consistency(d)


@pytest.fixture
def flagged(dataset):
    """Both testing modes, and an access event, a legal power and a developer agreement together."""
    d = copy.deepcopy(dataset)
    d["sources"] += [
        source("gb-act", "Synthetic Act 2024", "Synthetic parliament", legal_text=True),
        source("gb-epoch", "Notable AI models", "Epoch AI"),
    ]
    i = d["institutes"][0]
    i["testing_capacity"] = {
        "modes": [
            {"code": "in_house", "source_ids": ["gb-source"]},
            {"code": "via_other_bodies", "source_ids": ["gb-source"]},
        ]
    }
    i["model_access"] = {
        "access_events": [
            {
                "id": "event-x",
                "model": "Model X",
                "developer": "Dev A",
                "date": "2026-03",
                "kind": "pre_release",
                "epoch_basis": "frontier_model",
                "epoch_model": "Model X",
                "epoch_source_id": "gb-epoch",
                "source_ids": ["gb-source", "gb-epoch"],
            }
        ],
        "legal_power": {
            "act": "Synthetic Act 2024",
            "provision": "Article 9",
            "conditions": "Only for models with systemic risk.",
            "source_ids": ["gb-act"],
        },
        "developer_agreements": [
            {
                "id": "agreement-x",
                "agreement_id": "test-agreement",
                "epoch_model": "Model X",
                "epoch_source_id": "gb-epoch",
                "source_ids": ["gb-source", "gb-epoch"],
            }
        ],
    }
    bind(
        d,
        "gb-source",
        "The Synthetic institute evaluates models with its own staff.",
        f"{TC}/modes/0/code",
    )
    bind(
        d,
        "gb-source",
        "A partner lab tests models for the Synthetic institute.",
        f"{TC}/modes/1/code",
    )
    bind(
        d,
        "gb-source",
        "Dev A gave the Synthetic institute pre-release access to Model X in March 2026.",
        *(f"{MA}/access_events/0/{k}" for k in ("model", "developer", "date", "kind")),
    )
    bind(
        d,
        "gb-source",
        "Under the agreement Dev A gives the Synthetic institute access to its models.",
        f"{MA}/developer_agreements/0/agreement_id",
    )
    bind(
        d,
        "gb-act",
        "The Synthetic institute may require a provider to give it access to a model with systemic risk.",
        f"{MA}/legal_power/provision",
        f"{MA}/legal_power/conditions",
    )
    bind(
        d,
        "gb-epoch",
        "Model X | Dev A | frontier model: yes",
        f"{MA}/access_events/0/epoch_model",
        f"{MA}/developer_agreements/0/epoch_model",
    )
    assert schema_errors(d) == []
    assert build.check_consistency(d, require_roster=True, complete=True) == []
    return d


def published(d):
    return build.export_dataset(d)


def test_flags_are_required(dataset):
    assert schema_errors(dataset) == [] and errors(dataset) == []
    for field in flag_checks.FIELDS:
        d = copy.deepcopy(dataset)
        del d["institutes"][0][field]
        assert any(f"'{field}' is a required property" in e for e in schema_errors(d))
        assert any("added to every institute together" in e for e in errors(d))


def test_every_value_needs_its_own_excerpt(flagged):
    assert {
        f"{TC}/modes/0/code",
        f"{TC}/modes/1/code",
        *(
            f"{MA}/access_events/0/{k}"
            for k in ("model", "developer", "date", "kind", "epoch_model")
        ),
        f"{MA}/legal_power/provision",
        f"{MA}/legal_power/conditions",
        f"{MA}/developer_agreements/0/agreement_id",
        f"{MA}/developer_agreements/0/epoch_model",
    } <= required_excerpt_pointers(flagged)


@pytest.mark.parametrize(
    "pointer",
    [
        f"{TC}/modes/1/code",
        f"{MA}/access_events/0/kind",
        f"{MA}/access_events/0/epoch_model",
        f"{MA}/legal_power/provision",
        f"{MA}/developer_agreements/0/agreement_id",
    ],
)
def test_missing_excerpt_fails_and_a_parent_binding_does_not_count(flagged, pointer):
    parent = pointer.rsplit("/", 1)[0]
    for s in flagged["sources"]:
        for excerpt in s.get("excerpts", []):
            excerpt["supports"] = [
                parent if p == pointer else p for p in excerpt["supports"]
            ]
    assert f"required claim has no excerpt: {pointer}" in errors(flagged)


def test_planned_testing_needs_sources_and_a_bound_excerpt(flagged):
    flagged["institutes"][0]["testing_capacity"] = {
        "state": "planned",
        "source_ids": ["gb-source"],
        "notes": "Adopted government action plan.",
    }
    unbind(flagged, f"{TC}/modes")
    assert schema_errors(flagged) == []
    assert f"required claim has no excerpt: {TC}/state" in errors(flagged)
    bind(
        flagged,
        "gb-source",
        "The Synthetic institute will build a testing team.",
        f"{TC}/state",
    )
    assert errors(flagged) == []
    del flagged["institutes"][0]["testing_capacity"]["source_ids"]
    assert schema_errors(flagged)


def test_parent_body_attribution_is_rejected(flagged):
    flagged["sources"][0]["publisher"] = "Test ministry"
    excerpt = next(
        e
        for e in flagged["sources"][0]["excerpts"]
        if f"{TC}/modes/0/code" in e["supports"]
    )
    excerpt["text"] = "The Test ministry evaluates AI models with its own staff."
    assert any(
        e.startswith(
            "gb-test: testing_capacity in_house: no bound excerpt names the institute"
        )
        for e in errors(flagged)
    )


def test_access_event_attribution_is_checked(flagged):
    excerpt = next(
        e
        for e in flagged["sources"][0]["excerpts"]
        if f"{MA}/access_events/0/kind" in e["supports"]
    )
    excerpt["text"] = (
        "Dev A gave the Test ministry pre-release access to Model X in March 2026."
    )
    assert any(
        e.startswith(
            "gb-test: model_access event event-x: no bound excerpt names the institute"
        )
        for e in errors(flagged)
    )


def test_own_publication_may_speak_in_the_first_person(flagged):
    flagged["sources"][0]["publisher"] = "Synthetic institute"
    excerpt = next(
        e
        for e in flagged["sources"][0]["excerpts"]
        if f"{TC}/modes/0/code" in e["supports"]
    )
    excerpt["text"] = "We rigorously test leading AI systems."
    assert errors(flagged) == []


def test_named_as_records_another_name_and_never_a_parent(flagged):
    excerpt = next(
        e
        for e in flagged["sources"][0]["excerpts"]
        if f"{TC}/modes/0/code" in e["supports"]
    )
    excerpt["text"] = "The Lab evaluates models with its own staff."
    assert any(
        e.startswith(
            "gb-test: testing_capacity in_house: no bound excerpt names the institute"
        )
        for e in errors(flagged)
    )
    mode = flagged["institutes"][0]["testing_capacity"]["modes"][0]
    mode["named_as"] = "the Lab"
    assert errors(flagged) == []
    mode["named_as"] = "the Workshop"
    assert any(
        e.startswith(
            "gb-test: testing_capacity in_house: named_as 'the Workshop' does not name this institute"
        )
        for e in errors(flagged)
    )
    excerpt["text"] = "The Test ministry evaluates models with its own staff."
    mode["named_as"] = "the Test ministry"
    assert any(
        "is the host, the reporting-line body or a partner" in e
        for e in errors(flagged)
    )


def real_forms(iid):
    forms = flag_checks.institute_forms(REAL["institutes"])
    own, unshared = forms[iid]
    return (
        own,
        unshared,
        {f for oid, (fs, _) in forms.items() if oid != iid for f in fs} - own,
    )


def test_name_forms_on_the_real_institutes():
    own, unshared, others = real_forms("ca-caisi")
    assert "caisi" in own and "caisi" not in unshared  # shared with the former US name
    own, unshared, others = real_forms("gb-aisi")
    assert {
        "aisi",
        "ai security institute",
    } <= own - unshared  # shared with India and Spain
    assert not flag_checks.names_institute(
        "the Singapore AI Safety Institute ran tests", own, others
    )
    assert flag_checks.names_institute(
        "The AI Security Institute tests models", own, others
    )
    assert not flag_checks.names_institute(
        "The AI Security Institute tests models", unshared, others
    )
    own, unshared, others = real_forms("us-caissi")
    assert flag_checks.names_institute("CAISSI evaluates models", unshared, others)
    own, unshared, others = real_forms("kr-aisi")
    assert flag_checks.names_institute(
        "인공지능안전연구소는 평가를 수행했다", unshared, others
    )
    assert not flag_checks.names_institute(
        "AI안전연구소", own, others
    )  # an informal alias needs named_as


def minimal(origin, publisher="Someone"):
    absent = {
        "testing_capacity": {"state": "not_published", "searches": [SEARCH]},
        "model_access": {"searches": [SEARCH]},
        "staffing_model": {"code": "not_published", "searches": [SEARCH]},
    }
    return {
        "dataset": {"vocabularies": {}, "as_of": "2026-10-17"},
        "institutes": [
            {
                "id": "gb-x",
                "jurisdiction_id": "GB",
                "name": "Shared Name Institute",
                "short_name": "GB SNI",
                "host": "Ministry A",
                **absent,
                "testing_capacity": {
                    "modes": [{"code": "in_house", "source_ids": ["s"]}]
                },
            },
            {
                "id": "us-x",
                "jurisdiction_id": "US",
                "name": "Shared Name Institute",
                "short_name": "US SNI",
                "host": "Ministry B",
                **absent,
            },
        ],
        "sources": [
            {
                "id": "s",
                "origin": origin,
                "publisher": publisher,
                "excerpts": [
                    {
                        "text": "We test models. The Shared Name Institute ran evaluations.",
                        "supports": [f"{TC}/modes/0/code"],
                    }
                ],
            }
        ],
    }


def test_shared_name_counts_only_in_a_source_from_the_jurisdiction():
    assert flag_checks.check_flags(minimal("GB")) == []
    assert any(
        "no bound excerpt names the institute" in e
        for e in flag_checks.check_flags(minimal("US"))
    )
    assert (
        flag_checks.check_flags(minimal("US", publisher="GB SNI")) == []
    )  # an unshared name still counts


def test_alias_cannot_reenable_another_institutes_name():
    """Real data: a Singapore network credit bound to the UK's in_house with named_as "AISI"."""
    data = copy.deepcopy(REAL)
    for i in data["institutes"]:
        i["testing_capacity"] = {"state": "not_published", "searches": [SEARCH]}
        i["model_access"] = {"searches": [SEARCH]}
    gb = next(n for n, i in enumerate(data["institutes"]) if i["id"] == "gb-aisi")
    data["institutes"][gb]["testing_capacity"] = {
        "modes": [
            {
                "code": "in_house",
                "source_ids": ["network-multilingual-2025"],
                "named_as": "AISI",
            }
        ]
    }
    src = next(s for s in data["sources"] if s["id"] == "network-multilingual-2025")
    assert src.get("origin") != "GB"
    pointer = f"/institutes/{gb}/testing_capacity/modes/0/code"
    for text in ("Contributed by Singapore AISI", "Contributed by AISI"):
        data["sources"] = [{**src, "excerpts": [{"text": text, "supports": [pointer]}]}]
        assert any(
            e.startswith(
                "gb-aisi: testing_capacity in_house: named_as 'AISI' does not name this institute"
            )
            for e in flag_checks.check_flags(data)
        )


@pytest.mark.parametrize("provision", ["the AI law", "Synthetic Act 2024", "9"])
def test_provision_names_the_article(flagged, provision):
    flagged["institutes"][0]["model_access"]["legal_power"]["provision"] = provision
    assert (
        "gb-test: model_access legal_power: provision must name the article, for example 'Article 92'"
        in errors(flagged)
    )


@pytest.mark.parametrize("provision", ["Art. 9", "§ 9", "Section 9(2)", "제9조"])
def test_provision_locators_accepted(flagged, provision):
    flagged["institutes"][0]["model_access"]["legal_power"]["provision"] = provision
    assert errors(flagged) == []


@pytest.mark.parametrize(
    ("kind", "title", "legal_text"),
    [
        ("primary", "How the new AI law works", False),
        ("secondary", "How the new AI law works", False),
        (
            "primary",
            "Official guide to the Synthetic Act 2024",
            False,
        ),  # names the act, still an explainer
        ("primary", "Synthetic Regulation 2023", True),  # legal text, but another act
    ],
)
def test_explainer_only_legal_claim_is_rejected(flagged, kind, title, legal_text):
    unbind(flagged, f"{MA}/legal_power")
    explainer = source(
        "gb-explainer",
        title,
        "Synthetic institute",
        type=kind,
        **({"legal_text": True} if legal_text else {}),
    )
    flagged["sources"].append(explainer)
    flagged["institutes"][0]["model_access"]["legal_power"]["source_ids"] = [
        "gb-explainer"
    ]
    flagged["institutes"][0]["source_ids"].append(
        "gb-act"
    )  # keep the act cited elsewhere
    bind(
        flagged,
        "gb-explainer",
        "The law lets the Synthetic institute demand model access with systemic risk.",
        f"{MA}/legal_power/provision",
        f"{MA}/legal_power/conditions",
    )
    assert any(
        "the provision needs an excerpt from the legal text itself" in e
        for e in errors(flagged)
    )


def test_legal_text_marker_is_exported_and_only_true(flagged):
    rows = {r["id"]: r for r in build.tables(published(flagged))["sources"]}
    assert rows["gb-act"]["legal_text"] is True and rows["gb-epoch"]["legal_text"] == ""
    next(s for s in flagged["sources"] if s["id"] == "gb-act")["legal_text"] = False
    assert schema_errors(flagged)


def test_non_frontier_event_is_rejected(flagged):
    event = flagged["institutes"][0]["model_access"]["access_events"][0]
    del event["epoch_model"]
    assert schema_errors(flagged)  # no Epoch reference, no event
    event["epoch_model"] = "Model X"
    event["epoch_source_id"] = "gb-source"  # not Epoch AI's data
    assert (
        "gb-test: model_access event event-x: epoch_source_id must name a source published by Epoch AI"
        in errors(flagged)
    )
    event["epoch_source_id"] = "gb-epoch"
    event["source_ids"] = ["gb-source"]
    unbind(flagged, f"{MA}/access_events/0/epoch_model")
    bind(
        flagged,
        "gb-source",
        "Model X is a frontier model.",
        f"{MA}/access_events/0/epoch_model",
    )
    assert (
        "gb-test: model_access event event-x: epoch_source_id must be one of its source_ids"
        in errors(flagged)
    )
    event["source_ids"] = ["gb-source", "gb-epoch"]
    assert (
        "gb-test: model_access event event-x: epoch_model needs an excerpt from gb-epoch"
        in errors(flagged)
    )


def test_agreement_must_be_an_agreement_of_the_institute_with_a_developer(flagged):
    item = flagged["institutes"][0]["model_access"]["developer_agreements"][0]
    item["agreement_id"] = "missing"
    assert (
        "gb-test: model_access agreement agreement-x: agreement_id 'missing' is not an agreement of this institute"
        in errors(flagged)
    )
    item["agreement_id"] = "test-agreement"
    flagged["institutes"][0]["agreements"][0]["counterparty_type"] = "government"
    assert (
        "gb-test: model_access agreement agreement-x: the agreement's counterparty is not a developer"
        in errors(flagged)
    )


def test_event_after_the_data_date_is_rejected(flagged):
    flagged["institutes"][0]["model_access"]["access_events"][0]["date"] = "2026-11"
    assert (
        "gb-test: model_access event event-x: date 2026-11 is after as_of 2026-10-17"
        in errors(flagged)
    )


def summary(as_of, *dates, power=False, agreement=False):
    access = {"access_events": [{"date": d} for d in dates]}
    if power:
        access["legal_power"] = {"act": "A", "provision": "Article 1"}
    if agreement:
        access["developer_agreements"] = [{"agreement_id": "a"}]
    return flag_checks.access_summary(access, as_of)


@pytest.mark.parametrize(
    ("as_of", "dates", "value", "latest"),
    [
        (
            "2026-10-17",
            ["2025-10-17"],
            "recent_access",
            "2025-10-17",
        ),  # exactly twelve months: inclusive
        ("2026-10-17", ["2025-10-16"], "earlier_access", "2025-10-16"),  # one day older
        (
            "2026-10-17",
            ["2025-10"],
            "earlier_access",
            "2025-10",
        ),  # month only: counts from 1 October
        ("2026-10-17", ["2025-11"], "recent_access", "2025-11"),
        ("2026-10-17", ["2025"], "earlier_access", "2025"),
        (
            "2028-02-29",
            ["2027-02-28"],
            "recent_access",
            "2027-02-28",
        ),  # leap day: 28 February a year earlier
        (
            "2026-10-17",
            ["2023-05", "2026-03-01", "2024"],
            "recent_access",
            "2026-03-01",
        ),  # latest, not last listed
        (
            "2026-10-17",
            ["2026-03", "2026-03-01"],
            "recent_access",
            "2026-03-01",
        ),  # same first day: the precise date
    ],
)
def test_twelve_month_boundary(as_of, dates, value, latest):
    assert summary(as_of, *dates) == {"value": value, "latest_event": latest}


def test_precedence_events_then_power_then_agreement():
    assert summary("2026-10-17", "2023-05", power=True, agreement=True) == {
        "value": "earlier_access",
        "latest_event": "2023-05",
    }
    assert summary("2026-10-17", power=True, agreement=True) == {
        "value": "by_law",
        "latest_event": None,
    }
    assert summary("2026-10-17", agreement=True) == {
        "value": "access_agreed",
        "latest_event": None,
    }
    assert summary("2026-10-17") == {"value": "none_found", "latest_event": None}


def test_old_event_label_names_its_year(flagged):
    flagged["institutes"][0]["model_access"]["access_events"][0]["date"] = "2024-06"
    access = published(flagged)["institutes"][0]["model_access"]
    assert (access["value"], access["latest_event"]) == ("earlier_access", "2024-06")
    assert (
        flag_checks.access_label(access, REAL["dataset"]["vocabularies"])
        == "Yes, evidence from 2024"
    )


def test_nothing_recorded_needs_a_dated_search_log(flagged):
    flagged["institutes"][0]["model_access"] = {
        "notes": "Access to a non-frontier model only."
    }
    unbind(flagged, MA)
    flagged["institutes"][0]["source_ids"] += ["gb-act", "gb-epoch"]
    assert schema_errors(flagged)
    flagged["institutes"][0]["model_access"]["searches"] = [dict(SEARCH)]
    assert schema_errors(flagged) == [] and errors(flagged) == []
    assert published(flagged)["institutes"][0]["model_access"]["value"] == "none_found"


def test_not_published_testing_needs_a_dated_search_log(flagged):
    flagged["institutes"][0]["testing_capacity"] = {
        "state": "not_published",
        "notes": "Searched the institute site in 2026.",
    }
    unbind(flagged, TC)
    assert any("searches" in e for e in schema_errors(flagged))
    flagged["institutes"][0]["testing_capacity"]["searches"] = [dict(SEARCH)]
    assert schema_errors(flagged) == [] and errors(flagged) == []
    flagged["institutes"][0]["testing_capacity"]["searches"][0]["date"] = "2026-19-39"
    assert schema_errors(flagged)
    assert any("'2026-19-39' is not a real date" in e for e in errors(flagged))
    flagged["institutes"][0]["testing_capacity"]["searches"] = [
        {k: v for k, v in SEARCH.items() if k != "sources_read"}
    ]
    assert schema_errors(flagged)
    flagged["institutes"][0]["testing_capacity"]["searches"] = [dict(SEARCH)]
    flagged["institutes"][0]["testing_capacity"]["source_ids"] = ["gb-source"]
    assert schema_errors(flagged) == []  # sources of the notes
    del flagged["institutes"][0]["testing_capacity"]["notes"]
    assert schema_errors(flagged)  # not_published cites nothing but its notes


@pytest.mark.parametrize(
    "value",
    [
        {"modes": []},
        {
            "modes": [{"code": "in_house", "source_ids": ["gb-source"]}],
            "state": "planned",
        },
        {"modes": [{"code": "voluntary", "source_ids": ["gb-source"]}]},
        {"state": "by_law"},
        {},
    ],
)
def test_testing_shape_is_one_list_of_modes_or_one_state(flagged, value):
    flagged["institutes"][0]["testing_capacity"] = value
    assert schema_errors(flagged)


def test_repeated_or_reordered_modes_are_rejected(flagged):
    modes = flagged["institutes"][0]["testing_capacity"]["modes"]
    modes.reverse()
    assert any("must be in vocabulary order" in e for e in errors(flagged))
    modes[0]["code"] = modes[1]["code"]
    assert any("repeats a code" in e for e in errors(flagged))


def test_flags_are_added_to_every_institute_together(flagged):
    del flagged["institutes"][0]["model_access"]
    flagged["institutes"][0]["source_ids"] += ["gb-act", "gb-epoch"]
    unbind(flagged, MA)
    assert (
        "gb-test: testing_capacity and model_access are added to every institute together; missing model_access"
        in errors(flagged)
    )


def test_required_switch_rejects_an_institute_without_flags(dataset):
    for field in flag_checks.FIELDS:
        del dataset["institutes"][0][field]
    assert any("added to every institute together" in e for e in errors(dataset))


def test_vocabularies_must_define_exactly_the_codes(flagged):
    del flagged["dataset"]["vocabularies"]["model_access"]["none_found"]
    assert (
        "dataset.vocabularies.model_access must define exactly recent_access, earlier_access, by_law, access_agreed, none_found"
        in errors(flagged)
    )
    del flagged["dataset"]["vocabularies"]["testing_capacity"]
    assert schema_errors(flagged)
    assert set(REAL["dataset"]["vocabularies"]["model_access"]) == set(
        flag_checks.ACCESS_VALUES
    )


@pytest.mark.parametrize(
    "path", [("access_events", 0), ("developer_agreements", 0), ("legal_power",)]
)
def test_unknown_source_ids_in_access_records(flagged, path):
    node = flagged["institutes"][0]["model_access"]
    for key in path:
        node = node[key]
    node["source_ids"] = ["missing"]
    assert any(
        "model_access" in e and "unknown source id 'missing'" in e
        for e in errors(flagged)
    )


def test_derived_value_is_never_authored(flagged):
    flagged["institutes"][0]["model_access"]["value"] = "recent_access"
    assert schema_errors(flagged, authored_schema(SCHEMA, strict=True))
    with pytest.raises(
        ValueError, match="model_access/value: derived field must not be authored"
    ):
        materialise_facts(flagged, strict=True)
    flagged["institutes"][0]["model_access"]["value"] = (
        "by_law"  # transitional input: disagreement fails
    )
    with pytest.raises(
        ValueError,
        match="model_access/value: stored 'by_law' disagrees with derived 'recent_access'",
    ):
        materialise_facts(flagged)


def test_published_record_carries_flags_value_and_citations(flagged):
    pub = published(flagged)
    access = pub["institutes"][0]["model_access"]
    assert (access["value"], access["latest_event"]) == ("recent_access", "2026-03")
    assert access["legal_power"]["citations"]["provision"] == ["gb-act"]
    assert access["access_events"][0]["citations"]["epoch_model"] == ["gb-epoch"]
    assert pub["institutes"][0]["testing_capacity"]["modes"][1]["citations"][
        "code"
    ] == ["gb-source"]
    assert schema_errors(pub, SCHEMA) == []
    record = next(
        v
        for k, v in agent_files.api_files(pub).items()
        if k.endswith("/institutes/gb-test.json")
    )["record"]
    assert (
        record["model_access"] == access
        and record["testing_capacity"] == pub["institutes"][0]["testing_capacity"]
    )


def test_csv_columns(flagged):
    row = build.tables(published(flagged))["institutes"][0]
    assert row["testing_capacity"] == "in_house; via_other_bodies"
    assert (row["model_access"], row["model_access_latest_event"]) == (
        "recent_access",
        "2026-03",
    )
    assert row["model_access_events"] == "2026-03 Dev A Model X (pre_release)"
    assert row["model_access_legal_power"] == "Synthetic Act 2024, Article 9"
    assert row["model_access_agreement_ids"] == "test-agreement"
    assert row["model_access_source_ids"] == "gb-source; gb-epoch; gb-act"
    flagged["institutes"][0]["testing_capacity"] = {
        "state": "not_published",
        "notes": "proposed: draft bill, 2026.",
        "searches": [SEARCH],
    }
    unbind(flagged, TC)
    row = build.tables(published(flagged))["institutes"][0]
    assert (row["testing_capacity"], row["testing_capacity_notes"]) == (
        "not_published",
        "proposed: draft bill, 2026.",
    )
    assert (
        row["testing_capacity_searches"]
        == "2026-10-21 (en): Synthetic institute model access"
    )
    assert all(
        build.FIELD_DOCS.get(c)
        for c in row
        if c.startswith(("testing_capacity", "model_access"))
    )


def test_flag_only_sources_stay_off_the_page(flagged):
    institute = published(flagged)["institutes"][0]
    for sid in ("gb-act", "gb-epoch"):
        assert sid not in record_sources(institute)
        assert sid not in institute["display"]["source_links"]
        assert sid not in institute["citations"]["display"]


def test_selectors_round_trip_and_stale_selectors_fail(flagged):
    pub = published(flagged)
    assert stable_selector(pub, f"{TC}/modes/1/code") == [
        "institutes",
        {"id": "gb-test"},
        "testing_capacity",
        "modes",
        {"code": "via_other_bodies"},
        "code",
    ]
    assert stable_selector(pub, f"{MA}/access_events/0/kind")[4] == {"id": "event-x"}
    assert stable_selector(pub, f"{MA}/developer_agreements/0/agreement_id")[4] == {
        "id": "agreement-x"
    }
    pub["institutes"][0]["testing_capacity"]["modes"][1]["code"] = "in_house"
    with pytest.raises(ValueError, match="matched 2 records"):
        stable_selector(pub, f"{TC}/modes/0/code")
    data = copy.deepcopy(REAL)
    i = data["institutes"][0]
    i["testing_capacity"] = {
        "modes": [{"code": "in_house", "source_ids": i["source_ids"][:1]}]
    }
    data["sources"][0]["excerpts"] = [
        {
            "text": "x",
            "supports": [
                [
                    "institutes",
                    {"id": i["id"]},
                    "testing_capacity",
                    "modes",
                    {"code": "via_other_bodies"},
                    "code",
                ]
            ],
        }
    ]
    assert any("matched 0 records" in e for e in build.check_consistency(data))


def test_check_py_identity_keys_agree_with_references():
    """check.py keeps its own copy (the hook runs it without checkout imports); shared keys must agree."""
    tree = ast.parse((ROOT / "scripts/check.py").read_text())
    copies = [
        ast.literal_eval(node.func.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "get"
        and isinstance(node.func.value, ast.Dict)
        and "outputs" in ast.literal_eval(node.func.value)
    ]
    assert len(copies) == 1
    assert {k: v for k, v in copies[0].items() if k != "operating"} == IDENTITY_KEYS


def test_every_institute_has_both_flags():
    assert all(
        "testing_capacity" in i and "model_access" in i for i in REAL["institutes"]
    )
    assert flag_checks.FLAGS_REQUIRED


def test_alias_inside_another_institutes_name_does_not_count():
    d = minimal("GB")
    d["institutes"][0]["testing_capacity"]["modes"][0]["named_as"] = "SNI"
    d["sources"][0]["excerpts"][0]["text"] = "US SNI ran evaluations."
    assert any(
        "named_as 'SNI' does not name this institute" in e
        for e in flag_checks.check_flags(d)
    )
    d["sources"][0]["excerpts"][0]["text"] = "SNI ran evaluations."
    assert flag_checks.check_flags(d) == []


def test_notes_carry_their_own_sources_off_the_page(flagged):
    flagged["sources"].append(
        source("gb-note", "Kakao and the institute", "Synthetic institute")
    )
    access = flagged["institutes"][0]["model_access"]
    access["source_ids"] = ["gb-note"]
    assert schema_errors(flagged)  # sources need the notes they support
    access["notes"] = "Access to a model outside the frontier set, 2026."
    bind(
        flagged,
        "gb-note",
        "The Synthetic institute tested Model Y before release.",
        f"{MA}/notes",
    )
    assert schema_errors(flagged) == [] and errors(flagged) == []
    institute = published(flagged)["institutes"][0]
    assert "gb-note" not in institute["display"]["source_links"]
    assert build.tables(published(flagged))["institutes"][0][
        "model_access_source_ids"
    ].endswith("gb-note")
    access["source_ids"] = ["missing"]
    assert any(
        "institute gb-test model_access: unknown source id 'missing'" in e
        for e in errors(flagged)
    )


@pytest.mark.parametrize(
    ("date", "message"),
    [
        (None, "the referenced agreement has no date"),
        (
            "2027-01-01",
            "the referenced agreement's date 2027-01-01 is after as_of 2026-10-17",
        ),
    ],
)
def test_access_agreement_needs_a_date_not_after_as_of(flagged, date, message):
    agreement = flagged["institutes"][0]["agreements"][0]
    if date is None:
        del agreement["date"]
        unbind(flagged, "/institutes/0/agreements/0/date")
    else:
        agreement["date"] = date
    assert f"gb-test: model_access agreement agreement-x: {message}" in errors(flagged)


def leading(
    flagged, frontier_date="2024-06-20", notable_date="2025-08", event_date="2026-03"
):
    event = flagged["institutes"][0]["model_access"]["access_events"][0]
    event.update(
        epoch_basis="leading_developer",
        model="Model Z",
        date=event_date,
        epoch_model="Model X",
        epoch_model_date=frontier_date,
        epoch_notable_model="Model N",
        epoch_notable_date=notable_date,
    )
    bind(
        flagged,
        "gb-epoch",
        "Model X | Dev A | 2024-06-20 | frontier model: yes; Model N | Dev A | notable",
        *(f"{MA}/access_events/0/{k}" for k in flag_checks.LEADING_KEYS),
    )
    return event


def test_leading_developer_event_counts(flagged):
    leading(flagged)
    assert schema_errors(flagged) == [] and errors(flagged) == []


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        (
            {"frontier_date": "2022-11"},
            "a leading developer needs an Epoch frontier model released on or after 2023-01-01; 2022-11 is earlier",
        ),
        (
            {"frontier_date": "2026-04-01"},
            "a leading developer needs its Epoch frontier model released by the event date; 2026-04-01 is later",
        ),
        (
            {"frontier_date": "2027"},
            "a leading developer needs its Epoch frontier model released by the event date; 2027 is later",
        ),
        (
            {"notable_date": "2025-02-28"},
            "a leading developer needs an Epoch notable model in the twelve months before the event; 2025-02-28 is outside them",
        ),
        (
            {"notable_date": "2026-04"},
            "a leading developer needs an Epoch notable model in the twelve months before the event; 2026-04 is outside them",
        ),
        (
            {"notable_date": "2025"},
            "a leading developer needs an Epoch notable model in the twelve months before the event; 2025 is outside them",
        ),
    ],
)
def test_leading_developer_criterion_is_checked(flagged, kwargs, message):
    leading(flagged, **kwargs)
    assert f"gb-test: model_access event event-x: {message}" in errors(flagged)


@pytest.mark.parametrize(
    ("frontier_date", "notable_date", "event_date"),
    [
        ("2023-01-01", "2025-03-01", "2026-03"),  # both boundaries included
        ("2023", "2026-03-31", "2026-03"),  # notable model in the event's own month
        (
            "2026-03-31",
            "2026-03-31",
            "2026-03",
        ),  # frontier model in the event's own month
    ],
)
def test_leading_developer_boundaries(flagged, frontier_date, notable_date, event_date):
    leading(flagged, frontier_date, notable_date, event_date)
    assert errors(flagged) == []


def test_leading_developer_basis_is_complete_and_from_epoch(flagged):
    event = leading(flagged)
    del event["epoch_notable_date"]
    assert schema_errors(flagged)
    event["epoch_notable_date"] = "2025-08"
    event["epoch_basis"] = "frontier_model"
    assert schema_errors(flagged)  # a frontier-model event carries no developer basis
    event["epoch_basis"] = "leading_developer"
    unbind(flagged, f"{MA}/access_events/0/epoch_notable_model")
    bind(
        flagged,
        "gb-source",
        "Dev A released Model N in August 2025.",
        f"{MA}/access_events/0/epoch_notable_model",
    )
    assert (
        "gb-test: model_access event event-x: epoch_notable_model needs an excerpt from gb-epoch"
        in errors(flagged)
    )
    assert f"{MA}/access_events/0/epoch_notable_date" in required_excerpt_pointers(
        flagged
    )
