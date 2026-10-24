"""Independent display choices and field-specific evidence routes."""

import copy
import json
from pathlib import Path

import pytest

from scripts.published import assign_credit_provenance

from scripts.display import (
    comparable_funding,
    derive_display,
    first_sentence,
    funding_selection,
    record_sources,
    staff_selection,
)


def funding(name, start="2026-01-01", end="2026-12-31", **extra):
    return {
        "id": name,
        "kind": "annual",
        "coverage": "whole_institute",
        "annual_period": {"start": start, "end": end},
        "currency": "EUR",
        "annual_amount": 100,
        "eur_per_year_est": 100,
        "commitment": "adopted",
        "annual_basis_code": "other",
        **extra,
    }


def selected(records, as_of="2026-10-18"):
    index = comparable_funding(records, as_of)
    return records[index]["id"] if index is not None else None


def document():
    return {
        "dataset": {"as_of": "2026-10-18", "exchange_rates": [{"currency": "USD", "eur_per_unit": 1}]},
        "jurisdictions": [
            {
                "id": "GB",
                "name": "United Kingdom",
                "status": "established",
                "status_since": "2024",
                "institute_id": "institute",
                "population": {"value": 10_000_000},
                "summary": "A finding.",
                "source_ids": ["decoy"],
                "timeline": [
                    {
                        "id": "designation",
                        "event": "designated",
                        "date": "2024",
                        "instrument": "decision",
                        "description": "The institute was designated.",
                        "source_ids": ["decoy"],
                    }
                ],
            }
        ],
        "institutes": [
            {
                "id": "institute",
                "jurisdiction_id": "GB",
                "name": "Institute",
                "legal_form": "new_body",
                "host": {"name": "Ministry"},
                "mandate": "Tests models.",
                "mandate_short": "Tests models.",
                "source_ids": ["decoy"],
                "funding": [],
                "staff": [],
            }
        ],
        "sources": [],
    }


def source(data, identity, *supports):
    data["sources"].append({"id": identity, "excerpts": [{"supports": list(supports)}]})


def test_current_fiscal_dates_inclusive_and_order_independent():
    old = funding("old", "2025-04-01", "2026-03-31")
    current = funding("current", "2026-04-01", "2027-03-31", commitment="announced")
    future = funding("future", "2027-04-01", "2028-03-31")
    for records in ([old, future, current], [current, old, future]):
        assert selected(records, "2026-04-01") == "current"
        assert selected(records, "2027-03-31") == "current"
        assert selected(records, "2026-03-31") == "old"


def test_end_boundary_keeps_current_allocation_preference():
    mean = funding("mean", end="2026-10-18", annual_basis_code="period_mean")
    allocation = funding("allocation", end="2026-10-18", commitment="announced")
    assert selected([mean, allocation], "2026-10-18") == "allocation"
    assert selected([mean, allocation], "2026-10-19") == "mean"


def test_reference_year_overlap_and_proposals_never_compared():
    """Hand-worked: the reference year of 2026-10-18 is 2026; only committed
    periods overlapping it qualify, the one containing the date first."""
    last_year = funding("last-year", "2025-01-01", "2025-12-31")
    proposal = funding("proposal", commitment="proposed")
    next_year = funding("next-year", "2027-01-01", "2027-12-31", commitment="announced")
    assert selected([last_year, proposal, next_year]) is None
    ended = funding("ended", "2025-10-01", "2026-09-30")
    ahead = funding("ahead", "2026-11-01", "2027-10-31", commitment="announced")
    ahead_later = funding("ahead-later", "2026-12-01", "2027-11-30")
    for rows in ([ahead_later, ahead, last_year], [last_year, ahead, ahead_later]):
        assert selected(rows) == "ahead"
    for rows in ([ahead, ended, proposal], [proposal, ended, ahead]):
        assert selected(rows) == "ended"
    containing = funding("containing", "2026-04-01", "2027-03-31", commitment="announced")
    assert selected([ended, ahead, containing, proposal]) == "containing"
    # Year boundaries are inclusive: one shared day is an overlap.
    assert selected([funding("edge", "2025-01-01", "2026-01-01")]) == "edge"
    assert selected([funding("edge", "2026-12-31", "2027-12-30")]) == "edge"
    assert selected([funding("edge", "2027-01-01", "2027-12-31")], "2026-12-31") is None


