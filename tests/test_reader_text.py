"""Copy guards separate authored prose from verbatim research evidence."""

import pytest

from scripts import build
from scripts.reader_text import check_reader_text


@pytest.mark.parametrize(
    "fragment",
    [
        {"jurisdictions": [{"notes": "Checked.  Read the source."}]},
        {"institutes": [{"mandate": "Will  evaluate models."}]},
        {"dataset": {"method": ["A  method."]}},
        {"dataset": {"status_definitions": {"planned": "A  commitment."}}},
        {"dataset": {"vocabularies": {"scope": {"test": "A  definition."}}}},
        {"changelog": [{"changes": ["Changed  wording."]}]},
        {"sources": [{"title_en": "An  English title"}]},
        {"sources": [{"excerpts": [{"gloss": "An  English gloss."}]}]},
    ],
)
def test_double_spaces_in_authored_prose_fail(fragment):
    assert len(check_reader_text(fragment)) == 1
    assert "double spaces" in check_reader_text(fragment)[0]


def test_recorded_evidence_and_names_are_exempt():
    data = {
        "sources": [
            {
                "title": "Recorded  title",
                "url": "https://example.test/a  b",
                "excerpts": [{"text": "Verbatim  quote"}],
            }
        ],
        "institutes": [
            {
                "name": "Recorded  name",
                "outputs": [{"title": "Recorded  publication"}],
                "people": [{"role": "Published  title"}],
            }
        ],
        "jurisdictions": [{"check": {"searches": [{"query": "exact  query"}]}}],
    }
    assert check_reader_text(data) == []


def test_build_checks_nested_reader_text(dataset):
    dataset["jurisdictions"][0]["notes"] = "Checked.  Read the source."
    assert (
        "/jurisdictions/0/notes: reader-facing text contains double spaces"
        in build.check_consistency(dataset)
    )


def test_dataset_definition_is_reader_text(dataset):
    """The definition under the page headline obeys the copy rules like other authored prose."""
    dataset["dataset"]["definition"] = "We count a body.  Then more."
    assert "/dataset/definition: reader-facing text contains double spaces" in build.check_consistency(dataset)


def test_single_spaces_and_paragraph_breaks_pass():
    assert check_reader_text({"notes": "First paragraph.\n\nSecond paragraph."}) == []


@pytest.mark.parametrize(
    "fragment",
    [
        {"dataset": {"status_definitions": {"established": "See dataset.method."}}},
        {"dataset": {"glossary": {"AI": {"GB": "See dataset.method."}}}},
        {"dataset": {"method": ["See operating.evidence."]}},
        {"dataset": {"method": ["See check.last_checked."]}},
        {"jurisdictions": [{"summary": "See dataset.method."}]},
        {"institutes": [{"funding": [{"notes": "See funding[0].annual_amount."}]}]},
        {"jurisdictions": [{"closest_body": {"note": "See /institutes/0/status."}}]},
        {"jurisdictions": [{"timeline": [{"short": "See data.status_since."}]}]},
    ],
)
def test_page_field_paths_fail(fragment):
    assert any("field-path jargon" in e for e in check_reader_text(fragment))


def test_field_paths_allow_technical_metadata_and_ordinary_punctuation():
    assert (
        check_reader_text(
            {
                "dataset": {"vocabularies": {"event": {"act": "See dataset.method."}}},
                "jurisdictions": [{"check": {"language_note": "See check.searches."}}],
                "sources": [{"excerpts": [{"text": "See dataset.method."}]}],
                "notes": "E.g. version 1.0.0 costs 1.5 million; see example.org or https://example.org/annual_amount.",
            }
        )
        == []
    )


def test_build_rejects_page_jargon(dataset):
    dataset["dataset"]["status_definitions"]["established"] = "See dataset.method."
    assert any("field-path jargon" in e for e in build.check_consistency(dataset))


