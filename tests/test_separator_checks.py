import json
import subprocess
from pathlib import Path

import pytest
import soupsieve
from bs4 import BeautifulSoup

from scripts.separator_checks import PAGE_SEPARATORS_JS, SEPARATOR_LINES, separator_coverage_errors


def test_layout_pass_reaches_sibling_flex_items_and_restores_dots():
    source = (Path(__file__).resolve().parents[1] / 'site/index.html').read_text()
    block = source[source.index('  function blockOf('):source.index('  // Only the separator')]
    layout = source[source.index('  function hideLineEndDots('):source.index('  // Each item is one unbreakable box')]
    script = r"""
const assert=require('node:assert/strict');
const rect=(top,left=0,width=8)=>({top,bottom:top+24,left,right:left+width,width,height:24});
let offset=0, wrapped=false, zero=false, skipped=false;
function element(parent,display) {
  return {parentElement:parent,display,children:[],
    checkVisibility:()=>!skipped, getClientRects:()=>[{}], closest(){return null;}};
}
const row=element(null,'inline-flex'), items=[element(row,'block'),element(row,'block'),element(row,'block')];
row.children=items;
const dots=items.slice(0,2).map(item=>{
  const tail=element(item,'inline'),dot=element(tail,'inline');
  item.children=[tail]; tail.children=[dot];
  dot.closest=s=>s==='.meta-item'?item:null;
  dot.marked=false;dot.classList={toggle:(name,value)=>{dot.marked=value;}};
  dot.getBoundingClientRect=()=>zero?{width:0,height:0}:rect(offset,40);
  const text={data:' ·',parentElement:dot};dot.lastChild=text;dot.children=[text];
  return dot;
});
const after=items.slice(1).map((item,i)=>({data:i?'Funding':'Testing',parentElement:item}));
items[1].children.unshift(after[0]);items[2].children=[after[1]];
const flatten=n=>n.data?[n]:(n.children||[]).flatMap(flatten);
const document={body:row,createTreeWalker(root){
  const texts=flatten(root);let current;
  return {get currentNode(){return current;},set currentNode(n){current=n;},
    nextNode(){current=texts[texts.indexOf(current)+1];return current;}};
},createRange(){let node;return {setStart(n){node=n;},setEnd(){},getBoundingClientRect(){
  return zero?{width:0,height:0}:rect(offset+(wrapped&&node===after[1]?21.6:0),60);
}};}};
const NodeFilter={SHOW_TEXT:4},getComputedStyle=n=>({display:n.display,visibility:'visible'});
const root={querySelectorAll:()=>dots};
CODE
for(const y of [0,150,-120]) { // normal, sticky/translated and scrolled coordinates
  offset=y;wrapped=false;hideLineEndDots(root);
  assert.deepEqual(dots.map(d=>d.marked),[false,false],'same-line flex items keep their dots');
  wrapped=true;hideLineEndDots(root);
  assert.deepEqual(dots.map(d=>d.marked),[false,true],'only the pair crossing a line loses its dot');
  wrapped=false;hideLineEndDots(root);
  assert.deepEqual(dots.map(d=>d.marked),[false,false],'widening restores the dot');
}
zero=true;hideLineEndDots(root);assert.deepEqual(dots.map(d=>d.marked),[false,false]);
zero=false;skipped=true;wrapped=true;hideLineEndDots(root);assert.deepEqual(dots.map(d=>d.marked),[false,false]);
""".replace('CODE', block + layout)
    result = subprocess.run(['node', '-'], input=script, text=True, capture_output=True)
    assert result.returncode == 0, result.stderr


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
 closest:s=>s==='.line'?(classified?line:null):s==='.meta-dot, .qualifier-separator'&&!raw?{matches:()=>true}:null};
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