@pytest.mark.parametrize(
    "changes",
    [
        {"coverage": "component"},
        {"coverage": None},
        {"kind": "one_off"},
        {"kind": "not_institute_specific"},
        {"eur_per_year_est": None},
        {"eur_per_year_est": True},
        {"annual_period": None},
        {"decision_date": "2027"},
    ],
)
def test_invalid_comparison_candidates(changes):
    assert selected([funding("candidate", **changes)]) is None


def test_funding_preferences_and_stable_identity_tie_break():
    allocation = funding("allocation", commitment="announced")
    mean = funding("mean", annual_basis_code="period_mean", kind="multi_year")
    assert selected([mean, allocation]) == "allocation"
    adopted = funding("adopted")
    assert selected([allocation, adopted]) == "adopted"
    latest = funding("latest", decision_date="2026-10-18")
    assert selected([adopted, latest]) == "latest"
    a, b = funding("a"), funding("b")
    assert selected([b, a]) == selected([a, b]) == "a"
    # Missing identities fail rather than deriving identity from array position.
    with pytest.raises(KeyError, match="id"):
        comparable_funding([{k: v for k, v in row.items() if k != "id"} for row in (a, b)], "2026-10-18")


def test_budget_fallback_and_zero_are_preserved():
    component = funding("component", coverage="component")
    future = funding("future", "2027-01-01", "2027-12-31", commitment="announced")
    note = funding("one-off", kind="one_off", amount=10, currency="USD")
    assert funding_selection([component, future], "2026-10-18") == (None, None)
    assert funding_selection([component, future, note], "2026-10-18") == (None, 2)
    assert funding_selection([funding("zero", eur_per_year_est=0)], "2026-10-18") == (
        0,
        0,
    )


def test_staff_selection_prefers_latest_total_over_newer_technical():
    rows = [
        {"id": "zero", "total": 0, "as_of": "2024"},
        {"id": "technical", "technical": 8, "as_of": "2026"},
        {"id": "total", "total": 20, "as_of": "2025"},
        {"id": "description", "description": "Recruiting", "as_of": "2027"},
    ]
    assert staff_selection(rows) == (2, 2, 1)
    assert staff_selection([rows[1], rows[3]]) == (0, None, 0)
    assert staff_selection([rows[3]]) == (0, None, None)
    assert staff_selection([]) == (None, None, None)


def test_field_specific_citations_never_fall_back_to_record_sources():
    data = document()
    source(data, "decoy", "/institutes/0/notes")
    source(data, "name", "/institutes/0/name")
    source(data, "host", "/institutes/0/host/name")
    source(data, "mandate", "/institutes/0/mandate_short")
    source(data, "finding", "/jurisdictions/0/summary")
    source(data, "identifier", "/institutes/0/wikidata")
    data["institutes"][0].update(wikidata="Q1", identifier_source_ids=["identifier"])
    before = copy.deepcopy(data)
    result = derive_display(data)
    institute, jurisdiction = result["institutes"][0], result["jurisdictions"][0]
    assert institute["citations"]["form"] == ["host"]
    assert institute["citations"]["mandate"] == ["mandate"]
    assert institute["citations"]["inst"] == ["name"]
    assert jurisdiction["citations"]["inst"] == ["name"]
    assert jurisdiction["citations"]["finding"] == ["finding"]
    assert jurisdiction["citations"]["since"] == []
    assert "identifier" not in institute["citations"]["display"]
    assert data == before


