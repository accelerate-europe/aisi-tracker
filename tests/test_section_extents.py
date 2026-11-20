"""Width roles and exact edge mutations, without Chromium or a built site."""

import copy
import json
import re
import subprocess
from pathlib import Path

import pytest
import soupsieve
from bs4 import BeautifulSoup

from scripts import check_page, section_extents as extents


def role_for(markup, selector):
    node = BeautifulSoup(markup, "html.parser").select_one(selector)
    return extents.assign_role([role for role, contract in extents.ROLE_SELECTORS.items()
                                if soupsieve.match(contract, node)])


@pytest.mark.parametrize(("markup", "selector", "role"), [
    ('<main><section><div class="section-body"></div></section></main>', '.section-body', 'content'),
    ('<details class="more fn-how"><p class="running-text">Text</p></details>', 'details', 'fold'),
    ('<details class="more fn-how"><p class="running-text">Text</p></details>', 'p', 'text'),
    ('<div class="section-foot"><details class="srcs"></details></div>', '.section-foot', 'fill'),
    ('<div class="section-foot"><details class="srcs"></details></div>', 'details', 'fill'),
    ('<div class="fn"><p class="fn-summary">Caption</p></div>', 'p', 'fill'),
    ('<div class="fund-detail-body"><div class="fund-record-group"></div></div>', '.fund-record-group', 'fill'),
    ('<div class="fund-missing-line"><ul class="fund-name-list"><li class="fund-name-item"></li></ul></div>', 'div', 'funding-names'),
    ('<div class="fund-missing-line"><ul class="fund-name-list"><li class="fund-name-item"></li></ul></div>', 'ul', 'funding-names'),
    ('<div class="fund-missing-line"><ul class="fund-name-list"><li class="fund-name-item"></li></ul></div>', 'li', 'funding-names'),
    ('<ul class="group-list"><li><span class="group-note running-text">A note</span></li></ul>', 'span', 'intrinsic-text'),
    ('<ul class="srcs"><li class="running-text" data-source-id="source">Source</li></ul>', 'li', 'text'),
    ('<div class="fn-phone-functions"><p class="fn-function-summary running-text">Text</p></div>', 'p', 'function-summary'),
    ('<div class="fn-sources"><details class="quote quote-inline"></details></div>', 'details', 'inline-quote'),
    ('<table class="fn-table"></table>', 'table', 'function-table'),
    ('<div class="unplanned-wrapper" style="max-width:49rem"></div>', 'div', None),
    ('<div class="prose-panel"><div class="unplanned-wrapper"></div></div>', '.unplanned-wrapper', None),
])
def test_role_assignment_is_semantic_and_has_no_ancestor_or_css_fallback(markup, selector, role):
    assert role_for(markup, selector) == role


@pytest.mark.parametrize(("width", "page", "content"), [
    (1000, (20, 980), (20, 980)),
    (1200, (20, 1180), (20, 1096)),
    (1440, (80, 1360), (284, 1360)),
    (2000, (360, 1640), (564, 1640)),
])
def test_column_arithmetic_including_stacked_content_cap(width, page, content):
    assert extents.page_bounds(width) == page
    assert extents.content_bounds(width) == content


@pytest.mark.parametrize(("role", "parent", "options", "want"), [
    ('text', (284, 1360), {}, (284, 1068)),
    ('text', (24, 366), {}, (24, 366)),
    ('fill', (284, 1360), {}, (284, 1360)),
    ('card', (284, 1360), {'index': 1}, (656, 988)),
    ('card', (284, 1360), {'lone': True}, (284, 988)),
    ('card', (284, 1360), {'mini': True}, (284, 1360)),
    ('card', (284, 1360), {'mini': True, 'index': 4}, (284, 1360)),
    ('card-key', (284, 616), {}, (284, 404)),
    ('card-value', (284, 616), {}, (416, 616)),
    ('map-column', (80, 1360), {}, (80, 912)),
    ('map-side', (80, 1360), {}, (944, 1360)),
    ('function-claims', (300, 1344), {}, (324, 1344)),
    ('quote-body', (324, 1108), {}, (340, 1108)),
    ('function-track', (284, 1360), {'track': (500, 700)}, (500, 700)),
])
def test_local_and_text_roles_have_one_expected_edge(role, parent, options, want):
    assert extents.expected_bounds(role, parent, viewport=1440, **options) == want