def separator_oracle(rows):
    """Supply independent text geometry, including dots with no painted rectangle."""
    script = r"""
const kinds=KINDS, specs=ROWS, NodeFilter={SHOW_TEXT:4};
function element(parent,options={}) {
  const n={parentElement:parent,children:[],...options};
  if(parent) parent.children.push(n);
  n.checkVisibility=()=>!n.hidden && (!parent || parent.checkVisibility());
  n.getClientRects=()=>n.absent?[]:[{}];
  n.matches=s=>s==='.meta-dot'&&n.marker==='meta';
  n.closest=s=>{
    for(let p=n;p;p=p.parentElement) {
      if(p.kind && kinds[p.kind]===s) return p;
      if(s==='.meta-dot, .qualifier-separator' && p.marker) return p;
    }
    return null;
  };
  n.querySelectorAll=()=>n.children.filter(c=>c.item);
  return n;
}
function text(parent,data,x,y,height=24) {
  const t={data,length:data.length,parentElement:parent,x,y,height};parent.children.push(t);return t;
}
const body=element(null),lines=[];
for(const spec of specs) {
  const line=element(body,{kind:spec.kind,textContent:spec.label||'word · tail'});lines.push(line);
  const words=spec.positions||[0,0];
  words.forEach((y,i)=>{
    const item=element(line,{item:true});
    text(item,i?'tail':'word',i*100,y,spec.heights?.[i]||24);
    if(i<words.length-1 && spec.mode!=='removed') {
      const dot=element(item,{marker:spec.qualifier?'qualifier':'meta',hidden:spec.mode!=='visible',absent:spec.mode==='display-none'});
      text(dot,'·',i*100+50,spec.leading?y+30:y);
    }
  });
}
const flatten=n=>n.data!==undefined?[n]:n.children.flatMap(flatten);
const document={body,querySelectorAll:s=>lines.filter(n=>kinds[n.kind]===s),
  createTreeWalker(root){const nodes=flatten(root);let i=-1;return {currentNode:null,nextNode(){this.currentNode=nodes[++i];return this.currentNode;}};},
  createRange(){let node,i;return {setStart(n,k){node=n;i=k;},setEnd(){},getBoundingClientRect(){
    const zero=node.parentElement.absent;
    return {left:node.x+i*6,top:node.y,bottom:node.y+node.height,width:zero?0:5,height:zero?0:node.height};
  }};}
};
console.log(JSON.stringify((ORACLE)(kinds)));
""".replace('KINDS', json.dumps(SEPARATOR_LINES)).replace('ROWS', json.dumps(rows)).replace('ORACLE', PAGE_SEPARATORS_JS)
    result = subprocess.run(['node', '-'], input=script, text=True, capture_output=True, check=True)
    return json.loads(result.stdout)


@pytest.mark.parametrize('kind', SEPARATOR_LINES)
@pytest.mark.parametrize('mode', ['hidden', 'display-none'])
def test_every_kind_rejects_a_hidden_midline_separator(kind, mode):
    result = separator_oracle([dict(kind=kind, mode=mode)])
    assert any(f'{kind}: separator hidden mid-line:' in e for e in result['errors'])
    assert result['counts'][kind]['visible'] == 0


@pytest.mark.parametrize('mode, positions', [
    ('visible', [0, 0]), ('hidden', [0, 21.6]), ('display-none', [0, 21.6]),
])
def test_oracle_accepts_same_line_dots_and_hidden_wrapped_dots(mode, positions):
    rows = [dict(kind=kind, mode=mode, positions=positions) for kind in SEPARATOR_LINES]
    assert separator_oracle(rows)['errors'] == []


@pytest.mark.parametrize('kind', ['group navigation', 'functions switch', 'hero update', 'footer'])
@pytest.mark.parametrize('mode', ['hidden', 'display-none', 'removed'])
def test_defined_rows_need_visible_dots_even_when_another_row_has_them(kind, mode):
    result = separator_oracle([
        dict(kind=kind, mode='visible', label='healthy row'),
        dict(kind=kind, mode=mode, label='broken row'),
    ])
    assert f'{kind}: zero visible separators between items sharing a line: broken row' in result['errors']
    assert not any('healthy row' in e for e in result['errors'])


def test_wrapped_rows_need_no_dots_but_a_remaining_same_line_pair_does():
    assert separator_oracle([dict(kind='group navigation', mode='removed', positions=[0, 24, 48])])['errors'] == []
    result = separator_oracle([dict(kind='group navigation', mode='removed', positions=[0, 24, 24])])
    assert any('zero visible separators' in e for e in result['errors'])


