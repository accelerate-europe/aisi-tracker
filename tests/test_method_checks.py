"""Mutation checks for the owner's revised method, with hand-worked expectations."""

import copy

import pytest
from scripts.published import materialise_facts

from scripts.method_checks import (
    check_method_rules,
    negative_confidence_limit,
    required_excerpt_pointers,
)


def strict_dataset(dataset):
    d = copy.deepcopy(dataset)
    assert check_method_rules(d) == []
    return d


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (
            lambda d: d["institutes"][0]["outputs"][0].pop("attribution"),
            "institutional attribution",
        ),
        (
            lambda d: d["institutes"][0]["outputs"].append(
                copy.deepcopy(d["institutes"][0]["outputs"][0])
            ),
            "duplicate output lineage",
        ),
        (
            lambda d: d["institutes"][0]["agreements"][0].pop("participation"),
            "qualifying participation",
        ),
        (
            lambda d: d["institutes"][0]["agreements"][0].update(
                domestic_interagency=True
            ),
            "domestic exclusion",
        ),
        (
            lambda d: d["institutes"][0]["agreements"][0].update(
                counterparty_type="institute", peer_institute_id="inria"
            ),
            "identify a peer institute",
        ),
        (
            lambda d: d["institutes"][0]["check"].update(last_checked="2026-10-18"),
            "full check is after",
        ),
        (
            lambda d: d["institutes"][0]["check"].update(
                partial=[{"date": "2026-10-18", "scope": "Identifiers"}]
            ),
            "partial check is after",
        ),
        (
            lambda d: d["institutes"][0]["people"][0].update(
                title_status="unpublished"
            ),
            "unpublished title requires",
        ),
        (
            lambda d: d["sources"][0].update(
                snapshot={"retrieved": "2026-10-17T00:00:00Z", "sha256": "a" * 64},
                access_note="No usable source copy was retained.",
            ),
            "access_note says no copy was kept",
        ),
    ],
)
def test_revised_method_mutations(dataset, mutation, message):
    d = strict_dataset(dataset)
    mutation(d)
    assert any(message in e for e in check_method_rules(d))


def test_short_names_are_unique_and_never_a_bare_generic_acronym(dataset):
    d = strict_dataset(dataset)
    assert not [e for e in check_method_rules(d) if "short_name" in e]
    d["institutes"][0]["short_name"] = "AISI"
    assert any("bare generic acronym" in e for e in check_method_rules(d))
    d["institutes"][0]["short_name"] = "A-I S I"
    assert any("bare generic acronym" in e for e in check_method_rules(d))
    d["institutes"][0]["short_name"] = ""
    assert any("needs a short_name" in e for e in check_method_rules(d))
    d["institutes"].append({**d["institutes"][0], "id": "gb-test-2", "short_name": "UK Synthetic"})
    d["institutes"][0]["short_name"] = "uk synthetic"
    assert any("is also used by" in e for e in check_method_rules(d))


def test_non_latin_body_names_need_an_english_name(dataset):
    d = strict_dataset(dataset)
    body = d["jurisdictions"][0]["related_bodies"][0]
    assert not [e for e in check_method_rules(d) if "name_en" in e]
    body["name"] = "Комисия за финансов надзор"
    assert any("needs name_en" in e for e in check_method_rules(d))
    body["name_en"] = "Financial Supervision Commission"
    assert not [e for e in check_method_rules(d) if "name_en" in e]
    body["name"], body["name_en"] = "Synthetic regulator", "Synthetic regulator"
    assert any("repeats the name" in e for e in check_method_rules(d))


