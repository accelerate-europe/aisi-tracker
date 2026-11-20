"""Functions table gate mutations, without starting a browser."""

import copy
from pathlib import Path

import pytest

from scripts import check_page


@pytest.fixture
def functions():
    data = {
        "dataset": {"vocabularies": {"function": {
            "testing": {"label": "Testing", "short": "testing", "definition": "Tests models.", "group": "Science"},
            "old": {"label": "Retired", "definition": "Old test.", "group": "Retired", "deprecated": True},
            "advice": {"label": "Advice", "short": "advice", "definition": "Advises government.", "group": "Policy"},
            "research": {"label": "Research", "short": "research", "definition": "Does research.", "group": "Science"},
            "foresight": {"label": "Foresight", "short": "foresight", "definition": "Forecasts.", "group": "Policy"},
            "incidents": {"label": "Incidents", "short": "incidents", "definition": "Responds.", "group": "Policy"},
        }}},
        "jurisdictions": [{"id": "AA", "name": "Alpha", "status": "proposed", "status_since": "2020-01-01"},
                          {"id": "ZZ", "name": "Zulu", "status": "established", "status_since": "2024"}],
        "institutes": [
            {"id": "a", "jurisdiction_id": "AA", "name": "Alpha Institute", "acronym": "AI",
             "functions": [{"code": "testing", "value": "both", "claim_ids": []},
                           {"code": "advice", "value": "not_coded", "claim_ids": []},
                           {"code": "research", "value": "activity", "claim_ids": []},
                           {"code": "foresight", "value": "not_found", "claim_ids": []},
                           {"code": "incidents", "value": "not_found", "claim_ids": []}], "claims": []},
            {"id": "z", "jurisdiction_id": "ZZ", "name": "Zulu Institute", "short_name": "ZI",
             "functions": [{"code": "testing", "value": "activity", "claim_ids": ["c"]},
                           {"code": "advice", "value": "mandate", "claim_ids": []},
                           {"code": "research", "value": "not_found", "claim_ids": []},
                           {"code": "foresight", "value": "not_found", "claim_ids": []},
                           {"code": "incidents", "value": "not_found", "claim_ids": []}],
             "claims": [{"id": "fact", "kind": "fact", "text": "A separate fact."},
                        {"id": "c", "kind": "function", "basis": "activity", "text": "It tested a model.", "source_ids": ["s"]}]},
        ],
        "sources": [{"id": "s", "url": "https://example.org/source", "excerpts": [
            {"text": "We tested model X.", "supports": ["/institutes/1/claims/1/text"]}]}],
    }
    html = '''<nav class="secnav"><a href="#functions" data-sections="functions testing set-up funding">Compare</a></nav>
    <div class="group"><nav class="group-nav"><a href="#functions">Functions</a></nav>
    <section aria-labelledby="functions"><h2 id="functions">Functions</h2><div id="functions-table">
    <p class="fn-summary">Most common: testing (2 of 2 institutes), advice (1) and research (1).</p>
    <span id="hint" class="vh">Scrolls sideways</span>
    <div class="fn-scroll" role="region" tabindex="0" aria-labelledby="functions" aria-describedby="hint">
    <table class="fn-table"><caption class="vh">Functions by institute</caption><colgroup></colgroup><colgroup span="2"></colgroup><colgroup span="3"></colgroup><colgroup></colgroup><thead><tr>
    <th scope="col" rowspan="2">Institute</th><th scope="colgroup" colspan="2">Science</th><th scope="colgroup" colspan="3">Policy</th><td class="fn-pad" aria-hidden="true"></td>
    </tr><tr>
    <th scope="col" data-code="testing"><abbr class="g" tabindex="0" aria-description="Testing: Tests models.">Testing</abbr></th>
    <th scope="col" data-code="research"><abbr class="g" tabindex="0" aria-description="Research: Does research.">Research</abbr></th>
    <th scope="col" data-code="advice" class="fn-group-start"><abbr class="g" tabindex="0" aria-description="Advice: Advises government.">Advice</abbr></th>
    <th scope="col" data-code="foresight"><abbr class="g" tabindex="0" aria-description="Foresight: Forecasts.">Foresight</abbr></th>
    <th scope="col" data-code="incidents"><abbr class="g" tabindex="0" aria-description="Incidents: Responds.">Incidents</abbr></th><td class="fn-pad" aria-hidden="true"></td>
    </tr></thead>
    <tbody><tr class="fn-group"><th scope="rowgroup">Established</th><td class="fn-column fn-group-first" data-code="testing" aria-hidden="true"></td><td class="fn-column" data-code="research" aria-hidden="true"></td><td class="fn-column fn-group-start" data-code="advice" aria-hidden="true"></td><td class="fn-column" data-code="foresight" aria-hidden="true"></td><td class="fn-column" data-code="incidents" aria-hidden="true"></td><td class="fn-pad" aria-hidden="true"></td></tr>
    <tr data-institute="z"><th scope="row"><abbr class="g" tabindex="0" aria-description="Zulu Institute">Zulu</abbr></th>
    <td data-code="testing" data-value="activity"><button type="button" class="fn-cell" aria-label="Zulu, Testing: Active" aria-expanded="false" aria-controls="detail-z"><span class="fn-mark fn-activity" aria-hidden="true"></span></button></td>
    <td data-code="research" data-value="not_found"><span class="vh">not found</span></td>
    <td data-code="advice" data-value="mandate" class="fn-group-start"><button type="button" class="fn-cell" aria-label="Zulu, Advice: Assigned" aria-expanded="false" aria-controls="detail-z"><span class="fn-mark fn-mandate" aria-hidden="true"></span></button></td>
    <td data-code="foresight" data-value="not_found"><span class="vh">not found</span></td>
    <td data-code="incidents" data-value="not_found"><span class="vh">not found</span></td><td class="fn-pad" aria-hidden="true"></td></tr>
    </tbody><tbody><tr class="fn-group"><th scope="rowgroup">Not yet established</th><td class="fn-column fn-group-first" data-code="testing" aria-hidden="true"></td><td class="fn-column" data-code="research" aria-hidden="true"></td><td class="fn-column fn-group-start" data-code="advice" aria-hidden="true"></td><td class="fn-column" data-code="foresight" aria-hidden="true"></td><td class="fn-column" data-code="incidents" aria-hidden="true"></td><td class="fn-pad" aria-hidden="true"></td></tr>
    <tr data-institute="a"><th scope="row"><abbr class="g" tabindex="0" aria-description="Alpha Institute">Alpha</abbr></th>
    <td data-code="testing" data-value="both"><button type="button" class="fn-cell" aria-label="Alpha, Testing: Assigned and active" aria-expanded="false" aria-controls="detail-a"><span class="fn-mark fn-activity" aria-hidden="true"></span></button></td>
    <td data-code="research" data-value="activity"><button type="button" class="fn-cell" aria-label="Alpha, Research: Active" aria-expanded="false" aria-controls="detail-a"><span class="fn-mark fn-activity" aria-hidden="true"></span></button></td>
    <td data-code="advice" data-value="not_coded" class="fn-group-start"><span class="vh">not yet coded</span></td>
    <td data-code="foresight" data-value="not_found"><span class="vh">not found</span></td>
    <td data-code="incidents" data-value="not_found"><span class="vh">not found</span></td><td class="fn-pad" aria-hidden="true"></td></tr>
    </tbody></table></div>
    <ul class="fn-legend fn-desktop-only" role="list"><li data-value="activity">Active</li><li data-value="mandate">Assigned</li><li data-value="not_found">Blank: no public text found; it may still be done</li><li data-value="not_coded">not yet coded</li></ul>
    <div class="section-foot"><details class="more quiet prose-panel fn-how"><summary>How this is coded</summary>
    <p class="small fn-use fn-desktop-only">Select a mark to see the quote and source.</p>
    <p class="small fn-use fn-phone-only">Select a country to see its functions, then a function for its quote and source.</p>
    <p class="small">We record quoted public texts.</p>
    <p class="small fn-order">Rows: by the year the institute's current status began, oldest first.</p>
    <p class="small fn-provisional">The list of functions is provisional.</p></details><span class="section-foot-actions"><a class="standalone section-dl ico i-dl" href="data/functions.csv" download="functions.csv"><span class="vh">Download functions (CSV)</span></a></span></div>
    </div></section></div>'''
    return data, html


def test_functions_structure_keeps_unanimous_not_found_columns(functions):
    data, html = functions
    assert check_page.functions_structure(html, data) == []
    assert [i["id"] for i in check_page.function_rows(data)] == ["z", "a"]