def test_footer_registry_excludes_plain_licence_note_even_after_dots_are_removed():
    soup = BeautifulSoup('''<footer id="foot">
      <p><span class="meta-item">Publisher<span class="meta-dot"> ·</span></span>
         <span class="meta-item"><a class="foot-feed">Follow updates</a></span></p>
      <p>Quotations remain with their rights holders.</p>
    </footer>''', 'html.parser')
    line = soup.select_one('#foot > p')
    assert soup.select(SEPARATOR_LINES['footer']) == [line]
    line.select_one('.meta-dot').decompose()
    assert soup.select(SEPARATOR_LINES['footer']) == [line]


def test_oracle_checks_both_sides_and_uses_neighbour_ink_for_qualifiers():
    assert any('begins a line' in e for e in separator_oracle([dict(kind='sources', mode='visible', leading=True)])['errors'])
    assert any('ends a line' in e for e in separator_oracle([dict(kind='sources', mode='visible', positions=[0, 24])])['errors'])
    assert separator_oracle([dict(kind='function status', mode='display-none', qualifier=True, positions=[0, 24])])['errors'] == []
    assert any('hidden mid-line' in e for e in separator_oracle([dict(kind='function status', mode='display-none', qualifier=True)])['errors'])
    # Mixed font sizes and translated coordinates on one baseline are still one line.
    assert separator_oracle([dict(kind='sources', mode='visible', positions=[-100, -94], heights=[24, 18])])['errors'] == []


@pytest.mark.parametrize('qualifier', ['budget-qualifiers qualifiers', 'qualifiers'])
def test_worldwide_budget_and_staff_qualifiers_have_a_kind(qualifier):
    soup = BeautifulSoup(f'<table class="world"><tr><td><span class="{qualifier}"><span class="meta-dot"> ·</span></span></td></tr></table>', 'html.parser')
    dot = soup.select_one('.meta-dot')
    kinds = [kind for kind, selector in SEPARATOR_LINES.items()
             if soupsieve.closest(selector, dot)]
    assert kinds == ['worldwide qualifiers']


@pytest.mark.parametrize('finding', ['sources: visible separator begins a line: ',
                                    'news labels: separator hidden mid-line: ',
                                    'group navigation: zero visible separators between items sharing a line: '])
def test_error_identity_keeps_the_complete_line_and_width(finding):
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
                return {'errors': [finding + 'x' * 110 + tail for tail in ('A', 'B')],
                        'counts': {kind: {'lines': 1, 'separators': 1, 'visible': 1} for kind in (*SEPARATOR_LINES, 're-read tails', 'archived-copy tails')}}
            if script == check_page.SOURCE_SEPARATORS_JS:
                return [{'source': 'sample', 'leading': 0, 'separators': 1}]

    class Browser:
        def new_page(self, viewport):
            return Page(viewport['width'])

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(check_page, 'hydrated', lambda *a, **k: None)
        patch.setattr(check_page, 'layout_settled', lambda *a, **k: None)
        errors = check_page.check_page_separators(Browser(), 'unused')
    findings = [e for e in errors if finding in e]
    assert len(findings) == 8
    assert len(set(findings)) == 8
    assert {e.split('px:')[0] for e in findings} == {'320', '390', '1000', '1440'}


@pytest.mark.parametrize('prefix', ['', 'wrapped rows: ', '1440px dark: expanded wrapped rows: '])
def test_missing_baseline_items_block(prefix):
    from scripts import check_page

    class Page:
        def evaluate(self, script):
            return ['missing baseline items: .development-label']

    findings = [prefix + e for e in check_page.check_baselines_and_rhythm(Page())]
    assert findings == [prefix + 'missing baseline items: .development-label']


def test_legacy_sweep_hidden_midline_findings_block():
    from scripts import check_page

    class Page:
        def evaluate(self, script):
            return ['separator hidden mid-line: content: Functions']

    findings = ['1440px dark: ' + e for e in check_page.check_line_end_separators(Page())]
    assert findings == ['1440px dark: separator hidden mid-line: content: Functions']


def test_page_script_parses_without_a_browser():
    soup = BeautifulSoup((Path(__file__).resolve().parents[1] / 'site/index.html').read_text(), 'html.parser')
    scripts = [script.string for script in soup.select('script:not([src])') if script.get('type') != 'application/ld+json']
    subprocess.run(['node', '-'], input='for (const script of ' + json.dumps(scripts) + ') new Function(script);', check=True, capture_output=True, text=True)