@pytest.mark.parametrize(
    "path",
    [
        "status_definitions.established",
        "vocabularies.event",
        "glossary.AI",
        "method.freshness",
        "excerpts.text",
        "closest_body.note",
        "Dataset.method",
        "/changelog/0/summary",
        "annual_amount",
        "status_since",
    ],
)
def test_field_path_variants(path):
    assert any(
        "field-path jargon" in e for e in check_reader_text({"notes": "See " + path})
    )


def test_acronym_glossary_does_not_turn_domains_into_paths():
    assert (
        check_reader_text(
            {
                "dataset": {
                    "glossary": {
                        "OECD": "Organisation",
                        "NIST": "Institute",
                        "ICT": "Technology",
                    }
                },
                "notes": "OECD.AI, nist.gov/caisi and ict.gov.pg.",
            }
        )
        == []
    )


@pytest.mark.parametrize(
    "token", ["broad_search", "Annual_amount", "historical_upper_bound"]
)
def test_bare_codes_are_also_page_jargon(token):
    assert any("field-path jargon" in e for e in check_reader_text({"notes": token}))


def test_changelog_fallback_is_page_facing():
    data = {"changelog": [{"changes": ["See dataset.method."]}]}
    assert any("field-path jargon" in e for e in check_reader_text(data))
    data["changelog"][0]["summary"] = "Updated the method."
    assert check_reader_text(data) == []


@pytest.mark.parametrize(
    "note",
    [
        "Round 6 review (17 October 2026): The law was read.",
        "The act was read. Round 9 reclassifies the act.",
        "The round 8 attempt failed.",
        "The law was read. Searched on 17 October 2026 in English and German.",
        "No designation was identified in the bounded search on 17 October 2026.",
        "The plan was read. On 17 October 2026, searches covered indexed public English pages.",
        "Individual re-check performed on 17 October 2026. The decree was read.",
    ],
)
def test_reader_text_rejects_round_labels_and_check_dates(note):
    errors = check_reader_text({"jurisdictions": [{"notes": note}]})
    assert any("working round or check date" in e for e in errors)


@pytest.mark.parametrize(
    "fragment",
    [
        {"jurisdictions": [{"notes": "The second funding round 2 opened on 3 March 2026."}]},
        {"jurisdictions": [{"notes": "The parliament completed its review of the bill on 2 July 2026."}]},
        {"jurisdictions": [{"notes": "On 1 June 2026, the Commission published a checklist."}]},
        {"dataset": {"other_overviews": [{"note": "When checked on 16 Oct 2026 it lacked Germany."}]}},
        {"institutes": [{"identifier_notes": "Item verified in searches on 17 October 2026."}]},
        {"jurisdictions": [{"check": {"partial": [{"scope": "Round 9: reread the budget."}]}}]},
        {"changelog": [{"summary": "Round 6 review (17 October 2026): new data."}]},
    ],
)
def test_reader_text_exempts_other_dates_and_download_only_fields(fragment):
    assert not any("working round or check date" in e for e in check_reader_text(fragment))


@pytest.mark.parametrize(
    "text",
    [
        "Quotations from sources and the values each supports.",
        "Each status is dated from its act, and each card value links to its source.",
        "Séance du 17février 2026.",
        "Market surveillance for AnnexIII systems.",
    ],
)
def test_reader_text_rejects_each_constructions_and_glued_numbers(text):
    assert check_reader_text({"jurisdictions": [{"notes": text}]})


def test_glued_numbers_checked_in_source_titles():
    errors = check_reader_text({"sources": [{"title": "Заседание на Министерския съвет на09.07.2026"}]})
    assert any("space is missing" in e for e in errors)
    assert check_reader_text({"sources": [{"title": "G7 Hiroshima AI Process, 5G and Article 5(1)"}]}) == []


@pytest.mark.parametrize(
    "text",
    [
        "Officials are pointing to the plan; annexing was rejected.",
        "In FY25. Then COP28. Then Gemini2.5 Pro.",
        "The budget each year rises; the same each month.",
    ],
)
def test_reader_text_allows_ordinary_words_numbers_and_each_year(text):
    assert check_reader_text({"jurisdictions": [{"notes": text}]}) == []
