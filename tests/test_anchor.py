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
    [LEAD_IN, DEFINITION, {"text": "Article 3", "kind": "provision"}],  # three spans
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


def test_page_data_leaves_out_the_anchors(anchored):
    """The page shows nothing new in this step; step F shows the anchors with their quote."""
    published = build.export_dataset(anchored)
    page = json.loads(build.page_data(published))
    assert all("anchors" not in e for s in page["sources"] for e in s.get("excerpts", []))
    assert quoted(page) == {k: v for k, v in quoted(published).items() if k != "anchors"}


def test_gate_expects_the_page_data_the_build_writes(anchored):
    """The gate's expected page.json, written from its own list, matches the build's on a dataset with anchors,
    so a field one of them leaves out and the other keeps fails here, not first on a data branch."""
    published = build.export_dataset(anchored)
    assert any(e.get("anchors") for s in published["sources"] for e in s.get("excerpts", []))
    assert check_page.expected_page_data(published) == json.loads(build.page_data(published))
    assert "anchors" in check_page.PAGE_DATA_LEFT_OUT


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