def node(identity, role, left, right, parent=None, **kwargs):
    if role in {'text', 'intrinsic-text'}:
        kwargs.setdefault('text_right', right)
    return {'id': identity, 'name': identity, 'matches': [role] if role else [],
            'left': left, 'right': right, 'parent': parent, **kwargs}


@pytest.fixture
def snapshot():
    return {'viewport': 1440, 'nodes': [
        node('page', 'page', 80, 1360),
        node('body', 'content', 284, 1360, 'page'),
        node('table', 'fill', 284, 1360, 'body'),
        node('foot', 'fill', 284, 1360, 'body'),
        node('fold', 'fold', 284, 1360, 'foot'),
        node('paragraph', 'text', 284, 1068, 'fold'),
    ]}


def test_aligned_structural_and_text_roles_pass(snapshot):
    assert extents.extent_errors(snapshot) == []


@pytest.mark.parametrize(('role', 'width'), [('text', 784), ('intrinsic-text', 784), ('intrinsic-text', 200)])
def test_text_glyphs_must_fit_their_own_block_inside_a_wider_shell(snapshot, role, width):
    block = snapshot['nodes'][-1]
    block.update(matches=[role], right=block['left'] + width)
    block['text_right'] = block['right'] + 1
    assert extents.extent_errors(snapshot) == []
    # Only the glyph edge changes. It still fits the enclosing 1076px shell.
    block['text_right'] += .01
    assert block['text_right'] < snapshot['nodes'][-2]['right']
    assert any('text runs past its block' in e for e in extents.extent_errors(snapshot))


def test_running_text_under_a_layout_role_is_held_to_its_glyph_edge(snapshot):
    # A function summary carries the running-text class under its own layout role.
    block = snapshot['nodes'][-1]
    checked = [e for e in extents.extent_errors(snapshot | {'nodes': snapshot['nodes'][:-1] + [block | {'text_right': 1200}]})]
    assert any('text runs past its block: right 1200.00, maximum 1068.00' in e for e in checked)
    summary = node('summary', 'function-summary', 300, 1068, 'fold', text_left=300, text_right=1200)
    assert any('summary: text runs past its block' in e for e in extents.extent_errors(snapshot | {'nodes': snapshot['nodes'] + [summary]}))


@pytest.mark.parametrize('role', ['text', 'intrinsic-text'])
@pytest.mark.parametrize('value', [float('nan'), float('inf'), float('-inf')])
def test_nonfinite_text_geometry_fails(snapshot, role, value):
    snapshot['nodes'][-1].update(matches=[role], text_right=value)
    assert any('non-finite text geometry' in e for e in extents.extent_errors(snapshot))


def test_stacked_job_rejects_a_section_body_without_its_content_cap():
    width = 1279  # Last whole CSS pixel below the 1280px label breakpoint.
    page_left, page_right = extents.page_bounds(width)
    left, right = extents.content_bounds(width)
    assert right - left == (80 - 10.75 - 2) * 16 == 1076
    assert page_right - page_left == width - 2 * 1.25 * 16 == 1239
    body = node('body', 'content', left, right, 'section')
    sample = {'viewport': width, 'nodes': [
        node('section', 'page', page_left, page_right), body,
    ]}
    assert extents.extent_errors(sample) == []
    body['right'] = page_right
    assert any('body: content edges' in e for e in extents.extent_errors(sample))
    names = [check_page.job_id(*job) for job in check_page.browser_jobs(Path('dist'), {}, None)]
    assert f'interactions/check_section_extents/{width}/page' in names


def test_fold_at_the_valid_text_edge_is_wrong_for_its_own_role(snapshot):
    snapshot['nodes'][4]['right'] = 1068
    assert any('fold: fold edges' in e for e in extents.extent_errors(snapshot))


def test_narrow_parent_does_not_move_its_descendants_expected_edge(snapshot):
    for block in snapshot['nodes'][1:5]:
        block['right'] = 1200
    errors = extents.extent_errors(snapshot)
    assert any('body: content edges' in e for e in errors)
    assert any('fold: fold edges' in e for e in errors)


def test_unclassified_block_fails_even_when_its_box_is_correct(snapshot):
    snapshot['nodes'].append(node('new wrapper', None, 284, 1360, 'body'))
    assert 'unclassified block: new wrapper' in extents.extent_errors(snapshot)


