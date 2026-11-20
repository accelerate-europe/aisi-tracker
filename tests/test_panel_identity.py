"""State-first record heads and the browser gate's independent failure cases."""

import json
import io
import subprocess
from pathlib import Path

import pytest
from PIL import Image, ImageDraw, ImageFont

from scripts import check_page
from scripts.screenshot_pixels import Pixels, pixel_difference


def test_shared_identity_and_close_name_for_state_eu_and_no_institute():
    source = (Path(__file__).resolve().parents[1] / 'site/index.html').read_text()
    helper = source[source.index('  var DISPLAY_NAME ='):source.index('  // One markerless route')]
    close = source[source.index('  function closeButton('):source.index('  function readingEdge(')]
    script = r"""
const assert=require('node:assert/strict');
function el(tag,cls,text){return {tag,className:cls,text:text||'',children:[],dataset:{},
  get textContent(){return this.text+this.children.map(n=>n.textContent).join('');},
  appendChild(n){this.children.push(n);},setAttribute(k,v){this[k]=v;},addEventListener(k,v){this[k]=v;}};}
HELPERS
const institutes=[{id:'uk',name:'AI Security Institute',jurisdiction_id:'GB'},
  {id:'es',name:'AI Security Institute (planned)',jurisdiction_id:'ES'}];
nameInstitutes(institutes,{GB:{name:'United Kingdom'},ES:{name:'Spain'}});
assert.equal(DISPLAY_NAME.uk,'AI Security Institute (United Kingdom)');
assert.equal(DISPLAY_NAME.es,'AI Security Institute (Spain)');
for(const [j,i,wanted] of [[{name:'United Kingdom'},institutes[0],'United Kingdom, AI Security Institute'],
  [{name:'Spain'},institutes[1],'Spain, AI Security Institute'],
  [{name:'European Union'},{id:'eu',name:'EU AI Office'},'European Union, EU AI Office'],[{name:'Austria'},null,'Austria']]) {
  const h=panelIdentity('h4','fn-detail-head',j,i);
  assert.equal(h.textContent,wanted);assert.equal(h.title,wanted);
  assert.equal(h.children[0].textContent,j.name);
  if(i) assert.equal(h.children[2].className,'head-institute');
  let called=false;const b=closeButton(()=>called=true,h);
  assert.equal(b['aria-label'],'Close '+wanted);assert.equal(b.dataset.tip,b['aria-label']);
  b.click();assert.ok(called);
}
assert.equal(closeButton(()=>{})['aria-label'],'Close');
""".replace('HELPERS', helper + close)
    result = subprocess.run(['node', '-'], input=script, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


@pytest.fixture
def measured_identity():
    return dict(text='United Kingdom AI Security Institute', state='United Kingdom',
                institute='AI Security Institute', stateVisible=True, instituteMuted=True,
                phoneLines=True, panelName='United Kingdom, AI Security Institute',
                tip='United Kingdom, AI Security Institute', closeName='Close United Kingdom, AI Security Institute',
                top=93, bars=93,
                lines=[dict(part=part, inside=True)
                       for part in ('state', 'institute')])


@pytest.mark.parametrize('change, finding', [
    ({'text': 'AI Security Institute United Kingdom'}, 'start'),
    ({'state': 'UK'}, 'full state'),
    ({'stateVisible': False}, 'full state'),
    ({'institute': 'UK AISI'}, 'full institute'),
    ({'text': 'United Kingdom'}, 'full institute'),
    ({'instituteMuted': False}, 'muted'),
    ({'phoneLines': False}, 'own line'),
    ({'panelName': 'AI Security Institute'}, 'accessible name'),
    ({'tip': 'AI Security Institute'}, 'title'),
    ({'closeName': 'Close'}, 'close accessible'),
    ({'top': 94.01}, '1px'),
    ({'top': 91.99}, '1px'),
    ({'lines': []}, 'no measured text lines'),
])
def test_head_gate_rejects_identity_and_geometry_regressions(measured_identity, change, finding):
    def errors(m):
        return check_page.panel_identity_errors(m, 'United Kingdom', 'AI Security Institute', close=True)
    assert errors(measured_identity) == []
    assert any(finding in e for e in errors(measured_identity | change))


@pytest.mark.parametrize('part', ['state', 'institute', 'function'])
def test_head_gate_rejects_each_escaped_text_line(measured_identity, part):
    line = dict(part=part, inside=True)
    measure = measured_identity | {'lines': [line]}
    def errors(m):
        return check_page.panel_identity_errors(m, 'United Kingdom', 'AI Security Institute', close=True)
    assert errors(measure) == []
    assert any(part in e and 'border box' in e for e in errors(measure | {'lines': [line | {'inside': False}]}))


def test_measurement_keeps_each_text_line_and_compiles_without_paint_helpers():
    script = r"""
const assert=require('node:assert/strict');
MEASURE
const state={},institute={},head={},document={};
const getComputedStyle=()=>({visibility:'visible',opacity:'1'});
const line={rect:{left:30,right:200,top:140,bottom:160}};
const leaves=[{textContent:'EU AI Office',parentElement:institute},{textContent:'Model testing',parentElement:state}];
institute.closest=state.closest=()=>null;
const ranges=new Map([[leaves[0],[line.rect,{left:30,right:90,top:162,bottom:182}]],
  [leaves[1],[{left:30,right:180,top:184,bottom:204}]]]);
const NodeFilter={SHOW_TEXT:4};
document.createTreeWalker=()=>{let i=-1;return {nextNode(){return ++i<leaves.length;},get currentNode(){return leaves[i];}};};
document.createRange=()=>{let node;return {selectNodeContents:n=>{node=n;},getClientRects:()=>ranges.get(node).map(r=>
  ({...r,width:r.right-r.left,height:r.bottom-r.top}))};};
assert.equal(textLines(head).length,3);
""".replace('MEASURE', check_page.PANEL_TEXT_LINES_JS)
    for measurement in (check_page.PANEL_IDENTITY_JS, check_page.PANEL_POSITION_JS, check_page.PANEL_PIXELS_JS):
        script += '\nnew Function("return (" + ' + json.dumps(measurement) + ' + ")");'
    result = subprocess.run(['node', '-'], input=script, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_phone_panel_identity_uses_base_names_for_same_named_institutes():
    from test_check_page import _names_data

    data = _names_data()
    data['dataset'] = {'vocabularies': {'function': {}}}
    for state in data['jurisdictions']:
        state['status'] = 'established' if state['id'] == 'GB' else 'planned'
    rows = {row['id']: row for row in check_page.function_phone_model(data)['rows']}
    assert rows['gb-aisi']['head_name'] == 'United Kingdom, AI Security Institute'
    assert rows['es-isia']['head_name'] == 'Spain, AI Security Institute'
    # Standalone Institute fields still disambiguate the same base name with the state suffix.
    assert check_page.expected_display_names(data)['gb-aisi'] == 'AI Security Institute (United Kingdom)'
    assert check_page.expected_display_names(data)['es-isia'] == 'AI Security Institute (Spain)'


def test_panel_kind_cannot_disappear_from_coverage():
    counts = dict.fromkeys(check_page.PANEL_HEAD_KINDS, 2)
    uk_stuck = dict.fromkeys(check_page.PANEL_REQUIRED_STUCK, 1)
    assert check_page.panel_coverage_errors(counts, uk_stuck) == []
    for kind in counts:
        assert any(kind in e for e in check_page.panel_coverage_errors(counts | {kind: 0}, uk_stuck))
        assert any(kind in e for e in check_page.panel_coverage_errors({k: v for k, v in counts.items() if k != kind}, uk_stuck))
    for kind in uk_stuck:
        assert any(kind in e and 'United Kingdom' in e for e in check_page.panel_coverage_errors(counts, uk_stuck | {kind: 0}))


def test_position_reserves_second_scroll_and_skips_only_short_panels():
    script = """
const assert=require('node:assert/strict');
let height=600,mode=false,moved=null,group=null;
const leaf={textContent:'body',parentElement:{closest:()=>null}},NodeFilter={SHOW_TEXT:4};
let bodyRect={top:690,bottom:710,width:100,height:20},blank=false;
const document={querySelector:()=>({getBoundingClientRect:()=>({bottom:60,height:60})}),
  createTreeWalker:()=>{let read=false;return {nextNode:()=>!blank && !read && (read=true),currentNode:leaf};},
  createRange:()=>({selectNodeContents(){},getClientRects:()=>[bodyRect]})};
const getComputedStyle=()=>({overflowY:mode?'auto':'visible',visibility:'visible',opacity:'1'});
const scrollBy=(x,y)=>{moved=y;};
const box={matches:()=>mode,closest:()=>group,clientTop:0,clientHeight:300,scrollHeight:600,
  getBoundingClientRect:()=>({top:400,height}),querySelector:()=>({contains:()=>false,getBoundingClientRect:()=>({top:416,height:80})})};
const position=POSITION;
assert.deepEqual(position(box,{head:'.head',step:37}),{enough:true,body:true});assert.equal(moved,600);
// The group's navigation is currently unstuck. Its eventual 40px height must be reserved.
group={querySelector:()=>({getBoundingClientRect:()=>({top:200,bottom:240,height:40})})};
position(box,{head:'.head',step:37});assert.equal(moved,560);group=null;
// A large gap at the midpoint cannot make the check compare blank backgrounds.
bodyRect={top:850,bottom:870,width:100,height:20};position(box,{head:'.head',step:37});assert.equal(moved,760);
blank=true;assert.deepEqual(position(box,{head:'.head',step:37}),{enough:true,body:false});blank=false;
height=140;moved=null;assert.deepEqual(position(box,{head:'.head',step:37}),{enough:false,body:false});assert.equal(moved,null);
mode=true;bodyRect={top:550,bottom:570,width:100,height:20};
assert.deepEqual(position(box,{head:'.head',step:37}),{enough:true,body:true});assert.equal(box.scrollTop,120);
box.scrollHeight=340;assert.deepEqual(position(box,{head:'.head',step:37}),{enough:false,body:false});
""".replace('POSITION', check_page.PANEL_POSITION_JS)
    result = subprocess.run(['node', '-'], input=script, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_pixel_measurement_includes_band_and_sides_and_samples_only_panel_ground():
    script = """
const assert=require('node:assert/strict');
const rect=(left,top,right,bottom)=>({left,top,right,bottom,width:right-left,height:bottom-top});
const headRect=rect(24,60,366,140),bounds=rect(8,-200,382,500),
  control=rect(320,70,366,114), innerWidth=390,innerHeight=900,scrollY=500;
const element=()=>({closest:()=>null});
const state=element(),institute=element(),bodyElement=element(),
  leaves=[{textContent:'United Kingdom',parentElement:state,rect:rect(30,80,170,100)},
    {textContent:'Institute',parentElement:institute,rect:rect(30,104,170,124)},
    {textContent:'Body under head',parentElement:bodyElement,rect:rect(24,65,250,85)},
    {textContent:'Body below head',parentElement:bodyElement,rect:rect(24,160,250,180)}];
const head={getBoundingClientRect:()=>headRect,contains:n=>n===state || n===institute};
const box={getBoundingClientRect:()=>bounds,querySelector:()=>head,
  querySelectorAll:()=>[{getBoundingClientRect:()=>control}],
  closest:()=>null,matches:()=>false,clientLeft:0,clientWidth:374};
let transform='none';
const getComputedStyle=()=>new Proxy({visibility:'visible',opacity:'1',borderLeftWidth:'0',borderRightWidth:'0',
  borderTopWidth:'0',borderBottomWidth:'0',transform},{get:(s,k)=>{assert.ok(k in s,'unexpected style '+k);return s[k];}});
const NodeFilter={SHOW_TEXT:4};let ownGround=true;
const document={querySelector:s=>s==='.secnav'?{getBoundingClientRect:()=>({bottom:60,height:60})}:box,
  createTreeWalker:n=>{let index=-1;const nodes=n===head?leaves.slice(0,2):leaves;
    return {nextNode:()=>++index<nodes.length,get currentNode(){return nodes[index];}};},
  createRange:()=>{let node;return {selectNodeContents:n=>{node=n;},getClientRects:()=>[node.rect]};},
  elementFromPoint:()=>ownGround?box:bodyElement};
const measure=MEASURE;
let m=measure({owner:'.panel',head:'.head'});
assert.deepEqual(m.clip,{x:8,y:60,width:374,height:80});
assert.equal(m.bodyUnder,true);assert.equal(m.bodyTop,65);
assert.equal(m.fractionalMove,false);transform='matrix(1, 0, 0, 1, 0, 277.391)';
assert.equal(measure({owner:'.panel',head:'.head'}).fractionalMove,true);transform='matrix(1, 0, 0, 1, 0, 277)';
assert.equal(measure({owner:'.panel',head:'.head'}).fractionalMove,false);transform='none';
assert.deepEqual(m.ground,{x:10,y:144});
assert.equal(m.samples.filter(p=>p.name.startsWith('padding')).length,4);
assert.equal(m.samples.filter(p=>p.name==='band').length,3);
assert.ok(m.samples.some(p=>p.name==='panel side' && p.x===10 && p.y===114));
assert.ok(m.samples.some(p=>p.name==='panel side' && p.x===379 && p.y===114));
ownGround=false;assert.equal(measure({owner:'.panel',head:'.head'}).ground,null);
""".replace('MEASURE', check_page.PANEL_PIXELS_JS)
    result = subprocess.run(['node', '-'], input=script, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


@pytest.fixture
def stuck_pair():
    before = dict(clip=dict(x=8, y=60, width=374, height=80), top=60, bottom=140, left=24, right=366,
                  edge=60, panelBottom=500, scroll=200, bodyTop=70, bodyUnder=True)
    return before, before | dict(scroll=237, bodyTop=33, panelBottom=463)


@pytest.mark.parametrize('change,finding', [
    ({'scroll': 201}, '37px'),
    ({'top': 58}, 'not stuck'),
    ({'panelBottom': 138}, 'not stuck'),
    ({'left': 25}, 'border box'),
    ({'clip': dict(x=9, y=60, width=374, height=80)}, 'clip moved'),
    ({'bodyTop': 70}, 'moving body text'),
    ({'bodyTop': None}, 'moving body text'),
])
def test_long_panel_with_invalid_pair_fails(stuck_pair, change, finding):
    before, after = stuck_pair
    assert check_page.panel_pair_errors(before, after) == []
    assert any(finding in e for e in check_page.panel_pair_errors(before, after | change))


def test_scrolling_blank_space_cannot_prove_head_coverage(stuck_pair):
    before, after = stuck_pair
    assert check_page.panel_pair_errors(before | {'bodyUnder': False}, after | {'bodyUnder': False})
    # A body line may leave the protected area on the second position.
    assert check_page.panel_pair_errors(before, after | {'bodyUnder': False}) == []


class ScreenshotPage:
    def __init__(self, image):
        self.image = image

    def screenshot(self, *, clip, scale, animations):
        assert scale == 'css' and animations == 'disabled'
        stream = io.BytesIO()
        self.image.crop((clip['x'], clip['y'], clip['x'] + clip['width'], clip['y'] + clip['height'])).save(stream, format='PNG')
        return stream.getvalue()


def pixel_measurement():
    return dict(clip=dict(x=8, y=10, width=180, height=100), ground=dict(x=10, y=118),
                left=24, right=180, top=14, bottom=110,
                samples=[dict(name=name, x=x, y=y) for name, x, y in
                         [('padding 0/0', 10, 12), ('padding 0/1', 185, 12), ('band', 98, 12),
                          ('padding 1/0', 10, 107), ('panel side', 185, 50)]])


@pytest.mark.parametrize('alpha', [255, 128, 0])
@pytest.mark.parametrize('ground', [(250, 247, 242, 255), (32, 35, 38, 255)])
def test_head_with_translucent_ground_fails(alpha, ground):
    """Moving body text beneath the ground, with the head's own ink suppressed."""
    shots = []
    for shift in (0, 37):
        body = Image.new('RGBA', (200, 150), ground)
        ink = (0, 0, 0, 255) if ground[0] > 128 else (255, 255, 255, 255)
        draw = ImageDraw.Draw(body)
        draw.text((25, 55 - shift), 'Scrolling body text', fill=ink)
        head = Image.new('RGBA', (180, 100), (*ground[:3], alpha))
        body.alpha_composite(head, (8, 10))
        pixels, errors = check_page.panel_head_snapshot(ScreenshotPage(body), pixel_measurement())
        assert errors == []  # Padding can match even when text shows through elsewhere.
        shots.append(pixels)
    findings = check_page.panel_pixel_errors(*shots, pixel_measurement())
    if alpha == 255:
        assert findings == []
    else:
        assert len(findings) == 1
        assert 'differing pixels; bounding box' in findings[0]


@pytest.mark.parametrize('point', [(9, 50), (98, 11), (180, 90)])
def test_side_band_and_last_identity_line_are_in_the_comparison(point):
    before = Image.new('RGBA', (200, 150), (250, 247, 242, 255))
    after = before.copy()
    after.putpixel(point, (0, 0, 0, 255))
    shots = [check_page.panel_head_snapshot(ScreenshotPage(image), pixel_measurement())[0] for image in (before, after)]
    assert pixel_difference(*shots)[0] == 1
    assert check_page.panel_pixel_errors(*shots, pixel_measurement())  # Even one pixel in the side/band fails.


def test_ground_must_equal_panel_even_when_head_is_opaque_and_unchanging():
    image = Image.new('RGBA', (200, 150), (250, 247, 242, 255))
    draw = ImageDraw.Draw(image)
    draw.rectangle((8, 10, 187, 109), fill=(230, 228, 220, 255))
    _, errors = check_page.panel_head_snapshot(ScreenshotPage(image), pixel_measurement())
    assert len(errors) == 5
    assert all('differs from panel ground' in error for error in errors)


def test_ground_capture_is_not_part_of_head_comparison_and_missing_samples_fail():
    first = Image.new('RGBA', (200, 150), (250, 247, 242, 255))
    second = first.copy()
    second.putpixel((11, 117), (0, 0, 0, 255))
    before, errors = check_page.panel_head_snapshot(ScreenshotPage(first), pixel_measurement())
    after, more_errors = check_page.panel_head_snapshot(ScreenshotPage(second), pixel_measurement() | {'ground': dict(x=10, y=125)})
    assert errors == more_errors == check_page.panel_pixel_errors(before, after, pixel_measurement()) == []
    _, errors = check_page.panel_head_snapshot(ScreenshotPage(first), pixel_measurement() | {'ground': None, 'samples': []})
    assert len(errors) == 3


def raster_pixels(image):
    return Pixels(image.width, image.height, image.tobytes())


def realistic_head(width=1068, height=86, *, own_ink=False):
    image = Image.new('RGBA', (width, height), (250, 247, 242, 255))
    draw, font = ImageDraw.Draw(image), ImageFont.load_default(size=18)
    lines = [('United Kingdom', 24, 14), ('AI Security Institute', 24, 38)]
    if height == 108:
        lines.append(('Model testing', 24, 62))
    if own_ink:
        for words, x, y in lines:
            draw.text((x, y), words, font=font, fill='black', anchor='lt')
    measured = dict(clip=dict(x=0, y=0, width=width, height=height), left=0, top=0, right=width, bottom=height,
                    edge=0, panelBottom=600, scroll=200, bodyTop=62, bodyUnder=True)
    return image, font, measured


def test_readable_api_leak_fails_in_realistic_wide_head():
    image, font, measured = realistic_head()
    shots = []
    for shift in (0, 37):
        shot = image.copy()
        ImageDraw.Draw(shot).text((850, 62 - shift), 'API', font=font, fill='black', anchor='lt')
        shots.append(raster_pixels(shot))
    count, _ = pixel_difference(*shots)
    assert count == 338  # The reviewer's readable leak, previously below the 459px area-wide allowance.
    assert check_page.panel_pair_errors(measured, measured | dict(scroll=237, bodyTop=25, panelBottom=563)) == []
    assert any('338 differing pixels' in e for e in check_page.panel_pixel_errors(*shots, measured))


def test_readable_a_leak_fails_inside_former_phone_text_allowance():
    image, _, measured = realistic_head(390, 108)
    shots = []
    for shift in (0, 37):
        shot = image.copy()
        ImageDraw.Draw(shot).text((60, 28 - shift), 'a', font=ImageFont.load_default(size=16), fill='black', anchor='lt')
        shots.append(raster_pixels(shot))
    assert pixel_difference(*shots)[0] == 54
    assert check_page.panel_pair_errors(measured, measured | dict(scroll=237, bodyTop=25, panelBottom=563)) == []
    assert any('54 differing pixels' in e for e in check_page.panel_pixel_errors(*shots, measured))


def test_readable_word_in_former_identity_text_area_fails():
    before, font, measured = realistic_head()
    after = before.copy()
    ImageDraw.Draw(after).text((150, 14), 'API', font=font, fill='black', anchor='lt')
    shots = [raster_pixels(im) for im in (before, after)]
    assert pixel_difference(*shots)[0] > 64
    errors = check_page.panel_pixel_errors(*shots, measured)
    assert errors


@pytest.mark.parametrize('point,allowed', [
    ((100, 107), True), ((24, 107), True), ((365, 107), True),
    ((24, 20), False), ((365, 20), False), ((100, 10), False), ((100, 106), False),
    ((0, 20), False), ((389, 20), False), ((100, 0), False), ((10, 107), False), ((380, 107), False),
])
def test_only_the_heads_last_pixel_row_allows_fractional_edge_noise(point, allowed):
    before = Image.new('RGBA', (390, 108), (250, 247, 242, 255))
    after = before.copy()
    after.putpixel(point, (0, 0, 0, 255))
    # Nonzero screenshot origin, wider side padding and a band above a fractionally positioned head.
    # Production geometry: the rounded rectangle is whole pixels while the transform that moves the head is not.
    measured = dict(clip=dict(x=8, y=60, width=390, height=108), left=31.75, right=374.25, top=70, bottom=168, fractionalMove=True)
    assert bool(check_page.panel_pixel_errors(raster_pixels(before), raster_pixels(after), measured)) is not allowed


@pytest.mark.parametrize('fractional', [False, None])
def test_a_strip_on_the_last_row_of_an_integer_aligned_head_fails(fractional):
    before = Image.new('RGBA', (390, 108), (250, 247, 242, 255))
    after = before.copy()
    ImageDraw.Draw(after).line((24, 107, 365, 107), fill='black')
    measured = dict(clip=dict(x=8, y=60, width=390, height=108), left=32, right=374, top=70, bottom=168)
    if fractional is not None:
        measured['fractionalMove'] = fractional
    assert any('342 differing pixels' in e for e in check_page.panel_pixel_errors(raster_pixels(before), raster_pixels(after), measured))


@pytest.mark.parametrize('point', [(24, 14), (23, 13), (500, 14), (1038, 25)])
@pytest.mark.parametrize('delta', [2, 3])
def test_former_text_close_mark_and_padding_allow_no_pixels_beyond_channel_tolerance(point, delta):
    before, _, measured = realistic_head()
    after = before.copy()
    r, g, b, a = after.getpixel(point)
    after.putpixel(point, (r - delta, g, b, a))
    assert bool(check_page.panel_pixel_errors(raster_pixels(before), raster_pixels(after), measured)) is (delta > 2)


class InkPage:
    """Model the temporary CSS lifecycle without starting a browser."""
    def __init__(self):
        self.marked = False
        self.ink_style = None
        self.removal_checked = False

    def locator(self, selector):
        page = self

        class Locator:
            def evaluate(self, script, head):
                assert head == '.head'
                assert "box.querySelector(head)" in script
                assert ":scope > .close-control .close-glyph" in script
                page.marked = True

            def evaluate_all(self, script):
                assert "removeAttribute('data-panel-pixel-ink')" in script
                page.marked = False

            def count(self):
                return int(page.marked)

        return Locator()

    def add_style_tag(self, *, content):
        # This rule changes ink only, including the close mark's inherited currentColor.
        selectors, declarations = content.strip().rstrip('}').split('{')
        assert selectors.strip() == '[data-panel-pixel-ink], [data-panel-pixel-ink] *'
        assert dict(line.strip().split(': ', 1) for line in declarations.strip().rstrip(';').split(';')
                    if line.strip()) == {'color': 'transparent !important', 'text-decoration-color': 'transparent !important'}
        self.ink_style = content
        page = self

        class Style:
            def evaluate(self, script):
                if script == 'n=>n.remove()':
                    page.ink_style = None
                else:
                    assert script == 'n=>n.isConnected'
                    page.removal_checked = True
                    return page.ink_style is not None

            def dispose(self):
                pass

        return Style()


@pytest.mark.parametrize('exit_kind', ['normal', 'rejected-pair', 'capture-error'])
def test_own_ink_redraw_is_suppressed_and_style_removed_on_every_exit(exit_kind):
    page = InkPage()
    visible, _, measured = realistic_head(390, 108, own_ink=True)
    changed = visible.copy()
    edges = [(x, y) for y in range(visible.height) for x in range(visible.width)
             if 3 <= visible.getpixel((x, y))[0] <= 247]
    for x, y in edges[:48]:
        r, g, b, a = changed.getpixel((x, y))
        changed.putpixel((x, y), (r - 3, g, b, a))
    assert pixel_difference(raster_pixels(visible), raster_pixels(changed))[0] == 48

    def pair():
        with check_page.panel_hidden_ink(page, '.panel', '.head'):
            shots = []
            for image in (visible, changed):
                assert page.marked and page.ink_style
                # The ground remains; all of this fixture's moving pixels are the head's own ink.
                shot = Image.new('RGBA', image.size, (250, 247, 242, 255)) if page.ink_style else image
                shots.append(raster_pixels(shot))
            assert check_page.panel_pixel_errors(*shots, measured) == []
            if exit_kind == 'rejected-pair':
                return
            if exit_kind == 'capture-error':
                raise RuntimeError('capture failed')

    if exit_kind == 'capture-error':
        with pytest.raises(RuntimeError, match='capture failed'):
            pair()
    else:
        pair()
    assert not page.marked and page.ink_style is None and page.removal_checked


def test_inventory_takes_two_captures_per_long_panel_and_skips_short_ones(monkeypatch, stuck_pair):
    """Exercise the Python inventory/capture wiring; geometry and pixels have separate real unit tests."""
    pages, captures = [], []

    class Locator:
        def __init__(self, page, selector):
            self.page, self.selector = page, selector

        def count(self):
            return 1

        def evaluate(self, script, arg=None):
            if "setAttribute('data-panel-pixel-ink'" in script:
                self.page.marked = True
            if script == check_page.PANEL_POSITION_JS:
                self.page.scroll = 0
                self.page.mode = 'scrollport' if self.selector == '#record' and self.page.width == 1440 else 'viewport'
                return dict(enough=self.selector != '#c-at', body=self.selector != '#c-at')
            if 'args.mode' in script:
                assert arg['mode'] == self.page.mode
                self.page.scroll += arg['step']

        def evaluate_all(self, script):
            return dict(id='c-at', j='AT') if self.selector == '.card.mini' else None

        def get_by_role(self, *args, **kwargs):
            return self

        @property
        def first(self):
            return self

        def click(self):
            pass

        def fill(self, value):
            pass

    class Page(InkPage):
        mouse = type('Mouse', (), {'move': lambda *args: None})()

        def __init__(self, width):
            super().__init__()
            self.width, self.scroll, self.mode = width, 0, 'viewport'

        def locator(self, selector):
            if selector == '[data-panel-pixel-ink]':
                return super().locator(selector)
            return Locator(self, selector)

        def evaluate(self, script, arg=None):
            if script in (check_page.PANEL_IDENTITY_JS, check_page.READING_HEAD_JS):
                assert self.ink_style is None  # Colour assertions must see the restored head.
            if script == check_page.PANEL_PIXELS_JS:
                return stuck_pair[bool(self.scroll)] | {'mode': self.mode}
            return {}

        def close(self):
            pass

    def page_factory(browser, theme, *, viewport, reduced_motion):
        page = Page(viewport['width'])
        pages.append(page)
        return page

    def snapshot(page, measured):
        assert page.marked and page.ink_style
        captures.append((page, page.scroll))
        return None, []

    monkeypatch.setattr(check_page, 'themed_page', page_factory)
    monkeypatch.setattr(check_page, 'hydrated', lambda *args: None)
    monkeypatch.setattr(check_page, 'layout_settled', lambda *args: None)
    monkeypatch.setattr(check_page, 'unstuck_head_gap', lambda *args: 14)
    monkeypatch.setattr(check_page, 'reading_head_errors', lambda *args, **kwargs: [])
    monkeypatch.setattr(check_page, 'panel_identity_errors', lambda *args, **kwargs: [])
    monkeypatch.setattr(check_page, 'panel_head_snapshot', snapshot)
    monkeypatch.setattr(check_page, 'panel_pixel_errors', lambda *args: [])
    monkeypatch.setattr(check_page, 'expected_base_names', lambda _: dict(gb='Institute', eu='Institute'))
    data = dict(jurisdictions=[dict(id=jid, name=jid) for jid in ('GB', 'EU', 'AT')],
                institutes=[dict(id=jid.lower(), jurisdiction_id=jid, functions=[dict(code='testing', claim_ids=['one'])])
                            for jid in ('GB', 'EU')], dataset=dict(vocabularies=dict(function=dict(testing=dict(label='Model testing')))))
    errors = check_page.check_panel_heads(None, 'unused', data)
    assert check_page.blocking(errors) == []
    assert len(pages) == 4  # two widths and both themes
    for page in pages:
        assert [scroll for p, scroll in captures if p is page] == [0, 37] * 16
        assert not page.marked and page.ink_style is None and page.removal_checked
    assert len([e for e in errors if 'States mini card' in e and '1 not stuck (too short)' in e]) == 4


@pytest.mark.parametrize('replacement', ['AI Security Institute (Spain)', 'AI Security Institute (Spain), Spain', 'Spain, Spain AISI'])
def test_funding_content_requires_state_before_full_recorded_name(replacement):
    from test_check_page import _names_data, _names_html

    html = _names_html().replace('>Spain, AI Security Institute</span>', '>' + replacement + '</span>')
    assert any('in full' in e for e in check_page.check_institute_names(html, _names_data()))


@pytest.mark.parametrize('spoken,valid', [
    ('France, National Institute for AI Evaluation and Security', True),
    ('France , National Institute for AI Evaluation and Security', True),
    ('France,National Institute for AI Evaluation and Security', True),
    ('France National Institute for AI Evaluation and Security', False),
    ('France, National Institute', False),
    ('National Institute for AI Evaluation and Security, France', False),
])
def test_wrapped_accessible_identity_allows_only_separator_whitespace(spoken, valid):
    pattern = check_page.identity_name_pattern('France, National Institute for AI Evaluation and Security')
    assert bool(pattern.fullmatch(spoken)) is valid


def test_map_reading_edge_distinguishes_the_scrollport_from_the_page():
    script = """
const assert=require('node:assert/strict');
let top=120,overflowY='auto',record=true;
const document={querySelector:()=>({getBoundingClientRect:()=>({bottom:60,height:60})})};
const box={matches:()=>record,closest:()=>null,clientTop:1,getBoundingClientRect:()=>({top})};
const getComputedStyle=()=>({overflowY});
const measure=MEASURE;
assert.deepEqual(measure(box),{bars:60,edge:121,mode:'scrollport',nav:60,subnav:0});
top=20;assert.equal(measure(box).edge,60);
overflowY='visible';top=120;assert.equal(measure(box).edge,60);assert.equal(measure(box).mode,'viewport');
record=false;overflowY='auto';assert.equal(measure(box).edge,60);
""".replace('MEASURE', check_page.READING_EDGE_JS)
    result = subprocess.run(['node', '-'], input=script, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    for width, mode in ((390, 'viewport'), (1100, 'viewport'), (1440, 'scrollport')):
        assert check_page.map_head_coverage_errors(width, {mode: 2}) == []
        assert check_page.map_head_coverage_errors(width, {mode: 0})


def test_identity_check_uses_the_measured_scrollport_edge(measured_identity):
    measure = measured_identity | {'top': 121, 'edge': 121}
    assert check_page.panel_identity_errors(measure, 'United Kingdom', 'AI Security Institute', close=True) == []
    assert check_page.panel_identity_errors(measure | {'top': 93}, 'United Kingdom', 'AI Security Institute', close=True)
    assert check_page.head_padding_errors({'bars': 93, 'edge': 121, 'textTop': 133, 'unstuckGap': 12}) == []


@pytest.mark.parametrize('change', [
    {'functionText': 'Other function'}, {'functionVisible': False},
    {'functionBelow': False}, {'functionStyled': False},
])
def test_stuck_cell_identity_requires_the_visible_function_in_label_type(measured_identity, change):
    measure = measured_identity | {'functionText': 'Model testing', 'functionVisible': True, 'functionBelow': True, 'functionStyled': True}
    def errors(m):
        return check_page.panel_identity_errors(m, 'United Kingdom', 'AI Security Institute', close=True, function='Model testing')
    assert errors(measure) == []
    assert any('function label' in e for e in errors(measure | change))


def test_record_arrival_dismisses_its_automatic_tip_but_explicit_tips_keep_escape():
    source = (Path(__file__).resolve().parents[1] / 'site/index.html').read_text()
    start = source.index('      if (focus) { // the record heading takes focus')
    arrival = source[start:source.index('\n    }', start)]
    escape = next(line for line in source.splitlines() if 'e.key === "Escape" && view.sel' in line)
    script = """
const assert=require('node:assert/strict');
let handler,closed=0,scrolled=0,focus=true;
const tip={hidden:true},view={sel:'EU'},hideTip=()=>{tip.hidden=true;},pick=()=>{closed++;};
const name={focus(){tip.hidden=false;},getBoundingClientRect:()=>({top:100,bottom:160}),scrollIntoView(){scrolled++;}};
const document={addEventListener:(type,fn)=>{handler=fn;}},innerHeight=900,barsBottom=()=>60;
ARRIVAL
ESCAPE
const event={key:'Escape',target:{closest:selector=>selector==='section.first'?{}:null}};
assert.equal(tip.hidden,true);handler(event);assert.equal(closed,1);
tip.hidden=false;handler(event);assert.equal(closed,1); // an explicitly opened tooltip gets first Escape
hideTip();handler(event);assert.equal(closed,2);
""".replace('ARRIVAL', arrival).replace('ESCAPE', escape)
    result = subprocess.run(['node', '-'], input=script, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_grid_function_label_moves_with_identity_but_does_not_rename_close():
    source = (Path(__file__).resolve().parents[1] / 'site/index.html').read_text()
    identity = source[source.index('  var DISPLAY_NAME ='):source.index('  // One markerless route')]
    close = source[source.index('  function closeButton('):source.index('  function readingEdge(')]
    start = source.index('        function detailHeading(')
    heading = source[start:source.index('        function escapeDetail(', start)]
    start = source.index('          var detail = el("div", "fn-detail"), title = panelIdentity(')
    render = source[start:source.index('          appendClaims(detail, i, code);', start)]
    script = """
const assert=require('node:assert/strict');
function el(tag,cls,text=''){return {tag,className:cls,text,children:[],dataset:{},style:{setProperty(){}},
  get textContent(){return this.text+this.children.map(n=>n.textContent).join('');},
  appendChild(n){this.children.push(n);return n;},setAttribute(k,v){this[k]=v;},addEventListener(){},
  querySelector(selector){return this.children.find(n=>'.'+n.className===selector);}};}
const J={GB:{name:'United Kingdom'}},i={id:'uk',name:'AI Security Institute',jurisdiction_id:'GB'},
  code='testing',vocab={testing:{label:'Model testing'}},scroll={clientWidth:600},closeDetail=()=>{};
HELPERS
RENDER
assert.equal(detail.children.length,1);
const top=detail.children[0];assert.equal(top.className,'fn-detail-top');
assert.deepEqual(top.children.map(n=>n.className),['panel-identity fn-detail-head','close-control','fn-function-head']);
assert.equal(top.children[2].textContent,'Model testing');
assert.equal(top.children[1]['aria-label'],'Close United Kingdom, AI Security Institute');
assert.equal(detail['aria-labelledby'],title.id);
""".replace('HELPERS', identity + close + heading).replace('RENDER', render)
    result = subprocess.run(['node', '-'], input=script, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_funding_content_keeps_the_visually_hidden_separator():
    from test_check_page import _names_data, _names_html

    html = _names_html().replace('>Spain, AI Security Institute</span>',
                                '><span class="head-state">Spain</span><span class="head-separator vh">, </span>'
                                '<span class="head-institute">AI Security Institute</span></span>')
    assert check_page.check_institute_names(html, _names_data()) == []
