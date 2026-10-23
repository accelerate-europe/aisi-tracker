"""Hand-worked fact derivation cases, independent of the download renderer."""

import copy
import json
from pathlib import Path

import pytest

from scripts.migrate_facts import migrate
from scripts.migrate_references import migrate as migrate_references
from scripts.published import materialise_facts, timeline_status
from scripts.references import materialise_references
from scripts.shared_claims import resolve


def act(kind, date, identity, **extra):
    return {"id": identity, "event": kind, "date": date, **extra}


def output(lineage, date, **extra):
    return {
        "lineage": lineage,
        "date": date,
        "title": f"Report {lineage}",
        "source_ids": ["source"],
        "qualifies_as": "published_work",
        "attribution": "institutional_author",
        "type": "report",
        **extra,
    }


def primary(events=None, outputs=None):
    return {
        "dataset": {"exchange_rates": []},
        "jurisdictions": [{"id": "GB", "timeline": events or []}],
        "institutes": [
            {"id": "institute", "jurisdiction_id": "GB", "outputs": outputs or []}
        ],
        "sources": [],
    }


@pytest.mark.parametrize(
    ("events", "expected"),
    [
        ([], ("none_identified", None)),
        (
            [
                act("commitment", "2023", "a"),
                act("designated", "2024", "b"),
                act("withdrawn", "2025", "w", withdraws="b"),
            ],
            ("planned", "2023"),
        ),
        (
            [act("designated", "2023", "a"), act("closed", "2024", "b")],
            ("none_identified", None),
        ),
        (
            [
                act("designated", "2023", "a"),
                act("closed", "2024", "b"),
                act("commitment", "2025", "c"),
            ],
            ("planned", "2025"),
        ),
        (
            [
                act("designated", "2023", "a"),
                act("closed", "2024", "b"),
                act("withdrawn", "2025", "w", withdraws="b"),
            ],
            ("established", "2023"),
        ),
        (
            [act("designated", "2024-01", "a"), act("closed", "2024", "b")],
            ("none_identified", None),
        ),
    ],
)
def test_status_transitions(events, expected):
    assert timeline_status(events) == expected
    published = materialise_facts(primary(events), strict=True)
    jurisdiction = published["jurisdictions"][0]
    assert (jurisdiction["status"], jurisdiction.get("status_since")) == expected
    assert published["institutes"][0]["status"] == expected[0]


def test_historical_establishment_keeps_generated_announcement():
    data = primary(
        [
            act("proposed", "2023", "p"),
            act("designated", "2024", "d"),
            act("closed", "2025", "c"),
        ]
    )
    institute = materialise_facts(data, strict=True)["institutes"][0]
    assert institute["announced"] == "2023"
    assert institute["established"] == "2024"
    assert institute["status"] == "none_identified"


def test_designation_alone_gives_no_announcement():
    institute = materialise_facts(
        primary([act("designated", "2024-05-22", "d")]), strict=True
    )["institutes"][0]
    assert institute["established"] == "2024-05-22"
    assert institute["announced"] is None


@pytest.mark.parametrize(
    ("events", "expected"),
    [
        # Two commitments (AU): the first, not the later reaffirmation.
        (
            [
                act("commitment", "2025-11-25", "a"),
                act("commitment", "2025-12-02", "b"),
                act("designated", "2026", "d"),
            ],
            "2025-11-25",
        ),
        # A proposal before a commitment counts; a later designation never does.
        (
            [
                act("proposed", "2022-06", "p"),
                act("commitment", "2023", "c"),
                act("designated", "2024", "d"),
            ],
            "2022-06",
        ),
        # Mixed precision in date order (KE): the first event in timeline order.
        (
            [
                act("commitment", "2025-03-27", "a"),
                act("commitment", "2025", "b"),
                act("proposed", "2026-07", "p"),
            ],
            "2025-03-27",
        ),
        # A withdrawn commitment still dates the announcement.
        (
            [
                act("commitment", "2023-02", "a"),
                act("withdrawn", "2023-06", "w", withdraws="a"),
                act("commitment", "2024", "b"),
            ],
            "2023-02",
        ),
    ],
)
def test_announcement_is_the_first_commitment_or_proposal(events, expected):
    data = primary(events)
    data["dataset"]["as_of"] = "2026-10-20"
    assert materialise_facts(data, strict=True)["institutes"][0]["announced"] == expected


