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
    removeAttribute(k) { delete this.attrs[k]; },
    addEventListener(k, f) { this[k] = f; },
    querySelector(selector) { return this.children.find(n => n.className === selector.slice(1)); },
    appendChild(n) { this.children.push(n); return n; },
    set textContent(t) { this.children = [{text: t}]; },
    get textContent() { return this.children.map(n => n.text ?? n.textContent).join(''); },
    ...(text == null ? {} : {children: [{text}]})};
}
var window = {}, document = {createTextNode: text => ({text}), addEventListener() {}};
function nonbreakingDates(s) { return s; }
function rereadLine(s) { return el('span', 'reread', s.id || ('R' + (s.excerpts || []).length)); } // a source, or one quotation's own state
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


def test_page_script_parses_without_chromium():
    source = PAGE.read_text().split("<script>", 1)[1].split("</script>", 1)[0]
    result = subprocess.run(["node", "-"], input="new Function(" + json.dumps(source) + ");", text=True, capture_output=True)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("gloss, lang, marker", [(None, "en", ""), ("Models", "de", "German"), ("Models", "xx", "xx")])
@pytest.mark.parametrize("in_sentence", [False, True])
def test_quote_rule_translation_original_focus_and_language_fallback(gloss, lang, marker, in_sentence):
    option = ", true" if in_sentence else ""  # Quote folds keep the default.
    quote = page_call(f"window.quoteNode('<b>Modelle</b>', {json.dumps(gloss)}, {json.dumps(lang)}{option})")
    english = quote["children"][0]
    assert texts(english) == '"' + (gloss or "<b>Modelle</b>") + '"'
    assert english["tag"] == "span"
    if gloss:
        assert english["attrs"] == {"aria-description": "<b>Modelle</b>"}
        assert english["tabIndex"] == 0
        if in_sentence:
            assert len(quote["children"]) == 1
        else:
            assert texts(quote).endswith("translated from " + marker)
    else:
        assert english["attrs"] == {} and len(quote["children"]) == 1


@pytest.mark.parametrize("gloss", [None, "", " \t", "English translation"])
def test_quote_without_shown_has_no_cut_controls(gloss):
    box = page_call(f"quoteNode('Original quotation', {json.dumps(gloss)}, 'de')")
    assert all(child.get("tag") != "button" for child in box["children"])
    quote = box["children"][0]
    assert "aria-haspopup" not in quote["attrs"] and "aria-controls" not in quote["attrs"]
    if not gloss or not gloss.strip():
        assert quote["attrs"] == {} and texts(box) == '"Original quotation"'


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
    assert texts(body) == '"A fact" translated from GermanHeading: "Title" translated from GermanR1'  # the quotation's own state (R1), never its source's
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
            '<details class="quote" data-quote-claim="fact"><summary>Quote</summary></details></div></article>')
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
            '<details class="quote" data-quote-claim="development"><summary>Quote</summary></details></div></details>')
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


def test_browser_fixture_drops_the_data_bindings_of_the_claims_it_replaces():
    # A migrated record with claims of its own: their excerpts pointed at positions the fixture's claims take over,
    # so a fixture claim had two bindings and the shown-quote job failed on CI (16 November 2026).
    data = {"jurisdictions": [{"id": "AA", "status": "planned"},
                              {"id": "BB", "status": "none_identified", "europe_scope": True,
                               "claims": [{"id": "real-0", "kind": "function", "text": "A."}, {"id": "real-1", "kind": "function", "text": "B."}]}],
            "institutes": [], "sources": [{"id": "real-source", "excerpts": [
                {"text": "Real.", "supports": ["/jurisdictions/1/claims/1/text", "/jurisdictions/0/status"]}]}]}
    fixture, _ = check_page.claim_quote_fixture(data)
    shown = check_page.card_claims(fixture["jurisdictions"][1], {})
    assert [len(check_page.claim_bindings(fixture, c["id"])) for c in shown] == [1, 1, 1, 1, 1, 2]
    real = next(s for s in fixture["sources"] if s["id"] == "real-source")["excerpts"][0]
    assert real["supports"] == ["/jurisdictions/0/status"]
    assert data["sources"][0]["excerpts"][0]["supports"][0] == "/jurisdictions/1/claims/1/text"  # the input is untouched