def test_required_claims_need_exact_not_parent_pointers(dataset):
    d = strict_dataset(dataset)
    d["sources"][0]["excerpts"][-1]["supports"] = ["/institutes/0", "/jurisdictions/0"]
    errors = check_method_rules(d)
    assert "required claim has no excerpt: /jurisdictions/0/status_since" in errors
    assert "required claim has no excerpt: /institutes/0/funding/0/amount" in errors
    assert "required claim has no excerpt: /institutes/0/staff/0/total" in errors
    assert (
        "required claim has no excerpt: /institutes/0/agreements/0/counterparty"
        in errors
    )
    assert "required claim has no excerpt: /institutes/0/legal_basis/type" in errors


def test_glosses_do_not_repeat_across_distinct_excerpts(dataset):
    d = strict_dataset(dataset)
    d["sources"][0]["excerpts"] += [
        {
            "text": "Premier extrait.",
            "gloss": "One combined gloss.",
            "supports": ["/institutes/0/name"],
        },
        {
            "text": "Deuxieme extrait.",
            "gloss": "One combined gloss.",
            "supports": ["/institutes/0/name"],
        },
    ]
    assert any("identical glosses" in e for e in check_method_rules(d))


def negative_record():
    return {
        "id": "XX",
        "source_ids": ["one", "two"],
        "check": {
            "method": "individual",
            "governance_source_ids": ["one"],
            "governance_note": "The government describes its ministry-led AI strategy.",
            "independent_source_ids": ["one", "two"],
            "searches": [
                {
                    "date": "2026-10-17",
                    "language": "fr",
                    "query": "institut securite IA",
                    "national_or_official": True,
                    "source_ids": ["one", "two"],
                },
                {
                    "date": "2026-10-17",
                    "language": "en",
                    "query": "national AI safety institute",
                    "national_or_official": False,
                    "source_ids": [],
                },
            ],
            "explicit_absence": {"source_id": "one", "date": "2025-10-17"},
        },
    }


def negative_sources():
    return {
        "one": {
            "publisher": "Government",
            "type": "primary",
            "published": "2025-10-17",
        },
        "two": {"publisher": "Independent press", "type": "secondary"},
    }


@pytest.mark.parametrize(
    ("publication", "expected"),
    [("2025-10-17", "high"), ("2025-10-16", "medium"), ("2026-10-18", "medium")],
)
def test_negative_calendar_anniversary(publication, expected):
    record, sources = negative_record(), negative_sources()
    record["check"]["explicit_absence"]["date"] = publication
    sources["one"]["published"] = publication
    assert negative_confidence_limit(record, sources, "2026-10-17") == expected


def test_negative_leap_day():
    record, sources = negative_record(), negative_sources()
    for query in record["check"]["searches"]:
        query["date"] = "2024-02-29"
    record["check"]["explicit_absence"]["date"] = "2023-02-28"
    sources["one"]["published"] = "2023-02-28"
    assert negative_confidence_limit(record, sources, "2024-02-29") == "high"
    sources["one"]["published"] = record["check"]["explicit_absence"]["date"] = (
        "2023-02-27"
    )
    assert negative_confidence_limit(record, sources, "2024-02-29") == "medium"


@pytest.mark.parametrize(
    "missing", ["local_search", "english_search", "individual", "governance", "note", "primary", "date"]
)
def test_negative_requirements(missing):
    record, sources = negative_record(), negative_sources()
    if missing == "local_search":
        record["check"]["searches"][0]["national_or_official"] = False
    elif missing == "english_search":
        record["check"]["searches"].pop()
    elif missing == "individual":
        record["check"]["method"] = "broad_search"
    elif missing == "governance":
        record["check"]["governance_source_ids"] = []
    elif missing == "note":
        record["check"]["governance_note"] = "   "
    elif missing == "primary":
        sources["one"]["type"] = "secondary"
    else:
        sources["one"]["published"] = "2025-10"
    assert negative_confidence_limit(record, sources, "2026-10-17") == (
        "medium" if missing == "date" else "low"
    )