def test_authored_announcement_is_rejected():
    data = primary([act("commitment", "2023", "c")])
    data["institutes"][0]["announced"] = "2023"
    with pytest.raises(ValueError, match="announced: derived field must not be authored"):
        materialise_facts(data, strict=True)
    data["institutes"][0]["announced"] = "2022"
    with pytest.raises(ValueError, match="announced: stored '2022' disagrees with derived '2023'"):
        materialise_facts(data)


def test_name_dates_are_the_linked_event_dates():
    data = primary(
        [
            act("commitment", "2023-10-26", "c"),
            act("designated", "2023-11-02", "d"),
            act("launched", "2023-11-02", "l"),
            act("mandate_changed", "2025-02-14", "m"),
        ]
    )
    data["institutes"][0]["names"] = [
        {"id": "first", "name": "Safety institute", "event": "d"},
        {"id": "renamed", "name": "Security institute", "event": "m"},
    ]
    names = materialise_facts(data, strict=True)["institutes"][0]["names"]
    # A rename takes the date of its own act, not the designation's or the announcement's.
    assert [name["from"] for name in names] == ["2023-11-02", "2025-02-14"]


def test_name_linked_to_a_missing_event_fails():
    data = primary([act("designated", "2024", "d")])
    data["institutes"][0]["names"] = [{"id": "first", "name": "Institute", "event": "gone"}]
    with pytest.raises(ValueError, match="names/first: event 'gone' is not in the GB timeline"):
        materialise_facts(data, strict=True)


def test_authored_name_date_is_rejected():
    data = primary([act("designated", "2024", "d")])
    data["institutes"][0]["names"] = [
        {"id": "first", "name": "Institute", "event": "d", "from": "2024"}
    ]
    with pytest.raises(ValueError, match="names/first/from: derived field must not be authored"):
        materialise_facts(data, strict=True)
    data["institutes"][0]["names"][0]["from"] = "2023"
    with pytest.raises(ValueError, match="from: stored '2023' disagrees with derived '2024'"):
        materialise_facts(data)


def test_handworked_currency_inverse_and_annual_rounding():
    data = primary()
    data["dataset"]["exchange_rates"] = [
        {"currency": "GBP", "units_per_eur": 0.8},
        {"currency": "USD", "units_per_eur": 1.25},
        {"currency": "JPY", "units_per_eur": 3},
    ]
    data["institutes"][0]["funding"] = [
        {"currency": "GBP", "annual_amount": 12360},
        {"currency": "EUR", "annual_amount": 12000},
        {"currency": "USD", "annual_amount": 999500},
        {"currency": "JPY", "annual_amount": 100},
        {"currency": "USD", "annual_amount": None},
    ]
    result = materialise_facts(data)
    # GBP: 12,360 * 1.25 = 15,450 exactly; USD: 999,500 * 0.8 = 799,600; JPY: 100 / 3 = 33.3333 to 6 s.f.
    assert [r["eur_per_unit"] for r in result["dataset"]["exchange_rates"]] == [
        1.25,
        0.8,
        0.333333333333,
    ]
    assert [r["eur_per_year_est"] for r in result["institutes"][0]["funding"]] == [
        15450,
        12000,
        799600,
        33.3333,
        None,
    ]