def test_date_status_routes_and_small_state_advisory_boundaries():
    data = document()
    source(data, "date", "/jurisdictions/0/timeline/0/date")
    source(data, "act", "/jurisdictions/0/timeline/0/event")
    result = derive_display(data)
    assert result["jurisdictions"][0]["citations"]["since"] == ["date"]
    assert result["jurisdictions"][0]["citations"]["status_since"] == ["date"]
    assert result["jurisdictions"][0]["citations"]["status"] == ["date", "act"]
    assert result["institutes"][0]["citations"]["status"] == ["date", "act"]
    assert result["institutes"][0]["citations"]["announced"] == []
    assert result["institutes"][0]["citations"]["established"] == ["date"]
    assert {key: result["jurisdictions"][0]["display"][key] for key in ("small_state", "advisory")} == {
        "small_state": False,
        "advisory": False,
    }
    data["jurisdictions"][0]["population"]["value"] = 9_999_999
    data["jurisdictions"][0]["timeline"][0]["instrument"] = "advisory_report"
    assert {key: derive_display(data)["jurisdictions"][0]["display"][key] for key in ("small_state", "advisory")} == {
        "small_state": True,
        "advisory": True,
    }


def test_closure_status_cites_closure_without_inventing_since():
    data = document()
    jurisdiction = data["jurisdictions"][0]
    jurisdiction["status"] = "none_identified"
    jurisdiction.pop("status_since")
    jurisdiction["timeline"].append(
        {"id": "closed", "event": "closed", "date": "2025", "source_ids": ["decoy"]}
    )
    source(data, "closure", "/jurisdictions/0/timeline/1/event")
    source(data, "old", "/jurisdictions/0/timeline/0/event")
    result = derive_display(data)
    assert result["jurisdictions"][0]["citations"]["status"] == ["closure"]
    assert result["jurisdictions"][0]["citations"]["since"] == []


def test_reopening_with_same_partial_date_cites_current_run():
    data = document()
    data["jurisdictions"][0]["timeline"].extend(
        [
            {
                "id": "closed",
                "event": "closed",
                "date": "2024",
                "source_ids": ["decoy"],
            },
            {
                "id": "reopened",
                "event": "designated",
                "date": "2024",
                "source_ids": ["decoy"],
            },
        ]
    )
    source(data, "old", "/jurisdictions/0/timeline/0/date")
    source(data, "current", "/jurisdictions/0/timeline/2/date")
    assert derive_display(data)["jurisdictions"][0]["citations"]["since"] == ["current"]


def test_record_level_support_does_not_imply_every_field():
    data = document()
    source(data, "record", "/institutes/0")
    result = derive_display(data)
    assert result["institutes"][0]["citations"]["form"] == []
    assert result["institutes"][0]["citations"]["mandate"] == []


def test_operating_and_first_output_use_credit_leaves_including_exclusions():
    data = document()
    institute = data["institutes"][0]
    institute["outputs"] = [
        {
            "lineage": "early",
            "date": "2022",
            "qualifies_as": "none",
            "credit": {"institute_id": "institute", "date": "2023"},
            "source_ids": ["publication"],
        }
    ]
    institute["operating"] = [
        {"lineage": "early", "date": "2023", "source_ids": ["publication"]}
    ]
    data["jurisdictions"][0]["timeline"].append(
        {
            "id": "gb-first-output",
            "event": "first_output",
            "date": "2023",
            "output_lineage": "early",
            "short": "First output",
            "source_ids": ["publication"],
        }
    )
    source(data, "publication", "/institutes/0/outputs/0/date")
    source(data, "credit-name", "/institutes/0/outputs/0/credit/institute_id")
    source(data, "credit-date", "/institutes/0/outputs/0/credit/date")
    assign_credit_provenance(data)
    before = copy.deepcopy(data)
    result = derive_display(data)
    assert data == before
    assert result["institutes"][0]["operating"][0]["source_ids"] == before["institutes"][0]["operating"][0]["source_ids"]
    expected = ["credit-name", "credit-date"]
    assert result["institutes"][0]["operating"][0]["citations"]["display"] == expected
    assert result["jurisdictions"][0]["timeline"][1]["citations"]["display"] == expected
    del institute["outputs"][0]["credit"]
    assign_credit_provenance(data)
    assert (
        derive_display(data)["institutes"][0]["operating"][0]["citations"]["display"]
        == []
    )