@pytest.mark.parametrize("language", ["en", "en-GB"])
def test_negative_single_primary_source_and_official_english(language):
    record, sources = negative_record(), negative_sources()
    record["source_ids"] = ["one"]
    record["check"].pop("explicit_absence")
    record["check"].pop("independent_source_ids")
    record["check"]["searches"] = [dict(record["check"]["searches"][1], national_or_official=True, language=language)]
    assert negative_confidence_limit(record, sources, "2026-10-17") == "medium"
    record["check"]["searches"][0]["national_or_official"] = False
    assert negative_confidence_limit(record, sources, "2026-10-17") == "low"


def test_negative_independence_metadata_does_not_change_limit():
    record, sources = negative_record(), negative_sources()
    record["check"].pop("explicit_absence")
    record["check"]["independent_source_ids"] = []
    sources["two"]["publisher"] = sources["one"]["publisher"]
    assert negative_confidence_limit(record, sources, "2026-10-17") == "medium"
    record["check"]["independent_source_ids"] = ["one", "two"]
    record["check"].pop("governance_source_ids")
    assert negative_confidence_limit(record, sources, "2026-10-17") == "low"


def test_secondary_absence_statement_cannot_raise_medium_to_high():
    record, sources = negative_record(), negative_sources()
    record["check"]["governance_source_ids"] = ["two"]
    sources["two"].update(type="primary", publisher="Digital ministry")
    sources["one"]["type"] = "secondary"
    assert negative_confidence_limit(record, sources, "2026-10-17") == "medium"


@pytest.mark.parametrize("query", ["", "   "])
def test_negative_blank_query_does_not_count(query):
    record, sources = negative_record(), negative_sources()
    record["check"]["searches"][1]["query"] = query
    assert negative_confidence_limit(record, sources, "2026-10-17") == "low"


def test_current_allocation_overrides_mean(dataset):
    d = strict_dataset(dataset)
    f = d["institutes"][0]["funding"][0]
    f.update(
        amount=29_900_000,
        years=4,
        period="2025-26 to 2028-29",
        annual_amount=7_475_000,
        annual_basis="Mean over four years.",
        annual_basis_code="period_mean",
        annual_profile=[
            {
                "start": "2026-07-01",
                "end": "2027-06-30",
                "amount": 10_500_000,
                "source_ids": ["gb-source"],
            }
        ],
    )
    assert any("current fiscal profile" in e for e in check_method_rules(d))
    f.update(
        annual_basis_code="allocation_current_year",
        annual_amount=10_500_000,
        annual_period={"start": "2026-07-01", "end": "2027-06-30"},
    )
    assert not any(
        "annual" in e for e in check_method_rules(d) if "required claim" not in e
    )
    f["annual_period"]["end"] = "2026-06-30"
    assert any("cover dataset.as_of" in e for e in check_method_rules(d))


def test_mean_must_match_total_and_period(dataset):
    d = strict_dataset(dataset)
    f = d["institutes"][0]["funding"][0]
    f.update(
        amount=10,
        years=4,
        period="2023 to 2026",
        annual_amount=3,
        annual_basis="Mean",
        annual_basis_code="period_mean",
    )
    with pytest.raises(ValueError, match="annual_amount.*disagrees with derived 2.5"):
        materialise_facts(d)
    f["annual_amount"] = 2.5
    assert materialise_facts(d)["institutes"][0]["funding"][0]["annual_amount"] == 2.5


def test_authorities_need_basis_and_commission_list(dataset):
    d = strict_dataset(dataset)
    d["jurisdictions"][0]["eu_member"] = True
    body = d["jurisdictions"][0]["related_bodies"][0]
    body.update(kind="ai_act_authority", designation_state="designated")
    assert any("designation_basis" in e for e in check_method_rules(d))
    assert any("Commission list" in e for e in check_method_rules(d))
    body.update(kind="ai_act_authority", designation_basis="none_yet")
    assert any("enacted basis" in e for e in check_method_rules(d))