def test_direct_siblings_are_compared_without_rounding_away_drift(snapshot):
    snapshot['nodes'][2]['right'] -= .75
    snapshot['nodes'][3]['right'] += .75
    assert any('differs from sibling table' in e for e in extents.extent_errors(snapshot))


@pytest.mark.parametrize('value', [float('nan'), float('inf'), float('-inf')])
def test_nonfinite_geometry_fails(snapshot, value):
    snapshot['nodes'][2]['right'] = value
    assert any('non-finite' in e for e in extents.extent_errors(snapshot))


def test_one_css_pixel_is_the_exact_tolerance(snapshot):
    snapshot['nodes'][4]['right'] += 1
    assert extents.extent_errors(snapshot) == []
    snapshot['nodes'][4]['right'] += .01
    assert extents.extent_errors(snapshot)


def test_panel_padding_has_a_named_local_content_edge():
    sample = {'viewport': 1440, 'nodes': [
        node('body', 'content', 284, 1360),
        node('panel', 'function-panel', 284, 1360, 'body'),
        node('shell', 'fill', 300, 1344, 'panel'),
        node('claim', 'text', 300, 1084, 'shell'),
    ]}
    assert extents.extent_errors(sample) == []
    sample['nodes'][2]['right'] = 1084
    assert extents.extent_errors(sample)


def test_intrinsic_exceptions_cannot_exempt_an_enclosing_fold(snapshot):
    sample = copy.deepcopy(snapshot)
    sample['nodes'].append(node('button', 'control', 284, 400, 'fold'))
    assert extents.extent_errors(sample) == []
    sample['nodes'][-1]['right'] = 1362
    assert any('outside its named track' in e for e in extents.extent_errors(sample))


def test_missing_or_incomplete_states_fail_coverage():
    required = {'grid view': 1, 'source folds': 4, 'Funding details': 3}
    assert extents.coverage_errors({'grid view': 1, 'source folds': 4, 'Funding details': 3}, required) == []
    assert len(extents.coverage_errors({'source folds': 2}, required)) == 3


def test_geometry_job_replaces_panel_width_enforcement_once():
    names = [check_page.job_id(*job) for job in check_page.browser_jobs(Path('dist'), {}, None)]
    expected = {f'interactions/check_section_extents/{width}/{group}'
                for width in extents.WIDTHS for group in extents.EXTENT_GROUPS}
    expected.add('interactions/check_section_extents/1279/page')
    actual = [name for name in names if name.startswith('interactions/check_section_extents')]
    assert set(actual) == expected
    assert len(actual) == len(expected) == 13
    assert 'interactions/check_panel_measure' not in names


def test_split_jobs_keep_all_mandatory_block_kinds():
    required = {}
    for group in extents.EXTENT_GROUPS:
        required.update(extents.required_blocks(group))
    assert required == {kind: minimum for kind, (_, minimum) in extents.BLOCK_KINDS.items()}
    assert all(extents.required_blocks(group) for group in extents.EXTENT_GROUPS)


def test_missing_scoped_state_cannot_measure_an_unrelated_page():
    script = 'const document={querySelectorAll:()=>[],querySelector:()=>null};console.log(JSON.stringify((' + extents.SECTION_EXTENTS_JS + ')({selectors:{},scope:".missing",kinds:{}})));'
    result = subprocess.run(['node', '-e', script], check=True, capture_output=True, text=True)
    assert json.loads(result.stdout) == {'missing': '.missing'}