def test_funding_notes_and_currency_inputs_are_cited():
    data = document()
    data["dataset"]["exchange_rates"] = [
        {
            "currency": "GBP",
            "units_per_eur": 1,
            "eur_per_unit": 1,
            "source_ids": ["decoy"],
        },
        {
            "currency": "USD",
            "units_per_eur": 1.25,
            "eur_per_unit": 0.8,
            "source_ids": ["decoy"],
        },
    ]
    data["institutes"][0]["funding"] = [
        funding(
            "budget",
            currency="GBP",
            annual_amount=80,
            notes="An upper bound.",
            source_ids=["decoy"],
        )
    ]
    source(data, "amount", "/institutes/0/funding/0/annual_amount")
    source(data, "note", "/institutes/0/funding/0/notes")
    source(
        data,
        "rates",
        "/dataset/exchange_rates/0/units_per_eur",
        "/dataset/exchange_rates/1/units_per_eur",
    )
    result = derive_display(data)
    assert result["institutes"][0]["funding"][0]["citations"]["eur_per_year_est"] == [
        "amount",
        "rates",
    ]
    assert result["institutes"][0]["funding"][0]["citations"]["display"] == [
        "amount",
        "note",
        "rates",
    ]
    assert {key: result["institutes"][0]["display"][key] for key in ("comparison_funding", "budget", "staff", "total_staff", "technical_staff")} == {
        "comparison_funding": "budget",
        "budget": "budget",
        "staff": None,
        "total_staff": None,
        "technical_staff": None,
    }


@pytest.mark.parametrize(
    ("kind", "annual_amount", "usd", "shown", "usd_sources"),
    [
        ("annual", None, None, False, []),
        ("one_off", 80, 100, False, ["amount", "currency", "rates"]),
        ("annual", 0, 0, True, ["amount", "currency", "rates"]),
    ],
)
def test_funding_display_rates_only_accompany_shown_usd_values(
    kind, annual_amount, usd, shown, usd_sources
):
    data = document()
    data["dataset"]["exchange_rates"] = [
        {
            "currency": "GBP",
            "units_per_eur": 1,
            "eur_per_unit": 1,
            "source_ids": ["rates"],
        },
        {
            "currency": "USD",
            "units_per_eur": 1.25,
            "eur_per_unit": 0.8,
            "source_ids": ["rates"],
        },
    ]
    data["institutes"][0]["funding"] = [
        funding(
            "budget",
            kind=kind,
            currency="GBP",
            annual_amount=annual_amount,
            eur_per_year_est=usd,
            source_ids=["decoy"],
        )
    ]
    if annual_amount is not None:
        source(data, "amount", "/institutes/0/funding/0/annual_amount")
    source(data, "currency", "/institutes/0/funding/0/currency")
    source(
        data,
        "rates",
        "/dataset/exchange_rates/0/units_per_eur",
        "/dataset/exchange_rates/1/units_per_eur",
    )
    row = derive_display(data)["institutes"][0]["funding"][0]
    assert row["display"]["annual_estimate"] is shown
    assert row["citations"]["eur_per_year_est"] == usd_sources
    expected_display = (["amount"] if annual_amount is not None else []) + ["currency"]
    if shown:
        expected_display.append("rates")
    assert row["citations"]["display"] == expected_display