def test_claim_indent_oracle_catches_children_outside_the_parent_text_edge():
    """Measure the name's text, including descendants that escape an otherwise aligned block."""
    import subprocess

    script = r"""
const assert=require('node:assert/strict'), inspect=CHECKER;
const node=(name,left)=>({className:name,left,visible:true,closest:()=>null,
  getClientRects(){return this.visible ? [{left:this.left,width:100,height:20}] : [];},
  getBoundingClientRect(){return {left:this.left};}});
const name=node('function name',40), basis=node('fn-basis',40), sentence=node('claim-text',40),
  sources=node('fn-sources',40), body=node('quote-body',56), anchor=node('quote-anchor',56);
const children=[basis,sentence,sources,body,anchor];
const panel=Object.assign(node('fn-phone-claims',40), {matches:s=>s==='.fn-phone-claims',
  parentElement:{querySelector:()=>name},querySelectorAll:()=>children});
const document={createRange:()=>({selectNodeContents(){},getBoundingClientRect:()=>({left:40})})};
const getComputedStyle=n=>({visibility:n.visible?'visible':'hidden'});
assert.deepEqual(inspect(panel),[]);
for(const child of [panel,...children]) {
  const original=child.left; child.left=39;
  assert.ok(inspect(panel).some(e=>e.includes(child===panel?'block':'child')),child.className);
  child.left=original;
}
anchor.left=-100;anchor.visible=false;assert.deepEqual(inspect(panel),[]);
anchor.visible=true;assert.ok(inspect(panel).some(e=>e.includes('quote-anchor')));
anchor.left=56;panel.left=42;assert.ok(inspect(panel).some(e=>e.includes('does not align')));
// The grid has no pip: its claims share the actual heading text edge, including wrapped child rectangles.
panel.matches=()=>false;panel.querySelector=()=>name;panel.querySelectorAll=()=>[sources];
sources.querySelectorAll=()=>[anchor];sources.getClientRects=()=>[{left:40,width:100,height:20},{left:39,width:20,height:20}];
assert.ok(inspect(panel).some(e=>e.includes('fn-sources')));
""".replace('CHECKER', check_page.FN_CLAIM_INDENT_JS)
    subprocess.run(['node', '-'], input=script, text=True, capture_output=True, check=True)


@pytest.mark.parametrize("opened, source", [(False, False), (True, False), (False, True), (True, True)])
def test_claim_spacing_uses_text_edges_and_keeps_hit_height(opened, source):
    """A card reads sentence, Quote label, quote; a function panel reads sentence, source line, label, quote (owner, 18 Nov 2026)."""
    shift = 28 if source else 0  # the source line sits between the sentence and the label
    metrics = {"claim": {"top": 0, "bottom": 20}, "source": {"top": 28, "bottom": 48} if source else None,
               "label": {"top": 28 + shift, "bottom": 48 + shift},
               "quote": {"top": 56 + shift, "bottom": 96 + shift} if opened else None,
               "open": opened, "gap": 8, "hit": 28, "minHit": 28}
    assert check_page.claim_spacing_errors(metrics) == []
    broken = copy.deepcopy(metrics)
    broken["label"]["top"] -= 8
    assert any("text gaps" in e for e in check_page.claim_spacing_errors(broken))
    assert check_page.claim_spacing_errors(dict(metrics, hit=20)) == ["Quote label lost its hit height"]
    if opened:
        assert "missing visible" in check_page.claim_spacing_errors(dict(metrics, quote=None))[0]


def test_claim_spacing_rejects_reported_uneven_gaps():
    metrics = {"claim": {"top": 0, "bottom": 20}, "source": {"top": 20, "bottom": 40}, "label": {"top": 55, "bottom": 75},
               "quote": {"top": 82, "bottom": 122}, "open": True, "gap": 8, "hit": 28, "minHit": 28}
    assert any("[0, 15, 7]" in e for e in check_page.claim_spacing_errors(metrics))


def test_function_panel_width_checks_border_and_every_text_line():
    metrics = {"left": 40, "width": 800, "panel": {"left": 40, "right": 840, "width": 800},
               "lines": [{"left": 56, "right": 810}, {"left": 56, "right": 700}], "pixels": True, "sticky": True}
    assert check_page.function_panel_width_errors(metrics) == []
    for box in ('panel', 'line'):
        for edge, value in (('left', 38), ('right', 890)):
            broken = copy.deepcopy(metrics)
            (broken['panel'] if box == 'panel' else broken['lines'][-1])[edge] = value
            assert any('outside' in e for e in check_page.function_panel_width_errors(broken))
    assert any('pixels' in e for e in check_page.function_panel_width_errors(dict(metrics, pixels=False)))
    assert any('sticky' in e for e in check_page.function_panel_width_errors(dict(metrics, sticky=False)))
    assert check_page.function_panel_width_errors(None) == ["Functions panel or text lines missing"]


def test_function_pointer_grid_rejects_plain_pointers_and_hits_below_panel():
    plain = {'inside': True, 'interactive': False, 'cursor': 'auto'}
    assert check_page.function_pointer_errors([plain, dict(plain, interactive=True, cursor='pointer')]) == []
    assert check_page.function_pointer_errors([dict(plain, cursor='help')]) == []
    assert check_page.function_pointer_errors([dict(plain, cursor='pointer')]) == ['Functions plain panel area has a pointer cursor']
    # An underlying grid control is a leak even though that control itself is interactive.
    assert 'outside the panel' in check_page.function_pointer_errors([plain, dict(plain, inside=False, interactive=True)])[0]
    assert check_page.function_pointer_errors([]) == ['Functions pointer grid found no plain panel points']


def test_function_pointer_does_not_treat_scroll_tabindex_as_panel_control():
    import subprocess

    script = """
const scroll = {}, control = {}, panel = {contains: n => n === hit || n === control};
let nearest = scroll;
const hit = {closest: () => nearest};
const document = {elementFromPoint: () => hit}, getComputedStyle = () => ({cursor: 'auto'});
const sample = SAMPLE;
if (sample(panel, {x: 1, y: 1}).interactive) throw Error('scroll tabindex masks plain text');
nearest = control;
if (!sample(panel, {x: 1, y: 1}).interactive) throw Error('panel control not recognised');
""".replace('SAMPLE', check_page.FN_POINTER_POINT_JS)
    result = subprocess.run(['node', '-'], input=script, text=True, capture_output=True)
    assert result.returncode == 0, result.stderr


def test_clickable_width_threshold_and_only_documented_exceptions():
    control = {'name': 'Suggest a correction', 'width': 511, 'content': 153, 'allowed': None}
    assert '358.0px' in check_page.clickable_width_errors([control])[0]
    assert check_page.clickable_width_errors([dict(control, width=213)]) == []
    assert check_page.clickable_width_errors([dict(control, width=214)])
    assert check_page.clickable_width_errors([dict(control, width=153)]) == []
    for selector in ('.card.mini > summary', 'th[data-k] > button', '#changes > summary'):
        assert check_page.CLICK_WIDTH_ALLOW[selector]
        assert check_page.clickable_width_errors([dict(control, allowed=selector)]) == []
    assert check_page.clickable_width_errors([dict(control, allowed='.correction')])
    # Empty icon buttons are still measured, so a mistakenly stretched one cannot evade the gate.
    assert check_page.clickable_width_errors([dict(control, content=0)])
    assert check_page.clickable_width_errors([dict(control, width=153, correction=True, height=20, minHit=28)])


def test_function_geometry_scripts_parse_without_browser():
    import json
    import subprocess

    scripts = [check_page.TEXT_RECTS_JS, check_page.CLAIM_SPACING_JS, check_page.FN_PANEL_WIDTH_JS,
               check_page.FN_POINTER_JS, check_page.FN_OPEN_CONTROLS_JS, check_page.CLICK_WIDTH_JS, check_page.FN_VIEWS_JS,
               check_page.STATUS_QUALIFIERS_JS]
    subprocess.run(['node', '-'], input='for (const source of ' + json.dumps(scripts) + ') new Function(source);',
                   text=True, capture_output=True, check=True)


