"""Card claim bindings and saved-page checks, without starting Chromium."""

import copy
import json
from pathlib import Path
import subprocess

import pytest

from scripts import check_page

PAGE = Path(__file__).resolve().parents[1] / "site/index.html"

# A text-only DOM stub: no HTML parsing or browser. It records DOM construction so quotes cannot turn source
# markup into elements, and lets the same production helpers run on synthetic published bindings.
DOM = """
function el(tag, cls, text) {
  return {tag, tagName: tag.toUpperCase(), className: cls || '', attrs: {}, dataset: {}, children: [],
    classList: {add(c) {}},
    setAttribute(k, v) { this.attrs[k] = v; },
    appendChild(n) { this.children.push(n); return n; },
    set textContent(t) { this.children = [{text: t}]; },
    get textContent() { return this.children.map(n => n.text ?? n.textContent).join(''); },
    ...(text == null ? {} : {children: [{text}]})};
}
var window = {}, document = {createTextNode: text => ({text}), addEventListener() {}};
function nonbreakingDates(s) { return s; }
function rereadLine(s) { return el('span', 'reread', s.id || ('R' + (s.excerpts || []).length)); } // a source, or one quotation's own state
function archiveCopy(parent, s) {}
function andJoin(xs) { return xs.length < 2 ? xs.join('') : xs.slice(0, -1).join(', ') + ' and ' + xs[xs.length - 1]; }
"""


def page_call(expression):
    page = PAGE.read_text()
    source = page[page.index("  function withOriginal("):page.index("  // Institute name, linked to its official website")]
    source += page[page.index('  var FACTS_LABEL ='):page.index('  function sourceList(')]
    result = subprocess.run(["node", "-"], input=DOM + source + "\nconsole.log(JSON.stringify(" + expression + "));",
                            text=True, capture_output=True, check=True)
    return json.loads(result.stdout)


def texts(node):
    return node.get("text", "") + "".join(texts(child) for child in node.get("children", []))


@pytest.mark.parametrize("gloss", [None, "Models"])
def test_quote_rule_translation_original_and_focus(gloss):
    quote = page_call(f"window.quoteNode('<b>Modelle</b>', {json.dumps(gloss)})")
    english, = quote["children"]  # no marker follows a quote: the Quote fold's label names the translation
    assert texts(english) == '"' + (gloss or "<b>Modelle</b>") + '"'
    assert english["tag"] == "span"
    if gloss:
        assert english["attrs"] == {"aria-description": "<b>Modelle</b>"}
        assert english["tabIndex"] == 0
    else:
        assert english["attrs"] == {}


def test_quote_label_names_plain_languages_once():
    data = bound_data()
    data["sources"][0]["language"] = "nb"
    data["sources"].append({"id": "fr", "language": "fr"})
    data["sources"][0]["excerpts"][0]["anchors"].append({"kind": "membership", "text": "x", "gloss": "y", "source_id": "fr"})
    labels = page_call(f"(CLAIM_INDEX = claimIndex({json.dumps(data)}), [quoteLabel('fact'), quoteLabel('development')])")
    assert labels == ["Quote, translated from Norwegian and French", "Quote"]


def bound_data():
    claims = [{"id": "fact", "kind": "fact", "text": "A fact.", "citations": {"text": ["s"]}},
              {"id": "development", "kind": "development", "act": "the government acted", "date": "2026-09-01", "text": "assigns tests", "citations": {k: ["s"] for k in ("text", "act", "date")}}]
    prefix = "/jurisdictions/0/claims/"
    return {"jurisdictions": [{"id": "XX", "status": "planned", "claims": claims}], "institutes": [], "sources": [
        {"id": "s", "language": "de", "excerpts": [
            {"text": "Fakt", "gloss": "A fact", "supports": [prefix + "0/text"], "anchors": [{"kind": "heading", "text": "Titel", "gloss": "Title"}]},
            {"text": "Testing", "supports": [prefix + "1/text"]},
            {"text": "Act and date", "supports": [prefix + "1/act", prefix + "1/date"], "anchors": [{"kind": "provision", "text": "Article 1"}]},
            {"text": "Unrelated", "supports": ["/jurisdictions/0/summary"]},
        ]}]}