def test_unpublished_title_and_partial_check(dataset):
    d = strict_dataset(dataset)
    d["institutes"][0]["people"][0].update(
        role="lead", title_status="unpublished", notes="no formal title published"
    )
    d["institutes"][0]["check"]["partial"] = [
        {"date": "2026-10-17", "scope": "Sources only"}
    ]
    assert check_method_rules(d) == []


def test_strict_dispatch_and_schema(dataset):
    from scripts import build
    from jsonschema import Draft202012Validator
    import json

    d = strict_dataset(dataset)
    Draft202012Validator(json.loads(build.SCHEMA.read_text())).validate(d)
    assert build.check_consistency(d, complete=True) == []
    d["institutes"][0]["outputs"][0].pop("attribution")
    assert any(
        "institutional attribution" in e
        for e in build.check_consistency(d, complete=True)
    )


@pytest.mark.parametrize(
    ("section", "key", "value"),
    [
        ("outputs", "attribution", "staff_coauthor"),
        ("agreements", "participation", "government_only"),
        ("agreements", "domestic_interagency", True),
        ("funding", "annual_basis_code", "average"),
        ("people", "title_status", "guessed"),
    ],
)
def test_schema_rejects_invalid_method_fields(dataset, section, key, value):
    from scripts import build
    from jsonschema import Draft202012Validator
    import json

    d = strict_dataset(dataset)
    d["institutes"][0][section][0][key] = value
    errors = list(
        Draft202012Validator(json.loads(build.SCHEMA.read_text())).iter_errors(d)
    )
    assert (
        any(
            e.validator == "enum" and list(e.path) == ["institutes", 0, section, 0, key]
            for e in errors
        )
        if key != "domestic_interagency"
        else any(
            e.validator == "const"
            and list(e.path) == ["institutes", 0, section, 0, key]
            for e in errors
        )
    )


def test_negative_confidence_dispatch_and_official_quote(dataset):
    from scripts import build

    d = strict_dataset(dataset)
    d["dataset"]["exchange_rates"] = []
    j = d["jurisdictions"][0]
    j.update(status="none_identified", status_confidence="high", institute_id=None)
    j.pop("timeline")
    j.pop("status_since")
    d["institutes"] = []
    # Use the hand-worked two-publisher record, keeping the original source for roster data.
    r, sources = negative_record(), negative_sources()
    j["check"].update(r["check"])
    j["source_ids"] += r["source_ids"]
    for sid, source in sources.items():
        d["sources"].append(
            dict(
                id=sid,
                url=f"https://example.com/{sid}",
                title="Synthetic",
                language="en",
                accessed="2026-10-17",
                **source,
            )
        )
    d["sources"][0]["excerpts"] = []
    assert any(
        "explicit absence needs" in e for e in build.check_consistency(d, complete=True)
    )
    d["sources"][1]["excerpts"] = [
        {
            "text": "There is no institute and none is planned.",
            "supports": ["/jurisdictions/0/check/explicit_absence"],
        }
    ]
    assert build.check_consistency(d, complete=True) == []
    j["check"]["searches"][0]["date"] = "2026-10-18"
    errors = build.check_consistency(d, complete=True)
    assert any("negative confidence exceeds low" in e for e in errors)
    assert any("search is after" in e for e in errors)


def test_mean_requires_inputs_not_quotation_of_derived_amount(dataset):
    d = strict_dataset(dataset)
    f = d["institutes"][0]["funding"][0]
    f.update(
        amount=50,
        years=5,
        period="2024 to 2028",
        annual_amount=10,
        annual_basis="Five-year mean",
        annual_basis_code="period_mean",
        annual_profile=[
            {
                "start": "2024-01-01",
                "end": "2024-12-31",
                "amount": 8,
                "source_ids": ["gb-source"],
            }
        ],
    )
    pointers = required_excerpt_pointers(d)
    assert "/institutes/0/funding/0/annual_amount" not in pointers
    assert {
        "/institutes/0/funding/0/amount",
        "/institutes/0/funding/0/years",
        "/institutes/0/funding/0/period",
    } <= pointers
    assert not any("current fiscal profile" in e for e in check_method_rules(d))


