"""Exercise the funding row decisions in Node, without a build or browser."""

import re
import subprocess

import pytest

from conftest import ROOT


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


def test_short_periods_keep_the_allocation_year_and_financial_year_bounds():
    run_node(helper("fundingPeriod") + """
const period = (start, end) => fundingPeriod({annual_period: {start, end}});
assert.equal(period('2026-01-01', '2026-12-31'), '2026');
assert.equal(period('2025-10-01', '2026-09-30'), 'FY2026');
assert.equal(period('2026-04-01', '2030-03-31'), '2026/27–2029/30');
assert.equal(period('2024-04-01', '2029-03-31'), '2024/25–2028/29');
assert.equal(period('2026-07-01', '2027-06-30'), '2026/27');
assert.equal(period('2026-02-01', '2026-12-31'), '2026');
assert.equal(period('2026-01-01', '2027-06-30'), '2026–2027');
assert.equal(fundingPeriod({}), '');
""")


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
const line = {{children:parts, hidden:false, clientWidth:{width}, getClientRects:()=>[{{}}],
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