def test_qualifier_layout_remeasures_both_directions_and_skips_hidden_rows():
    """Run the actual layout pass against supplied baselines and rectangles, including a wrapped function name."""
    import subprocess

    source = (Path(__file__).resolve().parents[1] / "site/index.html").read_text()
    helpers = source[source.index("  function qualifierBaseline("):source.index("  function hideLineEndDots(")]
    script = r"""
const assert = require('node:assert/strict');
let width = 320, nameBottom = 20, visible = true, lastGlyph = 's';
const classes = new Set();
// An unattached baseline marker would wrap onto the qualifier's line. Only a
// marker bound to the final glyph measures the preceding line correctly.
const parent = {closest: () => null, getClientRects: () => [{}], insertBefore: probe => {probe.baseline = nameBottom + 24;}};
const original = 'Evaluation methods and tools  ';
const name = {nodeType: 3, textContent: original, parentElement: parent, parentNode: parent,
  splitText(i) {const suffix={textContent:this.textContent.slice(i),parentNode:parent,remove(){}};this.textContent=this.textContent.slice(0,i);return suffix;}};
const statusText = {};
const line = {};
const status = {
  checkVisibility: () => visible, getClientRects: () => [{}],
  contains: node => node === statusText,
  classList: {contains: c => c === 'fn-function-status' || classes.has(c), remove: c => classes.delete(c), toggle: (c, on) => on ? classes.add(c) : classes.delete(c)},
  previousElementSibling: {getBoundingClientRect: () => ({left: 0, right: 140, top: 0, bottom: nameBottom}), style: {setProperty: (k,v) => line[k]=v},
    parentElement: {getBoundingClientRect: () => ({right: width})}},
  getBoundingClientRect: () => ({right: classes.has('own-line') ? width : 240, bottom: classes.has('own-line') ? nameBottom+24 : nameBottom}),
  querySelector: () => ({appendChild: probe => {probe.baseline = classes.has('own-line') || width < 400 ? nameBottom + 24 : nameBottom;}})
};
const root = {querySelectorAll: () => [status]};
const el = () => ({style: {}, appendChild(probe) {assert.equal(this.style.whiteSpace,'nowrap');assert.equal(this.textContent,lastGlyph);probe.baseline=nameBottom;}, getBoundingClientRect() {return {top: this.baseline};}, remove() {}});
const document = {
  createTreeWalker: () => ({nodes: [name, statusText], nextNode() {this.currentNode = this.nodes.shift(); return this.currentNode;}})
};
const NodeFilter = {SHOW_TEXT: 4}, getComputedStyle = () => ({visibility: 'visible', paddingLeft: '0px'}), blockOf = () => root;
HELPERS
for (const [nextWidth, own] of [[320, true], [430, false], [360, true], [1440, false]]) {
  width = nextWidth; layoutQualifiers(root); assert.equal(classes.has('own-line'), own);
  assert.equal(name.textContent,original); // probes leave the original DOM text intact
  assert.equal(line['--fn-line-width'], (own ? width : 240) + 'px');
  assert.equal(line['--fn-line-height'], (own ? nameBottom+24 : nameBottom) + 'px');
  layoutQualifiers(root); assert.equal(classes.has('own-line'), own); // stable final state on repeated observer delivery
}
nameBottom = 44; width = 320; layoutQualifiers(root); assert.ok(classes.has('own-line'));
visible = false; width = 1440; layoutQualifiers(root); assert.ok(classes.has('own-line')); // no measuring hidden content
visible = true; layoutQualifiers(root); assert.ok(!classes.has('own-line'));
assert.equal(qualifierStartsLine(null, 26), false);
assert.equal(qualifierStartsLine(20, 20), false); // mixed body/meta type on one baseline
lastGlyph = '\u{10400}'; name.textContent = 'Source ' + lastGlyph + ' ';
assert.equal(qualifierBaseline(name),nameBottom);
assert.equal(name.textContent,'Source ' + lastGlyph + ' ');
""".replace("HELPERS", helpers)
    subprocess.run(['node', '-'], input=script, text=True, capture_output=True, check=True)


@pytest.fixture(params=['grouped', 'grid'])
def function_view(request):
    view = request.param
    caption = {'size': 14, 'colour': 'rgb(90, 90, 90)', 'weight': '400', 'decoration': 'none'}
    return {'view': view, 'grouped': view == 'grouped', 'grid': view == 'grid', 'meta': caption, 'bodySize': 16,
            'surface': {'top': 100, 'right': 900}, 'head': {'top': 100}, 'summary': {'bottom': 40},
            'control': {**caption, 'shown': True, 'role': 'group', 'name': 'Functions view', 'separator': '·',
                        'top': 60, 'bottom': 88, 'right': 900, 'items': [
                            {**caption, 'view': key, 'text': label, 'pressed': str(key == view).lower(), 'shown': True,
                             'type': 'button', 'controls': 'functions-table', 'marked': key == view, 'right': edge}
                            for key, label, edge in [('grouped', 'By group', 770), ('grid', 'All 5 functions', 900)]]}}


def test_functions_view_switch_accepts_both_states_and_one_pixel_alignment(function_view):
    assert check_page.function_view_errors(function_view, 5) == []
    function_view['control']['right'] -= 1
    function_view['control']['items'][-1]['right'] -= 1
    assert check_page.function_view_errors(function_view, 5) == []


@pytest.mark.parametrize('part, key, value, finding', [
    ('control', 'shown', False, 'hidden'),
    ('control', 'role', 'tablist', 'named group'),
    ('control', 'name', '', 'named group'),
    ('control', 'separator', '', 'separator'),
    ('button', 'text', 'Show grouped by institute', 'named buttons'),
    ('button', 'shown', False, 'named buttons'),
    ('button', 'type', 'submit', 'named buttons'),
    ('button', 'pressed', None, 'pressed state'),
    ('control', 'size', 16, 'caption size'),
    ('button', 'size', 13, 'caption size'),
    ('button', 'colour', 'rgb(0, 0, 0)', 'muted ink'),
    ('button', 'weight', '600', 'caption weight'),
    ('control', 'bottom', 102, 'own line'),
    ('control', 'top', 38, 'own line'),
    ('control', 'right', 902, 'right edge'),
    ('last', 'right', 898, 'right edge'),
])
def test_functions_view_switch_rejects_regressions(function_view, part, key, value, finding):
    target = function_view['control']
    if part != 'control':
        target = target['items'][-1 if part == 'last' else 0]
    target[key] = value
    assert any(finding in error for error in check_page.function_view_errors(function_view, 5))


def test_functions_view_switch_rejects_wrong_selection_and_underlined_current(function_view):
    current = next(b for b in function_view['control']['items'] if b['pressed'] == 'true')
    current['decoration'] = 'underline'
    current['marked'] = False
    for b in function_view['control']['items']:
        b['pressed'] = 'true'
    function_view['grid'] = function_view['grouped'] = True
    errors = check_page.function_view_errors(function_view, 5)
    assert any('pressed state' in e for e in errors)
    assert any('mark only' in e for e in errors)
    assert any('no text underline' in e for e in errors)


@pytest.mark.parametrize('initial', [None, 'grouped', 'grid'])
def test_functions_view_buttons_initialise_switch_and_keep_focus(initial):
    """Exercise the real controls, including saved view state and clicks that do not move focus."""
    import json
    import subprocess

    source = (Path(__file__).resolve().parents[1] / 'site/index.html').read_text()
    control = source[source.index('        var viewSwitch ='):source.index('        buildForm(false);')]
    setter = source[source.index('      var viewButtons ='):source.index('      build();', source.index('      var viewButtons ='))]
    copy_source = source[source.index('      var FN_COPY ='):source.index('      var names =', source.index('      var FN_COPY ='))]
    script = r'''
const assert = require('node:assert/strict');
let focused = null, updates = 0;
const el = (tag, cls, text) => ({tag, className: cls, textContent: text, dataset: {}, attrs: {}, children: [],
  setAttribute(k, v) { this.attrs[k] = v; }, appendChild(n) { this.children.push(n); },
  querySelectorAll() { return []; },
  addEventListener(k, fn) { this[k] = fn; }, focus() { focused = this; }});
const separated = parts => ({children: parts}); // layout has its own separator oracle; these are the live controls
const box = el('div'), columns = Array(5);
box.id = 'functions-table'; box.dataset.view = INITIAL;
const updateFits = () => updates++;
COPY
SETTER
CONTROL
assert.equal(viewSwitch.attrs.role, 'group');
assert.equal(viewSwitch.attrs['aria-label'], 'Functions view');
assert.deepEqual(viewButtons.map(b => b.textContent), ['By group', 'All 5 functions']);
assert.deepEqual(viewButtons.map(b => b.attrs['aria-pressed']), box.dataset.view === 'grid' ? ['false', 'true'] : ['true', 'false']);
for (const view of ['grid', 'grouped']) {
  focused = {hiddenView: true};
  const button = viewButtons.find(b => b.dataset.view === view);
  button.click();
  assert.equal(box.dataset.view, view);
  assert.equal(focused, button);
  assert.equal(viewButtons.filter(b => b.attrs['aria-pressed'] === 'true').length, 1);
  assert.equal(button.attrs['aria-pressed'], 'true');
  const before = updates;
  button.click();
  assert.equal(updates, before, 'current view is a no-op');
}
const before = focused;
setView('grid');
assert.equal(focused, before, 'programmatic arrival must not move focus');
'''
    for marker, value in {'INITIAL': json.dumps(initial), 'COPY': copy_source, 'SETTER': setter, 'CONTROL': control}.items():
        script = script.replace(marker, value)
    subprocess.run(['node', '-'], input=script, text=True, capture_output=True, check=True)


def test_functions_structure_rejects_a_missing_vocabulary_column(functions):
    from bs4 import BeautifulSoup

    data, html = functions
    soup = BeautifulSoup(html, "html.parser")
    for cell in soup.select('.fn-table [data-code="incidents"]'):
        cell.decompose()
    soup.select_one('th[scope="colgroup"][colspan="3"]')["colspan"] = "2"
    soup.select_one('colgroup[span="3"]')["span"] = "2"
    assert "Functions columns/order differ" in check_page.functions_structure(str(soup), data)