def test_derived_annual_amount_uses_component_and_period_inputs():
    data = document()
    data["institutes"][0]["funding"] = [
        funding(
            "mean",
            currency="USD",
            annual_basis_code="period_mean",
            amount=300,
            years=3,
            annual_amount=100,
            source_ids=["decoy"],
        ),
        funding("allocation", currency="USD", annual_amount=200, source_ids=["decoy"]),
        funding(
            "total",
            currency="USD",
            annual_amount=300,
            sum_of=[0, 1],
            source_ids=["decoy"],
        ),
    ]
    source(data, "total", "/institutes/0/funding/0/amount")
    source(data, "duration", "/institutes/0/funding/0/years")
    source(data, "allocation", "/institutes/0/funding/1/annual_amount")
    rows = derive_display(data)["institutes"][0]["funding"]
    assert rows[0]["citations"]["eur_per_year_est"] == ["total", "duration"]
    assert rows[2]["citations"]["eur_per_year_est"] == [
        "total",
        "duration",
        "allocation",
    ]


def test_current_profile_period_uses_only_selected_date_evidence():
    data = document()
    data["institutes"][0]["funding"] = [funding(
        "profile", annual_basis_code="allocation_current_year", annual_profile=[
            {"amount": 7, "start": "2025-01-01", "end": "2025-12-31"},
            {"amount": 11, "start": "2026-01-01", "end": "2026-12-31"},
        ], source_ids=["decoy"],
    )]
    prefix = "/institutes/0/funding/0/annual_profile"
    source(data, "old", f"{prefix}/0/start", f"{prefix}/0/end")
    source(data, "amount", f"{prefix}/1/amount")
    source(data, "start", f"{prefix}/1/start")
    source(data, "end", f"{prefix}/1/end")
    citations = derive_display(data)["institutes"][0]["funding"][0]["citations"]
    assert citations["annual_amount"] == ["amount"]
    assert citations["annual_period"] == ["start", "end"]


def test_recent_developments_group_full_days_only_and_limit_ten():
    data = document()
    base = {"event": "commitment", "short": "A short account.", "source_ids": ["decoy"]}
    data["jurisdictions"][0]["timeline"] = [
        dict(base, id=f"event-{n}", date=f"2026-10-{n:02d}") for n in range(1, 12)
    ]
    data["jurisdictions"][0]["timeline"].extend(
        [
            dict(base, id="same-day", date="2026-10-11"),
            dict(base, id="month-a", date="2026-10"),
            dict(base, id="month-b", date="2026-10"),
        ]
    )
    source(data, "event", "/jurisdictions/0/timeline/10/date")
    source(data, "text", "/jurisdictions/0/timeline/11/short")
    result = derive_display(data)["dataset"]["display"]["developments"]
    assert len(result) == 10
    assert result[0]["events"] == ["event-11", "same-day"]
    assert result[0]["source_ids"] == ["event", "text"]
    data["jurisdictions"][0]["timeline"] = data["jurisdictions"][0]["timeline"][-2:]
    data["sources"] = []
    assert len(derive_display(data)["dataset"]["display"]["developments"]) == 2


def test_sentence_abbreviations_and_resolved_pointer_requirement():
    assert first_sentence("Dr. Smith spoke. A second sentence.") == "Dr. Smith spoke."
    data = document()
    source(data, "bad", ["institutes", {"id": "institute"}, "name"])
    with pytest.raises(ValueError, match="requires resolved excerpt pointers"):
        derive_display(data)


def test_real_comparison_matches_hand_checked_owner_choices():
    from scripts.published import materialise_facts
    from scripts.migrate_references import migrate
    from scripts.references import materialise_references

    data = json.loads((Path(__file__).parents[1] / "data/aisi.json").read_text())
    result = derive_display(materialise_references(materialise_facts(migrate(data))))
    actual = {}
    for institute in result["institutes"]:
        n = institute["display"]["comparison_funding"]
        if n is not None:
            row = institute["display"]["funding_records"][n]
            actual[institute["jurisdiction_id"]] = (
                row["period"],
                row["eur_per_year_est"],
            )
    assert actual == {
        "GB": ("2026/27 to 2029/30", 70_028_600),
        "US": ("FY2026 enacted", 8_849_690),
        "CA": ("from 2024-25", 6_334_220),
        "KR": ("2026 total", 8_019_550),
        "AU": ("2025-26 to 2028-29", 5_993_970),
    }