def test_bindings_use_required_leaves_and_deduplicate_a_shared_act_and_date_excerpt():
    data = bound_data()
    index = page_call(f"claimIndex({json.dumps(data)})")
    assert [x["index"] for x in index["fact"]["excerpts"]] == [0]
    assert [x["index"] for x in index["development"]["excerpts"]] == [1, 2]
    assert [i for _, i, _ in check_page.claim_bindings(data, "development")] == [1, 2]
    body = page_call(f"(CLAIM_INDEX = claimIndex({json.dumps(data)}), claimQuoteBody('fact'))")
    assert len(body["children"]) == 1
    assert texts(body) == '"A fact"Heading: "Title"R1'  # the quotation's own state (R1), never its source's
    assert body["children"][0]["dataset"] == {"sourceId": "s", "excerptIndex": 0}


@pytest.mark.parametrize("ending", [".", " with them."])
def test_lead_keeps_its_authored_quoted_substring_and_uses_the_full_original(ending):
    data = bound_data()
    claim = data["jurisdictions"][0]["claims"][0]
    claim.update(lead=True, text='It says "fact"' + ending)
    line = page_call(f"(CLAIM_INDEX = claimIndex({json.dumps(data)}), claimSentence(CLAIM_INDEX.fact.claim, CLAIM_INDEX.fact.claim.text))")
    assert texts(line) == 'It says "fact"' + ending
    original = line["children"][1]["children"][0]
    assert original["attrs"] == {"aria-description": "Fakt"}
    assert original["tabIndex"] == 0


@pytest.mark.parametrize("state, kept", [("source_gone", True), ("blocked_or_failed", True), ("quote_gone", False)])
def test_expired_leads_and_body_roles_match_summary_selection(state, kept):
    record = {"closest_body": {"body_id": "body"}, "claims": [
        {"id": "lead", "kind": "function", "body_id": "body", "lead": True, "expired": True, "reread": {"state": state}},
        {"id": "role", "kind": "function", "body_id": "body"},
        {"id": "old-role", "kind": "function", "body_id": "body", "expired": True}]}
    actual = page_call(f"(j = {json.dumps(record)}, [!!shownLead(j), bodyClaims(j, 'body').map(c => c.id)])")
    assert actual == [kept, (["role", "old-role"] if kept else ["lead", "role", "old-role"])]


@pytest.mark.parametrize("named, sources, role, repeated", [
    (True, ["s1", "s2"], None, True),
    (True, ["s1"], None, False),
    (False, ["s1", "s2"], None, False),
    (True, ["s1", "s2"], {"lead": True}, True),
    (True, ["s1", "s2"], {}, False),
    (True, ["s1", "s2"], {"expired": True}, False),
])
def test_closest_body_repeated_requires_name_citations_and_no_role_claim(named, sources, role, repeated):
    record = {"summary": "Closest body: Fixture lab." if named else "An authority is listed.",
              "closest_body": {"name": "Fixture lab", "body_id": "lab", "display": {"source_links": ["s1", "s2"]}},
              "claims": [] if role is None else [{"id": "role", "kind": "function", "body_id": "lab", **role}]}
    assert page_call(f"closestBodyRepeated({json.dumps(record)}, {json.dumps(sources)})") is repeated
    record.pop("claims")
    assert page_call(f"closestBodyRepeated({json.dumps(record)}, {json.dumps(sources)})") is False


def test_facts_keep_jurisdiction_then_institute_order_without_functions_or_expired_facts():
    j = {"claims": [{"id": "j-first", "kind": "fact"}, {"id": "old", "kind": "fact", "expired": True}]}
    i = {"claims": [{"id": "function", "kind": "function"}, {"id": "i-last", "kind": "fact"}]}
    assert page_call(f"factClaims({json.dumps(j)}, {json.dumps(i)}).map(c => c.id)") == ["j-first", "i-last"]


def saved_claim_page():
    data = bound_data()
    data["jurisdictions"][0]["claims"] = data["jurisdictions"][0]["claims"][:1]
    html = ('<article id="c-xx"><div class="claim" data-claim-id="fact"><span class="claim-text">A fact.'
            '<span class="cites"><a class="cn" data-source-id="s">1</a></span></span>'
            '<details class="quote" data-quote-claim="fact"><summary>Quote, translated from German</summary></details></div></article>')
    return html, data


