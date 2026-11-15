"""An excerpt's anchors (docs/design/claims-1.1.md, attribution rule): shape, downloads, page data and the checks that
must not require them."""

import copy
import json

import pytest
from jsonschema import Draft202012Validator

from scripts import build, check_page, claim_checks
from scripts.references import authored_schema

SCHEMA = json.loads(build.SCHEMA.read_text())
AUTHORED = authored_schema(SCHEMA)
# The Commission decision establishing the AI Office: Article 3(1)'s lead-in and item (c), and Article 1.
LEAD_IN = {"text": "The Office shall perform the following tasks stemming from the forthcoming Regulation", "kind": "heading"}
DEFINITION = {"text": "The European Artificial Intelligence Office (the ‘Office’) is established.", "kind": "definition"}
ITEM = "monitoring the emergence of unforeseen risks stemming from general-purpose AI models"
# The fixture's English source and the excerpt the anchors go on, selected by key.
SOURCE, QUOTE = "gb-source", "Synthetic institute"


def errors(d, schema=AUTHORED):
    validator = Draft202012Validator(schema, format_checker=Draft202012Validator.FORMAT_CHECKER)
    return [e.message for e in validator.iter_errors(d)]


def source(d):
    return next(s for s in d["sources"] if s["id"] == SOURCE)


def quoted(d):
    return next(e for e in source(d)["excerpts"] if e["text"] == QUOTE)


def without_anchors(d):
    d = copy.deepcopy(d)
    for s in d["sources"]:
        for e in s.get("excerpts", []):
            e.pop("anchors", None)
    return d


@pytest.fixture
def plain(dataset):
    return without_anchors(dataset)


@pytest.fixture
def anchored(plain):
    d = copy.deepcopy(plain)
    assert source(d)["language"] == "en"
    quoted(d)["anchors"] = [LEAD_IN, DEFINITION]
    return d


def test_anchors_are_optional_and_valid(plain, anchored):
    assert errors(plain) == [] and errors(anchored) == []
    assert build.check_consistency(anchored, require_roster=True, complete=True) == []


@pytest.mark.parametrize("anchors", [
    [],
    [{"text": LEAD_IN["text"]}],  # no kind
    [{"kind": "heading"}],  # no text
    [{"text": LEAD_IN["text"], "kind": "footnote"}],  # not a kind
    [{"text": "", "kind": "heading"}],
    [{"text": "x" * 501, "kind": "heading"}],  # over the 500-character anchor limit
    [{**LEAD_IN, "url": "https://example.org/other-copy"}],  # never a copy of its own
    [LEAD_IN, DEFINITION, {"text": "Article 3", "kind": "provision"}, DEFINITION],  # four spans
])
def test_anchor_shape(anchored, anchors):
    quoted(anchored)["anchors"] = anchors
    assert errors(anchored)


def test_anchor_limit_is_500_and_the_excerpt_limit_stays_300(anchored):
    """Owner, 15 November: an anchor may hold up to 500 characters, so a naming sentence and its lead-in fit in one
    span; the quotation itself keeps its 300."""
    quoted(anchored)["anchors"] = [{"text": "x" * 500, "kind": "heading"}]
    assert errors(anchored) == []
    quoted(anchored)["anchors"] = [{"text": "x" * 501, "kind": "heading"}]
    assert errors(anchored)
    quoted(anchored)["anchors"] = [LEAD_IN]
    quoted(anchored)["text"] = "x" * 301
    assert errors(anchored)


@pytest.mark.parametrize("anchors", [
    [LEAD_IN, {"text": "Article 3 Implementation of the forthcoming Regulation", "kind": "provision"}],
    [DEFINITION, {"text": "(the ‘Office’)", "kind": "definition"}],
    [LEAD_IN, DEFINITION, DEFINITION],
])
def test_at_most_one_link_of_each_kind(anchored, anchors):
    """One link from the quote to its lead-in, one from a short name to its definition; a longer chain widens the excerpt."""
    quoted(anchored)["anchors"] = anchors
    found =[e for e in build.check_excerpts(anchored) if "anchors are at most" in e]
    assert len(found) == 1 and "widen the excerpt" in found[0]