@pytest.mark.parametrize(('width', 'group'), [(1000, group) for group in extents.EXTENT_GROUPS] + [(1279, 'page')])
def test_each_job_fails_missing_states_reuses_its_page_and_reports_progress(width, group, monkeypatch, capsys):
    import sys

    class Locator:
        first = property(lambda self: self)

        def count(self):
            return 0

        def fill(self, value):
            pass

        def press(self, key):
            pass

        def evaluate_all(self, script):
            return []

    class Page:
        closed = False

        def locator(self, selector):
            return Locator()

        def add_style_tag(self, **kwargs):
            pass

        def close(self):
            self.closed = True

        def evaluate(self, script, arg=None):
            if script == extents.OPEN_FOLDS_JS:
                return {'expected': {}, 'counts': {}, 'remaining': []}
            if script == extents.SECTION_EXTENTS_JS:
                if arg['scope']:
                    return {'missing': arg['scope']}
                return {'viewport': width, 'nodes': [], 'counts': {}, 'coverage': {}}

    class Browser:
        opened = 0

        def new_page(self, viewport):
            assert viewport['width'] == width
            self.opened += 1
            self.page = Page()
            return self.page

    monkeypatch.setitem(sys.modules, 'check_page', check_page)
    hydrated = []
    monkeypatch.setattr(check_page, 'hydrated', lambda *a, **k: hydrated.append(a[0]))
    monkeypatch.setattr(check_page, 'layout_settled', lambda *a, **k: None)
    browser = Browser()
    errors = extents.check_section_extents(browser, 'unused', width, group)
    assert browser.opened == len(hydrated) == 1
    assert browser.page.closed
    state = 'map record' if group == 'page' else 'Funding details' if group == 'funding' else 'grouped evidence' if group == 'grouped' else 'grid evidence'
    assert any(f'{state}: inspected 0 of 1 required states' in error for error in errors)
    assert all(any(f'{kind}: inspected 0 of {minimum} required states' in error for error in errors)
               for kind, minimum in extents.required_blocks(group).items())
    assert f'{width}px {group} section extents:' in capsys.readouterr().err


def test_browser_collectors_parse_in_node_without_a_browser():
    expressions = [extents.SECTION_EXTENTS_JS, extents.OPEN_FOLDS_JS]
    subprocess.run(['node', '-e', 'for (const source of ' + json.dumps(expressions) + ') new Function("return (" + source + ")");'], check=True, capture_output=True, text=True)


def test_absent_structural_kind_cannot_pass_silently():
    required = {kind: minimum for kind, (_, minimum) in extents.BLOCK_KINDS.items()}
    assert extents.coverage_errors(required, required) == []
    del required['grouped Functions']
    assert extents.coverage_errors(required, {'grouped Functions': 1})


def test_reference_tracks_must_span_the_assigned_owner(snapshot):
    snapshot['track_groups'] = [{'name': 'chart', 'owner': 'body', 'gap': 8,
                                'tracks': [(284, 400), (408, 1000), (1008, 1360)]}]
    assert extents.extent_errors(snapshot) == []
    snapshot['track_groups'][0]['tracks'][-1] = (1008, 1340)
    assert any('do not span' in e for e in extents.extent_errors(snapshot))


def run_js(source):
    result = subprocess.run(['node', '-e', source], check=True, capture_output=True, text=True)
    return json.loads(result.stdout)


