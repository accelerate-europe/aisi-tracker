"""The reader-text lint: mechanical classes are errors in a migrated record and counted warnings elsewhere; judgement
classes are listed for the reviewer (scripts/reader_text.py, docs/design/claims-1.1.md section 5)."""

import pytest

from scripts.reader_text import lint_errors, lint_reader_text, lint_warnings, stale

AS_OF = "2026-11-14"


def data(**record):
    """One jurisdiction with the given fields, as authored."""
    return {"dataset": {"as_of": AS_OF}, "jurisdictions": [{"id": "XX", **record}], "institutes": [], "changelog": []}


def classes(d, migrated=frozenset()):
    return sorted({hit[0] for hit in lint_reader_text(d, set(migrated))})


@pytest.mark.parametrize("text, found", [
    ("Several chapters commence 28 October 2026.", "commence 28 October 2026"),
    ("Several chapters commence on 28 October 2026.", "commence on 28 October 2026"),
    ("Its report is due 13 Nov 2026.", "is due 13 Nov 2026"),
    ("The law was still in draft in July 2026.", "still in draft in July 2026"),
    ("The act will take effect in October 2026.", "will take effect in October 2026"),
    ("The act is due to enter into force in July 2026.", "is due to enter into force in July 2026"),
    ("The act is scheduled to enter into force on 1 July 2026.", "is scheduled to enter into force on 1 July 2026"),
    ("The rules are set to take effect in August 2026.", "are set to take effect in August 2026"),
])
def test_future_wording_about_a_past_date(text, found):
    assert stale(text, AS_OF) == found


@pytest.mark.parametrize("text", [
    "The amendments are to take effect on 1 February 2027.",  # still ahead
    "The secretariat is to be based in Kuala Lumpur, as announced in January 2026.",  # the date is another clause's
    "The regulation took effect on 22 January 2026.",  # past tense
    "Displayed as in force from 1 June 2026.",
    "Its report is due in November 2026.",  # the month of as_of has not ended
    "It will open soon. Funding was approved in July 2026.",  # another sentence's date
    "It will evaluate models trained before 1 January 2025.",  # a date that is not the action's
    "The review was scheduled for February 2027.",  # a plan in the past tense is a warning, not this error
    "It will use data from July 2026.",  # the date describes the data
    "The act was scheduled to take effect in July 2026 and did so.",  # a past plan, not a pending one
    "The chapters were expected to commence on 28 October 2026.",
    "Parliament voted for the law to enter into force in March 2026.",
])
def test_future_wording_that_is_not_stale(text):
    assert stale(text, AS_OF) is None


def test_timeline_events_are_worded_as_of_their_date():
    d = data(timeline=[{"description": "The decision will enter into force on 21 February 2024."}])
    assert "future wording about a past date" not in classes(d)


@pytest.mark.parametrize("text", [
    "The resolution, PDF pages 145 and 158, concerns research.",
    "No role is assigned in the reviewed passages.",
    "The subsequently retrieved official journal confirms publication.",
    "This record supplies no amount.",
    "Its roles are described as pending in the cited sources.",
    "The only bodies found are AI Act authorities.",
])
def test_workshop_words(text):
    assert classes(data(summary=text)) == ["workshop words"]


def test_short_month():
    assert classes(data(summary="It reported on 26 Nov 2024.")) == ["short month"]
    assert classes(data(summary="It reported on 26 November 2024 and in May 2025.")) == []


def test_reading_notes_belong_in_check_log():
    d = data(check={"log": [{"date": "2026-11-01", "text": "Read PDF pages 145 and 158 in the retrieved copy."}]})
    assert classes(d) == []


def test_a_finding_in_check_log_is_listed():
    d = data(check={"log": [{"date": "2026-11-01", "text": "Article 5 designates the commission as single point of contact."}]})
    assert classes(d) == ["finding in check.log"]


def test_errors_only_in_a_migrated_record_warnings_elsewhere():
    log = {"log": [{"date": "2026-11-01", "text": "Searched on 26 Nov 2026 in Polish."}]}
    d = data(check=log)
    assert lint_errors(d, set()) == []
    assert lint_errors(d, {"XX"}) == ["/jurisdictions/0/check/log/0/text: short month: '26 Nov 2026'"]
    assert lint_warnings(d, {"XX"}) == []
    assert lint_warnings(d, set())[0].startswith("reader text [short month]: 1 in 1 records")


