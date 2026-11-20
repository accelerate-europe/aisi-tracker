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
    header = source[source.index('      var det = el("details", "more quiet fund-detail"'):source.index('      var close = closeButton(function () { closeFund(true); }, identity)')]
    authored = json.loads((PAGE.parents[1] / "data/aisi.json").read_text())
    data = {
        "institutes": [{key: i[key] for key in ("id", "name", "jurisdiction_id")} for i in authored["institutes"]],
        "jurisdictions": [{key: j[key] for key in ("id", "name")} for j in authored["jurisdictions"]],
    }
    script = """
const assert=require('node:assert/strict');
function el(tag,className,text){return {tag,className,text,dataset:{},children:[],
  get textContent(){return (this.text||'')+this.children.map(n=>n.textContent).join('');},
  setAttribute(k,v){this[k]=v;},appendChild(n){this.children.push(n);}};}
const FUND_DETAIL='Funding and staff records';
""" + names + "\nconst data=" + json.dumps({key: data[key] for key in ("institutes", "jurisdictions")}) + ";\n"
    script += "const expected=" + json.dumps(check_page.expected_base_names(data)) + ";\n"
    script += "const J=Object.fromEntries(data.jurisdictions.map(j=>[j.id,j])); nameInstitutes(data.institutes,J);\n"
    script += "for (const i of data.institutes) { const j=J[i.jurisdiction_id];\n" + header + """
assert.equal(identity.textContent,j.name+', '+expected[i.id]); assert.equal(identity.title,identity.textContent);
assert.equal(det['aria-labelledby'],identity.id);
assert.ok(summary.children.includes(identity)); assert.ok(det.children.includes(summary));
}
"""
    run_node(script)