def test_saved_claim_content_contract_accepts_sentences_with_citations_and_empty_disclosures():
    html, data = saved_claim_page()
    assert check_page.check_claim_cards(html, data) == []


@pytest.mark.parametrize("before, after", [
    ("A fact.", "Something else."), ('class="cn"', 'class="lost"'),
    ('data-source-id="s"', 'data-source-id="wrong"'), ('data-quote-claim="fact"', 'data-quote-claim="other"'),
    ("</summary>", '</summary><div class="quote-body">Leaked original</div>'),
    ('<details class="quote"', '<details open class="quote"'), ('class="claim"', 'class="gone"'),
])
def test_saved_content_check_detects_missing_claims_citations_and_saved_quotes(before, after):
    html, data = saved_claim_page()
    assert check_page.check_claim_cards(html.replace(before, after), data)


def test_saved_content_check_rejects_expired_facts_and_claims_added_to_legacy_cards():
    html, data = saved_claim_page()
    data["jurisdictions"][0]["claims"][0]["expired"] = True
    assert check_page.check_claim_cards(html, data)
    legacy = copy.deepcopy(data)
    legacy["jurisdictions"][0].update(status="none_identified", europe_scope=True)
    legacy["jurisdictions"][0].pop("claims")
    assert any("legacy" in e for e in check_page.check_claim_cards(html, legacy))


def test_development_sentence_and_all_three_citations_are_checked():
    data = bound_data()
    record = data["jurisdictions"][0]
    record.update(status="none_identified", europe_scope=True)
    record["claims"] = record["claims"][1:]
    html = ('<details id="c-xx"><div class="claim" data-claim-id="development"><span class="claim-text">'
            'On 1 Sep 2026 the government acted, which assigns tests.<span class="cites"><a class="cn" data-source-id="s">1</a></span></span>'
            '<details class="quote" data-quote-claim="development"><summary>Quote, translated from German</summary></details></div></details>')
    assert check_page.check_claim_cards(html, data) == []
    record["claims"][0]["citations"]["act"] = ["different-act-source"]
    assert any("citations" in e for e in check_page.check_claim_cards(html, data))


def test_translated_lead_keeps_the_exact_sentence_in_the_saved_page():
    html, data = saved_claim_page()
    data["jurisdictions"][0]["claims"][0]["text"] = 'It says "a fact".'
    html = html.replace("A fact.", 'It says <span class="quotation"><span class="quote-text" tabindex="0" aria-description="Fakt">"a fact"</span></span>.')
    assert check_page.check_claim_cards(html, data) == []


def test_browser_fixture_covers_every_role_and_development_binding():
    data = {"jurisdictions": [{"id": "AA", "status": "planned"}, {"id": "BB", "status": "none_identified", "europe_scope": True}], "institutes": [], "sources": []}
    fixture, ids = check_page.claim_quote_fixture(data)
    assert ids == ["AA", "BB"] and data["sources"] == []
    migrated = fixture["jurisdictions"][1]
    shown = check_page.card_claims(migrated, {})
    assert len(shown) == 6 and shown[0]["expired"]
    assert [len(check_page.claim_bindings(fixture, c["id"])) for c in shown] == [1, 1, 1, 1, 1, 2]
    other_copy = [(s, a) for s in fixture["sources"] for e in s.get("excerpts", []) for a in e.get("anchors", []) if "source_id" in a]
    assert other_copy
    for owner, anchor in other_copy:
        named = next(s for s in fixture["sources"] if s["id"] == anchor["source_id"])
        assert named["title"] and named["language"] != owner["language"]