def test_funding_primary_inputs_derive_sums_means_and_current_profile():
    data = primary()
    data["dataset"]["as_of"] = "2026-10-18"
    data["institutes"][0]["funding"] = [
        {"id": "a", "amount": 12, "annual_amount": 4, "currency": "EUR"},
        {"id": "b", "amount": 8, "annual_amount": 2, "currency": "EUR"},
        {"id": "total", "sum_of": ["a", "b"], "currency": "EUR"},
        {
            "id": "mean",
            "amount": 15,
            "years": 2,
            "annual_basis_code": "period_mean",
            "currency": "EUR",
        },
        {
            "id": "profile",
            "annual_basis_code": "allocation_current_year",
            "currency": "EUR",
            "annual_profile": [
                {"start": "2025-04-01", "end": "2026-03-31", "amount": 11},
                {"start": "2026-04-01", "end": "2027-03-31", "amount": 17},
            ],
        },
    ]
    result = materialise_facts(data, strict=True)["institutes"][0]["funding"]
    assert (
        result[2]["amount"],
        result[2]["annual_amount"],
        result[2]["eur_per_year_est"],
    ) == (20, 6, 6)
    assert (result[3]["annual_amount"], result[3]["eur_per_year_est"]) == (7.5, 7.5)
    assert result[4]["annual_amount"] == 17
    assert result[4]["annual_period"] == {"start": "2026-04-01", "end": "2027-03-31"}
    assert "annual_amount" not in data["institutes"][0]["funding"][2]
    for record, field in (
        (2, "amount"),
        (2, "annual_amount"),
        (3, "annual_amount"),
        (4, "annual_amount"),
        (4, "annual_period"),
    ):
        broken = copy.deepcopy(data)
        broken["institutes"][0]["funding"][record][field] = result[record][field]
        with pytest.raises(
            ValueError, match=f"{field}: derived field must not be authored"
        ):
            materialise_facts(broken, strict=True)


def test_first_output_includes_exclusions_and_credit_date_ties():
    data = primary(
        outputs=[
            output("later-credit", "2022", credited_from="2025"),
            output("b", "2023", qualifies_as="none"),
            output("a", "2023", qualifies_as="none"),
        ]
    )
    before = copy.deepcopy(data)
    result = materialise_facts(data)
    event = result["jurisdictions"][0]["timeline"][0]
    assert event == {
        "id": "gb-first-output",
        "date": "2023",
        "event": "first_output",
        "instrument": "publication",
        "description": "Earliest identified institutionally credited output: Report a",
        "short": "First published work credited to the institute.",
        "output_lineage": "a",
        "source_ids": ["source"],
    }
    assert result["institutes"][0]["operating"] == [
        {
            "kind": "published_work",
            "date": "2025",
            "lineage": "later-credit",
            "source_ids": ["source"],
        }
    ]
    assert data == before
    assert result["institutes"][0]["outputs"] == data["institutes"][0]["outputs"]


def test_inserted_output_does_not_reorder_overlapping_status_acts():
    data = primary(
        [act("designated", "2024-01", "a"), act("closed", "2024", "b")],
        [output("early", "2023")],
    )
    result = materialise_facts(data)
    assert [e["id"] for e in result["jurisdictions"][0]["timeline"]] == [
        "gb-first-output",
        "a",
        "b",
    ]
    assert result["jurisdictions"][0]["status"] == "none_identified"


@pytest.mark.parametrize(
    ("section", "field", "wrong"),
    [
        ("jurisdictions", "status", "established"),
        ("jurisdictions", "status_since", "2020"),
        ("institutes", "status", "planned"),
        ("institutes", "established", "2020"),
    ],
)
def test_repeated_field_disagreement_fails(section, field, wrong):
    data = primary()
    data[section][0][field] = wrong
    with pytest.raises(ValueError, match=f"{section}/.*{field}: stored.*disagrees"):
        materialise_facts(data)


def test_strict_rejects_matching_repeated_fields():
    data = primary()
    data["jurisdictions"][0]["status"] = "none_identified"
    assert materialise_facts(data)["jurisdictions"][0]["status"] == "none_identified"
    with pytest.raises(ValueError, match="derived field must not be authored"):
        materialise_facts(data, strict=True)


def test_stored_first_output_disagreement_fails():
    data = primary(
        [act("first_output", "2022", "gb-first-output", output_lineage="a")],
        [output("a", "2023")],
    )
    with pytest.raises(ValueError, match="first_output disagrees"):
        materialise_facts(data)


@pytest.mark.parametrize("field", ["eur_per_unit", "eur_per_year_est"])
def test_repeated_currency_disagreement_fails(field):
    data = primary()
    data["dataset"]["exchange_rates"] = [{"currency": "USD", "units_per_eur": 1.25}]
    data["institutes"][0]["funding"] = [{"currency": "USD", "annual_amount": 1000}]
    record = (
        data["dataset"]["exchange_rates"][0]
        if field == "eur_per_unit"
        else data["institutes"][0]["funding"][0]
    )
    record[field] = -1
    with pytest.raises(ValueError, match=f"{field}: stored.*disagrees"):
        materialise_facts(data)


