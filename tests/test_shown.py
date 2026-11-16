"""Display cuts leave the whole quotation as the evidence, identity and re-read unit."""

import copy
import json

import pytest
from jsonschema import Draft202012Validator

from scripts import agent_files, build, check_page, claim_checks, rereads
from scripts.references import authored_schema, excerpt_identity, excerpt_spans, located_spans

TEXT = "Opening context. " + "First visible quotation. " + "Unshown context. " * 18 + "Last visible quotation. Closing context."
PARTS = [{"text": "First visible quotation."}, {"text": "Last visible quotation."}]
SCHEMA = json.loads(build.SCHEMA.read_text())
EXCERPT_SCHEMA = SCHEMA["properties"]["sources"]["items"]["properties"]["excerpts"]["items"]


def excerpt(**extra):
    return {"text": TEXT, "supports": ["/institutes/0/name"], "shown": copy.deepcopy(PARTS), **extra}


def errors(quote):
    return build.check_excerpts({"sources": [{"id": "s", "language": "en", "excerpts": [quote]}],
                                 "institutes": [{"name": "Test institute", "source_ids": ["s"]}]})


@pytest.mark.parametrize("shown", [[], PARTS * 2, [{}], [{"text": "x" * 19}], [{"text": "x" * 20, "extra": True}], [{"text": 42}]])
def test_shown_shape(shown):
    assert list(Draft202012Validator(EXCERPT_SCHEMA).iter_errors(excerpt(shown=shown)))


@pytest.mark.parametrize("length", [300, 301, 500, 501])
def test_excerpt_length_and_cut_threshold(length):
    part = PARTS[0]["text"]
    quote = excerpt(text=part + "x" * (length - len(part)), shown=[{"text": part}])
    assert bool(list(Draft202012Validator(EXCERPT_SCHEMA).iter_errors(quote))) == (length > 500)
    assert bool(errors(quote)) == (length <= 300)


@pytest.mark.parametrize("parts, message", [
    ([{"text": "Not present in the whole quotation."}], "missing, out of order or overlapping"),
    (list(reversed(PARTS)), "missing, out of order or overlapping"),
    ([{"text": TEXT[:30]}, {"text": TEXT[20:45]}], "missing, out of order or overlapping"),
    ([{"text": TEXT[:180]}, {"text": TEXT[180:301]}], "together exceed 300"),
    ([{**PARTS[0], "gloss": "An extra translation."}], "gloss must be present"),
])
def test_invalid_cuts_name_source_and_text(parts, message):
    found = errors(excerpt(shown=parts))
    assert any(message in e for e in found)
    assert all("source s" in e and repr(TEXT[:60]) in e for e in found)


def test_missing_part_gloss_and_valid_translations():
    quote = excerpt(gloss="Full English translation.")
    assert len(errors(quote)) == 2
    for part in quote["shown"]:
        part["gloss"] = "English translation of this part."
    assert errors(quote) == []


def test_exactly_300_characters_with_a_gap_are_valid():
    first, last = "A" + "x" * 149, "B" + "y" * 149
    assert errors(excerpt(text=first + "Omitted context. " + last, shown=[{"text": first}, {"text": last}])) == []
    assert errors(excerpt()) == []
    whole = excerpt()
    whole.pop("shown")
    assert errors(whole) == []


@pytest.mark.parametrize("overlapping", [False, True])
def test_each_part_must_occur_exactly_once_including_overlapping_matches(overlapping):
    part = "a" * 21 if overlapping else PARTS[0]["text"]
    text = part + ("a" if overlapping else " " + part) + " Context." * 40
    quote = excerpt(text=text, shown=[{"text": part}])
    found = errors(quote)
    assert len(found) == 1 and "exactly once" in found[0]
    assert "source s" in found[0] and repr(text[:60]) in found[0]


def test_adjacent_parts_are_rejected():
    quote = excerpt(shown=[{"text": TEXT[:30]}, {"text": TEXT[30:65]}])
    assert any("must not be adjacent" in e for e in errors(quote))


@pytest.mark.parametrize("length", [19, 20])
def test_minimum_part_length_strips_whitespace(length):
    part = " \t" + "a" * length + "\n "
    quote = excerpt(text=part + " Context." * 40, shown=[{"text": part}])
    assert not list(Draft202012Validator(EXCERPT_SCHEMA).iter_errors(quote))
    found = errors(quote)
    assert bool(found) == (length < 20)
    if found:
        assert "at least 20 characters after stripping whitespace" in found[0]


@pytest.mark.parametrize("whole_gloss, part_gloss, valid", [
    (None, "", True), (None, " \t", True), ("", None, True), (" \t", None, True),
    (" \t", "", True), ("", "Translation", False), (" \t", "Translation", False),
    ("Translation", "", False), ("Translation", " \t", False),
])
def test_gloss_parity_treats_blank_glosses_as_absent(whole_gloss, part_gloss, valid):
    quote = excerpt()
    if whole_gloss is not None:
        quote["gloss"] = whole_gloss
    if part_gloss is not None:
        for part in quote["shown"]:
            part["gloss"] = part_gloss
    found = errors(quote)
    assert bool(found) != valid
    assert all("gloss must be present" in e for e in found)