def test_functions_desktop_missing_function_uses_uncoded_value(functions):
    data, html = functions
    data = copy.deepcopy(data)
    institute = next(i for i in data["institutes"] if i["id"] == "a")
    institute["functions"] = [row for row in institute["functions"] if row["code"] != "advice"]
    assert check_page.functions_structure(html, data) == []


def test_function_rows_order_by_year_then_name_then_id(functions):
    data, _ = functions
    data = copy.deepcopy(data)
    data["jurisdictions"] += [
        {"id": "BB", "name": "Beta", "status": "established", "status_since": "2024-12-31"},
        {"id": "CC", "name": "Later", "status": "established", "status_since": "2025"},
    ]
    data["institutes"] += [{"id": "b2", "jurisdiction_id": "BB"}, {"id": "b1", "jurisdiction_id": "BB"}, {"id": "c", "jurisdiction_id": "CC"}]
    assert [i["id"] for i in check_page.function_rows(data)] == ["b1", "b2", "z", "c", "a"]


@pytest.mark.parametrize(("old", "new", "finding"), [
    ('data-value="activity"', 'data-value="mandate"', "value counts"),
    ('<td class="fn-pad" aria-hidden="true"></td>', '<td class="fn-pad"></td>', "spacer column"),
    ('<colgroup span="2"></colgroup>', '<colgroup span="3"></colgroup>', "column groups"),
    ('aria-label="Zulu, Testing: Active"', 'aria-label="ZI, Testing: Active"', "coded control/name"),
    ('aria-label="Alpha, Testing: Assigned and active"', 'aria-label="Alpha, Testing: Active"', "coded control/name"),
    ('class="fn-cell"', 'class="not-a-button"', "coded control/name"),
    ('aria-description="Testing: Tests models."', 'aria-description="Wrong test."', "function label/test"),
    ('aria-description="Zulu Institute"', 'aria-description="Wrong institute"', "row name/institute tip"),
    ('aria-description="Zulu Institute"', 'aria-description="Zulu Institute (planned)"', "status word in row tip"),
    ('<th scope="rowgroup">', '<th scope="rowgroup" colspan="5">', "group scope/span"),
    ('<td class="fn-column" data-code="research" aria-hidden="true"></td>', '', "block band columns/order"),
    ('class="fn-column fn-group-start" data-code="advice"', 'class="fn-column" data-code="advice"', "block band group gap"),
    ('>Zulu</abbr>', '>Zulu (2 of 5)</abbr>', "row name/institute tip"),
    ('Not yet established</th>', 'Pending</th>', "not-yet-established"),
    ('Not yet established</th>', 'proposed</th>', "status word"),
    ('<li data-value="not_coded">not yet coded</li>', '', "legend differs"),
    ('<tr data-institute="z">', '<tr data-institute="a">', "rows/order"),
    ('class="fn-group-start"', 'class="missing-gap"', "group gap"),
    ('fn-mark fn-activity', 'fn-mark fn-both', "mark differs"),
    ('Most common: testing (2 of 2 institutes),', 'Most common: Zulu Institute (2 of 2 institutes),', "summary facts"),
    ('<div id="functions-table">', '<div id="functions-table"><p>It does this (active).</p>', "status word alone in parentheses"),
    ('<span class="vh">not found</span>', '<span>not found</span>', "absent value"),
    ('<div id="functions-table">', '<div id="functions-table"><p class="fn-blank">Old caveat.</p>', "obsolete lede or blank"),
    ('Blank: no public text found; it may still be done', 'Blank: no public text found', "legend differs"),
    ('Blank: no public text found; it may still be done', 'Blank: it may still be done', "legend differs"),
    ('The list of functions is provisional.', '', "provisional line"),
    ('class="more quiet prose-panel fn-how"', 'class="more quiet prose-panel"', "coding fold missing"),
    ('<details class="more quiet prose-panel fn-how">', '<details open class="more quiet prose-panel fn-how">', "coding fold missing or open"),
    ('<tbody>', '<tbody><tr class="fn-detail-row"><td>We tested model X.</td></tr>', "quote was pre-rendered"),
    ('aria-describedby="hint"', '', "scroll region"),
])
def test_functions_structure_rejects_mutations(functions, old, new, finding):
    data, html = functions
    assert old in html
    assert any(finding in e for e in check_page.functions_structure(html.replace(old, new), data))


@pytest.mark.parametrize("selector", ['.fn-order', '.fn-provisional'])
def test_functions_explanatory_lines_stay_inside_coding_fold(functions, selector):
    from bs4 import BeautifulSoup

    data, html = functions
    soup = BeautifulSoup(html, "html.parser")
    soup.select_one('#functions-table').append(soup.select_one(selector).extract())
    assert "Functions row order and provisional lines must end the coding fold" in check_page.functions_structure(str(soup), data)


@pytest.mark.parametrize("suffix", ["planned", "proposed", "exploring", "announced", "PLANNED"])
def test_functions_status_suffix_is_hidden_in_both_forms(functions, functions_phone, suffix):
    data, desktop = functions
    data = copy.deepcopy(data)
    for institute in data["institutes"]:
        institute["name"] += f" ({suffix})"
    # The hand-written displays still contain the full institute name, without the recorded suffix.
    assert check_page.functions_structure(desktop, data) == []
    assert check_page.functions_phone_structure(functions_phone[1], data) == []


@pytest.mark.parametrize(("name", "shown"), [
    ("Institute (ISI) (Proposed)", "Institute (ISI)"),
    ("Institute (ISI)", "Institute (ISI)"),
    ("Institute (planned research)", "Institute (planned research)"),
    ("Institute (planned) branch", "Institute (planned) branch"),
    ("Institute announced", "Institute announced"),
])
def test_function_display_name_preserves_everything_except_trailing_status(name, shown):
    assert check_page.function_display_name({"name": name, "short_name": "Short", "acronym": "I"}) == shown


def test_function_quote_oracle_uses_original_indices_and_requires_one_binding(functions):
    data, _ = functions
    assert check_page.function_quotes(data)["c"]["excerpt"]["text"] == "We tested model X."
    broken = copy.deepcopy(data)
    broken["sources"][0]["excerpts"][0]["supports"] = ["/institutes/1/claims/0/text"]
    with pytest.raises(ValueError, match="expected one bound quote, got 0"):
        check_page.function_quotes(broken)
    broken = copy.deepcopy(data)
    broken["sources"][0]["excerpts"] *= 2
    with pytest.raises(ValueError, match="expected one bound quote, got 2"):
        check_page.function_quotes(broken)


@pytest.mark.parametrize(("values", "expected"), [
    ([15, 13, 12, 11], "Most common: a (15 of 15 institutes), b (13) and c (12)."),
    ([15, 13, 12, 12], "Most common: a (15 of 15 institutes) and b (13)."),
    ([15, 12, 12, 12], "Most common: a (15 of 15 institutes)."),
    ([12, 12, 12, 12], "Most common: a, b, c and d (12 of 15 institutes each)."),
    ([12, 12, 12, 0], "Most common: a (12 of 15 institutes), b (12) and c (12)."),
    ([0, 1, 1, 2], "Most common: d (2 of 15 institutes), b (1) and c (1)."),
    ([0, 0, 0, 0], ""),
])
def test_function_summary_keeps_vocabulary_order_and_whole_ties(values, expected):
    vocab = {code: {"short": code} for code in "abcd"}
    assert check_page.function_summary(vocab, dict(zip(vocab, values)), 15) == expected


def test_function_jobs_are_registered_for_shards(functions):
    jobs = check_page.browser_jobs(Path("dist"), functions[0], None)
    names = [check_page.job_id(*job) for job in jobs]
    for name in ("check_functions_table", "check_functions_detail", "check_functions_layout", "check_functions_phone", "check_status_qualifiers", "check_functions_sticky", "check_functions_tint", "check_functions_zero_carrier", "check_function_group_tabs", "check_selection_boxes", "check_tinted_rows"):
        assert names.count("interactions/" + name) == 1


def test_functions_group_type_requires_its_leading_token():
    page = (Path(__file__).resolve().parents[1] / "site/index.html").read_text()
    changed = page.replace("/var(--leading-fn-group)", "/2")
    assert changed != page
    assert check_page.report_line("fn-group line height does not use its role token") in check_page.check_style_source(changed)


@pytest.fixture
def slanted_function_flag():
    # A 40px track after a gutter: a 60px lean over 60*sqrt(3) height.
    # Hand-worked corners, independent of the page's CSS and measurement routine.
    return {"track": {"left": 100, "right": 140, "top": 20, "bottom": 123.923048454},
            "points": [[160, 20], [200, 20], [140, 123.923048454], [100, 123.923048454]],
            "group": [160, 280], "matrix": [1, 0, -.577350269, 1, 0, 0],
            "exists": True, "position": "absolute", "head_transparent": True, "label_transparent": True,
            "tinted": True, "label_z": 1, "label_on_top": True}


def test_function_flag_geometry_accepts_slanted_track_after_gutter(slanted_function_flag):
    assert check_page.function_flag_geometry_errors(slanted_function_flag) == []