def test_fallback_own_allocation_uses_source_dates_and_ids_only():
    old = funding("older", kind="one_off", amount=380, currency="JPY", annual_period={"start": "2024-04-01", "end": "2025-03-31"})
    latest = funding("latest", kind="one_off", amount=8800, currency="JPY", annual_period={"start": "2025-04-01", "end": "2026-03-31"})
    wider = dict(latest, id="wider", coverage="not_institute_specific", decision_date="2026-10-01")
    component = dict(latest, id="component", coverage="component")
    future = dict(latest, id="future", decision_date="2027-01-01")
    proposal = dict(latest, id="proposal", commitment="proposed")
    undated = dict(latest, id="a-undated", annual_period=None)
    for rows in ([old, latest, wider, component, future, proposal, undated], [undated, proposal, future, component, wider, latest, old]):
        comparison, budget = funding_selection(rows, "2026-10-18")
        assert comparison is None
        assert rows[budget]["id"] == "latest"
    tied = dict(latest, id="a-latest")
    assert [tied, latest][funding_selection([tied, latest], "2026-10-18")[1]]["id"] == "a-latest"
    # A proposal supplies Budget only when no committed own funding qualifies.
    assert funding_selection([wider, component, future, proposal], "2026-10-18") == (None, 3)
    assert funding_selection([wider, component, future], "2026-10-18") == (None, None)


def test_fallback_keeps_announced_future_period_and_excludes_future_decision():
    """Owner rule: only a decision date wholly after the dataset date excludes a record."""
    current = funding("current", kind="one_off", amount=10, currency="AUD", annual_period={"start": "2025-07-01", "end": "2026-06-30"})
    ahead = dict(current, id="ahead", commitment="announced", annual_period={"start": "2029-07-01", "end": "2030-06-30"})
    for rows in ([current, ahead], [ahead, current]):
        assert rows[funding_selection(rows, "2026-10-18")[1]]["id"] == "ahead"
    decided_later = dict(ahead, decision_date="2026-11")
    same_month = dict(ahead, id="same-month", decision_date="2026-10")
    assert [current, decided_later][funding_selection([current, decided_later], "2026-10-18")[1]]["id"] == "current"
    assert [current, same_month][funding_selection([current, same_month], "2026-10-18")[1]]["id"] == "same-month"
    yearly = dict(ahead, id="yearly", kind="annual", annual_period={"start": "2030-07-01", "end": "2031-06-30"})
    for rows in ([current, ahead, yearly], [yearly, ahead, current]):
        comparison, budget = funding_selection(rows, "2026-10-18")
        assert comparison is None and rows[budget]["id"] == "yearly"


def test_staff_and_development_equal_date_ties_use_ids():
    records = [{"id": "b", "total": 12, "as_of": "2026"}, {"id": "a", "total": 9, "as_of": "2026"}]
    for rows in (records, records[::-1]):
        assert rows[staff_selection(rows)[0]]["id"] == "a"
    from scripts.display import recent_developments
    events = [{"id": identity, "event": "designated", "date": "2026", "short": identity} for identity in ("b", "a")]
    for rows in (events, events[::-1]):
        groups = recent_developments([{"id": "XX", "timeline": rows}], lambda *values: [])
        assert [group["events"] for group in groups] == [["a"], ["b"]]
    for event in events:
        event["date"] = "2026-10-01"
    assert recent_developments([{"id": "XX", "timeline": events}], lambda *values: [])[0]["events"] == ["a", "b"]