def csv_rows(d):
    """source_excerpts.csv rows by (source id, excerpt text), the row's natural key."""
    return {(r["source_id"], r["text"]): r for r in build.tables(build.export_dataset(d))["source_excerpts"]}


def test_anchors_are_published_and_in_the_csv(plain, anchored):
    published = build.export_dataset(anchored)
    assert quoted(published)["anchors"] == [LEAD_IN, DEFINITION]
    assert [e.message for e in Draft202012Validator(SCHEMA).iter_errors(published)] == []
    before, after = csv_rows(plain), csv_rows(anchored)
    row = after[SOURCE, QUOTE]
    assert list(row) == build.EMPTY_COLUMNS["source_excerpts"]
    assert json.loads(row["anchors"]) == [LEAD_IN, DEFINITION]
    assert before[SOURCE, QUOTE]["anchors"] == ""
    assert {k: r for k, r in after.items() if k != (SOURCE, QUOTE)} == {k: r for k, r in before.items() if k != (SOURCE, QUOTE)}
    assert build.FIELD_DOCS["anchors"]


def test_anchors_do_not_change_the_excerpt_identity(plain, anchored):
    assert {k: r["id"] for k, r in csv_rows(plain).items()} == {k: r["id"] for k, r in csv_rows(anchored).items()}


def test_page_data_keeps_the_anchors(anchored):
    """Cards read the original and translated anchors when a quote is opened."""
    published = build.export_dataset(anchored)
    page = json.loads(build.page_data(published))
    assert quoted(page) == quoted(published)


def test_gate_expects_the_page_data_the_build_writes(anchored):
    """The gate's expected page.json, written from its own list, matches the build's on a dataset with anchors,
    so a field one of them leaves out and the other keeps fails here, not first on a data branch."""
    published = build.export_dataset(anchored)
    assert any(e.get("anchors") for s in published["sources"] for e in s.get("excerpts", []))
    assert check_page.expected_page_data(published) == json.loads(build.page_data(published))
    assert "anchors" not in check_page.PAGE_DATA_LEFT_OUT


def test_non_english_anchor_needs_a_gloss(anchored):
    d = copy.deepcopy(anchored)
    source(d)["language"] = "de"
    for e in source(d)["excerpts"]:
        e["gloss"] = "Synthetic gloss " + e["text"][:20]
    quoted(d)["anchors"] = [{"text": "Das Institut", "kind": "definition"}]
    assert any("non-English anchor needs an English gloss" in e for e in build.check_excerpts(d))
    quoted(d)["anchors"][0]["gloss"] = "The institute"
    assert not any("anchor" in e for e in build.check_excerpts(d))


def test_lead_quote_must_still_be_in_the_excerpt():
    """claim_checks is unchanged: a lead claim's quoted span found only in an anchor does not pass."""
    excerpt = {"text": ITEM, "anchors": [LEAD_IN, DEFINITION]}
    claim = {"text": 'The decision says the Office has "the following tasks".'}
    assert claim_checks.check_lead("lead", claim, {"text": [({}, excerpt)]})
    claim = {"text": f'The decision assigns it "{ITEM}".'}
    assert claim_checks.check_lead("lead", claim, {"text": [({}, excerpt)]}) == []


@pytest.fixture
def cross_copy(plain):
    d = copy.deepcopy(plain)
    named = {**source(d), "id": "unit-source", "url": "https://example.org/unit", "excerpts": [],
             "snapshot": {"sha256": "a" * 64, "retrieved": "2026-10-17T00:00:00Z"}}
    d["sources"].append(named)
    quoted(d)["supports"] = ["/institutes/0/name"]
    d["institutes"][0]["source_ids"].append(named["id"])
    quoted(d)["anchors"] = [{"kind": "membership", "text": "The lab is part of the institute.", "source_id": named["id"]}]
    return d