# B. Hand-worked group endpoints: a 60px lean over 60*sqrt(3) height, with a 1px head rule.
@pytest.fixture
def leaning_group_tab():
    return {"boundary": 100, "next_boundary": 260, "lean": 60, "label_y": 20,
            "rule_top": 123.923048454, "rule_bottom": 124.923048454,
            "top": [160, 20], "bottom": [100, 123.923048454], "label_left": 160,
            "underline": [[160, 20], [320, 20]], "upright": [100, 124.923048454],
            "painted": True, "underline_painted": True, "upright_painted": True, "same_rule": True}


def test_function_group_tabs_include_first_and_final_boundaries(leaning_group_tab):
    assert check_page.function_group_tab_errors(leaning_group_tab) == []
    leaning_group_tab.update(next_boundary=None, underline=None, label_left=None)
    assert check_page.function_group_tab_errors(leaning_group_tab) == []


@pytest.mark.parametrize(("field", "value", "finding"), [
    ("bottom", [72, 123.923048454], "foot"),  # cell border edge instead of padding edge after the gutter
    ("top", [100, 20], "60 degrees"),  # an upright line in the head zone
    ("top", [160, 16], "label line"),
    ("underline", [[160, 20], [292, 20]], "adjacent boundary"),  # missed next group's gutter
    ("label_left", 164, "label"),
    ("upright", [101, 124.923048454], "continue"),
    ("upright", [100, 126], "continue"),
    ("same_rule", False, "colour/width"),
    ("painted", False, "missing"),
    ("underline_painted", False, "underline is missing"),
    ("upright_painted", False, "continue"),
    ("underline", None, "underline is missing"),
])
def test_function_group_tabs_reject_disconnected_or_wrong_rules(leaning_group_tab, field, value, finding):
    leaning_group_tab[field] = value
    assert any(finding in e for e in check_page.function_group_tab_errors(leaning_group_tab))


@pytest.mark.parametrize(("delta", "accepted"), [(.49, True), (.51, False)])
def test_function_group_tab_foot_uses_half_pixel_tolerance(leaning_group_tab, delta, accepted):
    leaning_group_tab["bottom"][0] += delta
    assert (check_page.function_group_tab_errors(leaning_group_tab) == []) is accepted


@pytest.mark.parametrize(("field", "value", "finding"), [
    ("exists", False, "missing"),
    ("position", "static", "positioned"),
    ("head_transparent", False, "background"),
    ("label_transparent", False, "background"),
    ("tinted", False, "tint"),
    ("matrix", [1, 0, 0, 1, 0, 0], "skewed"),
    ("matrix", [1, 0, .577350269, 1, 0, 0], "skewed"),
    ("group", [161, 280], "escapes its shifted group"),
    ("group", [160, 199], "escapes its shifted group"),
    ("label_z", 0, "on top"),
    ("label_on_top", False, "on top"),
])
def test_function_flag_geometry_rejects_wrong_paint_skew_group_or_stacking(slanted_function_flag, field, value, finding):
    slanted_function_flag[field] = value
    assert any(finding in e for e in check_page.function_flag_geometry_errors(slanted_function_flag))


@pytest.mark.parametrize(("corner", "axis", "delta", "finding"), [
    (3, 0, -28, "foot"),  # wrong origin at the border box includes the group gutter
    (2, 0, -20, "foot"),  # the old label-sized band does not fill the track
    (3, 1, 1, "foot"),  # bottom border is not part of the padding-box flag
    (0, 1, 16, "full head-zone lean"),  # old head padding makes H and the band drift apart
    (1, 0, -16, "full head-zone lean"),
])
def test_function_flag_geometry_rejects_wrong_foot_and_top(slanted_function_flag, corner, axis, delta, finding):
    slanted_function_flag["points"][corner][axis] += delta
    assert any(finding in e for e in check_page.function_flag_geometry_errors(slanted_function_flag))


@pytest.mark.parametrize(("shift", "accepted"), [(0.49, True), (0.51, False)])
def test_function_flag_geometry_uses_half_pixel_tolerance(slanted_function_flag, shift, accepted):
    for point in slanted_function_flag["points"]:
        point[0] += shift
    assert (check_page.function_flag_geometry_errors(slanted_function_flag) == []) is accepted


def test_functions_structure_keeps_a_group_coded_nowhere(functions):
    """A group with uncoded and not-found functions stays visible and is absent from the summary."""
    data, html = functions
    data = copy.deepcopy(data)
    next(r for r in data["institutes"][1]["functions"] if r["code"] == "advice")["value"] = "not_coded"
    mandate = '<td data-code="advice" data-value="mandate" class="fn-group-start"><button type="button" class="fn-cell" aria-label="Zulu, Advice: Assigned" aria-expanded="false" aria-controls="detail-z"><span class="fn-mark fn-mandate" aria-hidden="true"></span></button></td>'
    assert mandate in html
    html = (html.replace(mandate, '<td data-code="advice" data-value="not_coded" class="fn-group-start"><span class="vh">not yet coded</span></td>')
            .replace('<td data-code="advice">1</td>', '<td data-code="advice">0</td>')
            .replace("testing (2 of 2 institutes), advice (1) and research (1).", "testing (2 of 2 institutes) and research (1)."))
    assert check_page.functions_structure(html, data) == []


def test_function_rows_put_an_undated_status_last_in_its_block(functions):
    """A record without status_since (an edge the card-grid gate exercises) sorts after the dated ones, never fails."""
    data, _ = functions
    data = copy.deepcopy(data)
    for j in data["jurisdictions"]:
        j.setdefault("status_since", "2024-01-01")
    data["jurisdictions"][0].pop("status_since")
    assert [i["id"] for i in check_page.function_rows(data)] == ["z", "a"]


@pytest.mark.parametrize(("text", "expected"), [
    ("The institute is doing it (active).", ["(active)"]),
    ("The government gives it the function (ASSIGNED).", ["(ASSIGNED)"]),
    ("It does it ( active ) and is tasked with it (assigned).", ["( active )", "(assigned)"]),
    ("Active: it does the work. Assigned: the law gives it the function.", []),
    ("It has a role (assigned by law) and staff (active since 2024).", []),
    ("(active)", []),
    ("A heading. (Assigned).", []),
    ("(active)\nA separate sentence.", []),
])
def test_function_copy_parenthesised_status_rule(text, expected):
    from scripts.reader_text import status_parentheses

    assert status_parentheses(text) == expected


def test_function_copy_reader_rule_covers_prose_and_masks_evidence():
    from scripts.reader_text import check_reader_text

    data = {
        "dataset": {
            "definition": "It does the work (active).",
            "vocabularies": {"function": {"testing": {"definition": "The law assigns it (assigned)."}}},
        },
        "jurisdictions": [{"id": "AA", "claims": [
            {"text": 'It reports "We do it (active)." and is tasked with it (assigned).'},
        ]}],
        "sources": [{"title": "Doing it (active)", "excerpts": [
            {"text": "We do it (assigned).", "gloss": "A translated description (active)."},
        ]}],
    }
    errors = [error for error in check_reader_text(data) if "parenthesised status" in error]
    assert errors == [
        "/dataset/definition: reword parenthesised status '(active)' as a plain sentence",
        "/dataset/vocabularies/function/testing/definition: reword parenthesised status '(assigned)' as a plain sentence",
        "/jurisdictions/0/claims/0/text: reword parenthesised status '(assigned)' as a plain sentence",
        "/sources/0/excerpts/0/gloss: reword parenthesised status '(active)' as a plain sentence",
    ]


def test_function_copy_status_rule_reads_only_fn_copy_strings():
    from scripts.reader_text import functions_copy_errors

    html = r'''<script>
    var other = "Elsewhere it does work (active).";
    var FN_COPY = {
      intro: "Which of {count} functions it does (active).",
      how: 'The institute does it (assigned).',
      escaped: "It does work \u0028active\u0029.",
      label: "(assigned)",
      plain: "Active: a public text reports the institute doing it."
    };
    </script>'''
    assert functions_copy_errors(html) == [
        "FN_COPY.intro: reword parenthesised status '(active)' as a plain sentence",
        "FN_COPY.how: reword parenthesised status '(assigned)' as a plain sentence",
        "FN_COPY.escaped: reword parenthesised status '(active)' as a plain sentence",
    ]
    assert functions_copy_errors('<p>Elsewhere it does work (active).</p>') == []