def test_strict_rejects_matching_first_output():
    data = primary(
        [act("first_output", "2023", "gb-first-output", output_lineage="a")],
        [output("a", "2023")],
    )
    materialise_facts(data)
    with pytest.raises(ValueError, match="first_output event must not be authored"):
        materialise_facts(data, strict=True)


def test_references_resolve_after_insertion():
    data = primary([act("designated", "2024", "d")], [output("a", "2023")])
    selector = ["jurisdictions", {"id": "GB"}, "timeline", {"id": "d"}, "date"]
    data["sources"] = [{"id": "source", "excerpts": [{"supports": [selector]}]}]
    facts = materialise_facts(data)
    assert facts["sources"][0]["excerpts"][0]["supports"] == [selector]
    result = materialise_references(facts)
    assert result["sources"][0]["excerpts"][0]["supports"] == [
        "/jurisdictions/0/timeline/1/date"
    ]
    assert resolve(result, selector)[0] == "2024"


def test_migration_rebinds_then_removes_repeated_facts():
    data = primary(
        [
            act("proposed", "2023", "p"),
            act("designated", "2024", "d"),
            act(
                "first_output",
                "2025",
                "gb-first-output",
                output_lineage="a",
                description="Old wording",
            ),
        ],
        [output("a", "2025", credit={"institute_id": "institute", "date": "2025"})],
    )
    data["jurisdictions"][0].update(status="established", status_since="2024")
    data["institutes"][0].update(
        status="established", announced="2023", established="2024",
        names=[
            {"id": "old", "name": "Old", "event": "p", "from": "2023"},
            {"id": "new", "name": "New", "event": "d", "from": "2024"},
        ],
    )
    data["sources"] = [
        {
            "id": "source",
            "excerpts": [
                {
                    "supports": [
                        "/jurisdictions/0/status",
                        "/institutes/0/announced",
                        "/institutes/0/names/1/from",
                        "/jurisdictions/0/timeline/2/date",
                        "/jurisdictions/0/timeline/2/description",
                    ]
                }
            ],
        }
    ]
    migrated = migrate(data)
    assert "status" not in migrated["jurisdictions"][0]
    assert "announced" not in migrated["institutes"][0]
    assert all("from" not in name for name in migrated["institutes"][0]["names"])
    assert [e["id"] for e in migrated["jurisdictions"][0]["timeline"]] == ["p", "d"]
    assert migrated["sources"][0]["excerpts"][0]["supports"] == [
        ["jurisdictions", {"id": "GB"}, "timeline", {"id": "d"}, "event"],
        ["jurisdictions", {"id": "GB"}, "timeline", {"id": "p"}, "date"],
        ["jurisdictions", {"id": "GB"}, "timeline", {"id": "d"}, "date"],
        [
            "institutes",
            {"id": "institute"},
            "outputs",
            {"lineage": "a"},
            "date",
        ],
        ["institutes", {"id": "institute"}, "outputs", {"lineage": "a"}, "title"],
    ]
    materialise_references(materialise_facts(migrated, strict=True))
    assert migrate(migrated) == migrated


def test_fact_migration_requires_reviewed_credit():
    data = primary(outputs=[output("a", "2025")])
    before = copy.deepcopy(data)
    with pytest.raises(ValueError, match="reviewed credit is required"):
        migrate(data)
    assert data == before


@pytest.mark.parametrize("suffix", ["", "/event", "/id", "/source_ids"])
def test_first_output_event_or_metadata_evidence_needs_review(suffix):
    data = primary(
        [act("first_output", "2025", "first", output_lineage="a")],
        [output("a", "2025", credit={"institute_id": "institute", "date": "2025"})],
    )
    data["sources"] = [
        {
            "id": "source",
            "excerpts": [{"supports": [f"/jurisdictions/0/timeline/0{suffix}"]}],
        }
    ]
    before = copy.deepcopy(data)
    with pytest.raises(ValueError, match="first_output evidence needs review"):
        migrate(data)
    assert data == before