@pytest.mark.parametrize("gloss", ["", " \t"])
def test_lead_with_blank_gloss_uses_original_text(gloss):
    quote = excerpt(gloss=gloss, shown=[{**p, "gloss": gloss} for p in PARTS])
    assert claim_checks.check_lead("lead", {"text": 'It says "First visible quotation".'}, {"text": [({"id": "s"}, quote)]}) == []


@pytest.mark.parametrize("translated", [False, True])
def test_lead_quotes_must_be_in_both_the_whole_span_and_one_visible_part(translated):
    quote = excerpt()
    if translated:
        quote["gloss"] = "First translated quotation. Omitted qualification. Last translated quotation."
        quote["shown"] = [{**p, "gloss": gloss} for p, gloss in zip(PARTS, ["First translated quotation.", "Last translated quotation."])]
    visible = "First translated quotation" if translated else "First visible quotation"
    hidden = "Omitted qualification" if translated else "Unshown context"
    leaves = {"text": [({"id": "s"}, quote)]}
    assert claim_checks.check_lead("lead", {"text": f'It says "{visible.upper()}".'}, leaves) == []
    found = claim_checks.check_lead("lead", {"text": f'It says "{hidden}".'}, leaves)
    assert len(found) == 1 and "source s" in found[0] and repr(TEXT[:60]) in found[0]
    assert claim_checks.check_lead("lead", {"text": 'It says "quotation. Last".'}, leaves)
    if translated:
        quote["shown"][0]["gloss"] = "Words absent from the full gloss."
        assert claim_checks.check_lead("lead", {"text": 'It says "Words absent".'}, leaves)


def test_shown_survives_authored_schema_downloads_api_page_and_csv(dataset):
    source = next(s for s in dataset["sources"] if s["id"] == "gb-source")
    quote, = source["excerpts"]
    quote.update(text=TEXT, shown=copy.deepcopy(PARTS))
    assert "shown" in authored_schema(SCHEMA)["properties"]["sources"]["items"]["properties"]["excerpts"]["items"]["properties"]
    published = build.export_dataset(dataset)
    published_source = next(s for s in published["sources"] if s["id"] == source["id"])
    exported, = published_source["excerpts"]
    assert exported["shown"] == PARTS and exported["text"] == TEXT
    assert list(Draft202012Validator(SCHEMA).iter_errors(published)) == []
    page = json.loads(build.page_data(published))
    assert check_page.expected_page_data(published) == page
    assert next(s for s in page["sources"] if s["id"] == source["id"])["excerpts"] == published_source["excerpts"]
    for kind in ("jurisdictions", "institutes"):
        api = agent_files.record_file(published, kind, 0)  # the fixture has one of each
        api_quote, = next(s for s in api["sources"] if s["id"] == source["id"])["excerpts"]
        assert api_quote["shown"] == PARTS and api_quote["text"] == TEXT
    row, = build.tables(published)["source_excerpts"]
    assert row["shown"] == "First visible quotation. [...] Last visible quotation."
    assert list(row) == build.EMPTY_COLUMNS["source_excerpts"]
    fields = build.table_schema([row], build.table_descriptions(SCHEMA, "source_excerpts"))["fields"]
    assert fields[-1] == {"name": "shown", "type": "string", "description": build.FIELD_DOCS["shown"]}
    exported.pop("shown")
    assert build.tables(published)["source_excerpts"][0]["shown"] == ""


def test_shown_changes_neither_identity_nor_reread_state():
    quote = excerpt(anchors=[{"kind": "heading", "text": "The institute's work"}])
    whole = {k: v for k, v in quote.items() if k != "shown"}
    assert excerpt_identity(quote) == excerpt_identity(whole) == rereads.quote_id(TEXT)
    assert excerpt_spans(quote) == excerpt_spans(whole)
    assert located_spans("s", quote) == located_spans("s", whole)
    data = {"dataset": {"as_of": "2026-11-16"}, "institutes": [], "jurisdictions": [],
            "sources": [{"id": "s", "url": "https://example.org/source", "snapshot": {"retrieved": "2026-11-01T00:00:00Z"}, "excerpts": [whole]}]}
    state = {"state": "quote_gone", "last_found": "2026-11-01", "since": "2026-11-10", "last_attempt": "2026-11-10"}
    observations = {"sources": {"s": {rereads.quote_id(TEXT): state}}}
    before = rereads.quote_states(data, observations)
    rereads.join(data, observations)
    expected = copy.deepcopy(whole["reread"])
    whole["shown"] = quote["shown"]
    assert rereads.quote_states(data, observations) == before
    rereads.join(data, observations)
    assert whole["reread"] == expected and expected["state"] == "quote_gone"
