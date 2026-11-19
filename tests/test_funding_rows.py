"""Exercise the funding row decisions in Node, without a build or browser."""

import copy
import json
import re
import subprocess

import pytest
from bs4 import BeautifulSoup

from conftest import ROOT
from scripts import check_page
from scripts.display import funding_label


def helper(name):
    page = (ROOT / "site/index.html").read_text()
    match = re.search(rf"    function {name}\([^\n]*\n.*?\n    }}", page, re.S)
    assert match, f"missing page helper: {name}"
    return match.group(0)


def run_node(script):
    subprocess.run(
        ["node", "-"], input="const assert = require('node:assert/strict');\n" + script,
        text=True, capture_output=True, check=True,
    )


# Record initial DOM construction only: no browser, layout, event dispatch or build.
DETAIL_DOM = r"""
function el(tag, cls, text) {
  return {tag, className:cls||'', dataset:{}, attrs:{}, children:text == null ? [] : [{text}],
    setAttribute(k,v){this.attrs[k]=v;}, getAttribute(k){return this.attrs[k];},
    appendChild(n){this.children.push(n);return n;},
    insertBefore(n,before){this.children.splice(this.children.indexOf(before),0,n);},
    get firstChild(){return this.children[0];}, get lastChild(){return this.children.at(-1);},
    get lastElementChild(){return this.children.filter(n=>n.tag).at(-1);},
    matches(s){return s.split(',').some(p=>p.trim().split('.').slice(1).every(c=>this.className.split(' ').includes(c)));}};
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
function separated(parts){const n=el('span');parts.forEach(p=>{const item=el('span','meta-item');item.appendChild(typeof p==='string'?{text:p}:p);n.appendChild(item);});return n;}
function confTerms(parts){return parts;} function confTag(){return null;}
function missing(){return el('span','missing','Not published');}
function term(text,tip,cls){const n=el('abbr','g '+cls,text);n.setAttribute('tabindex','0');n.setAttribute('aria-description',tip);return n;}
function sourcesBlock(){return el('div','srcs');} function foldClose(){}
const S={},m={},fundOrder=[],FUND_DETAIL='Funding and staff records',PER_PERSON_TIP='Population caveat';
const LABEL={established:'Established',planned:'Planned',proposed:'Proposed'};
"""


