"""The internal language reference: CLDR seeding, exceptions by date, the derived flag and coverage of the dataset."""

import json

import pytest

from scripts import state_languages as sl

TERRITORY = {
    "XA": {"languagePopulation": {
        "xa": {"_officialStatus": "official"},
        "sr": {"_officialStatus": "official_regional"},
        "sr_Latn": {"_officialStatus": "official"},
        "en": {"_officialStatus": "de_facto_official"},
        "rg": {"_officialStatus": "official_regional"},
        "zz": {"_populationPercent": "3"},
    }},
}
COUNTS = {"official": True, "de_facto_official": True, "national": True, "working": True, "official_use": None, "unresolved": None,
          "official_regional": None, "link": None, "not_official": False}
SOURCE = {"url": "https://example.org/constitution", "title": "Constitution", "retrieved": "2026-11-06",
          "excerpt": "The official language is Xa."}


def ref(exceptions=()):
    meta = {"version": "49.0.0"}
    block = sl.cldr_block(TERRITORY, [{"id": "XA", "name": "Xland"}], meta, {"iw": "he"})
    return {"policy": {"counts": COUNTS}, "aliases": {"iw": "he"}, "exception_only": ["EU"], "cldr": block,
            "exceptions": list(exceptions)}


def exc(**kw):
    return {"id": "xa-2023", "jurisdiction": "XA", "languages": {"en": "not_official"}, "basis": "test",
            "sources": [SOURCE]} | kw


def test_seed_keeps_only_official_statuses_and_takes_the_strongest_script_variant():
    langs = ref()["cldr"]["jurisdictions"]["XA"]["languages"]
    assert set(langs) == {"xa", "sr", "en", "rg"}
    assert langs["sr"] == {"status": "official", "cldr_codes": ["sr", "sr_Latn"]}


def test_derived_flag_by_status():
    r = ref()
    assert sl.national_or_official(r, "XA", "xa", "2026-10-24")[0] is True
    assert sl.national_or_official(r, "XA", "en-GB", "2026-10-24")[0] is True  # de facto counts
    assert sl.national_or_official(r, "XA", "sr-Cyrl", "2026-10-24")[0] is True
    assert sl.national_or_official(r, "XA", "zz", "2026-10-24")[0] is False  # listed, no official status
    assert sl.national_or_official(r, "XA", "fr", "2026-10-24")[0] is False  # not listed


def test_unresolved_cases_stay_unknown():
    r = ref()
    flag, reason = sl.national_or_official(r, "XA", "rg", "2026-10-24")
    assert flag is None and "policy open" in reason
    assert sl.national_or_official(r, "YY", "en", "2026-10-24")[0] is None
    assert sl.national_or_official(r, "XA", "", "2026-10-24")[0] is None


def test_exception_applies_only_in_its_date_range():
    r = ref([exc(**{"from": "2023-07-22"})])
    assert sl.national_or_official(r, "XA", "en", "2023-07-21")[0] is True
    assert sl.national_or_official(r, "XA", "en", "2023-07-22") == (False, "not_official (exception xa-2023)")
    r = ref([exc(until="2023-07-22")])
    assert sl.national_or_official(r, "XA", "en", "2023-07-22")[0] is True


def test_exception_only_jurisdiction_counts_listed_languages_only():
    r = ref([exc(jurisdiction="EU", languages={"en": "working", "de": "working"})])
    assert sl.validate(r, {"jurisdictions": [{"id": "XA"}, {"id": "EU"}]}) == []
    assert sl.national_or_official(r, "EU", "de", "2026-10-24")[0] is True
    assert sl.national_or_official(r, "EU", "es", "2026-10-24")[0] is False