def test_other_copy_anchor_shows_its_source_title_language_and_original():
    data = bound_data()
    named = {"id": "unit-source", "title": "Unit <membership>", "language": "fr"}
    data["sources"].append(named)
    anchor = {"kind": "membership", "text": "Le laboratoire appartient à l'institut.", "gloss": "The lab belongs to the institute.", "source_id": named["id"]}
    data["sources"][0]["excerpts"][0]["anchors"].append(anchor)
    body = page_call(f"(CLAIM_INDEX = claimIndex({json.dumps(data)}), claimQuoteBody('fact'))")
    line, = [n for n in body["children"][0]["children"] if n.get("className") == "quote-anchor" and texts(n).startswith("Part of:")]
    assert texts(line) == 'Part of: "The lab belongs to the institute." (Unit <membership>)'
    quote = line["children"][1]["children"][0]
    assert quote["attrs"]["aria-description"] == anchor["text"] and quote["tabIndex"] == 0
    assert line["children"][-1] == {"text": ")"} and texts(line["children"][-2]) == "Unit <membership>"
    assert line["children"][-2].get("lang") == "fr" or line["children"][-2].get("attrs", {}).get("lang") == "fr"


def test_other_copy_anchor_title_shows_english_with_the_original_as_tip():
    data = bound_data()
    named = {"id": "unit-source", "title": "Unité <membership>", "title_en": "Unit <membership>", "language": "fr"}
    data["sources"].append(named)
    anchor = {"kind": "membership", "text": "Le laboratoire appartient à l'institut.", "gloss": "The lab belongs to the institute.", "source_id": named["id"]}
    data["sources"][0]["excerpts"][0]["anchors"].append(anchor)
    body = page_call(f"(CLAIM_INDEX = claimIndex({json.dumps(data)}), claimQuoteBody('fact'))")
    line, = [n for n in body["children"][0]["children"] if n.get("className") == "quote-anchor" and texts(n).startswith("Part of:")]
    assert texts(line).endswith("(Unit <membership>)")
    title = line["children"][-2]
    assert title["attrs"]["aria-description"] == "Unité <membership>" and title["tabIndex"] == 0


def test_expired_body_role_stays_with_a_warning_but_not_in_the_finding():
    html, data = saved_claim_page()
    j = data["jurisdictions"][0]
    j.update(status="none_identified", europe_scope=True, closest_body={"body_id": "body"}, related_bodies=[{"id": "body"}])
    c = j["claims"][0]
    c.update(kind="function", body_id="body", lead=True, expired=True, reread={"state": "quote_gone"})
    assert check_page.card_claims(j, {}) == [c]
    assert any("warning" in e for e in check_page.check_claim_cards(html, data))
    html = html.replace('<details class="quote"', '<span class="reread">Not found in the source; under review.</span><details class="quote"')
    assert check_page.check_claim_cards(html, data) == []


def test_a_claim_sentence_is_its_own_value_for_citation_ends():
    from bs4 import BeautifulSoup
    from scripts import check_page

    ok = ('<div class="v"><div class="claim"><span class="claim-text">It tests models.<span class="cites"><a class="cn">1</a></span></span>'
          '<details class="quote"><summary>Quote</summary></details></div></div>')
    assert check_page.check_citation_ends(BeautifulSoup(ok, "html.parser")) == []
    bad = ok.replace('<a class="cn">1</a></span></span>', '<a class="cn">1</a></span> later words</span>')
    assert check_page.check_citation_ends(BeautifulSoup(bad, "html.parser")) == ["citation group is not at the end of its value"]


def test_a_development_without_a_closest_body_is_the_finding():
    # Pattern 3: the summary row shows the development sentence; the fold shows it once, as a cited claim, and never
    # brings back the dropped "No qualifying institute identified" opening (review of 15 November).
    card = page_call("""(function () {
      fmtDate = d => d; fieldCitations = () => ['s1']; var cite = ids => el('span', 'cites', ids.join(','));
      var j = {id: 'SI', summary: 'No qualifying institute identified as of October 2026. On 2026-03-05 the government adopted the strategy, which plans tests.',
               claims: [{id: 'claims-d', kind: 'development', date: '2026-03-05', act: 'the government adopted the strategy', text: 'plans tests'}]};
      var card = el('div'); migratedFinding(card, j, 'On 2026-03-05 the government adopted the strategy, which plans tests.', cite); return card; })()""")
    shown = texts(card)
    assert "No qualifying institute" not in shown
    assert shown.count("the government adopted the strategy") == 1 and "s1" in shown