def test_saved_names_conversion_and_template_render_restore_disclosures():
    source = PAGE.read_text()
    live = source[source.index('    function fundNamesLine('):source.index('    function otherFundWords(')]
    saved = source[source.index('      // Names remain plain text'):source.index('      // Attributes that only the running script uses')]
    run_node("""
const assert=require('node:assert/strict');
function el(tag,className='',text=''){return {tag,className,text,children:[],attrs:{},events:{},
  get firstChild(){return this.children[0];},
  get textContent(){return this.text+this.children.map(n=>n.textContent).join('');},
  set textContent(s){this.text=s;},
  appendChild(n){if(n.parent)n.parent.children.splice(n.parent.children.indexOf(n),1);n.parent=this;this.children.push(n);return n;},
  replaceWith(n){this.parent.children.splice(this.parent.children.indexOf(this),1,n);n.parent=this.parent;},
  setAttribute(k,v){this.attrs[k]=v;},addEventListener(k,v){this.events[k]=v;}};}
const J={GB:{name:'United Kingdom'}}, entries=[{institute:{id:'uk',jurisdiction_id:'GB'}}];
const fundButton=(i,cls)=>el('button',cls), fundOpen=null, returnToOpener=()=>{};
""" + live + """
const lines=['missing','other'].map(category=>fundNamesLine('staff',category,entries));
const document={querySelectorAll:s=>{assert.equal(s,'button.fund-names-toggle');return lines.map(line=>line.children[0]);}};
const labels=lines.map(line=>line.children[0].textContent);
""" + saved + """
for(const [i,line] of lines.entries()) {
  const text=line.children[0];assert.equal(text.tag,'span');assert.equal(text.className,'fund-names-toggle');
  assert.deepEqual(text.attrs,{});assert.deepEqual(text.events,{});assert.equal(text.textContent,labels[i]);
  assert.equal(line.children[1].hidden,true);
}
// Hydration calls the same renderer on the pristine template, restoring the full disclosure.
for(const category of ['missing','other']) {
  const line=fundNamesLine('staff',category,entries), button=line.children[0], list=line.children[1];
  assert.equal(button.tag,'button');assert.equal(button.type,'button');
  assert.equal(button.attrs['aria-controls'],list.id);assert.equal(button.attrs['aria-expanded'],'false');
  button.events.click();assert.equal(list.hidden,false);assert.equal(button.attrs['aria-expanded'],'true');
  button.events.click();assert.equal(list.hidden,true);assert.equal(button.attrs['aria-expanded'],'false');
}
""")


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
      <strong><abbr aria-description="A year, 2026, announced.">100</abbr></strong><span class="cites"><a class="cn">1</a></span>{tail}</span></div></div>'''
    errors = check_page.check_citation_ends(BeautifulSoup(html, "html.parser"))
    assert errors == (["citation group is not at the end of its value"] if tail else [])


@pytest.mark.parametrize("kind", ["budget", "staff"])
def test_chart_citations_bind_to_the_value_without_splitting_its_tooltip(kind):
    source = PAGE.read_text()
    start = source.index('        var value = el("div", "fund-value"), figure = el("strong");')
    figure = source[start:source.index("        measure.appendChild(value);", start)]
    glue = source[source.index("  function glueCitations("):source.index("  // Row wrappers")]
    run_node("""
const assert=require('node:assert/strict');
function el(tag,className='',text=''){return {tag,className,text,childNodes:[],
  appendChild(n){n.parentElement=this;this.childNodes.push(n);return n;},
  closest(selectors){return selectors.split(',').some(s=>this.className.split(' ').includes(s.trim().slice(1)))
    ? this : this.parentElement?.closest(selectors);}};}
const i={id:'sample'}, record={}, tooltip=el('abbr','g val','140+');
tooltip.tabIndex=0;tooltip.description='As at 12 Oct 2026, at least.';
const markers=el('span','cites');markers.appendChild(el('a','cn','1'));
const budgetCitations=()=>['amount','rate'], staffCitations=()=>['total','date'];
const fundFigure=()=>tooltip;
""" + f"const kind={json.dumps(kind)};\n" + """
const fundRecords={sample:{cite(ids){assert.deepEqual(ids,kind==='budget'?['amount','rate']:['total','date']);return markers;}}};
""" + figure + glue + """
assert.equal(markers.closest('.cite-glue'),citedFigure);
assert.deepEqual(citedFigure.childNodes,[figure,markers]);
const root={querySelectorAll:()=>[markers]};
// Already atomic: gluing twice must not extract, clone or split the focusable value.
const document={createTreeWalker(){assert.fail('the chart value is already glued');}};
glueCitations(root);glueCitations(root);
assert.deepEqual(value.childNodes,[citedFigure]);
assert.deepEqual(figure.childNodes,[tooltip]);
assert.deepEqual(citedFigure.childNodes,[figure,markers]);
assert.equal(tooltip.text,'140+');assert.equal(tooltip.tabIndex,0);
assert.equal(tooltip.description,'As at 12 Oct 2026, at least.');
""")


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


def test_funding_hit_check_rejects_short_or_overlapping_disclosures_and_rows():
    class Page:
        def evaluate(self, script, scope):
            run_node("""
const assert=require('node:assert/strict');
const rect=(left,top,width,height)=>({left,top,right:left+width,bottom:top+height,width,height});
const controls=['fund-names-toggle','fund-name-row','fund-row'].map((cls,i)=>({
  id:'',tagName:'BUTTON',textContent:'Country '+i,top:i*44,hit:44,
  checkVisibility:()=>true,getClientRects(){return [this.getBoundingClientRect()];},
  getBoundingClientRect(){return rect(0,this.top,100,this.hit);},
  getAttribute:()=> cls, matches:s=>s.split(',').some(part=>part.trim()==='.'+cls),closest:()=>null
}));
const outside={...controls[0],tagName:'circle',hit:27,matches:()=>false};
const document={querySelector:scope=>({querySelectorAll:()=>scope==='#fund-rows'?controls:[...controls,outside]})}, DOMMatrix=class {e=0;f=0;};
const getComputedStyle=(n,pseudo)=>pseudo ? {content:'none'} : {display:'block'};
const measure=SCRIPT,check=()=>measure('#fund-rows');
assert.deepEqual(check(),[]);
assert.ok(measure('body').some(e=>e.includes('circle#')));
controls[1].hit=28;assert.ok(check().some(e=>e.includes('hit height')));
controls[1].hit=44;controls[1].top=20;assert.ok(check().some(e=>e.includes('hit areas overlap')));
controls[1].top=0;assert.ok(check().some(e=>e.includes('hit areas overlap')));
controls[1].getBoundingClientRect=()=>rect(105,0,100,44);
assert.deepEqual(check(),[]);
""".replace("SCRIPT", script))
    check_page.check_hit_targets(Page(), '#fund-rows')


def test_names_disclosures_keep_flowing_text_independent_lists_and_record_behaviour():
    source = PAGE.read_text()
    helpers = source[source.index("    function closeFund("):source.index("    fundItems.forEach(function (i) {")]
    listing = source[source.index("    function fundNamesLine("):source.index("    function otherFundWords(")]
    run_node("""
const assert=require('node:assert/strict');
let focused=null,fundOpen=null;
const events=[];
function el(tag,cls,text=''){return {tag,className:cls,children:[],attrs:{},dataset:{},text,
  style:{setProperty(){}},
  get textContent(){return this.text+this.children.map(n=>n.textContent).join('');},
  set textContent(value){this.text=value;this.children=[];},
  appendChild(n){if(n.parent)n.parent.children.splice(n.parent.children.indexOf(n),1);n.parent=this;this.children.push(n);return n;},
  setAttribute(k,v){this.attrs[k]=v;},getAttribute(k){return this.attrs[k];},
  addEventListener(type,fn){this[type]=fn;},focus(){focused=this;},
  contains(n){return this===n||this.children.some(child=>child.contains(n));},
  querySelector(){return {getBoundingClientRect:()=>({height:44})};}};}
const fallback=el('div');
const J={FR:{name:'France'},DE:{name:'Germany'},JP:{name:'Japan'}};
const DISPLAY_NAME={fr:'French Institute',de:'German Institute',jp:'Japanese Institute'};
const FUND_DETAIL='Funding and staff records';
const institutes=Object.keys(DISPLAY_NAME).map(id=>({id,jurisdiction_id:id.toUpperCase(),short_name:id.toUpperCase()}));
const fundRecords=Object.fromEntries(institutes.map(i=>{const detail=el('details');detail.id='records-'+i.id;fallback.appendChild(detail);return [i.id,{detail,own:{id:'funding-'+i.id}}];}));
const returnToOpener=button=>{events.push('focus');focused=button;};
""" + helpers + listing + """
const missing=fundNamesLine('budget','missing',institutes.slice(0,2).map(institute=>({institute,words:null})));
const other=fundNamesLine('budget','other',[{institute:institutes[2],words:'one-off amount'}]);
const staff=fundNamesLine('staff','missing',[{institute:institutes[2],words:null}]);
const [missingToggle,missingList]=missing.children,[otherToggle,otherList]=other.children,[staffToggle,staffList]=staff.children;
assert.equal(missingToggle.children[0].textContent,'Not published: France, Germany');
assert.equal(otherToggle.children[0].textContent,'Not compared: Japan (one-off amount)');
assert.equal(missingToggle.children[0].tag,'span');assert.equal(missingToggle.children[0].children.length,0);
for(const [toggle,list] of [missing.children,other.children,staff.children]) {
  assert.equal(toggle.tag,'button');assert.equal(toggle.type,'button');assert.equal(toggle.children.length,1);
  assert.equal(toggle.attrs['aria-expanded'],'false');assert.equal(toggle.attrs['aria-controls'],list.id);
  assert.equal(list.tag,'ul');assert.equal(list.attrs.role,'list');assert.equal(list.hidden,true);
  assert.ok(list.children.every(row=>row.tag==='li'&&row.className==='fund-name-item'&&row.children.length===1));
}
assert.equal(new Set([missingList.id,otherList.id,staffList.id]).size,3);
const france=missingList.children[0].children[0],japan=otherList.children[0].children[0],staffJapan=staffList.children[0].children[0];
assert.deepEqual(france.dataset,{institute:'fr',j:'FR'});
assert.deepEqual(japan.dataset,{institute:'jp',j:'JP',fundingId:'funding-jp'});
assert.equal(japan.textContent,'Japan (one-off amount)');assert.equal(japan.title,'Japanese Institute (JP)');
assert.equal(japan.attrs['aria-label'],'Japan, Japanese Institute (JP): Funding and staff records');
assert.equal(japan.attrs['aria-controls'],'records-jp');
missingToggle.click();otherToggle.click();
assert.equal(missingList.hidden,false);assert.equal(otherList.hidden,false);
france.click();assert.equal(fundOpen.button,france);assert.equal(fundRecords.fr.detail.parent,missingList.children[0]);
japan.click();assert.equal(fundOpen.button,japan);assert.equal(fundRecords.fr.detail.parent,fallback);
staffToggle.click();staffJapan.click();assert.equal(fundOpen.button,staffJapan);assert.equal(fundRecords.jp.detail.parent,staffList.children[0]);
otherToggle.click();assert.equal(fundOpen.button,staffJapan);assert.equal(otherList.hidden,true);assert.equal(missingList.hidden,false);
const originalClose=closeFund;
closeFund=function(focus){assert.equal(staffList.hidden,false);events.push('close');return originalClose(focus);};
events.length=0;staffToggle.click();
assert.deepEqual(events,['close','focus']);assert.equal(fundOpen,null);assert.equal(focused,staffToggle);
assert.equal(staffList.hidden,true);assert.equal(staffToggle.attrs['aria-expanded'],'false');
assert.equal(staffJapan.attrs['aria-expanded'],'false');assert.equal(fundRecords.jp.detail.parent,fallback);
assert.equal(missingList.hidden,false);
""")


@pytest.mark.parametrize("tops,valid", [([10], True), ([10, 32.4, 54.8], True), ([10, 33], False), ([10, 54], False)])
def test_wrapped_summary_line_spacing_rejects_reserved_hit_gaps(tops, valid):
    errors = check_page.funding_summary_spacing_errors([{"key": "budget missing", "fontSize": 14, "lineTops": tops}])
    assert (errors == []) is valid


def test_summary_spacing_measures_text_ranges_and_deduplicates_wrapped_line_boxes():
    run_node("""
const assert=require('node:assert/strict');
const box=(top,width=100,height=16)=>({top,bottom:top+height,width,height});
const texts=[{boxes:[box(54.8),box(10),box(999,0)]},{boxes:[box(32.4),box(10.2),box(999,100,0)]}];
const names={},seen=[],NodeFilter={SHOW_TEXT:4};
const button={querySelector(selector){assert.equal(selector,'.fund-missing-names, .fund-other-names');return names;},
  closest:()=>({dataset:{kind:'budget'}}),
  getAttribute:()=> 'fund-budget-missing-list',getBoundingClientRect(){assert.fail('measure the text, not the button height');}};
const getComputedStyle=node=>{assert.equal(node,names);return {fontSize:'14px'};};
const document={
  querySelectorAll(selector){assert.equal(selector,'.fund-names-toggle[aria-expanded="false"]');return [button];},
  createTreeWalker(node,kind){assert.equal(node,names);assert.equal(kind,NodeFilter.SHOW_TEXT);let index=-1;
    return {get currentNode(){return texts[index];},nextNode(){return ++index<texts.length;}};},
  createRange(){let selected;return {selectNodeContents(node){selected=node;seen.push(node);},getClientRects(){return selected.boxes;}};}
};
const measure=""" + check_page.FUNDING_SUMMARY_LINES_JS + """;
assert.deepEqual(measure(),[{key:'fund-budget-missing-list',kind:'budget',fontSize:14,lineTops:[10,32.4,54.8],textBottom:70.8}]);
assert.deepEqual(seen,texts);
""")


@pytest.mark.parametrize('gap,valid', [(19.5, True), (22.4, True), (22.6, False), (30, False), (-1, False)])
def test_wrapped_summary_to_next_summary_gap_respects_centred_hit_height(gap, valid):
    summaries = [
        {'key': 'missing', 'kind': 'budget', 'fontSize': 14, 'lineTops': [10, 28.9], 'textBottom': 45},
        {'key': 'other', 'kind': 'budget', 'fontSize': 14, 'lineTops': [45 + gap], 'textBottom': 61 + gap},
    ]
    assert (check_page.funding_summary_spacing_errors(summaries) == []) is valid
    summaries[1]['kind'] = 'staff'
    assert check_page.funding_summary_spacing_errors(summaries) == []
    summaries[1]['kind'] = 'budget'
    summaries[0]['lineTops'] = [10]
    assert check_page.funding_summary_spacing_errors(summaries) == []


def test_record_placement_requires_current_row_parent_and_immediate_adjacency():
    run_node("""
const assert=require('node:assert/strict');
const row={},other={};let top=44;
const detail={open:true,parentElement:row,getBoundingClientRect:()=>({top})};
const document={getElementById:id=>{assert.equal(id,'detail');return detail;}};
const button={getAttribute:()=> 'detail',closest:selector=>{assert.equal(selector,'.fund-item,.fund-name-item');return row;},
  getBoundingClientRect:()=>({bottom:44})};
const measure=MEASURE;
assert.equal(measure(button),true);
for(const badTop of [40,48]) {top=badTop;assert.equal(measure(button),false);}
top=44;detail.parentElement=other;assert.equal(measure(button),false);
detail.parentElement=row;detail.open=false;assert.equal(measure(button),false);
""".replace('MEASURE', check_page.FUNDING_RECORD_PLACEMENT_JS))


@pytest.mark.parametrize("period,basis,expected", [
    ("FY2026 enacted", "annual", "FY2026 enacted"),
    ("2026 total", "annual", "2026 total"),
    ("2026-27 to 2029-30", "period_mean", "2026-27 to 2029-30"),
    ("2024-25 to 2028-29", "period_mean", "2024-25 to 2028-29"),
    ("2025/26 Supplementary Estimate", "annual", "2025/26 Supplementary Estimate"),
    ("2025-26 to 2028-29", "allocation_current_year", "2026-27 allocation"),
    ("2025/26 to 2028/29", "allocation_current_year", "2026/27 allocation"),
    ("a year", "annual", "1 Jul 2026 to 30 Jun 2027"),
    ("", "annual", "1 Jul 2026 to 30 Jun 2027"),
])
def test_chart_period_uses_the_cards_and_records_formatter(period, basis, expected):
    source = PAGE.read_text()
    helper = source[source.index("  function annualPeriod("):source.index("  function withPeriod(")]
    helper += source[source.index("    function fundPeriod("):source.index("    function fundExplanation(")]
    record = {"period": period, "annual_basis_code": basis, "commitment": "adopted", "currency": "EUR", "annual_amount": 100,
              "annual_period": {"start": "2026-07-01", "end": "2027-06-30"}}
    record["display"] = {"qualifiers": funding_label(record, annual=True)[1]}
    run_node("const assert = require('node:assert/strict');\nconst fmtDate=d=>({'2026-07-01':'1 Jul 2026','2027-06-30':'30 Jun 2027'}[d]);\n" + helper
             + f"\nassert.equal(fundPeriod({json.dumps(record)}), {json.dumps(expected)});")


@pytest.mark.parametrize("commitment", ["adopted", "announced"])
@pytest.mark.parametrize("bound,value,figure,words", [
    (None, "~EUR 110m (GBP 90m)", "~EUR 110m", ""),
    ("about", "about EUR 110m (GBP 90m)", "~EUR 110m", ", about"),
    ("at_least", "at least ~EUR 110m (GBP 90m)", "~EUR 110m+", ", at least"),
    ("at_most", "up to ~EUR 110m (GBP 90m)", "up to ~EUR 110m", ", up to"),
])
def test_budget_figure_keeps_bounds_and_full_period_in_accessible_tooltip(commitment, bound, value, figure, words):
    source = PAGE.read_text()
    helper = source[source.index("  function annualPeriod("):source.index("  function withPeriod(")]
    helper += source[source.index("  function term("):source.index("\n", source.index("  function term("))] + "\n"
    helper += source[source.index("    function fundPeriod("):source.index("    var fundItems =")]
    record = {"period": "2026-27 to 2029-30", "annual_basis_code": "period_mean",
              "amount_qualifier": bound, "commitment": commitment,
              "display": {"value": value, "qualifiers": ["a year", "average over period", "2026-27 to 2029-30", commitment]}}
    run_node("""
const assert=require('node:assert/strict');
function el(tag,cls,text){return {tag,cls,text,attrs:{},setAttribute(k,v){this.attrs[k]=v;}};}
""" + helper + f"""
const figure=fundFigure({json.dumps(record)},'budget');
assert.equal(figure.text,{json.dumps(figure)});
assert.equal(figure.tabIndex,0);
assert.equal(figure.attrs['aria-description'],{json.dumps("A year, average over 2026-27 to 2029-30" + words + ", " + commitment + ".")});
""")


@pytest.mark.parametrize("bound,value,words", [
    ("about", "~240", ", about"), ("at_least", "140+", ", at least"),
    ("at_most", "up to 50", ", up to"), (None, "34", ""),
])
def test_staff_figure_explanation_keeps_date_and_bound(bound, value, words):
    source = PAGE.read_text()
    helper = source[source.index("    function staffBound("):source.index("    function measureBar(")]
    helper += source[source.index("    function fundExplanation("):source.index("    var fundItems =")]
    record = {"as_of": "2026-10-12", "total_qualifier": bound, "display": {"total": value}}
    run_node("""
const assert=require('node:assert/strict');
const fmtDate=date=>{assert.equal(date,'2026-10-12');return '12 Oct 2026';};
const term=(value,description)=>({value,description});
""" + helper + f"""
const figure=fundFigure({json.dumps(record)},'staff');
assert.equal(figure.value,{json.dumps(value)});
assert.equal(figure.description,{json.dumps("As at 12 Oct 2026" + words + ".")});
""")


def test_figure_tap_does_not_forward_to_the_records_opener():
    source = PAGE.read_text()
    listener = next(line for line in source.splitlines() if 'event.target !== button' in line and 'button.click()' in line)
    run_node("""
const assert=require('node:assert/strict');
let clicked=0, handler;
const button={click(){clicked++;}},row={addEventListener(type,fn){handler=fn;}};
""" + listener + """
const target=selector=>({closest:selectors=>selectors.split(', ').includes(selector)});
handler({target:target('[tabindex]')});assert.equal(clicked,0);
handler({target:target('a')});assert.equal(clicked,0);
handler({target:target('.fund-detail')});assert.equal(clicked,0);
handler({target:target('h4')});assert.equal(clicked,1);
handler({target:target('.fund-bar')});assert.equal(clicked,2);
handler({target:button});assert.equal(clicked,2);
""")


def test_commitment_key_geometry_rejects_missing_words_and_clipped_text():
    script = check_page.FUNDING_LAYOUT_JS
    helper = script[script.index("  const commitmentVisible="):script.index("  const heads=")]
    run_node("""
const assert=require('node:assert/strict');
const Node={TEXT_NODE:3},list={},rect=n=>n.bounds,visible=n=>n.shown;
const parent={parentElement:list,style:{overflowX:'visible',overflowY:'visible'},bounds:{left:0,right:200,top:0,bottom:40}};
const q={parentElement:parent,shown:true,
  textContent:'announced',firstChild:{nodeType:3},bounds:{left:0,right:200,top:0,bottom:40},
  style:{opacity:'1',overflowX:'visible',overflowY:'visible'}};
let wordBox={left:60,right:120,top:0,bottom:16};
const getComputedStyle=n=>n.style;
const document={createRange:()=>({setStart(n,at){assert.equal(q.textContent.slice(at),'announced');},
  setEnd(n,at){assert.equal(at,q.textContent.length);},getClientRects:()=>[{...wordBox,width:wordBox.right-wordBox.left,height:wordBox.bottom-wordBox.top}]})};
""" + helper + """
assert.equal(commitmentVisible(q),true);
q.textContent='2026-27 to 2029-30';assert.equal(commitmentVisible(q),false);
q.textContent='announced';q.shown=false;assert.equal(commitmentVisible(q),false);q.shown=true;
wordBox={left:0,right:60,top:20,bottom:36};assert.equal(commitmentVisible(q),true); // second line
q.bounds.bottom=20;assert.equal(commitmentVisible(q),false);q.bounds.bottom=40;
wordBox.right=220;assert.equal(commitmentVisible(q),false);wordBox.right=60;
parent.style.overflowY='hidden';parent.bounds.bottom=20;assert.equal(commitmentVisible(q),false);
""")


def test_same_records_move_between_charts_and_close_returns_to_the_exact_control():
    source = PAGE.read_text()
    helper = source[source.index("    function closeFund("):source.index("    fundItems.forEach(function (i) {")]
    escape = next(line for line in source.splitlines() if 'addEventListener("keydown"' in line and 'fundOpen' in line)
    run_node("""
const assert=require('node:assert/strict');
function parent(headerHeight=56){return {headerHeight,children:[],appendChild(n){
  if(n.parent) n.parent.children.splice(n.parent.children.indexOf(n),1);
  n.parent=this;this.children.push(n);
}};}
function el(){return {dataset:{},events:{},attrs:{},
  setAttribute(k,v){this.attrs[k]=v;},addEventListener(k,f){this.events[k]=f;},focus(){
    assert.equal(fundOpen.detail.style['--reading-head-height'],fundOpen.detail.parent.headerHeight+'px');
    focused=this;
  }};}
function detail(id){return {id,open:false,style:{setProperty(k,v){this[k]=v;}},
  querySelector(selector){assert.equal(selector,':scope > summary');return {getBoundingClientRect:()=>{
    assert.equal(this.open,true);assert.notEqual(this.parent,fallback);
    return {height:this.parent.headerHeight};
  }};}
};}
const institute={id:'first',jurisdiction_id:'AA',status:'established'}, second={id:'second',jurisdiction_id:'BB',status:'established'};
const J={AA:{name:'First'},BB:{name:'Second'}}, LABEL={established:'Established',planned:'Planned'}, FUND_DETAIL='Funding and staff records';
const DISPLAY_NAME={first:'First Institute',second:'Second Institute'};
const first=detail('fund-detail-first'), next=detail('fund-detail-second');
const fundRecords={first:{detail:first},second:{detail:next}}, fallback=parent();
let fundOpen=null,focused=null;
const returnToOpener=button=>{focused=button;};
fallback.appendChild(first);fallback.appendChild(next);
""" + helper + """
const budgetRow=parent(56), staffRow=parent(83), missingLine=parent(110), otherLine=parent(56);
const budget=fundButton(institute,'fund-row',budgetRow), staff=fundButton(institute,'fund-row',staffRow);
const missing=fundButton(second,'fund-name-row',missingLine), other=fundButton(second,'fund-name-row',otherLine);
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
other.events.click();assert.equal(next.parent,otherLine);assert.equal(missing.attrs['aria-expanded'],'false');
closeFund(true);assert.equal(next.open,false);assert.equal(next.parent,fallback);
assert.equal(focused,other);assert.equal(fundOpen,null);
staffRow.headerHeight=137; // reopening after a resize must replace the previous measurement
staff.events.click();staff.events.click();assert.equal(first.open,false);
assert.equal(focused,staff);assert.equal(fundOpen,null);
assert.deepEqual(new Set(fallback.children),new Set([first,next]));
staff.events.click();
let keydown;
const $=id=>{assert.equal(id,'fund-rows');return {addEventListener(type,fn){assert.equal(type,'keydown');keydown=fn;}};};
""" + escape + """
for(const event of [{key:'Enter',defaultPrevented:false},{key:'Escape',defaultPrevented:true}]) {
  keydown({...event,preventDefault(){assert.fail('unrelated or handled keys must remain untouched');}});
  assert.equal(fundOpen.button,staff);
}
let prevented=false;
keydown({key:'Escape',defaultPrevented:false,preventDefault(){prevented=true;}});
assert.equal(prevented,true);assert.equal(fundOpen,null);assert.equal(focused,staff);
assert.equal(first.parent,fallback);assert.equal(staff.attrs['aria-expanded'],'false');
""")