def test_self_peer_missing_codes_and_orphan_first_output(dataset):
    d = strict_dataset(dataset)
    i = d["institutes"][0]
    i["agreements"][0].update(counterparty_type="institute", peer_institute_id=i["id"])
    i["people"][0].pop("title_status")
    i["funding"][0].update(annual_amount=1, annual_basis="Annual")
    i["outputs"] = []
    errors = check_method_rules(d)
    for expected in [
        "identify a peer",
        "leadership needs title_status",
        "annual amount needs annual_basis_code",
    ]:
        assert any(expected in e for e in errors)


def test_legacy_orphan_first_output_is_rejected_by_migration(dataset):
    dataset["institutes"][0]["outputs"] = []
    with pytest.raises(ValueError, match="first_output disagrees with output catalogue"):
        materialise_facts(dataset)


def test_peer_and_authority_positive_contract(dataset):
    d = strict_dataset(dataset)
    other = copy.deepcopy(d["institutes"][0])
    other.update(id="eu-office-test", jurisdiction_id="EU", short_name="EU Synthetic")
    other["agreements"] = []
    d["institutes"].append(other)
    j = copy.deepcopy(d["jurisdictions"][0])
    j.update(id="EU", institute_id=other["id"])
    d["jurisdictions"].append(j)
    d["institutes"][0]["agreements"][0].update(
        counterparty_type="institute", peer_institute_id=other["id"]
    )
    d["jurisdictions"][0]["eu_member"] = True
    body = d["jurisdictions"][0]["related_bodies"][0]
    body.update(kind="ai_act_authority", designation_state="designated", designation_basis="law")
    body["source_ids"].append("eu-ai-act-authorities-2026")
    d["sources"][0]["excerpts"][-1]["supports"] = sorted(required_excerpt_pointers(d))
    assert check_method_rules(d) == []
    d["institutes"][0]["agreements"][0]["counterparty_type"] = "other"
    assert any("non-institute counterparty" in e for e in check_method_rules(d))


def test_governance_source_must_be_cited_by_record():
    r, sources = negative_record(), negative_sources()
    r["source_ids"] = ["two"]
    assert negative_confidence_limit(r, sources, "2026-10-17") == "low"


@pytest.mark.parametrize("fault", ["uncited", "secondary", "unknown", "no_note", "no_sources", "empty_sources_only"])
def test_governance_metadata_validated_even_on_low_records(dataset, fault):
    d = strict_dataset(dataset)
    j = d["jurisdictions"][0]
    j["status"] = "none_identified"
    j["status_confidence"] = "low"
    j["check"].update(governance_source_ids=["gb-source"], governance_note="AI governance ministry.")
    assert not any("governance" in e for e in check_method_rules(d))
    if fault == "uncited":
        j["source_ids"] = []
    elif fault == "secondary":
        next(s for s in d["sources"] if s["id"] == "gb-source")["type"] = "secondary"
    elif fault == "unknown":
        j["check"]["governance_source_ids"] = ["absent"]
    elif fault == "no_note":
        j["check"]["governance_note"] = " "
    else:
        j["check"]["governance_source_ids"] = []
        if fault == "empty_sources_only":
            j["check"].pop("governance_note")
    assert any("governance" in e for e in check_method_rules(d))


def test_fiscal_profile_without_annual_amount_and_invalid_ranges(dataset):
    d = strict_dataset(dataset)
    f = d["institutes"][0]["funding"][0]
    f["annual_profile"] = [
        {
            "start": "2026-01-01",
            "end": "2026-12-31",
            "amount": 8,
            "source_ids": ["gb-source"],
        }
    ]
    assert any("current fiscal profile" in e for e in check_method_rules(d))
    f["annual_profile"].append(dict(f["annual_profile"][0]))
    assert any("overlapping current" in e for e in check_method_rules(d))
    f["annual_profile"] = [
        {
            "start": "2027-01-01",
            "end": "2026-12-31",
            "amount": 8,
            "source_ids": ["gb-source"],
        }
    ]
    assert any("invalid fiscal profile" in e for e in check_method_rules(d))