@pytest.mark.parametrize("form", ["three", "other-definition", "same-membership"])
def test_cross_copy_anchor_groups_are_valid(cross_copy, form):
    anchor, = quoted(cross_copy)["anchors"]
    if form == "three":
        quoted(cross_copy)["anchors"] = [LEAD_IN, DEFINITION, anchor]
    elif form == "other-definition":
        anchor["kind"] = "definition"
    else:
        anchor.pop("source_id")
    assert errors(cross_copy) == []
    assert build.check_excerpts(cross_copy) == []
    published = build.export_dataset(cross_copy)
    assert errors(published, SCHEMA) == []
    assert quoted(published)["anchors"] == quoted(cross_copy)["anchors"]
    assert json.loads(csv_rows(cross_copy)[SOURCE, QUOTE]["anchors"]) == quoted(cross_copy)["anchors"]


@pytest.mark.parametrize("kind", ["heading", "provision"])
def test_other_copy_lead_is_rejected_by_schema_and_build(cross_copy, kind):
    quoted(cross_copy)["anchors"][0]["kind"] = kind
    assert errors(cross_copy)
    assert any("other-copy anchor must be a definition or membership" in e for e in build.check_excerpts(cross_copy))


@pytest.mark.parametrize("change, message", [
    ("two-other", "anchors are at most"),
    ("same-id", "leave source_id out"),
    ("unknown", "is unknown"),
    ("uncited", "does not cite anchor source"),
    ("no-snapshot", "needs snapshot.sha256"),
    ("two-same-links", "anchors are at most"),
])
def test_cross_copy_anchor_errors(cross_copy, change, message):
    excerpt = quoted(cross_copy)
    anchor, = excerpt["anchors"]
    named = next(s for s in cross_copy["sources"] if s["id"] == "unit-source")
    if change == "two-other":
        excerpt["anchors"].append({**DEFINITION, "source_id": named["id"]})
    elif change == "same-id":
        anchor["source_id"] = SOURCE
    elif change == "unknown":
        anchor["source_id"] = "unknown"
    elif change == "uncited":
        cross_copy["institutes"][0]["source_ids"].remove(named["id"])
    elif change == "no-snapshot":
        named.pop("snapshot")
    else:
        excerpt["anchors"] = [DEFINITION, {"kind": "membership", "text": "Part of the institute."}]
    assert any(message in e for e in build.check_excerpts(cross_copy))


def test_other_copy_citation_can_be_in_any_ancestor_but_must_cover_every_support(cross_copy):
    institute = cross_copy["institutes"][0]
    institute["claims"] = [{"id": "test-claim", "kind": "function", "text": "Tests models.", "source_ids": [SOURCE]}]
    quoted(cross_copy)["supports"] = ["/institutes/0/claims/0/text"]
    assert build.check_excerpts(cross_copy) == []  # record ancestor, beyond the nearest citation
    institute["source_ids"].remove("unit-source")
    institute["claims"][0]["source_ids"].append("unit-source")
    assert build.check_excerpts(cross_copy) == []  # claim citation
    quoted(cross_copy)["supports"].append("/jurisdictions/0/summary")
    found = build.check_excerpts(cross_copy)
    assert len(found) == 1 and "/jurisdictions/0/summary" in found[0] and "does not cite anchor source" in found[0]


def test_other_copy_gloss_uses_the_named_source_language(cross_copy):
    named = next(s for s in cross_copy["sources"] if s["id"] == "unit-source")
    named["language"] = "de"
    assert any("non-English anchor needs an English gloss" in e for e in build.check_excerpts(cross_copy))
    quoted(cross_copy)["anchors"][0]["gloss"] = "The lab belongs to the institute."
    assert build.check_excerpts(cross_copy) == []
    named["language"] = "en"
    source(cross_copy)["language"] = "de"
    quoted(cross_copy)["gloss"] = "Synthetic institute"
    quoted(cross_copy)["anchors"][0].pop("gloss")
    assert build.check_excerpts(cross_copy) == []
