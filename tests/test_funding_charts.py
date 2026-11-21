"""Compact chart labels and shared disclosure behaviour, without a browser or build."""

import json
import subprocess
from pathlib import Path

import pytest

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
const confTerms=parts=>parts, separated=parts=>el('span','',parts.join(' '));
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