@pytest.fixture
def functions_phone(functions):
    """Hand-written phone projection: all five values, two unanimous not-found codes and two status blocks."""
    data, _ = functions
    html = '''<section aria-labelledby="functions"><h2 id="functions">Functions</h2><div id="functions-table">
    <div class="fn-phone"><div class="fn-phone-head"><span></span><span><span class="fn-group-label fn-label-full">Science</span><span class="fn-group-label fn-label-short">Science</span></span><span><span class="fn-group-label fn-label-full">Policy</span><span class="fn-group-label fn-label-short">Policy</span></span></div>
    <div class="fn-phone-block"><h3>Established</h3><ul role="list"><li data-institute="z">
    <button type="button" class="fn-phone-row" aria-label="Zulu, Zulu Institute: show functions" aria-expanded="false" aria-controls="fn-phone-detail-z"></button><span class="fn-phone-country" aria-hidden="true">Zulu</span><span class="fn-pips" aria-hidden="true"><span class="fn-mark fn-activity fn-pip" aria-hidden="true"></span><span class="fn-mark fn-not_found fn-pip" aria-hidden="true"></span></span><span class="fn-pips" aria-hidden="true"><span class="fn-mark fn-mandate fn-pip" aria-hidden="true"></span><span class="fn-mark fn-not_found fn-pip" aria-hidden="true"></span><span class="fn-mark fn-not_found fn-pip" aria-hidden="true"></span></span>
    </li></ul></div>
    <div class="fn-phone-block"><h3>Not yet established</h3><ul role="list"><li data-institute="a">
    <button type="button" class="fn-phone-row" aria-label="Alpha, Alpha Institute: show functions" aria-expanded="false" aria-controls="fn-phone-detail-a"></button><span class="fn-phone-country" aria-hidden="true">Alpha</span><span class="fn-pips" aria-hidden="true"><span class="fn-mark fn-activity fn-pip" aria-hidden="true"></span><span class="fn-mark fn-activity fn-pip" aria-hidden="true"></span></span><span class="fn-pips" aria-hidden="true"><span class="fn-mark fn-not_coded fn-pip" aria-hidden="true"></span><span class="fn-mark fn-not_found fn-pip" aria-hidden="true"></span><span class="fn-mark fn-not_found fn-pip" aria-hidden="true"></span></span>
    </li></ul></div></div>
    <ul class="fn-legend fn-phone-only" role="list"><li data-value="activity"><span class="fn-mark fn-activity fn-pip" aria-hidden="true"></span>Active</li><li data-value="mandate"><span class="fn-mark fn-mandate fn-pip" aria-hidden="true"></span>Assigned</li><li data-value="not_found"><span class="fn-mark fn-not_found fn-pip" aria-hidden="true"></span>No public text found; it may still be done</li><li data-value="not_coded"><span class="fn-mark fn-not_coded fn-pip" aria-hidden="true"></span>not yet coded</li></ul>
    </div></section>'''
    return data, html


def test_functions_phone_structure_accepts_groups_values_and_row_order(functions_phone):
    data, html = functions_phone
    assert check_page.functions_phone_structure(html, data) == []


def test_function_group_labels_keep_unknown_names():
    import json
    import subprocess

    expected = {"Testing and science": "Testing and science", "Rules": "Rules and standards", "Informing government": "Informing government", "Ecosystem": "Ecosystem and adoption",
                "Other and new": "Other and new", "constructor": "constructor", "__proto__": "__proto__"}
    source = (Path(__file__).resolve().parents[1] / "site/index.html").read_text()
    helpers = source[source.index("      var GROUP_LABELS ="):source.index("      function columnClass(")]
    result = subprocess.run(["node", "-e", helpers + "console.log(JSON.stringify(" + json.dumps(list(expected)) + ".map(groupName)));"],
                            capture_output=True, text=True, check=True)
    assert json.loads(result.stdout) == list(expected.values())
    assert [check_page.fn_group_name(group) for group in expected] == list(expected.values())


@pytest.mark.parametrize("first, second, first_label, second_label", [
    ("Testing and science", "Rules", "Testing and science", "Rules and standards"),
    ("Informing government", "Ecosystem", "Informing government", "Ecosystem and adoption"),
])
def test_function_group_labels_in_desktop_and_phone_heads(functions, functions_phone, first, second, first_label, second_label):
    for (data, html), check in ((functions, check_page.functions_structure), (functions_phone, check_page.functions_phone_structure)):
        data = copy.deepcopy(data)
        for entry in data["dataset"]["vocabularies"]["function"].values():
            entry["group"] = {"Science": first, "Policy": second}.get(entry["group"], entry["group"])
        html = html.replace(">Science<", ">" + first_label + "<").replace(">Policy<", ">" + second_label + "<")
        for label in (first_label, second_label):  # the phone head's short form prints "&" for "and"
            html = html.replace('fn-label-short">' + label + "<", 'fn-label-short">' + label.replace(" and ", "\u00a0& ") + "<")
        assert check(html, data) == []
        assert any("groups/order" in error or "group heads/order" in error for error in check(html.replace(">" + first_label + "<", ">Wrong group<"), data))


def test_function_claims_keep_grid_status_and_mixed_basis_labels():
    """Exercise the real claim renderer with single/mixed bases, inline and in grid panels."""
    import subprocess

    source = (Path(__file__).resolve().parents[1] / 'site/index.html').read_text()
    copy = source[source.index('      var FN_COPY ='):source.index('      var names =', source.index('      var FN_COPY ='))]
    renderer = source[source.index('      function appendClaims('):source.index('      function build()', source.index('      function appendClaims('))]
    script = """
const assert=require('node:assert/strict');
function el(tag,cls='',text='') {
  return {className:cls,text,children:[],dataset:{},classList:{add(){}},
    get textContent(){return this.text+this.children.map(n=>n.textContent).join('');},
    appendChild(n){this.children.push(n);return n;}, querySelectorAll:()=>[],querySelector:()=>null,insertBefore(){}};
}
const document={createTextNode:text=>el('#text','',text)}, S={}, SOURCE_ORDERS={};
const citations=()=>{}, fieldCitations=()=>[], fnSources=()=>el('ul'), claimBlock=()=>el('div','fn-claim');
const quoteOnSourceLine=()=>{}, glueCitations=()=>{}, externalLinks=()=>{};
const instShort=i=>el('span','',i.short_name), names={mandate:'Assigned',activity:'Active',both:'Assigned and active'};
const i={id:'sample',jurisdiction_id:'XX',short_name:'UK AISI'}, rows={sample:{test:{}}};
const CLAIM_INDEX={a:{claim:{basis:'activity'},excerpts:[]},m:{claim:{basis:'mandate'},excerpts:[]},m2:{claim:{basis:'mandate'},excerpts:[]}};
COPY
RENDERER
for(const [value,ids,sentence] of [
  ['mandate',['m'],'Assigned to UK AISI'],
  ['activity',['a'],'UK AISI is active, without an assignment'],
  ['both',['a','m','m2'],'Assigned to UK AISI, and active']
]) for(const inline of [false,true]) {
  rows.sample.test={value,claim_ids:ids};
  const detail=el('div'); appendClaims(detail,i,'test',inline);
  const contexts=detail.children.filter(n=>n.className.split(' ').includes('fn-detail-context'));
  assert.deepEqual(contexts.map(n=>n.textContent),inline?[]:[sentence]);
  const evidence=detail.children.filter(n=>!n.className.split(' ').includes('fn-detail-context'));
  assert.deepEqual(evidence.map(n=>n.className==='fn-basis'?n.textContent:n.dataset.claim),
    value==='both'?['Assignment','m','m2','Activity','a']:ids);
}
""".replace('COPY', copy).replace('RENDERER', renderer)
    subprocess.run(['node', '-'], input=script, text=True, capture_output=True, check=True)


