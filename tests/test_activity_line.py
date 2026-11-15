"""Activity-line rendering and content checks, without a browser or a site build."""

import copy
import json
from pathlib import Path
import subprocess

import pytest
from bs4 import BeautifulSoup

from content.test_activity_line import FAMILY, RULE_CASES, activity_expectation, check_activity_lines
from scripts import check_page, published
from test_check_page import alignment_case, interval_case, interval_html  # noqa: F401 (shared content fixtures)

ROOT = Path(__file__).resolve().parents[1]
SHORTS = dict(zip(
    ("model_testing", "evaluation_methods", "research", "standards_guidance", "regulatory_enforcement",
     "regulatory_support", "risk_monitoring", "foresight", "policy_advice", "incident_response",
     "international_cooperation", "convening", "capacity_building", "talent", "adoption",
     "public_access_continuity", "certification", "sandbox"),
    ("model testing", "evaluation methods", "research", "standards", "enforcement", "regulator support",
     "risk monitoring", "foresight", "policy advice", "incident response", "international cooperation",
     "convening", "capacity building", "training", "adoption", "public-sector access", "certification", "sandboxes"),
    strict=True,
))

# Records DOM construction, including moving the original Status children into their own paragraph.
DOM = r"""
function el(tag, cls, text) {
  return {tag, className:cls||'', dataset:{}, attrs:{}, children:text == null ? [] : [{text}],
    setAttribute(k,v){this.attrs[k]=v;},
    appendChild(n){if(n.parent)n.parent.children.splice(n.parent.children.indexOf(n),1);this.children.push(n);n.parent=this;return n;},
    get firstChild(){return this.children[0];},
    get classList(){return {add: c => {this.className += ' '+c;}};}};
}
const document={createTextNode:text=>({text})};
function esc(x){return String(x).replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('"','&quot;');}
function html(n){if('text' in n)return esc(n.text);return '<'+n.tag+
  (n.className?' class="'+esc(n.className)+'"':'')+(n.id?' id="'+esc(n.id)+'"':'')+
  Object.entries(n.attrs).map(([k,v])=>' '+k+'="'+esc(v)+'"').join('')+
  Object.entries(n.dataset).map(([k,v])=>' data-'+k.replace(/[A-Z]/g,c=>'-'+c.toLowerCase())+'="'+esc(v)+'"').join('')+
  '>'+n.children.map(html).join('')+'</'+n.tag+'>';}
function cite(ids){const n=el('span','cites');[...new Set(ids)].forEach((id,i)=>{
  const a=el('a','cn',String(i+1));a.dataset.sourceId=id;n.appendChild(a);});return n;}
function bodyClaims(j,id){return (j.claims||[]).filter(c=>c.kind==='function'&&c.body_id===id&&!c.lead);}
function claimBlock(c){return el('span','claim',c.text||'Role sentence.');}
function rereadLine(){return null;}
function fmtDate(s){return s;}
function separated(parts){const n=el('span');parts.forEach(p=>n.appendChild(typeof p==='string'?{text:p}:p));return n;}
function statusSince(j){return j.status+' since '+j.status_since;}
function statusConfidence(){return 'Medium confidence';}
function intervalQualifier(t){return {text:t};}
function fieldCitations(j, fields){return fields[0]==='text'?(j.citations||{}).text||[]:j.status_sources||[];}
const LABEL={established:'Established',planned:'Planned',proposed:'Proposed',exploring:'Exploring',none_identified:'None identified'};
"""