@pytest.mark.parametrize('role', ['text', 'intrinsic-text'])
@pytest.mark.parametrize(('clipping', 'right', 'overflows'), [
    ('none', 1090, True), ('owner', 284, False),
    ('inline', 1090, True), ('visible', 1090, True),  # an inline span with the ellipsis properties clips nothing
])
def test_collector_measures_inline_glyphs_but_excludes_hidden_and_ellipsised_text(role, clipping, right, overflows):
    # Text rectangles deliberately differ from element rectangles. Inline text
    # is absent from the block inventory but must still reach the glyph oracle.
    script = r'''
const {role,clipping}=CONFIG, innerWidth=1440, NodeFilter={SHOW_TEXT:4};
const document={documentElement:{font:true}};
const getComputedStyle=n=>n.font?{fontSize:'16px'}:{display:'block',overflowX:'visible',...n.style};
function element(tag, role, left, right, children=[], style={}, classes=[]) {
  const n={tagName:tag.toUpperCase(),classList:classes,role,children,style,
    getClientRects:()=>[{}],checkVisibility:()=>style.visibility!=='hidden',
    getBoundingClientRect:()=>({left,right}), matches:s=>s===role,
    querySelectorAll:s=>s==='*'?children.filter(c=>!c.data).flatMap(c=>[c,...c.querySelectorAll('*')]):[],
    closest:s=>{
      for(let p=n;p;p=p.parentElement) if(s.includes('.vh') && p.classList.includes('vh')) return p;
      return null;
    }};
  children.forEach(c=>c.parentElement=n);
  return n;
}
const text=(right, data='Words')=>({data,rects:[{right:900,width:100,height:18},{right,width:100,height:18}]});
const inlineStyle=clipping==='inline'?{textOverflow:'ellipsis',overflowX:'hidden'}:
  clipping==='visible'?{textOverflow:'ellipsis',overflowX:'visible'}:{};
const overflow=element('span','',284,1068,[element('a','',284,1068,[text(1090)],{display:'inline'})],
  {display:'inline',...inlineStyle});
const ellipsis=element('span','',284,1068,[element('a','',284,1068,[text(1800)],{display:'inline'})],
  {display:'inline-block',textOverflow:'ellipsis',overflowX:'hidden'});
const hidden=element('span','',284,1068,[text(1900)],{display:'inline',visibility:'hidden'});
const vh=element('span','',284,1068,[text(2000)],{display:'inline'},['vh']);
const empty=text(2100,'   '), zero=text(2200);
zero.rects=[{right:2200,width:0,height:18},{right:2200,width:100,height:0}];
const owner=element('p',role,284,1068,[text(1068),overflow,ellipsis,hidden,vh,empty,zero],
  clipping==='owner'?{textOverflow:'ellipsis',overflowX:'hidden'}:{});
const body=element('div','content',284,1360,[owner]);
document.querySelectorAll=()=>[body];
const texts=n=>n.data?[n]:(n.children||[]).flatMap(texts);
document.createTreeWalker=(n,filter)=>{
  if(filter!==NodeFilter.SHOW_TEXT) throw new Error('expected text nodes');
  const found=texts(n);let i=0;return {nextNode:()=>found[i++]};
};
document.createRange=()=>{
  let selected;return {selectNodeContents:n=>{selected=n;},getClientRects:()=>selected.rects};
};
console.log(JSON.stringify((COLLECTOR)({selectors:{content:'content',[role]:role},scope:null,kinds:{}})));
'''.replace('CONFIG', json.dumps({'role': role, 'clipping': clipping})).replace('COLLECTOR', extents.SECTION_EXTENTS_JS)
    sample = run_js(script)
    assert len(sample['nodes']) == 2
    assert sample['nodes'][1]['text_right'] == right
    errors = extents.extent_errors(sample)
    if overflows:
        assert len(errors) == 1 and 'text runs past its block' in errors[0]
    else:
        assert errors == []


@pytest.mark.parametrize(('style', 'parent', 'named', 'want'), [
    *[({'display': display}, {}, False, False) for display in (
        'inline', 'inline-block', 'inline-flex', 'inline-grid', 'inline-table',
        'inline flow-root', 'ruby', 'block ruby', 'ruby-base', 'ruby-text',
        'table-column', 'table-column-group', 'none', 'contents',
    )],
    *[({'display': display}, {}, False, True) for display in (
        'block', 'flow-root', 'flex', 'grid', 'list-item', 'table',
        'table-caption', 'table-row', 'table-cell', 'table-row-group',
    )],
    # These are computed, blockified displays: a mark/letter/label does not fill
    # its row just because the browser reports block or flex as its display.
    ({'display': 'block', 'flexGrow': '0'}, {'display': 'flex', 'flexDirection': 'row'}, False, False),
    ({'display': 'flex', 'flexGrow': '0'}, {'display': 'inline-flex', 'flexDirection': 'row'}, False, False),
    ({'display': 'block', 'flexGrow': '1'}, {'display': 'flex', 'flexDirection': 'row'}, False, True),
    ({'display': 'block', 'alignSelf': 'auto'}, {'display': 'flex', 'flexDirection': 'column', 'alignItems': 'stretch'}, False, True),
    ({'display': 'block', 'alignSelf': 'start'}, {'display': 'flex', 'flexDirection': 'column', 'alignItems': 'stretch'}, False, False),
    ({'display': 'block', 'justifySelf': 'auto'}, {'display': 'grid', 'justifyItems': 'normal'}, False, True),
    ({'display': 'block', 'justifySelf': 'auto'}, {'display': 'inline-grid', 'justifyItems': 'center'}, False, False),
    ({'display': 'block', 'justifySelf': 'start'}, {'display': 'grid', 'justifyItems': 'stretch'}, False, False),
    ({'display': 'block', 'flexGrow': '0'}, {'display': 'flex'}, True, True),
    ({'display': 'flex', 'position': 'absolute'}, {}, True, True),
    ({'display': 'block', 'position': 'absolute'}, {}, False, False),
])
def test_formatting_participation_not_blockified_display_selects_column_boxes(style, parent, named, want):
    args = json.dumps([style, parent, named])
    assert run_js('console.log(JSON.stringify((' + extents.BLOCK_PARTICIPATION_JS + ')(...' + args + ')));') is want