@pytest.fixture(scope="module")
def funding_details():
    record = {
        "id": "f-gb", "amount": 360_000_000, "currency": "GBP", "years": 4,
        "annual_amount": 90_000_000, "annual_basis_code": "period_mean", "kind": "multi_year",
        "coverage": "whole_institute", "commitment": "adopted",
        "citations": {"amount": ["amount"], "currency": ["amount"], "annual_amount": ["amount"]},
        "display": {"value": "~EUR 110m (GBP 90m)", "native_value": "GBP 360m",
                    "native_qualifiers": ["2026 to 2030", "adopted"],
                    "qualifiers": ["a year", "average over period", "2026 to 2030", "adopted"],
                    "budget_citations": ["amount", "ecb"], "per_person": "~EUR 1.5 per person"},
        "ratio_denominators": {"population": {"year": 2025}},
    }
    data = {
        "dataset": {"exchange_rates": [{"currency": "GBP", "eur_per_unit": 1.167, "source_ids": ["ecb"], "citations": {"eur_per_unit": ["ecb"]}}]},
        "jurisdictions": [{"id": jid, "name": name} for jid, name in (("GB", "United Kingdom"), ("ES", "Spain"), ("KE", "Kenya"), ("NO", "Norway"))],
        "institutes": [{"id": iid, "jurisdiction_id": jid, "name": name, "status": status, "funding": funds}
                       for iid, jid, name, status, funds in (
                           ("gb", "GB", "AI Security Institute", "established", [record]),
                           ("es", "ES", "AI Security Institute (planned)", "planned", []),
                           ("ke", "KE", "Kenya AI Safety Institute (proposed)", "proposed", []),
                           ("no", "NO", "KI Norge AI safety mandate (planned)", "planned", []),
                       )],
    }
    for institute in data["institutes"]:
        institute["display"] = {"funding_ids": [f["id"] for f in institute["funding"]],
                                "funding_records": {f["id"]: f for f in institute["funding"]},
                                "staff_ids": [], "staff_records": {}}
    page = (ROOT / "site/index.html").read_text()
    helpers = page[page.index("  function fieldCitations("):page.index("  function glueCitations(")]
    names = page[page.index("  var DISPLAY_NAME ="):page.index("  // One markerless route")]
    body = re.search(r"      function detailsBody\(\).*?\n      }", page, re.S).group()
    start = page.index('      var row = el("li", "fund-item"')
    row = page[start:page.index('      var identity = el("div", "fund-identity")', start)]
    start = page.index('      var det = el("details", "more quiet fund-detail")')
    head = page[start:page.index('      button.addEventListener("click"', start)]
    script = DETAIL_DOM + helpers + names + "\nconst data=" + json.dumps(data) + ";\n" + """
const J=Object.fromEntries(data.jurisdictions.map(j=>[j.id,j]));nameInstitutes(data.institutes,J);useRates(data.dataset.exchange_rates);
const fundBands={established:el('ul'),'not-established':el('ul')};
data.institutes.forEach(i=>{
  const j=J[i.jurisdiction_id],fu=i.display.funding_records['f-gb']||null,own=fu,fundingCites=cite;
  function staffSummary(){return el('div','fund-staff-summary');}
""" + body + row + head + """
});console.log('<div id="fund-rows">'+Object.values(fundBands).map(html).join('')+'</div>');
"""
    result = subprocess.run(["node", "-"], input=script, text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    return data, result.stdout


def test_initial_details_separate_annual_summary_from_the_record(funding_details):
    data, html = funding_details
    soup = BeautifulSoup(html, "html.parser")
    summary, = soup.select("[data-budget-summary]")
    record, = soup.select('[data-record="funding"]')
    assert summary["data-budget-summary"] == record["data-record-id"] == "f-gb"
    assert not summary.find_parent(attrs={"data-record-id": True})
    assert soup.select_one(".fund-detail-body > :first-child h4").get_text() == "Budget"
    assert summary.select_one(".budget-amount").get_text() == "~EUR 110m (GBP 90m)"
    assert record.select_one(".budget-amount").get_text() == "~EUR 420m (GBP 360m)"
    for qualifier in data["institutes"][0]["funding"][0]["display"]["qualifiers"]:
        assert qualifier in summary.get_text()
    assert "per resident (2025)" in summary.get_text()
    assert check_page.check_euro_amounts(html, data, {"GB": "f-gb"}) == []


@pytest.mark.parametrize("mutation, finding", [
    ("summary-amount", "Budget summary: amount shown"), ("record-amount", "f-gb: amount shown"),
    ("amount-citation", "Budget summary: euro figure"), ("rate-citation", "Budget summary: euro figure"),
    ("missing", "exactly one Budget summary"), ("duplicate", "exactly one Budget summary"),
    ("wrong-id", "exactly one Budget summary"), ("record-id", "separate from funding records"),
    ("nested", "separate from funding records"),
])
def test_saved_summary_checks_reject_wrong_values_identity_and_citations(funding_details, mutation, finding):
    data, html = funding_details
    soup = BeautifulSoup(html, "html.parser")
    summary = soup.select_one("[data-budget-summary]")
    record = soup.select_one('[data-record="funding"]')
    if mutation.endswith("-amount"):
        (summary if mutation == "summary-amount" else record).select_one(".budget-amount").string = "~EUR 1.0m (GBP 1.0m)"
    elif mutation.endswith("-citation"):
        # A valid citation elsewhere in the record must not rescue the summary.
        summary.select_one(f'[data-source-id="{"amount" if mutation == "amount-citation" else "ecb"}"]').decompose()
    elif mutation == "missing":
        summary.decompose()
    elif mutation == "duplicate":
        summary.insert_after(copy.copy(summary))
    elif mutation == "wrong-id":
        summary["data-budget-summary"] = "other"
    elif mutation == "record-id":
        summary["data-record-id"] = "f-gb"
    elif mutation == "nested":
        record.insert(0, summary.extract())
    assert any(finding in e for e in check_page.check_euro_amounts(str(soup), data, {"GB": "f-gb"}))


def test_initial_detail_headers_and_row_accessible_names_use_full_names(funding_details):
    _, html = funding_details
    soup = BeautifulSoup(html, "html.parser")
    for iid, name in {"gb": "AI Security Institute (United Kingdom)", "es": "AI Security Institute (Spain)",
                      "ke": "Kenya AI Safety Institute", "no": "KI Norge AI safety mandate"}.items():
        row = soup.select_one(f'[data-institute="{iid}"]')
        assert row.select_one(".fund-detail > summary .fund-head-name").get_text() == name
        assert name in row.select_one(".fund-row")["aria-label"]


@pytest.mark.parametrize("period,start,end,basis,want", [
    ("2026-27 to 2029-30", "2026-04-01", "2030-03-31", "period_mean", "2026-27 to 2029-30"),
    ("2024-25 to 2028-29", "2024-04-01", "2029-03-31", "period_mean", "2024-25 to 2028-29"),
    ("2026 to 2028", "2026-01-01", "2028-12-31", "period_mean", "2026 to 2028"),
    ("FY2026 enacted", "2025-10-01", "2026-09-30", "other", "FY2026 enacted"),
    ("2025-26 to 2028-29", "2026-07-01", "2027-06-30", "allocation_current_year", "2026-27 allocation"),
    ("a year", "2026-01-01", "2026-12-31", "other", ""),
])
def test_row_reuses_the_record_and_card_period_formatter(period, start, end, basis, want):
    record = {"period": period, "annual_period": {"start": start, "end": end}, "annual_basis_code": basis,
              "annual_amount": 1_000_000, "currency": "EUR", "commitment": "announced"}
    record["display"] = {"qualifiers": funding_label(record, annual=True)[1]}
    page = (ROOT / "site/index.html").read_text()
    annual_period = page[page.index("  function annualPeriod("):page.index("  function withPeriod(")]
    run_node(annual_period + helper("fundingPeriod") + f"\nassert.equal(fundingPeriod({json.dumps(record)}), {json.dumps(want)});")
    assert not want or want in record["display"]["qualifiers"]


def test_unscaled_budget_words_distinguish_proposals_one_offs_and_missing_rates():
    run_node("const RATES = {JPY: {}, NOK: {}};\n" + helper("fundingBudgetNote") + """
assert.equal(fundingBudgetNote({currency:'JPY', kind:'one_off', commitment:'adopted'}), 'One-off amount, in the records');
assert.equal(fundingBudgetNote({currency:'NOK', kind:'annual', commitment:'proposed'}), 'Proposal, in the records');
assert.equal(fundingBudgetNote({currency:'KES', kind:'one_off', commitment:'proposed'}), 'No euro rate, in the records');
assert.equal(fundingBudgetNote({currency:'EUR', annual_period:{start:'2020-01-01'}}), 'Another period, in the records');
assert.equal(fundingBudgetNote({currency:'EUR'}), 'Amount, in the records');
""")


def test_unscaled_staff_words_do_not_print_targets_ranges_or_technical_counts():
    run_node(helper("fundingStaffNote") + """
assert.equal(fundingStaffNote({total:0}, null, null, 'established'), 'Undated total, in the records');
assert.equal(fundingStaffNote(null, {technical:24}, null, 'established'), 'Technical count, in the records');
assert.equal(fundingStaffNote(null, null, {target:100}, 'planned'), 'Target, in the records');
assert.equal(fundingStaffNote(null, null, {description:'12 to 15 people'}, 'proposed'), 'Planned range, in the records');
assert.equal(fundingStaffNote(null, null, {description:'12–15 people'}, 'established'), 'Range, in the records');
assert.equal(fundingStaffNote(null, null, {description:'Seconded staff'}, 'established'), 'Staff record, in the records');
""")


def test_staff_bounds_include_approximation_from_rounding():
    run_node(helper("staffBound") + """
assert.equal(staffBound({total_qualifier:'at_least', display:{total:'140+'}}), 'at least');
assert.equal(staffBound({total_qualifier:'exact', display:{total:'~240'}}), 'about');
assert.equal(staffBound({total_qualifier:'about', display:{total:'~30'}}), 'about');
assert.equal(staffBound({total_qualifier:'exact', display:{total:'0'}}), null);
""")


@pytest.mark.parametrize("width, kept", [(200, 3), (140, 2), (60, 1), (10, 0)])
def test_qualifier_fitting_drops_whole_trailing_parts_and_restores_them(width, kept):
    run_node(helper("fitFundingQualifiers") + f"""
const parts = [{{width:50, hidden:false}}, {{width:80, hidden:false}}, {{width:60, hidden:false}}];
parts.forEach(p=>p.hasAttribute=()=>false);
const getComputedStyle=()=>({{whiteSpace:'nowrap',lineHeight:'20px'}});
const line = {{children:parts, hidden:false, clientWidth:{width}, clientHeight:20, getClientRects:()=>[{{}}],
  get scrollWidth() {{ return Math.max(this.clientWidth, parts.filter(p=>!p.hidden).reduce((n,p)=>n+p.width,0)); }} }};
const $ = () => ({{querySelectorAll:()=>[line]}});
fitFundingQualifiers();
assert.deepEqual(parts.map(p=>p.hidden), [0,1,2].map(n=>n>={kept}));
assert.equal(line.hidden, {kept}===0);
line.clientWidth = 300;
fitFundingQualifiers();
assert.deepEqual(parts.map(p=>p.hidden), [false,false,false]);
assert.equal(line.hidden, false);
""")


@pytest.mark.parametrize("status", ["adopted", "announced"])
@pytest.mark.parametrize("wrap,width,hidden", [(False, 400, False), (False, 250, True), (False, 100, True),
                                               (True, 250, False), (True, 200, True), (True, 100, True)])
def test_budget_drops_only_period_and_allows_two_phone_lines(status, wrap, width, hidden):
    run_node(helper("fundingQualifier") + helper("fitFundingQualifiers") + """
function el(tag, cls, text) {
  return {className:cls,textContent:text,dataset:{},hidden:false,children:[],
    appendChild(n){this.children.push(n);},hasAttribute(name){return name==='data-required' && this.dataset.required==='true';}};
}
""" + f"""
const line=fundingQualifier(['a year','2026-27 to 2029-30',{json.dumps(status)}], 'budget-qualifiers', {json.dumps(status)});
line.children.forEach((p,i)=>p.width=[50,160,100][i]);
const wraps={json.dumps(wrap)};
const getComputedStyle=()=>({{whiteSpace:wraps?'normal':'nowrap',lineHeight:'20px'}});
line.clientWidth={width};line.getClientRects=()=>[{{}}];
Object.defineProperty(line,'scrollWidth',{{get(){{const widths=this.children.filter(p=>!p.hidden).map(p=>p.width);return Math.max(this.clientWidth,wraps?Math.max(...widths):widths.reduce((a,b)=>a+b,0));}}}});
Object.defineProperty(line,'clientHeight',{{get(){{let lines=1,used=0;for(const p of this.children.filter(p=>!p.hidden)){{if(wraps && used && used+p.width>this.clientWidth){{lines++;used=0;}}used+=p.width;}}return lines*20;}}}});
const $=()=>({{querySelectorAll:()=>[line]}});
assert.deepEqual(line.children.map(p=>p.hasAttribute('data-required')),[true,false,true]);
fitFundingQualifiers();
assert.deepEqual(line.children.map(p=>p.hidden),[false,{json.dumps(hidden)},false]);
assert.equal(line.hidden,false);
assert.equal(line.children.filter(p=>!p.hidden).map(p=>p.textContent).join(''),
  {json.dumps('a year' + ('' if hidden else ' · 2026-27 to 2029-30') + ' · ' + status)});
if(wraps) assert.ok(line.clientHeight<=40);
line.clientWidth=500;fitFundingQualifiers();
assert.deepEqual(line.children.map(p=>p.hidden),[false,false,false]);
""")