def test_legacy_credited_from_rebinds_only_existing_credit_date():
    data = primary(
        outputs=[
            output(
                "a",
                "2024",
                credited_from="2025",
                credit={"institute_id": "institute", "date": "2025"},
            )
        ]
    )
    data["sources"] = [
        {
            "id": "source",
            "excerpts": [{"supports": ["/institutes/0/outputs/0/credited_from"]}],
        }
    ]
    result = migrate(data)
    assert result["sources"][0]["excerpts"][0]["supports"] == [
        [
            "institutes",
            {"id": "institute"},
            "outputs",
            {"lineage": "a"},
            "credit",
            "date",
        ]
    ]
    assert "credited_from" not in result["institutes"][0]["outputs"][0]
    assert migrate(result) == result


def test_current_data_migrates_without_hidden_fact_conflicts():
    data = json.loads((Path(__file__).parents[1] / "data/aisi.json").read_text())
    if any(
        "credit" not in output
        for institute in data["institutes"]
        for output in institute.get("outputs", [])
    ):
        with pytest.raises(ValueError, match="reviewed credit is required"):
            migrate(migrate_references(data))
        return
    transitional = materialise_facts(data)
    migrated = migrate(migrate_references(data))
    assert migrate(migrated) == migrated
    result = materialise_references(materialise_facts(migrated, strict=True))
    assert len(result["jurisdictions"]) == len(data["jurisdictions"])
    assert [
        (j["id"], j["status"], j.get("status_since")) for j in result["jurisdictions"]
    ] == [
        (j["id"], j["status"], j.get("status_since"))
        for j in transitional["jurisdictions"]
    ]
    for source in result["sources"]:
        for excerpt in source.get("excerpts", []):
            for pointer in excerpt["supports"]:
                # Resolve each pointer independently of stable-selector migration.
                node = result
                for token in pointer.strip("/").split("/"):
                    token = token.replace("~1", "/").replace("~0", "~")
                    node = node[int(token)] if isinstance(node, list) else node[token]


@pytest.mark.parametrize(
    ("category", "leaf", "message"),
    [
        ("sum_of", "amount", "sum_of evidence needs review"),
        ("sum_of", "annual_amount", "sum_of evidence needs review"),
        ("period_mean", "annual_amount", "period_mean evidence needs review"),
        (
            "usd_annual",
            "eur_per_year_est",
            "EUR annual conversion evidence needs review",
        ),
        ("usd_rate", "eur_per_unit", "EUR rate evidence needs review"),
    ],
)
def test_aggregate_and_conversion_evidence_requires_review(category, leaf, message):
    data = primary()
    if category == "sum_of":
        records = [
            {"id": "a", "amount": 12, "annual_amount": 4, "currency": "EUR"},
            {"id": "b", "amount": 8, "annual_amount": 2, "currency": "EUR"},
            {
                "id": "total",
                "sum_of": ["a", "b"],
                "amount": 20,
                "annual_amount": 6,
                "currency": "EUR",
            },
        ]
        pointer = f"/institutes/0/funding/2/{leaf}"
    elif category == "period_mean":
        records = [
            {
                "id": "mean",
                "amount": 15,
                "years": 3,
                "annual_amount": 5,
                "annual_basis_code": "period_mean",
                "currency": "EUR",
            }
        ]
        pointer = f"/institutes/0/funding/0/{leaf}"
    else:
        data["dataset"]["exchange_rates"] = [
            {"currency": "GBP", "units_per_eur": 0.8, "eur_per_unit": 1.25},
            {"currency": "USD", "units_per_eur": 1.25, "eur_per_unit": 0.8},
        ]
        records = [
            {
                "id": "annual",
                "currency": "GBP",
                "annual_amount": 8,
                "eur_per_year_est": 10,
            }
        ]
        pointer = (
            f"/dataset/exchange_rates/0/{leaf}"
            if category == "usd_rate"
            else f"/institutes/0/funding/0/{leaf}"
        )
    data["institutes"][0]["funding"] = records
    data["sources"] = [
        {
            "id": "source",
            "excerpts": [
                {"text": "A quotation of the derived figure.", "supports": [pointer]}
            ],
        }
    ]
    before = copy.deepcopy(data)
    manifest = []
    with pytest.raises(ValueError, match=message):
        migrate(data, manifest=manifest)
    assert data == before
    assert manifest == []