@pytest.mark.parametrize('width', extents.WIDTHS)
def test_unknown_wrapper_reports_itself_without_resetting_the_content_column(width):
    left, right = extents.content_bounds(width)
    sample = {'viewport': width, 'nodes': [
        node('body', 'content', left, right),
        node('unknown', None, left, right, 'body'),
        node('list', 'funding-names', left, right, 'unknown'),
    ]}
    assert extents.extent_errors(sample) == ['unclassified block: unknown']


@pytest.mark.parametrize('width', extents.WIDTHS)
def test_missing_funding_names_keep_their_section_shell_through_opened_evidence(width):
    left, right = extents.content_bounds(width)
    inner, end = left + 16, right - 16
    sample = {'viewport': width, 'nodes': [
        node('body', 'content', left, right),
        node('missing line', 'funding-names', left, right, 'body'),
        node('toggle', 'fill', left, right, 'missing line'),
        node('list', 'funding-names', left, right, 'missing line'),
        node('item', 'funding-names', left, right, 'list'),
        node('panel', 'funding-panel', left, right, 'item'),
        node('summary', 'fill', inner, end, 'panel', opened_summary=True),
        node('body shell', 'fill', inner, end, 'panel'),
        node('heading', 'heading', inner, end, 'body shell'),
        node('source list', 'fill', inner, end, 'body shell'),
        node('prose', 'text', inner, inner + 784, 'source list'),
        node('value line', 'fill', inner, inner + 784, 'prose'),
        node('source caveat', 'intrinsic', inner, inner + 770, 'prose'),
    ]}
    assert extents.extent_errors(sample) == []
    sample['nodes'][3]['left'] = extents.page_bounds(width)[0] - 2
    assert any('list: funding-names' in e for e in extents.extent_errors(sample))


@pytest.mark.parametrize(('width', 'label_right'), [(1000, 162.83), (1440, 252), (2000, 532)])
def test_intrinsic_headings_anchor_left_and_do_not_compete_with_full_width_siblings(width, label_right):
    page_left, page_right = extents.page_bounds(width)
    left, right = extents.content_bounds(width)
    sample = {'viewport': width, 'nodes': [
        node('section', 'page', page_left, page_right),
        node('label', 'section-label', page_left, label_right, 'section'),
        node('h2', 'heading', page_left, label_right, 'label'),
        node('body', 'content', left, right, 'section'),
        node('h3', 'heading', left, left + 69.3, 'body'),
    ]}
    assert extents.extent_errors(sample) == []
    sample['nodes'][-1]['left'] += 2
    assert any('h3: heading left' in e for e in extents.extent_errors(sample))
    sample['nodes'][-1].update(left=left, right=right + 2)
    assert any('maximum' in e for e in extents.extent_errors(sample))


@pytest.mark.parametrize(('width', 'old_left'), [(1000, 169.08), (1440, 447.23), (2000, 727.34)])
def test_function_summary_uses_the_name_text_edge_and_rejects_the_reported_flex_shift(width, old_left):
    left, right = extents.content_bounds(width)
    inner = left + 16
    summary = node('summary', 'function-summary', inner, inner + 784, 'row', text_left=inner + 24)
    sample = {'viewport': width, 'nodes': [
        node('body', 'content', left, right),
        node('panel', 'function-panel', left, right, 'body'),
        node('row', 'fill', inner, right - 16, 'panel'),
        summary,
    ]}
    assert extents.extent_errors(sample) == []
    summary['right'] = inner + 300  # It owes a maximum measure, not a fixed width.
    assert extents.extent_errors(sample) == []
    summary.update(left=old_left, right=old_left + 784)
    assert any('function-summary left' in e for e in extents.extent_errors(sample))
    summary.update(left=inner, right=inner + 786)
    assert extents.extent_errors(sample)
    summary.update(left=inner, right=right)
    assert any('outside its panel' in e for e in extents.extent_errors(sample))
    summary.update(left=inner - 2, right=inner + 100, text_left=inner + 22)
    assert any('outside its panel' in e for e in extents.extent_errors(sample))
    summary.update(left=inner, right=inner + 300, text_left=None)
    assert any('missing function name' in e for e in extents.extent_errors(sample))