def test_validate_requires_coverage_cited_exceptions_and_no_overlap():
    data = {"jurisdictions": [{"id": "XA"}, {"id": "XB"}]}
    bad = exc(sources=[{"url": "https://example.org", "title": None, "retrieved": "6.11.2026"}],
              languages={"en": "lingua_franca"})
    errors = sl.validate(ref([bad, exc(id="xa-2", **{"from": "2020-01-01"})]), data)
    assert "XB: not in the reference" in errors
    assert "EU: exception_only without an exception" in errors
    assert "exception xa-2023 en: unknown status lingua_franca" in errors
    assert "exception xa-2023: source excerpt missing" in errors
    assert "exception xa-2023: source title missing" in errors  # null is not a value
    assert "exception xa-2023: source retrieved is not a YYYY-MM-DD date" in errors
    assert "XA en: overlapping exceptions" in errors


def test_validate_rejects_bad_dates_and_noncanonical_keys():
    data = {"jurisdictions": [{"id": "XA"}, {"id": "EU"}]}
    bad = exc(languages={"iw": "official"}, **{"from": "2023-7-1", "until": "2023-01-01"})
    errors = sl.validate(ref([bad, exc(id="eu", jurisdiction="EU", languages={"en": "working"})]), data)
    assert "exception xa-2023: from is not a YYYY-MM-DD date" in errors
    assert "exception xa-2023 iw: stored key is not canonical" in errors
    errors = sl.validate(ref([exc(**{"from": "2024-01-01", "until": "2023-01-01"}),
                              exc(id="eu", jurisdiction="EU", languages={"en": "working"})]), data)
    assert errors == ["exception xa-2023: from is not before until"]
    numeric = exc(**{"from": 20240101, "until": "2025-01-01"})
    errors = sl.validate(ref([numeric, exc(id="xa-2"), exc(id="eu", jurisdiction="EU", languages={"en": "working"})]), data)
    assert errors == ["exception xa-2023: from is not a YYYY-MM-DD date"]


def test_an_exception_does_not_stand_in_for_missing_cldr_coverage():
    r = ref([exc(jurisdiction="XB")])
    assert "XB: not in the reference" in sl.validate(r, {"jurisdictions": [{"id": "XA"}, {"id": "XB"}]})
    assert sl.national_or_official(r, "XB", "fr", "2026-10-24")[0] is None


def test_alias_maps_deprecated_codes_in_queries_and_seed():
    assert sl.base_code("zh_Hant", {}) == "zh"
    block = sl.cldr_block({"XA": {"languagePopulation": {"iw": {"_officialStatus": "official"}}}},
                          [{"id": "XA", "name": "Xland"}], {}, {"iw": "he"})
    assert list(block["jurisdictions"]["XA"]["languages"]) == ["he"]
    r = ref()
    r["cldr"] = block | {"version": "49.0.0"}
    assert sl.national_or_official(r, "XA", "iw", "2026-10-24")[0] is True


@pytest.mark.skipif(not sl.REF.is_file(), reason="the internal language reference is not in public exports")
def test_reference_file_covers_the_dataset():
    data = json.loads(sl.DATA.read_text(encoding="utf-8"))
    assert sl.validate(sl.load(), data) == []


def test_cldr_checks_need_a_cldr_status_a_known_outcome_and_a_citation():
    r = ref([exc(id="eu", jurisdiction="EU", languages={"en": "working"})])
    r["cldr_checks"] = [{"jurisdiction": "XA", "language": "en", "outcome": "silent", "sources": [SOURCE]},
                        {"jurisdiction": "XA", "language": "fr", "outcome": "contradicts", "sources": []}]
    errors = sl.validate(r, {"jurisdictions": [{"id": "XA"}, {"id": "EU"}]})
    assert errors == [
        "cldr_check XA fr: CLDR gives no status to check",
        "cldr_check XA fr: outcome must be supports or silent; a contradiction needs an exception",
        "cldr_check XA fr: source url missing", "cldr_check XA fr: source title missing",
        "cldr_check XA fr: source retrieved missing", "cldr_check XA fr: source excerpt missing",
    ]