def render_cards(data):
    source = (ROOT / "site/index.html").read_text()
    words = source[source.index("  var ACTIVITY_WORDS ="):source.index("  // Status definitions, short:")]
    helpers = source[source.index("  function qualified("):source.index("  // A None identified state's finding")]
    join = next(line for line in source.splitlines() if line.startswith("  function andJoin("))
    bodies = source[source.index("  var ROLE_ORDER ="):source.index("  // Research notes in one muted block.")]
    status = source[source.index("      var statusValue;"):source.index("      var form =", source.index("      var statusValue;"))]
    script = DOM + words + helpers + join + bodies + "\nconst data=" + json.dumps(data) + ";\n" + """
const m=data.dataset,I=Object.fromEntries(data.institutes.map(i=>[i.id,i]));
const cards=data.jurisdictions.filter(j=>j.europe_scope||j.kind==='supranational'||j.status!=='none_identified').map(j=>{
 const inst=I[j.institute_id],card=el('article','card'+(inst?'':' mini'));card.id='c-'+j.id.toLowerCase();card.dataset.j=j.id;
 if(inst){
  function row(label,value,ids){const r=el('div','row'),v=el('div','v');r.dataset.track='3';
   v.appendChild(typeof value==='string'?{text:value}:value);
   if(ids&&ids.length)v.appendChild(cite(ids));r.appendChild(v);card.appendChild(r);return v;}
""" + status + """
 }else{nonIdentifiedBodies(j,cite,m).forEach(g=>card.appendChild(g));}
 return html(card);
});console.log(cards.join(''));
"""
    result = subprocess.run(["node", "-"], input=script, text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    return result.stdout


def fixture(case, count=1, status=None):
    j = {"id": "XX", "status": status or "none_identified", "claims": [], "europe_scope": True}
    data = {"dataset": {"vocabularies": {"function": {c: {"short": s} for c, s in SHORTS.items()}}}, "jurisdictions": [j], "institutes": []}
    claims = [{"id": f"claim-{code}", "kind": "function", "code": code, "basis": "activity", "source_ids": [code, "shared"], "body_id": "body"}
              for code in list(SHORTS)[:count]]
    for c in claims:
        c["citations"] = {"text": c["source_ids"]}
    rows = [{"code": c["code"], "claim_ids": [c["id"]]} for c in claims]
    if case.startswith("closest"):
        j["claims"] = claims
        j["closest_body"] = {"body_id": "body", "name": "Body", "stage": "planned", "display": {"source_links": ["name-source"]}}
        if case == "closest with activity":
            j["closest_body"]["active_in"] = rows
        else:
            claims[0]["basis"] = "mandate"
    else:
        j.update(institute_id="institute", status=status or ("planned" if case.startswith("planned") else "established"),
                 status_since="2026-01-01", status_confidence="medium", status_sources=["status-source"])
        inst = {"id": "institute", "claims": claims, "functions": [{**r, "value": "activity"} for r in rows],
                "display": {"operating": {"items": []}, "intervals": {"status_since": "Announced on 1 Jan 2025, 365 days earlier"}}}
        data["institutes"] = [inst]
        if case == "planned or proposed without activity":
            inst["functions"][0]["value"] = "mandate"
            claims[0]["basis"] = "mandate"
        elif case == "established with work":
            inst["display"]["operating"]["items"] = [{"kind": "published_work"}]
        elif case == "other or legacy":
            inst.pop("claims")
    return data


@pytest.mark.parametrize("case", RULE_CASES)
def test_each_rule_case_renders_and_is_checked(case):
    data = fixture(case)
    html = render_cards(data)
    assert activity_expectation(data["jurisdictions"][0], next(iter(data["institutes"]), None), data["dataset"]["vocabularies"]["function"])[0] == case
    assert FAMILY["activity line"](html, data, case) == []
    assert check_page.check_citation_ends(BeautifulSoup(html, "html.parser")) == []
    if "without activity" in case or case in ("established with work", "other or legacy"):
        assert "activity-line" not in html


@pytest.mark.parametrize("case", ["closest with activity", "planned or proposed with activity"])
@pytest.mark.parametrize("count, work", [(1, "model testing"), (2, "model testing and evaluation methods"),
    (3, "model testing, evaluation methods and research"), (4, "model testing, evaluation methods, research and 1 more"),
    (5, "model testing, evaluation methods, research and 2 more")])
def test_lists_use_vocabulary_order_and_cite_all_activity_claims(case, count, work):
    data = fixture(case, count)
    record = data["institutes"][0] if data["institutes"] else data["jurisdictions"][0]
    rows = record["functions"] if data["institutes"] else record["closest_body"]["active_in"]
    rows.reverse()
    html = render_cards(data)
    assert "Active in " + work + "." in html
    assert check_activity_lines(html, data) == []
    line = BeautifulSoup(html, "html.parser").select_one(".activity-line")
    assert len(line.select('[data-source-id="shared"]')) == 1
    assert len(line.select(".cn")) == count + 1


@pytest.mark.parametrize("status", ["planned", "proposed"])
def test_both_cites_activity_but_not_mandate_and_preserves_function_rows(status):
    data = fixture("planned or proposed with activity", status=status)
    inst = data["institutes"][0]
    mandate = {**inst["claims"][0], "id": "mandate", "basis": "mandate", "source_ids": ["mandate-source"]}
    inst["claims"].append(mandate)
    inst["claims"][0]["expired"] = True  # function rows keep their published semantics
    inst["functions"][0].update(value="both", claim_ids=[mandate["id"], inst["claims"][0]["id"]])
    html = render_cards(data)
    assert check_activity_lines(html, data) == []
    assert "mandate-source" not in html and "status-source" in html
    assert check_page.check_citation_ends(BeautifulSoup(html, "html.parser")) == []


@pytest.mark.parametrize("status", ["exploring", "none_identified"])
def test_institutes_without_a_qualifying_status_have_no_line(status):
    data = fixture("planned or proposed with activity", status=status)
    assert "activity-line" not in render_cards(data)


def test_legacy_closest_body_ignores_activity():
    data = fixture("closest with activity")
    data["jurisdictions"][0].pop("claims")
    assert "activity-line" not in render_cards(data)


@pytest.mark.parametrize("before, after", [
    ("Not yet established.", "Established."), ("activity-line", "lost-line"),
    ('data-source-id="shared"', 'data-source-id="wrong"'), ("small qualifiers", "qualifiers"),
    ('data-track="3"', 'data-track="2"'),
])
def test_content_check_rejects_wrong_words_missing_line_citations_and_placement(before, after):
    data = fixture("planned or proposed with activity")
    assert check_activity_lines(render_cards(data).replace(before, after), data)


def test_content_check_rejects_extra_lines_and_wrong_body_or_stage_order():
    data = fixture("closest with activity")
    html = render_cards(data)
    assert check_activity_lines(html.replace('data-body-id="body"', 'data-body-id="other"'), data)
    assert check_activity_lines(html.replace("Planned.", "Proposed."), data)
    soup = BeautifulSoup(html, "html.parser")
    line = soup.select_one(".activity-line")
    line.insert_after(copy.copy(line))
    assert check_activity_lines(str(soup), data)
    # Positive HTML must fail when the same body's claim is mandate-only, expired or about another body.
    for change in ({"basis": "mandate"}, {"expired": True}, {"body_id": "other"}):
        changed = copy.deepcopy(data)
        changed["jurisdictions"][0]["claims"][0].update(change)
        assert check_activity_lines(html, changed)


def test_todays_data_and_all_rendered_cards_follow_the_rule():
    data = published.publish(json.loads((ROOT / "data/aisi.json").read_text()))
    assert {c: v["short"] for c, v in data["dataset"]["vocabularies"]["function"].items()} == SHORTS
    assert all("active_in" not in j["closest_body"] for j in data["jurisdictions"] if "closest_body" in j)
    institutes = {i["id"]: i for i in data["institutes"]}
    expected = {j["id"]: text for j in data["jurisdictions"]
                if (text := activity_expectation(j, institutes.get(j.get("institute_id")), data["dataset"]["vocabularies"]["function"])[1])}
    assert expected == {"KE": "Not yet established. Active in international cooperation.",
                        **dict.fromkeys(("DE", "MY", "IN"), "Established. No published work found yet.")}
    html = render_cards(data)
    assert check_activity_lines(html, data) == []
    assert check_page.check_citation_ends(BeautifulSoup(html, "html.parser")) == []


def test_status_table_alignment_excludes_the_separate_activity_evidence(alignment_case):  # noqa: F811
    soup, data = alignment_case
    assert check_page.check_source_alignment(str(soup), data) == []
    value = soup.select_one('.row[data-track="3"] .v')
    paragraph = soup.new_tag("p")
    for child in list(value.contents):
        paragraph.append(child.extract())
    value.append(paragraph)
    line = soup.new_tag("span", attrs={"class": "small qualifiers activity-line"})
    line.append("Not yet established. Active in model testing.")
    line.append(copy.copy(paragraph.select_one(".cites")))
    value.append(line)
    assert check_page.check_source_alignment(str(soup), data) == []
    paragraph.select_one(".cn")["data-source-id"] = "wrong"
    assert any("since citations differ" in e for e in check_page.check_source_alignment(str(soup), data))


def test_status_interval_checks_keep_their_own_evidence():
    data, html = interval_case(), interval_html()
    assert check_page.check_interval_output(html, data) == []
    soup = BeautifulSoup(html, "html.parser")
    row = soup.select_one('.row[data-track="3"]')
    row.append(BeautifulSoup('<span class="small qualifiers activity-line">Not yet established. Active in model testing.'
                            '<span class="cites"><a class="cn" data-source-id="activity-source">3</a></span></span>', "html.parser"))
    assert check_page.check_interval_output(str(soup), data) == []
    row.select_one(".cn")["data-source-id"] = "wrong"
    assert any("Status citations" in e for e in check_page.check_interval_output(str(soup), data))


def test_closest_stage_order_is_accepted_by_existing_content_check():
    data = fixture("closest with activity")
    data["jurisdictions"][0]["claims"][0]["text"] = "It tests models."
    soup = BeautifulSoup(render_cards(data), "html.parser")
    soup.article.name = "details"
    assert check_page.check_nonidentified_folds(str(soup), data) == []
    # The stage cannot drift back below the role while an activity line is shown.
    stage = soup.select_one(".det")
    stage.parent.append(stage.extract())
    assert any("role sentences" in e for e in check_page.check_nonidentified_folds(str(soup), data))