def test_summary_reserves_a_full_flex_line_without_widening_its_text():
    source = (Path(__file__).resolve().parents[1] / 'site/index.html').read_text()
    rule = re.search(r'\.fn-function-summary \{([^}]+)\}', source)[1]
    assert 'flex-basis: 100%' in rule
    assert 'margin-inline-end: max(0px, calc(100% - var(--text-width)))' in rule
    assert 'padding-left: var(--fn-text-inset)' in rule
    assert '.running-text { max-width: var(--text-width); }' in source


@pytest.mark.parametrize('width', extents.WIDTHS)
def test_function_table_must_end_at_content_edge_even_if_oversizing_makes_it_scroll(width):
    left, right = extents.content_bounds(width)
    table = node('table', 'function-table', left, right, 'body', overflow_width=right - left + 8)
    sample = {'viewport': width, 'nodes': [node('body', 'content', left, right), table]}
    assert extents.extent_errors(sample) == []
    table['right'] += 8
    assert any('function-table edges' in e for e in extents.extent_errors(sample))


@pytest.mark.parametrize('width', extents.WIDTHS)
@pytest.mark.parametrize(('table_role', 'overflow'), [('function-table', 0), ('table', 0), ('table', 160)])
def test_collector_uses_table_box_not_painted_overflow_and_a_separate_summary_reference(width, table_role, overflow):
    left, right = extents.content_bounds(width)
    # A small DOM double exercises the collector, including its table and name
    # references. No browser, layout implementation or built page is involved.
    script = r'''
const {width,left,right,tableRole,overflow}=CONFIG;
const innerWidth=width, document={documentElement:{font:true}};
const getComputedStyle=n=>n.font?{fontSize:'16px'}:{display:'block',...n.style};
function element(tag, role, left, right, children=[], classes=[]) {
  classes.contains=c=>classes.includes(c);
  const n={tagName:tag.toUpperCase(),classList:classes,role,left,right,children,
    style:{},getClientRects:()=>[{}],checkVisibility:()=>true,
    getBoundingClientRect:()=>({left,right}),
    matches:s=>s===role || (s==='.fn-phone-detail' && role==='function-panel'),
    querySelectorAll:s=>s==='*'?children.flatMap(c=>[c,...c.querySelectorAll('*')]):[],
    closest:s=>s==='.fn-scroll, .scroll'?scroller:s==='.fund-list'?chart:null,
    querySelector:s=>s===':scope > .fn-function-label > span:not(.fn-mark)'?nameText:null};
  children.forEach(c=>c.parentElement=n);
  return n;
}
const table=element('table',tableRole,left,right+overflow);
// The decorative tint extends 8px past the table, only while selected.
table.scrollWidth=right-left+overflow+8;
const scroller=element('div','fill',left,right,[table],overflow?[]:['fits']);
scroller.clientWidth=right-left;
const nameText=element('span','',left+40,left+200);
const summary=element('p','function-summary',left+16,left+800);
const panel=element('div','function-panel',left,right,[summary]);
const names=element('div','funding-names',left,right);
const chart=element('div','fill',left,right,[names]);
// Names share a populated chart's shell, but are not a chart track. Dispatch
// by a funding- prefix would try to look up a nonexistent funding-names track.
chart.querySelector=()=>({querySelector:()=>nameText});
const body=element('div','content',left,right,[scroller,panel,chart]);
document.querySelectorAll=()=>[body];
const selectors=Object.fromEntries(['content','fill',tableRole,'function-panel','function-summary','funding-names'].map(s=>[s,s]));
console.log(JSON.stringify((COLLECTOR)({selectors,scope:null,kinds:{}})));
'''.replace('CONFIG', json.dumps({'width': width, 'left': left, 'right': right, 'tableRole': table_role, 'overflow': overflow})).replace('COLLECTOR', extents.SECTION_EXTENTS_JS)
    sample = run_js(script)
    table = next(n for n in sample['nodes'] if n['matches'] == [table_role])
    summary = next(n for n in sample['nodes'] if n['matches'] == ['function-summary'])
    assert table.get('overflow_width', right - left) == right - left + overflow
    assert summary['text_left'] == left + 40
    assert extents.extent_errors(sample) == []
    if not overflow:
        table['right'] += 8
        assert any('table edges' in e for e in extents.extent_errors(sample))
    else:
        table['left'] += 2
        assert any('table edges' in e for e in extents.extent_errors(sample))