def test_nested_toggles_refresh_both_separator_kinds_after_lazy_handlers():
    source = (Path(__file__).resolve().parents[1] / 'site/index.html').read_text()
    start = source.index('  function layoutSeparators(')
    code = source[start:source.index('  // Each item is one unbreakable box', start)]
    start = source.index('    document.addEventListener("toggle", scheduleSeparators')
    code += source[start:source.index('    document.addEventListener("contentvisibilityautostatechange"', start)]
    script = r"""
const assert=require('node:assert/strict'), handlers={}, frames=[], measured=[];
let lazy=false, PRERENDERING=false;
const document={addEventListener:(type,callback,capture)=>{assert.equal(capture,true);handlers[type]=callback;}};
const requestAnimationFrame=callback=>{frames.push(callback);return frames.length;};
const layoutQualifiers=root=>{assert.equal(root,document);assert.ok(lazy);measured.push('qualifiers');};
const hideLineEndDots=root=>{assert.equal(root,document);measured.push('dots');};
CODE
handlers.toggle({target:{open:true}});handlers.toggle({target:{open:true}});
assert.equal(frames.length,1);assert.deepEqual(measured,[]);
lazy=true;frames.shift()();assert.deepEqual(measured,['qualifiers','dots']);
handlers.toggle({target:{open:false}});frames.shift()();assert.deepEqual(measured,['qualifiers','dots','qualifiers','dots']);
PRERENDERING=true;scheduleSeparators();assert.equal(frames.length,0);
""".replace('CODE', code)
    subprocess.run(['node', '-'], input=script, check=True, capture_output=True, text=True)


def test_map_record_refreshes_separators_on_open_and_refill_without_resize_or_toggle():
    source = (Path(__file__).resolve().parents[1] / 'site/index.html').read_text()
    start = source.index('  function layoutSeparators(')
    scheduler = source[start:source.index('  // Each item is one unbreakable box', start)]
    start = source.index('    function drawRecord(')
    draw = source[start:source.index('    // On wide screens the record covers the list', start)]
    script = r"""
const assert=require('node:assert/strict'), frames=[], measured=[];
let focused=false;
const el=(tag,cls,text)=>({tag,className:cls,textContent:text||'',children:[],dataset:{},classList:{add(){}},
  appendChild(n){this.children.push(n);return n;},setAttribute(){},addEventListener(){},
  focus(){focused=true;},getBoundingClientRect:()=>({top:200,bottom:240})});
const box=el('div'), $=()=>box, document={getElementById:()=>null,createTextNode:text=>el('text',null,text)};
const view={sel:'GB'}, J={GB:{id:'GB',institute_id:'gb'},NO:{id:'NO',institute_id:'no'}},
  I={gb:{},no:{}}, S={}, SOURCE_ORDERS={GB:[],NO:[]}, m={};
const coverList=()=>{}, citations=()=>()=>el('cite'), panelIdentity=()=>el('h3'),closeButton=()=>el('button'),
  explainNone=x=>x,statusMark=()=>el('status'),instName=()=>el('institute'),fieldCitations=()=>[],
  selectedRecord=()=>({}),budgetValue=()=>el('budget'),staffValue=()=>el('staff'),
  budgetCitations=()=>[],staffCitations=()=>[],
  glossify=()=>{},glueCitations=()=>{},instantTips=()=>{},externalLinks=()=>{},hideTip=()=>{},barsBottom=()=>60;
const innerHeight=900, PRERENDERING=false,requestAnimationFrame=f=>{frames.push(f);return frames.length;};
const layoutQualifiers=root=>{assert.equal(root,document);assert.equal(box.hidden,false);measured.push(['qualifiers',box.dataset.j,focused]);};
const hideLineEndDots=root=>{assert.equal(root,document);measured.push(['dots',box.dataset.j,focused]);};
CODE
drawRecord(true);assert.equal(frames.length,1);assert.deepEqual(measured,[]);
frames.shift()();assert.deepEqual(measured,[['qualifiers','GB',true],['dots','GB',true]]);
view.sel='NO';focused=false;drawRecord(false);assert.equal(frames.length,1);
frames.shift()();assert.deepEqual(measured.slice(2),[['qualifiers','NO',false],['dots','NO',false]]);
view.sel=null;drawRecord(false);assert.equal(box.hidden,true);assert.equal(frames.length,0);
""".replace('CODE', scheduler + draw)
    subprocess.run(['node', '-'], input=script, check=True, capture_output=True, text=True)