@pytest.mark.parametrize("qualifier,prefix", [("at_most", "Up to "), ("at_least", "At least "), ("more_than", "More than "), ("less_than", "Less than "), ("nearly", "Nearly "), ("about", "About ")])
def test_structured_budget_bounds_survive_annual_conversion(qualifier, prefix):
    from scripts.display import funding_label

    value, qualifiers = funding_label(
        funding(
            "budget",
            currency="USD",
            annual_amount=10_000_000,
            eur_per_year_est=10_000_000,
            amount_qualifier=qualifier,
            period="FY2026 enacted",
        ),
        rates={"USD": 1},
    )
    assert value == prefix + ("" if qualifier == "about" else "~") + "EUR 10m (USD 10m)"
    assert qualifiers == ["a year", "FY2026 enacted"]  # the period label already states the commitment


def test_all_display_choices_survive_reversal_and_fixed_shuffle():
    import random
    from scripts.published import publish

    authored = json.loads((Path(__file__).parents[1] / "data/aisi.json").read_text())
    original = publish(authored, strict=True)
    def choices(data):
        return {
            "institutes": {row["id"]: row["display"] for row in data["institutes"]},
            "jurisdictions": {row["id"]: row["display"] for row in data["jurisdictions"]},
            "dataset": data["dataset"]["display"],
        }
    for shuffled in (False, True):
        data = copy.deepcopy(authored)
        randomiser = random.Random(1964)
        def reorder(values):
            if shuffled:
                randomiser.shuffle(values)
            else:
                values.reverse()
        for institute in data["institutes"]:
            for collection in ("funding", "staff", "outputs"):
                reorder(institute.get(collection, []))
        for jurisdiction in data["jurisdictions"]:
            reorder(jurisdiction.get("timeline", []))
        reorder(data["dataset"]["maintainers"])
        reorder(data["changelog"])
        assert choices(publish(data, strict=True)) == choices(original)


def test_activity_display_same_date_uses_kind_and_lineage_keys():
    data = document()
    data["institutes"][0]["operating"] = [
        {"kind": "published_work", "date": "2026", "lineage": "report", "source_ids": []},
        {"kind": "joint_testing", "date": "2026", "lineage": "exercise", "source_ids": []},
    ]
    first = derive_display(data)["institutes"][0]["display"]["operating"]
    data["institutes"][0]["operating"].reverse()
    assert derive_display(data)["institutes"][0]["display"]["operating"] == first
    assert first["since"] == "2026"
    assert first["first_lineage"] == "exercise"
    assert [row["lineage"] for row in first["items"]] == ["exercise", "report"]


def test_multiyear_budget_fallback_keeps_native_total_without_comparison():
    data = document()
    data["institutes"][0]["funding"] = [funding(
        "allocation", kind="multi_year", annual_period=None, currency="CAD",
        amount=50_000_000, eur_per_year_est=7_150_000, period="over five years",
        source_ids=[],
    )]
    display = derive_display(data)["institutes"][0]["display"]
    assert display["comparison_funding"] is None
    assert display["budget"] == "allocation"
    record = display["funding_records"]["allocation"]
    assert record["display"]["value"] == "CAD 50m"
    assert record["display"]["qualifiers"] == ["multi-year", "over five years", "adopted"]


def test_native_funding_amount_is_rounded_to_two_significant_figures():
    from scripts.display import funding_label
    record = {"kind": "one_off", "amount": 12_345_678, "currency": "USD", "commitment": "adopted"}
    assert funding_label(record, annual=False, context=False)[0] == "~USD 12m"


def test_compact_budget_keeps_notes_for_the_disclosure():
    from scripts.display import funding_label
    record = funding("own", kind="one_off", amount=8_800_000_000, currency="JPY", period="FY2025 supplementary", notes="Long scope reasoning.")
    value, qualifiers = funding_label(record, annual=False)
    assert value == "JPY 8.8bn"
    assert qualifiers == ["one-off", "FY2025 supplementary", "adopted"]