def test_all_new_evidence_shapes_and_orphan_basis(dataset):
    from scripts import build
    from jsonschema import Draft202012Validator
    import json

    d = strict_dataset(dataset)
    check = d["jurisdictions"][0]["check"]
    check.update(negative_record()["check"])
    check.update(
        partial=[{"date": "2026-10-17", "scope": "Source excerpts"}],
        language_note="French query",
        independence_note="Separate evidence origins",
    )
    i = d["institutes"][0]
    i["funding"][0].update(
        annual_amount=1,
        annual_basis="Current year",
        annual_basis_code="allocation_current_year",
        annual_period={"start": "2026-01-01", "end": "2026-12-31"},
        annual_profile=[
            {
                "start": "2026-01-01",
                "end": "2026-12-31",
                "amount": 1,
                "source_ids": ["gb-source"],
            }
        ],
    )
    i["agreements"][0].update(
        counterparty_type="institute", peer_institute_id="eu-ai-office"
    )
    body = d["jurisdictions"][0]["related_bodies"][0]
    body.update(
        kind="ai_act_authority",
        designation_state="designated",
        designation_basis="law",
        designation_note="National law takes precedence",
    )
    validator = Draft202012Validator(json.loads(build.SCHEMA.read_text()))
    validator.validate(d)
    i["funding"][0].pop("annual_amount")
    assert any(e.validator == "dependentRequired" for e in validator.iter_errors(d))
    i["funding"][0]["annual_amount"] = None
    assert any(e.validator == "type" for e in validator.iter_errors(d))


def test_current_allocation_needs_period_and_non_eu_authority(dataset):
    d = strict_dataset(dataset)
    f = d["institutes"][0]["funding"][0]
    f.pop("annual_period")
    f.update(
        annual_amount=1,
        annual_basis="Current",
        annual_basis_code="allocation_current_year",
    )
    assert any("cover dataset.as_of" in e for e in check_method_rules(d))
    body = d["jurisdictions"][0]["related_bodies"][0]
    body.update(kind="ai_act_authority", designation_basis="law", designation_state="designated")
    assert not any("Commission list" in e for e in check_method_rules(d))


@pytest.mark.parametrize("state", ["in_force", "foreseen"])
@pytest.mark.parametrize("leaf", ["type", "state"])
def test_none_found_legal_basis_requires_each_exact_leaf_quote(dataset, state, leaf):
    d = strict_dataset(dataset)
    d["institutes"][0]["legal_basis"].update(type="none_found", state=state)
    pointer = f"/institutes/0/legal_basis/{leaf}"
    assert pointer in required_excerpt_pointers(d)
    assert check_method_rules(d) == []
    pointers = d["sources"][0]["excerpts"][0]["supports"]
    pointers.remove(pointer)
    pointers.append("/institutes/0/legal_basis")
    assert f"required claim has no excerpt: {pointer}" in check_method_rules(d)


def test_omitted_legal_basis_has_no_required_claim(dataset):
    d = strict_dataset(dataset)
    d["institutes"][0].pop("legal_basis")
    pointers = d["sources"][0]["excerpts"][0]["supports"]
    pointers.remove("/institutes/0/legal_basis/type")
    pointers.remove("/institutes/0/legal_basis/state")
    assert not any(
        p.startswith("/institutes/0/legal_basis/") for p in required_excerpt_pointers(d)
    )
    assert check_method_rules(d) == []