def test_phone_function_summaries_statuses_and_claim_toggle():
    """Run the phone renderer and item checks on all real codes with every status, without Chromium."""
    import inspect
    import json
    import subprocess

    root = Path(__file__).resolve().parents[1]
    source = (root / "site/index.html").read_text()
    vocab = json.loads((root / "data/aisi.json").read_text())["dataset"]["vocabularies"]["function"]
    vocab = {code: entry for code, entry in vocab.items() if not entry.get("deprecated")}
    helpers = source[source.index("      var GROUP_LABELS ="):source.index("      function columnClass(")]
    helpers += next(line for line in source.splitlines() if "function mark(value, pip)" in line)
    helpers += next(line for line in source.splitlines() if "function coded(value)" in line)
    helpers += source[source.index("  function trailingQualifier("):source.index("  function qualifierStartsLine(")]
    start = source.rindex("groups.forEach(function (group) {", 0, source.index('var heading = el("h4", null, groupName(group))'))
    rendering = source[start:source.index(" button.parentNode.appendChild(detail);", start)]
    start = source.index("              var existing = button.parentNode.querySelector")
    toggle = source[start:source.index("\n            }", start)]
    gate = inspect.getsource(check_page.check_functions_phone)
    start = gate.index("const label = item.querySelector(':scope > .fn-function-label');")
    item_check = gate[start:gate.index("pip(label?.querySelector", start)]
    script = r'''
const assert = require('node:assert/strict');
function el(tag, className = '', text = '') {
  return {tagName: tag.toUpperCase(), className, text, children: [], attrs: {}, dataset: {},
    get textContent() { return this.text + this.children.map(n => n.textContent).join(''); },
    get nextElementSibling() { return this.parentNode?.children[this.parentNode.children.indexOf(this) + 1] || null; },
    setAttribute(k, v) { this.attrs[k] = v; }, getAttribute(k) { return this.attrs[k] ?? null; },
    removeAttribute(k) { delete this.attrs[k]; },
    appendChild(n) { n.parentNode = this; this.children.push(n); return n; },
    remove() { this.parentNode.children.splice(this.parentNode.children.indexOf(this), 1); },
    querySelector(selector) {
      for (const part of selector.split(', ')) {
        const direct = part.startsWith(':scope > '), cls = part.replace(':scope > ', '').slice(1);
        for (const n of this.children) {
          if ((n.className || '').split(' ').includes(cls)) return n;
          const found = !direct && n.querySelector(part); if (found) return found;
        }
      }
      return null;
    },
    closest() { return {dataset: {institute: 'sample'}}; }
  };
}
const document = {createTextNode: text => el('#text', '', text)};
function layoutQualifiers() {} // rendering and reading order here; the geometry oracle is tested separately
const vocab = VOCAB;
const names = {activity: 'Active', mandate: 'Assigned', both: 'Assigned and active', not_found: 'not found', not_coded: 'not yet coded'};
const groups = [...new Set(Object.values(vocab).map(v => v.group))];
const columns = groups.flatMap(g => Object.keys(vocab).filter(c => vocab[c].group === g));
const i = {id: 'sample'}, I = {sample: i}, rows = {sample: {}};
const appendClaims = claims => claims.appendChild(el('p', 'fn-claim', 'Evidence.'));
HELPERS
function toggle(button) { TOGGLE }
function checkItem(item, f) {
  const errors = [], key = 'sample', data = {dataset: {vocabularies: {function: vocab}}};
  const need = (ok, message) => { if (!ok) errors.push(message); };
  ITEM_CHECK
  return errors;
}
for (const value of Object.keys(names)) {
  for (const code of columns) rows.sample[code] = {value};
  const detail = el('div'); detail.id = 'detail';
  RENDERING
  assert.deepEqual(detail.children.filter(n => n.tagName === 'H4').map(n => n.textContent), ['Testing and science', 'Rules and standards', 'Informing government', 'Ecosystem and adoption']);
  const items = detail.children.filter(n => n.tagName === 'UL').flatMap(n => n.children);
  assert.equal(items.length, 18);
  items.forEach((item, n) => {
    const code = columns[n], f = {...vocab[code], code, value};
    const [label, status, summary] = item.children;
    assert.equal(label.textContent, f.label);
    assert.equal(label.children[0].getAttribute('aria-hidden'), 'true');
    assert.equal(label.children[0].getAttribute('aria-label'), null);
    const statusText = value === 'activity' ? 'active, without an assignment' : names[value].toLowerCase();
    assert.equal(status.textContent, ' · ' + statusText);
    assert.equal(status.querySelector('.qualifier-separator').getAttribute('aria-hidden'), 'true');
    assert.equal(status.querySelector('.qualifier-text').textContent, statusText);
    assert.equal(status.id, 'detail-' + code + '-status');
    assert.equal(label.getAttribute('aria-describedby'), ['activity', 'mandate', 'both'].includes(value) ? status.id : null);
    assert.equal(summary.textContent, f.summary);
    assert.deepEqual(checkItem(item, f), []);
    for (const node of [status, summary]) {
      const text = node.text; node.text = 'Wrong';
      assert.ok(checkItem(item, f).length); node.text = text;
      node.remove(); label.appendChild(node);
      assert.ok(checkItem(item, f).some(e => e.includes('outside its control')));
      node.remove(); item.children = [label, status, summary]; node.parentNode = item;
    }
    if (['activity', 'mandate', 'both'].includes(value)) {
      assert.equal(label.tagName, 'BUTTON');
      for (let repeat = 0; repeat < 2; repeat++) {
        toggle(label);
        const claims = item.querySelector(':scope > .fn-phone-claims');
        assert.equal(label.getAttribute('aria-expanded'), 'true');
        assert.equal(claims.id, label.getAttribute('aria-controls'));
        assert.equal(claims.getAttribute('role'), 'region');
        assert.equal(claims.getAttribute('aria-labelledby'), label.id);
        assert.deepEqual(claims.children.map(n => n.className), ['fn-claim']);
        assert.equal(summary.nextElementSibling, claims);
        assert.deepEqual(checkItem(item, f), []);
        toggle(label);
        assert.equal(label.getAttribute('aria-expanded'), 'false');
        assert.deepEqual(item.children, [label, status, summary]);
      }
    } else {
      assert.equal(label.tagName, 'SPAN');
      assert.equal(label.getAttribute('aria-controls'), null);
    }
  });
}
const spokenText = node => node.getAttribute('aria-hidden') === 'true' ? '' : node.text + node.children.map(spokenText).join('');
const caveat = trailingQualifier('last read 17 Nov 2026', 'reread-tail', 'Source offline since 18 Nov 2026; last read 17 Nov 2026.');
assert.equal(caveat.textContent, ' · last read 17 Nov 2026 Source offline since 18 Nov 2026; last read 17 Nov 2026.');
assert.equal(spokenText(caveat), ' Source offline since 18 Nov 2026; last read 17 Nov 2026.');
const archive = trailingQualifier(el('a', 'archive', 'archived copy'), 'archive-copy');
assert.equal(archive.textContent, ' · archived copy');
assert.equal(spokenText(archive), 'archived copy');
'''
    for marker, value in {"VOCAB": json.dumps(vocab), "HELPERS": helpers, "TOGGLE": toggle, "ITEM_CHECK": item_check, "RENDERING": rendering}.items():
        script = script.replace(marker, value)
    result = subprocess.run(["node", "-e", script], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(("old", "new", "finding"), [
    ('<div class="fn-phone">', '<div class="fn-phone"></div><div class="fn-phone">', "exactly one phone list"),
    ('fn-label-full">Science<', 'fn-label-full">Policy<', "group heads/order"),
    ('<h3>Not yet established</h3>', '<h3>proposed</h3>', "blocks/order"),
    ('data-institute="z"', 'data-institute="a"', "rows/order"),
    ('Zulu, Zulu Institute: show functions', 'Zulu: 2 functions with a public text', "row control/name"),
    ('Zulu, Zulu Institute: show functions', 'Zulu, Zulu Institute: proposed', "status words"),
    ('aria-expanded="false" aria-controls="fn-phone-detail-z"', 'aria-expanded="true" aria-controls="fn-phone-detail-z"', "row control/name"),
    ('aria-controls="fn-phone-detail-a"', 'aria-controls="fn-phone-detail-z"', "controls are not unique"),
    ('</button>', '<span></span></button>', "row button must be empty"),
    ('</button>', 'Zulu</button>', "row button must be empty"),
    ('</button>', ' </button>', "row button must be empty"),
    ('class="fn-phone-country"', 'class="fn-phone-country" tabindex="0"', "other focus targets"),
    ('class="fn-phone-country"', 'class="fn-phone-country" aria-description="Zulu Institute"', "country has a tooltip"),
    ('class="fn-phone-country" aria-hidden="true"', 'class="fn-phone-country"', "decorative phone country"),
    ('class="fn-phone-country" aria-hidden="true"', 'class="fn-phone-country" aria-hidden="false"', "decorative phone country"),
    ('</button>', '</button><a href="#functions">Extra</a>', "other focus targets"),
    ('>Zulu</span>', '>Wrong country</span>', "country label"),
    ('>Zulu</span>', '>Zulu (2)</span>', "counts or status words"),
    ('class="fn-mark fn-not_found fn-pip"', 'class="fn-mark fn-activity fn-pip"', "pip mark differs"),
    ('class="fn-mark fn-activity fn-pip"', 'class="fn-mark fn-both fn-pip"', "pip mark differs"),
    ('class="fn-mark fn-activity fn-pip"', 'class="fn-mark fn-activity fn-mandate fn-pip"', "pip mark differs"),
    ('class="fn-pips" aria-hidden="true"', 'class="fn-pips" aria-hidden="false"', "decorative phone pip group"),
    ('No public text found; it may still be done</li>', 'No public text found</li>', "legend label/mark"),
    ('No public text found; it may still be done</li>', 'It may still be done</li>', "legend label/mark"),
    ('<div class="fn-phone">', '<p class="fn-pip-note"></p><div class="fn-phone">', "obsolete phone pip note"),
    ('<div class="fn-phone">', '<p>One pip per function, in the order of the groups above; tap a row for the names.</p><div class="fn-phone">', "obsolete phone pip note"),
    ('<div class="fn-phone">', '<div class="fn-phone"><div class="fn-phone-detail"></div>', "detail is present"),
    ('<div id="functions-table">', '<div id="functions-table"><p class="fn-blank">Old caveat.</p>', "obsolete blank"),
])
def test_functions_phone_structure_rejects_mutations(functions_phone, old, new, finding):
    data, html = functions_phone
    assert old in html
    assert any(finding in error for error in check_page.functions_phone_structure(html.replace(old, new), data))


@pytest.mark.parametrize("fixture", ["functions_phone", "functions_both"])
@pytest.mark.parametrize(("selector", "finding"), [
    ('.fn-phone-country', "country label differs"),
    ('.fn-pips', "pip groups/order differ"),
])
def test_functions_phone_visuals_must_be_row_siblings(request, fixture, selector, finding):
    """The hydrated and saved forms both reject putting visual content back inside the button."""
    from bs4 import BeautifulSoup

    data, html = request.getfixturevalue(fixture)
    soup = BeautifulSoup(html, "html.parser")
    row = soup.select_one('.fn-phone li[data-institute="z"]')
    row.select_one(':scope > button').append(row.select_one(':scope > ' + selector).extract())
    errors = check_page.functions_phone_structure(str(soup), data)
    assert any("row button must be empty" in error for error in errors)
    assert any(finding in error for error in errors)


@pytest.mark.parametrize(("selector", "change", "finding"), [
    (':scope > .fn-pips', "reverse", "pip count/order"),
    (':scope > .fn-pips', "remove", "pip groups/order"),
    (':scope > .fn-pips', "duplicate", "pip groups/order"),
    (':scope > .fn-pips > .fn-pip', "reverse", "pip mark differs"),
    (':scope > .fn-pips > .fn-pip', "remove", "pip count/order"),
    (':scope > .fn-pips > .fn-pip', "duplicate", "pip count/order"),
])
def test_functions_phone_structure_checks_pip_positions_and_counts(functions_phone, selector, change, finding):
    from bs4 import BeautifulSoup

    data, html = functions_phone
    soup = BeautifulSoup(html, "html.parser")
    row = soup.select_one('li[data-institute="z"]')
    # First two groups, or the two Science pips: their positions and shapes differ in this fixture.
    first, second, *_ = row.select(selector)
    if change == "reverse":
        first.insert_before(second.extract())
    elif change == "remove":
        first.decompose()
    else:
        first.insert_after(copy.deepcopy(first))
    assert any(finding in error for error in check_page.functions_phone_structure(str(soup), data))


@pytest.fixture
def functions_both(functions, functions_phone):
    """The saved page contains both hand-written projections with one shared set of caveats."""
    from bs4 import BeautifulSoup

    data, html = functions
    soup = BeautifulSoup(html, "html.parser")
    phone = BeautifulSoup(functions_phone[1], "html.parser")
    box = soup.select_one('#functions-table')
    for selector in ('.fn-phone', '.fn-legend'):
        box.select_one('.section-foot').insert_before(phone.select_one(selector).extract())
    return data, str(soup)


def test_functions_saved_forms_accept_both_and_scope_legends(functions_both):
    data, html = functions_both
    for check in (check_page.functions_forms_structure, check_page.functions_structure, check_page.functions_phone_structure):
        assert check(html, data) == []


def test_explanation_folds_follow_their_content(functions_both):
    from bs4 import BeautifulSoup

    data, html = functions_both
    soup = BeautifulSoup(html, "html.parser")
    box = soup.select_one('#functions-table')
    box.insert(0, box.select_one('.section-foot').extract())
    assert any(e.startswith("Functions coding fold must follow the table and legends") for e in check_page.functions_structure(str(soup), data))
    page = Path('site/index.html').read_text()
    soup = BeautifulSoup(page, "html.parser")
    # Authored HTML has an empty mount; saved/rendered output has the two chart heads.
    soup.select_one('#fund-rows').append(BeautifulSoup('<div class="fund-list"><div class="fund-axis"></div></div><div class="fund-list"><div class="fund-axis"></div></div>', 'html.parser'))
    assert check_page.explanation_order(str(soup)) == []
    soup.select_one('#fund-rows').insert_before(soup.select_one('#fund-how').extract())
    assert check_page.explanation_order(str(soup)) == ["Funding explanation must follow every row and its comparison head"]


@pytest.mark.parametrize("selector", ['.fn-use.fn-desktop-only', '.fn-use.fn-phone-only'])
def test_function_instructions_stay_in_coding_fold(functions_both, selector):
    from bs4 import BeautifulSoup

    data, html = functions_both
    soup = BeautifulSoup(html, "html.parser")
    soup.select_one('#functions-table').append(soup.select_one(selector).extract())
    assert any("missing from coding fold" in e for e in check_page.functions_forms_structure(str(soup), data))


@pytest.mark.parametrize("selector", ['section[aria-labelledby="functions"]', '.fn-scroll', '.fn-phone'])
@pytest.mark.parametrize(("paragraph", "findings"), [
    ('<p class="fn-empty">No public text found at any institute yet for: foresight and incidents.</p>',
     ["Functions obsolete .fn-empty element is present", "Functions obsolete folded-functions sentence is present"]),
    ('<p class="fn-empty"></p>', ["Functions obsolete .fn-empty element is present"]),
    ('<p>No public text found\n at any institute <span>yet for</span>: foresight and incidents.</p>',
     ["Functions obsolete folded-functions sentence is present"]),
])
def test_functions_structure_rejects_obsolete_empty_paragraph(functions_both, selector, paragraph, findings):
    from bs4 import BeautifulSoup

    data, html = functions_both
    assert check_page.functions_structure(html, data) == []
    soup = BeautifulSoup(html, "html.parser")
    soup.select_one(selector).append(BeautifulSoup(paragraph, "html.parser").p)
    assert check_page.functions_structure(str(soup), data) == findings


@pytest.mark.parametrize(("old", "new", "finding"), [
    ('class="fn-phone"', 'class="missing-phone"', "exactly one saved .fn-phone"),
    ('class="fn-scroll"', 'class="missing-scroll"', "exactly one saved .fn-scroll"),
    ('class="fn-legend fn-phone-only"', 'class="fn-legend"', "exactly one saved .fn-legend.fn-phone-only"),
    ('fn-use fn-phone-only', 'fn-use fn-desktop-only', "phone instructions missing"),
    ('Select a country to see its functions, then a function for its quote and source.', 'Select a mark to see the quote and source.', "phone instructions missing"),
    ('aria-controls="fn-phone-detail-z"', 'aria-controls="detail-z"', "forms share detail controls"),
    ('<div class="fn-phone">', '<div class="fn-phone"><div class="fn-phone-detail"></div>', "detail was saved"),
])
def test_functions_saved_forms_reject_missing_forms_copy_and_shared_controls(functions_both, old, new, finding):
    data, html = functions_both
    assert old in html
    assert any(finding in error for error in check_page.functions_forms_structure(html.replace(old, new), data))


def test_functions_phone_structure_ignores_desktop_detail(functions_both):
    data, html = functions_both
    html = html.replace('<table class="fn-table">', '<div class="fn-detail"><div class="fn-claim">Desktop evidence</div></div><table class="fn-table">')
    assert check_page.functions_phone_structure(html, data) == []


def test_functions_phone_structure_rejects_wrong_block_without_reordering(functions_phone):
    from bs4 import BeautifulSoup

    data, html = functions_phone
    soup = BeautifulSoup(html, "html.parser")
    row = soup.select_one('li[data-institute="z"]').extract()
    soup.select('.fn-phone-block > ul')[1].insert(0, row)
    assert any("block membership" in error for error in check_page.functions_phone_structure(str(soup), data))


def test_functions_phone_missing_function_uses_uncoded_value(functions_phone):
    data, html = functions_phone
    data = copy.deepcopy(data)
    institute = next(i for i in data["institutes"] if i["id"] == "a")
    institute["functions"] = [row for row in institute["functions"] if row["code"] != "advice"]
    assert check_page.functions_phone_structure(html, data) == []


def test_functions_phone_model_keeps_unanimous_not_found_columns_and_groups(functions_phone):
    data, _ = functions_phone
    data = copy.deepcopy(data)
    for institute in data["institutes"]:
        for row in institute["functions"]:
            if row["code"] == "advice":
                row["value"] = "not_found"
    groups = [
        {"name": "Science", "codes": ["testing", "research"]},
        {"name": "Policy", "codes": ["advice", "foresight", "incidents"]},
    ]
    model = check_page.function_phone_model(data)
    assert model["groups"] == groups
    for row in model["rows"]:
        assert [function["code"] for function in row["functions"]] == ["testing", "research", "advice", "foresight", "incidents"]
        assert all(function["value"] == "not_found" for function in row["functions"] if function["group"] == "Policy")
    next(row for row in data["institutes"][0]["functions"] if row["code"] == "advice")["value"] = "not_coded"
    assert check_page.function_phone_model(data)["groups"] == groups


def test_functions_phone_structure_rejects_a_missing_not_found_pip(functions_phone):
    from bs4 import BeautifulSoup

    data, html = functions_phone
    soup = BeautifulSoup(html, "html.parser")
    # The third Policy pip represents incidents, even though every institute is not-found for it.
    for row in soup.select('.fn-phone li[data-institute]'):
        row.select(':scope > .fn-pips')[1].select(':scope > .fn-pip')[2].decompose()
    assert any("phone pip count/order differs" in error for error in check_page.functions_phone_structure(str(soup), data))