def test_generated_texts_of_a_migrated_record_are_not_linted():
    d = data(summary="The only bodies found are AI Act authorities.")
    assert classes(d, {"XX"}) == []
    assert classes(d) == ["workshop words"]


def test_claims_are_checked_outside_their_quotes():
    claim = {"id": "claims-1", "kind": "function", "text": 'It says it does "not test models" for the ministry.'}
    assert classes(data(claims=[claim])) == []
    claim["text"] = "It is not a government evaluator."
    assert classes(data(claims=[claim])) == ["negative wording in a claim"]


def test_judgement_classes_are_warnings_even_when_migrated():
    d = data(related_bodies=[{"name": "A", "role": "It runs a lab, not a national body."}],
             claims=[{"id": "claims-1", "kind": "function", "text": "It does not assign testing."}])
    assert lint_errors(d, {"XX"}) == []
    assert any(line.startswith("reader text [negative wording in a claim]") for line in lint_warnings(d, {"XX"}))


def test_a_repeated_verdict_is_listed_once_per_record():
    d = data(summary="It does not assign testing.", notes="These texts do not establish a role. This does not qualify.")
    assert [hit for hit in lint_reader_text(d, set()) if hit[0] == "verdict repeated in one record"] == [
        ("verdict repeated in one record", "XX", "/jurisdictions/XX", "3 verdicts")]


@pytest.mark.parametrize("text, hit", [
    ("Resolution 130 plans a network; no national advanced-model evaluation role is assigned.", True),
    ("A national mandate to evaluate advanced models was not established.", True),
    ("It is a regional process, not a national body.", True),
    ("The national AI strategy plans testing of algorithms.", False),
    ("It evaluates frontier models for the ministry.", False),
])
def test_national_advanced_severe_risk_as_a_bar(text, hit):
    assert ("national, advanced or severe-risk as a bar" in classes(data(summary=text))) is hit


def test_bar_words_are_listed_in_claims_too_but_never_errors():
    claim = {"id": "claims-1", "kind": "function", "text": "It has no national mandate to test models."}
    d = data(claims=[claim])
    assert "national, advanced or severe-risk as a bar" in classes(d, {"XX"})
    assert lint_errors(d, {"XX"}) == []


def test_ordinary_prose_is_not_workshop_words():
    assert classes(data(summary="Both bodies found vulnerabilities in the model.")) == []


def test_a_plan_in_the_past_tense_is_a_warning():
    d = data(summary="Full commission operation was scheduled for November.")
    assert classes(d) == ["plan in the past tense"]
    assert lint_errors(d, {"XX"}) == [] and classes(d, {"XX"}) == []  # a migrated record's summary is generated


def test_judgement_classes_read_claim_texts_and_bar_words_read_the_log():
    claim = {"id": "claims-1", "kind": "function", "text": "Resolution 130 gives it a role, not a national body."}
    found = classes(data(claims=[claim]), {"XX"})
    assert {"bare act number", "', not X' tail", "national, advanced or severe-risk as a bar"} <= set(found)
    log = {"log": [{"date": "2026-11-01", "text": "Searched; no national mandate was found for the centre."}]}
    assert "national, advanced or severe-risk as a bar" in classes(data(check=log))


def test_the_report_lists_every_hit():
    from scripts.reader_text import lint_report
    d = data(summary="It reported on 26 Nov 2024.", notes="It ran on 3 Dec 2025 and 4 Jan 2026.", claims=[])
    report = lint_report(d, set())
    assert "## short month (2)" in report and "/jurisdictions/0/summary" in report and "/jurisdictions/0/notes" in report


def test_a_past_plan_in_a_migrated_claim_is_not_an_error():
    claim = {"id": "claims-1", "kind": "fact", "text": "The act was scheduled to take effect in July 2026 and did so."}
    d = data(claims=[claim])
    assert lint_errors(d, {"XX"}) == []
    assert classes(d, {"XX"}) == ["plan in the past tense"]
