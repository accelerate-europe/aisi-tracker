"""Compact chart labels and shared disclosure behaviour, without a browser or build."""

import json
import subprocess
from pathlib import Path

import pytest
from bs4 import BeautifulSoup

from scripts import check_page
from scripts.display import funding_label


PAGE = Path(__file__).resolve().parents[1] / "site/index.html"


def run_node(script):
    result = subprocess.run(["node", "-e", script], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_funding_headers_render_full_names_before_any_disclosure_opens():
    source = PAGE.read_text()
    names = source[source.index("  var DISPLAY_NAME ="):source.index("  // One markerless route")]
    header = source[source.index('      var det = el("details", "more quiet fund-detail"'):source.index('      var close = closeButton(function () { closeFund(true); })')]
    authored = json.loads((PAGE.parents[1] / "data/aisi.json").read_text())
    data = {
        "institutes": [{key: i[key] for key in ("id", "name", "jurisdiction_id")} for i in authored["institutes"]],
        "jurisdictions": [{key: j[key] for key in ("id", "name")} for j in authored["jurisdictions"]],
    }
    script = """
const assert=require('node:assert/strict');
function el(tag,className,text){return {tag,className,text,dataset:{},children:[],appendChild(n){this.children.push(n);}};}
const FUND_DETAIL='Funding and staff records';
""" + names + "\nconst data=" + json.dumps({key: data[key] for key in ("institutes", "jurisdictions")}) + ";\n"
    script += "const expected=" + json.dumps(check_page.expected_display_names(data)) + ";\n"
    script += "const J=Object.fromEntries(data.jurisdictions.map(j=>[j.id,j])); nameInstitutes(data.institutes,J);\n"
    script += "for (const i of data.institutes) { const j=J[i.jurisdiction_id];\n" + header + """
assert.equal(identity.text,expected[i.id]); assert.equal(identity.title,expected[i.id]);
assert.ok(summary.children.includes(identity)); assert.ok(det.children.includes(summary));
}
"""
    run_node(script)


def test_annual_summary_renders_source_identity_and_amount_and_rate_citations():
    source = PAGE.read_text()
    helpers = source[source.index("  function fieldCitations("):source.index("  // Citations follow the last word;")]
    start = source.index('        var body = el("div", "fund-detail-body");')
    summary = source[start:source.index("        body.appendChild(staffSummary());", start)]
    run_node("""
const assert=require('node:assert/strict');
function el(tag,className,text){return {tag,className:className||'',text:text||'',dataset:{},children:[],
  appendChild(n){this.children.push(n);return n;},get lastElementChild(){return this.children.at(-1);}};}
const document={createTextNode:text=>el('text','',text)};
const confTerms=parts=>parts, separated=parts=>el('span','',parts.join(' · '));
const citeTarget=box=>box.lastElementChild, fundingCites=ids=>el('span','cites',ids.join(','));
const fu={id:'multi-year',amount:120000000,currency:'USD',
  citations:{amount:['amount-source'],currency:['amount-source']},
  display:{value:'~EUR 27m (USD 30m)',qualifiers:['a year','average over period'],budget_citations:['amount-source','rate-source']}};
""" + helpers + """
useRates([{currency:'USD',citations:{eur_per_unit:['rate-source']}}]);
""" + summary + """
const saved=body.children[0];
assert.equal(saved.className,'fund-budget-summary');
assert.deepEqual(saved.dataset,{sourceFundingId:'multi-year'});
assert.equal(budgetFigure.children[0].text,'~EUR 27m (USD 30m)');
assert.equal(units.children.at(-1).text,'amount-source,rate-source');
""")


@pytest.mark.parametrize("kind", ["budget", "staff"])
@pytest.mark.parametrize("tail", ["", " trailing words", "<span>trailing words</span>"])
def test_citation_ends_at_the_figure_but_rejects_text_inside_it(kind, tail):
    html = f'''<div class="fund-{kind}"><div class="fund-value"><span class="fund-figure">
      <strong>100</strong><span class="cites"><a class="cn">1</a></span>{tail}</span>
      <span class="fund-qualifier">2026 · announced</span></div></div>'''
    errors = check_page.check_citation_ends(BeautifulSoup(html, "html.parser"))
    assert errors == (["citation group is not at the end of its value"] if tail else [])


@pytest.mark.parametrize("labels,valid", [
    ([{"kind": "budget", "text": "Budget 0 to EUR 100m a year adopted announced"}, {"kind": "staff", "text": "Staff 0 to 250 people"}], True),
    ([{"kind": "staff", "text": "Staff 0 to 250 people"}, {"kind": "budget", "text": "Budget 0 to EUR 100m a year"}], True),
    ([], False),
    ([{"kind": "budget", "text": "Budget 0 to EUR 100m a year"}], False),
    ([{"kind": "budget", "text": "Budget 0 to EUR 100m a year"}] * 2, False),
    ([{"kind": "budget", "text": "Budget 10 to EUR 100m a year"}, {"kind": "staff", "text": "Staff 0 to 250 people"}], False),
    ([{"kind": "budget", "text": "Budget 0 to EUR 100m a year"}, {"kind": "staff", "text": ""}], False),
    ([{"kind": "budget", "text": "Budget 0 to EUR 100m a year"}, {"kind": "staff", "text": "Staff 0 to 250"}], False),
])
def test_both_funding_heads_keep_their_own_units_and_zero(labels, valid):
    assert bool(check_page.funding_head_label_errors(labels)) is not valid


def test_inline_hit_check_keeps_normal_lines_and_rejects_horizontal_overlap():
    class Page:
        def evaluate(self, script):
            run_node("""
const assert=require('node:assert/strict');
const rect=(left,top,width,height)=>({left,top,right:left+width,bottom:top+height,width,height});
const controls=[0,1,2].map(i=>({
  id:'',tagName:'BUTTON',textContent:'Country '+i,top:i*20,hit:44,
  checkVisibility:()=>true,getClientRects(){return [this.getBoundingClientRect()];},
  getBoundingClientRect(){return rect(0,this.top,100,20);},
  getAttribute:()=> 'fund-inline', matches:s=>s==='.fund-inline',closest:()=>null
}));
const document={querySelectorAll:()=>controls}, DOMMatrix=class {e=0;f=0;};
const getComputedStyle=(n,pseudo)=>pseudo ? {content:'""',position:'absolute',transform:'none',left:'0',
  top:String((20-n.hit)/2),width:'100',height:String(n.hit),minHeight:'0'} : {display:'block'};
const check=SCRIPT;
assert.deepEqual(check(),[]); // hit layers extend between ordinary 20px text lines
controls[1].hit=28;assert.ok(check().some(e=>e.includes('hit height')));
controls[1].hit=44;controls[1].top=0;assert.ok(check().some(e=>e.includes('hit areas overlap')));
controls[1].getBoundingClientRect=()=>rect(105,0,100,20);
assert.deepEqual(check(),[]); // neighbours on the same line stay disjoint
""".replace("SCRIPT", script))
    check_page.check_hit_targets(Page())


def test_missing_list_commas_stay_with_the_previous_name_and_are_hidden_from_speech():
    source = PAGE.read_text()
    listing = source[source.index('      var other = el("div", "fund-other")'):source.index('      chart.appendChild(other);')]
    run_node("""
const assert=require('node:assert/strict');
function el(tag,cls,text){return {tag,className:cls,children:[],attrs:{},text:text||'',
  appendChild(n){this.children.push(n);},setAttribute(k,v){this.attrs[k]=v;},
  get lastElementChild(){return this.children.filter(n=>n.tag!=='text').at(-1);},
  querySelector(cls){return this.children.find(n=>'.'+n.className===cls);}};}
const document={createTextNode:t=>el('text',null,t)},kind='budget',select=()=>false,otherFundWords=()=>null;
const fundItems=[{name:'France'},{name:'Germany'},{name:'Singapore'}];
const fundButton=i=>{const button=el('button');button.setAttribute('aria-label',i.name+', Established');return button;};
const fundName=(i,tag,cls)=>el(tag,cls,i.name);
""" + listing + """
const buttons=missingNames.children.filter(n=>n.tag==='button');
assert.equal(buttons.length,3);
for(const [index,button] of buttons.entries()) {
  const name=button.querySelector('.fund-inline-name');
  assert.equal(name.text,fundItems[index].name);
  assert.equal(name.children.length,index<2?1:0);
  if(index<2){const comma=name.children[0];assert.equal(comma.text,',');assert.equal(comma.attrs['aria-hidden'],'true');}
  assert.ok(!button.attrs['aria-label'].endsWith(','));
}
assert.ok(!missingNames.children.some(n=>n.tag==='text'&&n.text.includes(',')));
""")


@pytest.mark.parametrize("period,basis,expected", [
    ("FY2026 enacted", "annual", "FY2026 enacted"),
    ("2026 total", "annual", "2026 total"),
    ("2026-27 to 2029-30", "period_mean", "2026-27 to 2029-30"),
    ("2024-25 to 2028-29", "period_mean", "2024-25 to 2028-29"),
    ("2025/26 Supplementary Estimate", "annual", "2025/26 Supplementary Estimate"),
    ("2025-26 to 2028-29", "allocation_current_year", "2026-27 allocation"),
    ("2025/26 to 2028/29", "allocation_current_year", "2026/27 allocation"),
    ("a year", "annual", ""),
    ("", "annual", ""),
])
def test_chart_period_uses_the_cards_and_records_formatter(period, basis, expected):
    source = PAGE.read_text()
    helper = source[source.index("  function annualPeriod("):source.index("  function withPeriod(")]
    helper += source[source.index("    function fundPeriod("):source.index("    function fundQualifier(")]
    record = {"period": period, "annual_basis_code": basis, "commitment": "adopted", "currency": "EUR", "annual_amount": 100,
              "annual_period": {"start": "2026-07-01", "end": "2027-06-30"}}
    record["display"] = {"qualifiers": funding_label(record, annual=True)[1]}
    run_node("const assert = require('node:assert/strict');\n" + helper
             + f"\nassert.equal(fundPeriod({json.dumps(record)}), {json.dumps(expected)});")


@pytest.mark.parametrize("commitment", ["adopted", "announced"])
def test_budget_qualifiers_drop_period_before_commitment_and_wrap_on_phones(commitment):
    source = PAGE.read_text()
    helper = source[source.index("    function fitFundQualifiers("):source.index("    var fundItems =")]
    run_node("""
const assert = require('node:assert/strict');
const parts=['a year','2026-27 to 2029-30',COMMITMENT];
let wraps=false;
const line={dataset:{parts:JSON.stringify(parts)},textContent:'',clientWidth:200,classList:{contains:()=>true},
  get scrollWidth(){return wraps?Math.min(this.clientWidth,this.textContent.length):this.textContent.length;},
  get scrollHeight(){return (wraps?Math.ceil(this.textContent.length/this.clientWidth):1)*20;}};
const getComputedStyle=()=>({whiteSpace:wraps?'normal':'nowrap',lineHeight:'20px'});
function $(){return {querySelectorAll(){return [line];}};}
""".replace("COMMITMENT", json.dumps(commitment)) + helper + """
fitFundQualifiers(); assert.equal(line.textContent,parts.join(' · '));
line.clientWidth=27; fitFundQualifiers(); assert.equal(line.textContent,'a year · '+parts.at(-1));
line.clientWidth=8; fitFundQualifiers(); assert.equal(line.textContent,'a year · '+parts.at(-1));
wraps=true;line.clientWidth=27;fitFundQualifiers();assert.equal(line.textContent,parts.join(' · '));
line.clientWidth=10;fitFundQualifiers();assert.equal(line.textContent,'a year · '+parts.at(-1));
line.clientWidth=200; fitFundQualifiers(); assert.equal(line.textContent,parts.join(' · '));
assert.deepEqual(JSON.parse(line.dataset.parts),parts);
line.dataset.parts=JSON.stringify(['a year',parts.at(-1)]);
line.clientWidth=10;fitFundQualifiers();assert.equal(line.textContent,'a year · '+parts.at(-1));
line.classList.contains=()=>false;wraps=false;
line.dataset.parts=JSON.stringify(['at least','as at Oct 2026']);
line.clientWidth=15;fitFundQualifiers();assert.equal(line.textContent,'at least');
""")


def test_commitment_geometry_rejects_missing_words_and_clipped_text():
    script = check_page.FUNDING_LAYOUT_JS
    helper = script[script.index("  const commitmentVisible="):script.index("  const heads=")]
    run_node("""
const assert=require('node:assert/strict');
const Node={TEXT_NODE:3},list={},rect=n=>n.bounds,visible=n=>n.shown;
const parent={parentElement:list,style:{overflowX:'visible',overflowY:'visible'},bounds:{left:0,right:200,top:0,bottom:40}};
const q={parentElement:parent,shown:true,dataset:{parts:'["a year","2026-27 to 2029-30","announced"]'},
  textContent:'a year · announced',firstChild:{nodeType:3},bounds:{left:0,right:200,top:0,bottom:40},
  style:{opacity:'1',overflowX:'visible',overflowY:'visible'}};
let wordBox={left:60,right:120,top:0,bottom:16};
const getComputedStyle=n=>n.style;
const document={createRange:()=>({setStart(n,at){assert.equal(q.textContent.slice(at),'announced');},
  setEnd(n,at){assert.equal(at,q.textContent.length);},getClientRects:()=>[{...wordBox,width:wordBox.right-wordBox.left,height:wordBox.bottom-wordBox.top}]})};
""" + helper + """
assert.equal(commitmentVisible(q),true);
q.textContent='a year · 2026-27 to 2029-30';assert.equal(commitmentVisible(q),false);
q.textContent='a year · announced';q.shown=false;assert.equal(commitmentVisible(q),false);q.shown=true;
wordBox={left:0,right:60,top:20,bottom:36};assert.equal(commitmentVisible(q),true); // second line
q.bounds.bottom=20;assert.equal(commitmentVisible(q),false);q.bounds.bottom=40;
wordBox.right=220;assert.equal(commitmentVisible(q),false);wordBox.right=60;
parent.style.overflowY='hidden';parent.bounds.bottom=20;assert.equal(commitmentVisible(q),false);
""")


def test_same_records_move_between_charts_and_close_returns_to_the_exact_control():
    source = PAGE.read_text()
    helper = source[source.index("    function closeFund("):source.index("    fundItems.forEach(function (i) {")]
    run_node("""
const assert=require('node:assert/strict');
function parent(){return {children:[],appendChild(n){
  if(n.parent) n.parent.children.splice(n.parent.children.indexOf(n),1);
  n.parent=this;this.children.push(n);
}};}
function el(){return {dataset:{},events:{},attrs:{},
  setAttribute(k,v){this.attrs[k]=v;},addEventListener(k,f){this.events[k]=f;},focus(){focused=this;}};}
const institute={id:'first',jurisdiction_id:'AA',status:'established'}, second={id:'second',jurisdiction_id:'BB',status:'planned'};
const J={AA:{name:'First'},BB:{name:'Second'}}, LABEL={established:'Established',planned:'Planned'}, FUND_DETAIL='Funding and staff records';
const DISPLAY_NAME={first:'First Institute',second:'Second Institute'};
const first={id:'fund-detail-first',open:false}, next={id:'fund-detail-second',open:false};
const fundRecords={first:{detail:first},second:{detail:next}}, fallback=parent();
let fundOpen=null,focused=null;
const returnToOpener=button=>{focused=button;};
fallback.appendChild(first);fallback.appendChild(next);
""" + helper + """
const budgetRow=parent(), staffRow=parent(), missingLine=parent();
const budget=fundButton(institute,'fund-row',budgetRow), staff=fundButton(institute,'fund-row',staffRow);
const missing=fundButton(second,'fund-inline',missingLine);
assert.equal(budget.title,'First Institute');
assert.ok(budget.attrs['aria-label'].includes('First Institute'));
assert.equal(missing.title,'Second Institute');
budget.events.click();assert.equal(first.parent,budgetRow);assert.equal(first.open,true);
assert.equal(budget.attrs['aria-expanded'],'true');
staff.events.click();assert.equal(first.parent,staffRow);assert.equal(first.open,true);
assert.equal(budget.attrs['aria-expanded'],'false');assert.equal(staff.attrs['aria-expanded'],'true');
assert.equal(budget.attrs['aria-controls'],staff.attrs['aria-controls']);
missing.events.click();assert.equal(first.open,false);assert.equal(first.parent,fallback);
assert.equal(staff.attrs['aria-expanded'],'false');assert.equal(next.parent,missingLine);
assert.equal(next.open,true);assert.equal(fundOpen.button,missing);
closeFund(true);assert.equal(next.open,false);assert.equal(next.parent,fallback);
assert.equal(focused,missing);assert.equal(fundOpen,null);
staff.events.click();staff.events.click();assert.equal(first.open,false);
assert.equal(focused,staff);assert.equal(fundOpen,null);
assert.deepEqual(new Set(fallback.children),new Set([first,next]));
""")