def test_compact_confidence_qualifier_carries_its_citation():
    data = document()
    data["institutes"][0]["funding"] = [
        {"id": "own", "kind": "one_off", "amount": 5, "currency": "USD", "coverage": "whole_institute", "commitment": "adopted", "confidence": "medium", "source_ids": []}
    ]
    source(data, "amount", "/institutes/0/funding/0/amount")
    source(data, "confidence", "/institutes/0/funding/0/confidence")
    row = derive_display(data)["institutes"][0]["funding"][0]
    assert "indirect or secondary" in row["display"]["qualifiers"]
    assert row["display"]["budget_citations"] == ["amount", "confidence"]


@pytest.mark.parametrize(
    ("period", "commitment", "shown"),
    [
        ("2027 budget proposal", "proposed", False),
        ("FY2027 President's Budget request", "proposed", False),
        ("FY2026 enacted", "adopted", False),
        ("2026", "adopted", True),
        ("2027 budget proposal", "adopted", True),
        ("2025-26 to 2028-29", "announced", True),
    ],
)
def test_commitment_is_not_repeated_by_its_period_label(period, commitment, shown):
    record = {"amount": 5000000, "currency": "EUR", "kind": "annual", "period": period, "commitment": commitment}
    from scripts.display import funding_label

    _, qualifiers = funding_label(record, annual=False)
    assert (commitment in qualifiers) is shown


def test_bodies_carry_their_own_sources_for_the_marker_after_their_entry():
    data = document()
    data["jurisdictions"][0]["related_bodies"] = [
        {"id": "b1", "name": "Regulator", "kind": "other", "role": "Role", "source_ids": ["z", "a"]},
        {"id": "b2", "name": "Other", "kind": "other", "role": "Role"},
    ]
    data["jurisdictions"][0]["closest_body"] = {"name": "Lab", "stage": "exists", "gap": "remit", "note": "n", "source_ids": ["a"]}
    j = derive_display(data)["jurisdictions"][0]
    assert j["related_bodies"][0]["display"]["source_links"] == ["a", "z"]
    assert j["related_bodies"][1]["display"]["source_links"] == []
    assert j["closest_body"]["display"]["source_links"] == ["a"]


def test_population_and_region_sources_are_generic_not_entry_sources():
    data = document()
    j = data["jurisdictions"][0]
    j["population"] = {"value": 1, "year": 2025, "source_ids": ["pop"]}
    j["region"] = {"id": "150", "name": "Europe", "source_ids": ["m49"]}
    result = derive_display(data)
    assert "pop" not in result["jurisdictions"][0]["display"]["source_links"]
    assert "m49" not in result["jurisdictions"][0]["display"]["source_links"]
    assert "decoy" in result["jurisdictions"][0]["display"]["source_links"]
    assert result["dataset"]["display"]["generic_sources"] == ["m49", "pop"]


@pytest.mark.parametrize("where, side", [("source_ids", "jurisdictions"), ("timeline", "jurisdictions"), ("funding", "institutes")])
def test_a_source_that_is_also_population_stays_when_it_supports_the_entry(where, side):
    data = document()
    j = data["jurisdictions"][0]
    j["population"] = {"value": 1, "year": 2025, "source_ids": ["shared"]}
    j["source_ids"] = ["decoy"]
    j["timeline"][0]["source_ids"] = ["decoy"]
    inst = data["institutes"][0]
    if where == "source_ids":
        j["source_ids"].append("shared")
    elif where == "timeline":
        j["timeline"][0]["source_ids"] = ["shared"]
    else:
        inst["funding"] = [{**funding("f"), "source_ids": ["shared"]}]
    result = derive_display(data)
    assert "shared" in result[side][0]["display"]["source_links"]


def test_generic_fields_are_skipped_on_the_record_only_not_in_nested_records():
    record = {"population": {"source_ids": ["p"]}, "source_ids": ["a"], "timeline": [{"population": {"source_ids": ["n"]}}]}
    assert sorted(record_sources(record)) == ["a", "n"]