def test_migration_manifest_records_changed_binding_without_certifying_it():
    data = primary(
        [
            act(
                "first_output",
                "2025",
                "first",
                output_lineage="a",
                description="An account of an output",
            )
        ],
        [output("a", "2025", credit={"institute_id": "institute", "date": "2025"})],
    )
    old = ["jurisdictions", {"id": "GB"}, "timeline", {"id": "first"}, "description"]
    unchanged = ["institutes", {"id": "institute"}, "outputs", {"lineage": "a"}, "date"]
    data["sources"] = [
        {
            "id": "source",
            "excerpts": [
                {
                    "text": "Original narrative, to be reviewed against the title.",
                    "supports": [old, unchanged],
                }
            ],
        }
    ]
    before = copy.deepcopy(data)
    manifest = []
    result = migrate(data, manifest=manifest)
    assert result == migrate(data)
    assert data == before
    assert manifest == [
        {
            "source_id": "source",
            "excerpt_index": 0,
            "text": "Original narrative, to be reviewed against the title.",
            "old_reference": old,
            "old_pointer": "/jurisdictions/0/timeline/0/description",
            "new_selectors": [
                [
                    "institutes",
                    {"id": "institute"},
                    "outputs",
                    {"lineage": "a"},
                    "title",
                ]
            ],
        }
    ]
    second_manifest = []
    assert migrate(result, manifest=second_manifest) == result
    assert second_manifest == []
    manifest[0]["new_selectors"][0][-1] = "altered"
    assert result["sources"][0]["excerpts"][0]["supports"][0][-1] == "title"


def test_failed_migration_does_not_emit_partial_manifest():
    data = primary(
        [
            act("designated", "2024", "d"),
            act("first_output", "2025", "first", output_lineage="a"),
        ],
        [output("a", "2025", credit={"institute_id": "institute", "date": "2025"})],
    )
    data["jurisdictions"][0]["status"] = "established"
    data["sources"] = [
        {
            "id": "source",
            "excerpts": [
                {
                    "text": "A status statement.",
                    "supports": ["/jurisdictions/0/status"],
                },
                {
                    "text": "An output event.",
                    "supports": ["/jurisdictions/0/timeline/1/event"],
                },
            ],
        }
    ]
    manifest = [{"existing": "entry"}]
    with pytest.raises(ValueError, match="first_output evidence needs review"):
        migrate(data, manifest=manifest)
    assert manifest == [{"existing": "entry"}]


@pytest.mark.parametrize("primary_leaves", [["amount"], ["amount", "annual_amount"]])
def test_registry_drops_computed_copies_without_rebinding_their_evidence(
    primary_leaves,
):
    data = primary()
    data["institutes"][0]["funding"] = [
        {
            "id": "annual",
            "currency": "EUR",
            "amount": 10,
            "annual_amount": 10,
            "eur_per_year_est": 10,
        }
    ]
    base = ["institutes", {"id": "institute"}, "funding", {"id": "annual"}]
    data["dataset"]["shared_claims"] = [
        {
            "id": "annual-copy",
            "targets": [
                base + [leaf] for leaf in [*primary_leaves, "eur_per_year_est"]
            ],
        }
    ]
    result = migrate(data)
    expected = (
        [{"id": "annual-copy", "targets": [base + [leaf] for leaf in primary_leaves]}]
        if len(primary_leaves) == 2
        else []
    )
    assert result["dataset"]["shared_claims"] == expected
    assert migrate(result) == result
    data["sources"] = [
        {
            "id": "source",
            "excerpts": [
                {
                    "text": "A EUR estimate.",
                    "supports": [base + ["eur_per_year_est"]],
                }
            ],
        }
    ]
    with pytest.raises(ValueError, match="EUR annual conversion evidence needs review"):
        migrate(data)