@pytest.mark.parametrize("translated", [False, True])
def test_shown_quote_gate_is_an_independent_job_with_valid_fixture(translated):
    from scripts import build, claim_checks

    data = {"jurisdictions": [{"id": "AA", "status": "planned"}, {"id": "BB", "status": "none_identified", "europe_scope": True}], "institutes": [], "sources": []}
    fixture, cases, quote = check_page.shown_quote_fixture(data, translated)
    assert len(cases) == 2 and 300 < len(quote["text"]) <= 500 and data["sources"] == []
    assert not [e for e in build.check_excerpts(fixture) if "shown" in e]
    for jid, cid in cases:
        record = next(j for j in fixture["jurisdictions"] if j["id"] == jid)
        claim = next(c for c in record["claims"] if c["id"] == cid)
        source, _, bound = check_page.claim_bindings(fixture, cid)[0]  # fixture uniqueness asserted below
        assert len(check_page.claim_bindings(fixture, cid)) == 1 and bound["shown"] == quote["shown"]
        if claim.get("lead"):
            assert claim_checks.check_lead("lead", claim, {"text": [(source, bound)]}) == []
    assert ("interactions", check_page.check_shown_quotes, (data,)) in check_page.browser_jobs(Path("site"), data, None)


@pytest.mark.parametrize("parts, expected", [
    ([{"text": "First visible quotation."}], "First visible quotation. [...]"),
    ([{"text": "Middle visible quotation."}], "[...] Middle visible quotation. [...]"),
    ([{"text": "Last visible quotation."}], "[...] Last visible quotation."),
    ([{"text": "First visible quotation."}, {"text": "Last visible quotation."}], "First visible quotation. [...] Last visible quotation."),
    ([{"text": "First visible quotation."}, {"text": "Middle visible quotation."}, {"text": "Last visible quotation."}], "First visible quotation. [...] Middle visible quotation. [...] Last visible quotation."),
])
def test_cut_markers_follow_omitted_edges_and_part_order(parts, expected):
    text = "First visible quotation. " + "Hidden context. " * 12 + "Middle visible quotation. " + "Hidden context. " * 8 + "Last visible quotation."
    result = page_call(f"quoteDisplay({json.dumps(text)}, null, {json.dumps(parts)})")
    assert result == {"text": expected, "gloss": None}


def test_saved_lead_check_requires_the_whole_original():
    html, data = saved_claim_page()
    record = data["jurisdictions"][0]
    record.update(status="none_identified", europe_scope=True, closest_body={"body_id": "body"})
    claim, = record["claims"]
    claim.update(kind="function", body_id="body", lead=True, text='It says "a fact".')
    bound = data["sources"][0]["excerpts"][0]
    part = "An original-language quotation."
    bound.update(text="Opening context. " + part + " Hidden context. " * 20,
                 shown=[{"text": part, "gloss": "A fact"}])
    original = bound["text"]
    html = html.replace("A fact.", f'It says <span class="quotation"><span class="quote-text" tabindex="0" aria-description="{original}">"a fact"</span><button class="quote-whole">Whole quote</button></span>.')
    assert check_page.check_claim_cards(html, data) == []
    assert any("bound original" in e for e in check_page.check_claim_cards(html.replace(original, "[...] " + part + " [...]"), data))


