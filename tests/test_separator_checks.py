import json
import subprocess
from pathlib import Path

import pytest
import soupsieve
from bs4 import BeautifulSoup

from scripts.separator_checks import PAGE_SEPARATORS_JS, SEPARATOR_LINES, separator_coverage_errors


def test_missing_separator_kind_fails_even_when_another_kind_has_many_lines():
    counts = {'sources': {'lines': 100, 'separators': 200}}
    assert separator_coverage_errors(counts, ('sources', 'news')) == ['no separator coverage: news']
    assert separator_coverage_errors({'news': {'lines': 0, 'separators': 2}}, ('news',))


@pytest.mark.parametrize('raw, leading, classified, expected', [
    (False, False, True, []),
    (False, True, True, ['begins a line']),
    (True, False, True, ['unbound separator']),
    (True, True, True, ['unbound separator', 'begins a line']),
    (False, False, False, ['unclassified separator']),
])
def test_separator_oracle_uses_glyph_positions_and_catches_raw_dots(raw, leading, classified, expected):
    script = r"""
const raw=RAW, leading=LEADING, classified=CLASSIFIED;
const line={textContent:'word · tail',checkVisibility:()=>true,getClientRects:()=>[{}],closest:()=>null};
const parent={textContent:'word · tail',outerHTML:'<span>word · tail</span>',checkVisibility:()=>true,getClientRects:()=>[{}],
 closest:s=>s==='.line'?(classified?line:null):s==='.meta-dot, .qualifier-separator'&&!raw?{}:null};
const node={data:'word · tail',length:11,parentElement:parent};
const document={body:{},querySelectorAll:()=>[line],
 createTreeWalker:()=>{let done=false;return {currentNode:node,nextNode(){if(done)return false;done=true;return true;}};},
 createRange:()=>{let i;return {setStart(n,k){i=k;},setEnd(){},getBoundingClientRect(){
   const second=leading&&i>=5;const x=second?(i-5)*6:i*6;
   return {left:x,width:5,height:16,top:second?20:0,bottom:second?36:16};}};}};
const NodeFilter={SHOW_TEXT:4};
console.log(JSON.stringify((ORACLE)({news:'.line'})));
""".replace('RAW', json.dumps(raw)).replace('LEADING', json.dumps(leading)).replace('CLASSIFIED', json.dumps(classified)).replace('ORACLE', PAGE_SEPARATORS_JS)
    result = subprocess.run(['node', '-'], input=script, text=True, capture_output=True, check=True)
    findings = json.loads(result.stdout)['errors']
    assert len(findings) == len(expected)
    assert all(any(fragment in error for error in findings) for fragment in expected)


@pytest.mark.parametrize('qualifier', ['budget-qualifiers qualifiers', 'qualifiers'])
def test_worldwide_budget_and_staff_qualifiers_have_a_kind(qualifier):
    soup = BeautifulSoup(f'<table class="world"><tr><td><span class="{qualifier}"><span class="meta-dot"> ·</span></span></td></tr></table>', 'html.parser')
    dot = soup.select_one('.meta-dot')
    kinds = [kind for kind, selector in SEPARATOR_LINES.items()
             if soupsieve.closest(selector, dot)]
    assert kinds == ['worldwide qualifiers']


def test_error_identity_keeps_the_complete_line_and_width():
    """The same finding across states is one line; a different suffix/width survives."""
    from scripts import check_page

    # Exercise the runner with repeated state snapshots, without loading a page.
    class Locator:
        first = property(lambda self: self)

        def count(self):
            return 0

        def fill(self, value):
            pass

        def press(self, key):
            pass

        def click(self):
            pass

        def evaluate_all(self, script):
            pass

    class Page:
        def __init__(self, width):
            self.width = width

        def locator(self, selector):
            return Locator()

        def add_style_tag(self, **kwargs):
            pass

        def close(self):
            pass

        def evaluate(self, script, arg=None):
            if script == PAGE_SEPARATORS_JS:
                return {'errors': ['sources: visible separator begins a line: ' + 'x' * 110 + tail for tail in ('A', 'B')],
                        'counts': {kind: {'lines': 1, 'separators': 1} for kind in (*SEPARATOR_LINES, 're-read tails', 'archived-copy tails')}}
            if script == check_page.SOURCE_SEPARATORS_JS:
                return [{'source': 'sample', 'leading': 0, 'separators': 1}]

    class Browser:
        def new_page(self, viewport):
            return Page(viewport['width'])

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(check_page, 'hydrated', lambda *a, **k: None)
        patch.setattr(check_page, 'layout_settled', lambda *a, **k: None)
        errors = check_page.check_page_separators(Browser(), 'unused')
    findings = [e for e in errors if 'visible separator begins' in e]
    assert len(findings) == 8
    assert len(set(findings)) == 8


def test_page_script_parses_without_a_browser():
    soup = BeautifulSoup((Path(__file__).resolve().parents[1] / 'site/index.html').read_text(), 'html.parser')
    scripts = [script.string for script in soup.select('script:not([src])') if script.get('type') != 'application/ld+json']
    subprocess.run(['node', '-'], input='for (const script of ' + json.dumps(scripts) + ') new Function(script);', check=True, capture_output=True, text=True)


def test_nested_toggles_refresh_all_qualifiers_after_lazy_handlers():
    source = (Path(__file__).resolve().parents[1] / 'site/index.html').read_text()
    start = source.index('    var qualifierFrame = ')
    code = source[start:source.index('    document.addEventListener("contentvisibilityautostatechange"', start)]
    script = r"""
const assert=require('node:assert/strict'), handlers={}, frames=[];
let lazy=false, refreshed=0;
const document={addEventListener:(type,callback,capture)=>{assert.equal(capture,true);handlers[type]=callback;}};
const requestAnimationFrame=callback=>{frames.push(callback);return frames.length;};
const refreshQualifiers=()=>{assert.ok(lazy);refreshed++;};
CODE
handlers.toggle({target:{open:true}});handlers.toggle({target:{open:true}});
assert.equal(frames.length,1);assert.equal(refreshed,0);
lazy=true;frames.shift()();assert.equal(refreshed,1);
handlers.toggle({target:{open:false}});frames.shift()();assert.equal(refreshed,2);
""".replace('CODE', code)
    subprocess.run(['node', '-'], input=script, check=True, capture_output=True, text=True)