def test_each_published_fiscal_allocation_needs_exact_amount_quote(dataset):
    d = strict_dataset(dataset)
    d["institutes"][0]["funding"][0]["annual_profile"] = [
        {
            "start": "2024-01-01",
            "end": "2024-12-31",
            "amount": 8,
            "source_ids": ["gb-source"],
        },
        {
            "start": "2025-01-01",
            "end": "2025-12-31",
            "amount": 9,
            "source_ids": ["gb-source"],
        },
    ]
    expected = {
        "/institutes/0/funding/0/annual_profile/0/amount",
        "/institutes/0/funding/0/annual_profile/1/amount",
    }
    assert expected <= required_excerpt_pointers(d)
    supports = d["sources"][0]["excerpts"][0]["supports"]
    supports.append("/institutes/0/funding/0/annual_profile")
    assert {f"required claim has no excerpt: {p}" for p in expected} <= set(
        check_method_rules(d)
    )
    supports.extend(expected)
    assert check_method_rules(d) == []


@pytest.mark.parametrize("currency", ["USD", "GBP"])
@pytest.mark.parametrize("period_mean", [False, True])
def test_usd_estimate_needs_input_quotes_not_a_quote_of_the_result(
    dataset, currency, period_mean
):
    d = strict_dataset(dataset)
    funding = d["institutes"][0]["funding"][0]
    annual_amount = 10 if currency == "USD" else 8
    funding.update(amount=annual_amount, currency=currency, annual_amount=annual_amount,
                   annual_basis="Stated annual allocation.", annual_basis_code="other", eur_per_year_est=10)
    supports = d["sources"][0]["excerpts"][0]["supports"]
    if period_mean:
        funding.update(
            amount=annual_amount * 4,
            years=4,
            period="2023 to 2026",
            annual_amount=annual_amount,
            annual_basis="Mean over four years.",
            annual_basis_code="period_mean",
        )
        supports.extend(
            ["/institutes/0/funding/0/years", "/institutes/0/funding/0/period"]
        )
    pointer = "/institutes/0/funding/0/eur_per_year_est"
    assert pointer not in required_excerpt_pointers(d)
    if not period_mean:
        annual_pointer = "/institutes/0/funding/0/annual_amount"
        assert check_method_rules(d) == [f"required claim has no excerpt: {annual_pointer}"]
        supports.append(annual_pointer)
    assert check_method_rules(d) == []
    supports.remove("/institutes/0/funding/0/amount")
    assert check_method_rules(d) == ["required claim has no excerpt: /institutes/0/funding/0/amount"]


def test_temporary_method_revision_is_not_in_released_schema(dataset):
    from scripts import build
    from jsonschema import Draft202012Validator
    import json

    dataset["dataset"]["method_revision"] = 2
    errors = list(
        Draft202012Validator(json.loads(build.SCHEMA.read_text())).iter_errors(dataset)
    )
    assert any(
        e.validator == "additionalProperties" and "method_revision" in e.message
        for e in errors
    )


def test_historical_check_date_basis_schema(dataset):
    import json

    from jsonschema import Draft202012Validator
    from scripts import build

    data = strict_dataset(dataset)
    data["jurisdictions"][0]["check"]["last_checked_basis"] = "historical_upper_bound"
    schema = json.loads(build.SCHEMA.read_text())
    validator = Draft202012Validator(
        schema, format_checker=Draft202012Validator.FORMAT_CHECKER
    )
    assert not list(validator.iter_errors(data))
    data["jurisdictions"][0]["check"]["last_checked_basis"] = "guessed"
    assert any(
        list(e.absolute_path)[-1] == "last_checked_basis"
        for e in validator.iter_errors(data)
    )