@pytest.mark.parametrize("translated", [False, True])
def test_shown_quote_toggle_keeps_the_same_text_and_button_nodes(translated):
    text = "Opening. <b>First visible quotation.</b> " + "Hidden context. " * 20 + "Last visible quotation. Closing."
    parts = [{"text": "<b>First visible quotation.</b>"}, {"text": "Last visible quotation."}]
    gloss = "A full English translation." if translated else None
    if translated:
        for part in parts:
            part["gloss"] = "Translation of " + part["text"]
    args = ", ".join(json.dumps(v) for v in (text, gloss, "de" if translated else "en", False, parts))
    result = page_call("""(() => {
      var box = quoteNode(""" + args + """), quote = box.querySelector('.quote-text'), button = box.querySelector('.btn quote-whole');
      function state() { return {text: quote.textContent, original: quote.attrs['aria-description'] || null, expanded: button.attrs['aria-expanded'], label: button.textContent}; }
      var states = [state()]; button.click(); states.push(state()); button.click(); states.push(state());
      return {states, same: quote === box.querySelector('.quote-text') && button === box.querySelector('.btn quote-whole'), type: button.type, quote};
    })()""")
    cut = "[...] " + " [...] ".join(p.get("gloss", p["text"]) for p in parts) + " [...]"
    original = text if translated else None
    assert result["states"] == [
        {"text": '"' + cut + '"', "original": original, "expanded": "false", "label": "Whole quote"},
        {"text": '"' + (gloss or text) + '"', "original": text if translated else None, "expanded": "true", "label": "Short quote"},
        {"text": '"' + cut + '"', "original": original, "expanded": "false", "label": "Whole quote"},
    ]
    assert result["same"] and result["type"] == "button"
    assert result["quote"]["children"] == [{"text": '"' + cut + '"'}]  # markup stays text
    assert "aria-haspopup" not in result["quote"]["attrs"] and "aria-controls" not in result["quote"]["attrs"]


@pytest.mark.parametrize("translated", [False, True])
def test_lead_keeps_quoted_span_and_toggles_inline_with_a_whole_original_tip(translated):
    data = {"jurisdictions": [{"id": "AA", "status": "planned"}, {"id": "BB", "status": "none_identified", "europe_scope": True}], "institutes": [], "sources": []}
    fixture, cases, excerpt = check_page.shown_quote_fixture(data, translated)
    _, cid = next(case for case in cases if case[0] == "BB")
    result = page_call("""(() => {
      CLAIM_INDEX = claimIndex(""" + json.dumps(fixture) + """);
      var claim = CLAIM_INDEX[""" + json.dumps(cid) + """].claim, line = claimSentence(claim, claim.text);
      var box = line.querySelector('.quotation'), quote = box.querySelector('.quote-text'), button = box.querySelector('.btn quote-whole');
      function state() { return {sentence: line.textContent, original: quote.attrs['aria-description'] || null}; }
      var states = [state()]; button.click(); states.push(state()); button.click(); states.push(state());
      return {states, quote};
    })()""")
    original = excerpt["text"] if translated else None
    short = {"sentence": 'It says it "tests models"Whole quote.', "original": original}
    assert result["states"] == [short, {"sentence": 'It says it "' + excerpt.get("gloss", excerpt["text"]) + '"Short quote.', "original": original}, short]
    assert "quoteCut" not in result["quote"]
    assert "aria-haspopup" not in result["quote"]["attrs"] and "aria-controls" not in result["quote"]["attrs"]


def test_other_copy_anchor_shows_its_source_title_language_and_original():
    data = bound_data()
    named = {"id": "unit-source", "title": "Unit <membership>", "language": "fr"}
    data["sources"].append(named)
    anchor = {"kind": "membership", "text": "Le laboratoire appartient à l'institut.", "gloss": "The lab belongs to the institute.", "source_id": named["id"]}
    data["sources"][0]["excerpts"][0]["anchors"].append(anchor)
    body = page_call(f"(CLAIM_INDEX = claimIndex({json.dumps(data)}), claimQuoteBody('fact'))")
    line, = [n for n in body["children"][0]["children"] if n.get("className") == "quote-anchor" and texts(n).startswith("Part of:")]
    assert texts(line) == 'Part of: "The lab belongs to the institute." translated from French (Unit <membership>)'
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