@pytest.mark.parametrize("jurisdiction_id,eu_member", [("DE", True), ("NO", False)])
def test_authority_cannot_escape_checks_by_removing_designation_metadata(dataset, jurisdiction_id, eu_member):
    d = strict_dataset(dataset)
    j = d["jurisdictions"][0]
    j.update(id=jurisdiction_id, eu_member=eu_member)
    body = j["related_bodies"][0]
    body.update(kind="ai_act_authority", designation_state="designated",
                designation_basis="law", authority_roles=["market_surveillance"])
    body["source_ids"].append("eu-ai-act-authorities-2026")
    for key in ("designation_state", "designation_basis", "authority_roles"):
        body.pop(key)
    body["source_ids"].remove("eu-ai-act-authorities-2026")
    errors = check_method_rules(d)
    for required in ("designation_basis", "Commission list"):
        assert any(required in error for error in errors)


def test_all_related_body_kinds_cannot_be_omitted(dataset):
    import json
    from jsonschema import Draft202012Validator
    from scripts import build

    d = strict_dataset(dataset)
    for j in d["jurisdictions"]:
        for body in j.get("related_bodies", []):
            body.pop("kind")
    validator = Draft202012Validator(json.loads(build.SCHEMA.read_text()))
    assert any(e.validator == "required" and "kind" in e.message for e in validator.iter_errors(d))
    assert any("related body needs kind" in e for e in check_method_rules(d))


@pytest.mark.parametrize("field,value", [
    ("designation_state", "designated"),
    ("designation_basis", "law"),
    ("authority_roles", ["market_surveillance"]),
    ("designation_note", "National allocation unresolved."),
])
def test_other_body_cannot_carry_authority_metadata(dataset, field, value):
    import json
    from jsonschema import Draft202012Validator
    from scripts import build

    d = strict_dataset(dataset)
    d["jurisdictions"][0]["related_bodies"][0][field] = value
    validator = Draft202012Validator(json.loads(build.SCHEMA.read_text()))
    assert list(validator.iter_errors(d))
    assert any("authority metadata requires" in e for e in check_method_rules(d))


def test_first_output_uses_first_eligible_institute_credit(dataset):
    d = strict_dataset(dataset)
    outputs = d["institutes"][0]["outputs"]
    outputs[0]["credited_from"] = "2025-01"
    d["sources"].append({**copy.deepcopy(d["sources"][0]), "id": "second-source", "url": "https://example.org/second-report", "output_lineage": "second-report"})
    d["sources"][-1]["excerpts"][0]["supports"] = ["/institutes/0/outputs/1/credited_from"]
    outputs.append({**outputs[0], "lineage": "second-report", "date": "2024-12", "credited_from": "2024-12", "source_ids": ["second-source"]})
    d["jurisdictions"][0]["timeline"][-1]["date"] = "2024-12"
    d["jurisdictions"][0]["timeline"][-1]["output_lineage"] = "second-report"
    d["jurisdictions"][0]["timeline"][-1]["source_ids"].append("second-source")
    supports = d["sources"][0]["excerpts"][0]["supports"]
    supports.extend(["/institutes/0/outputs/0/credited_from", "/institutes/0/outputs/1/credited_from"])
    assert check_method_rules(d) == []
    supports.remove("/institutes/0/outputs/0/credited_from")
    assert "required claim has no excerpt: /institutes/0/outputs/0/credited_from" in check_method_rules(d)
    supports.append("/institutes/0/outputs/0/credited_from")
    outputs[0]["credited_from"] = "2024-09"
    assert any("credit cannot precede" in e for e in check_method_rules(d))


def test_shared_lineage_must_retain_one_publication_date(dataset):
    d = strict_dataset(dataset)
    second = copy.deepcopy(d["institutes"][0])
    second["id"] = "second-institute"
    second["outputs"][0]["date"] = "2024-11"
    d["institutes"].append(second)
    assert any("shared output lineage" in e for e in check_method_rules(d))


def test_authority_may_leave_designation_state_unresolved(dataset):
    d = strict_dataset(dataset)
    body = d["jurisdictions"][0]["related_bodies"][0]
    body.update(kind="ai_act_authority", designation_basis="none_yet")
    assert "designation_state" not in body
    assert check_method_rules(d) == []
