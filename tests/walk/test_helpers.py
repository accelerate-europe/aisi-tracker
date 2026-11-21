"""No browser or Playwright imports are needed for the report contract."""

import json
import subprocess

import pytest

from scripts.walk import INSTALL, MEASURE, RULE_HELPERS, group_findings, report, signature, write_result


def javascript(body, data=None, source=RULE_HELPERS):
    result = subprocess.run(
        ["node", "-e", "const fs = require('node:fs'); const p = JSON.parse(fs.readFileSync(0, 'utf8')); "
         "Promise.resolve(new Function('data', p.source + '\\n' + p.body)(p.data))"
         ".then(value => process.stdout.write(JSON.stringify(value)))"
         ".catch(error => { console.error(error); process.exitCode = 1; });"],
        input=json.dumps({"source": source, "body": body, "data": data}),
        text=True, capture_output=True, timeout=10, check=True,
    )
    return json.loads(result.stdout)


@pytest.mark.parametrize("rect, expected", [
    ({"left": 0, "right": 20, "top": 18.9, "bottom": 38.9}, None),
    ({"left": 0, "right": 20, "top": 15, "bottom": 35}, None),
    ({"left": 0, "right": 20, "top": 14.9, "bottom": 34.9}, {"width": 20, "height": 5.1}),
    ({"left": 18, "right": 38, "top": 0, "bottom": 20}, None),
    ({"left": 17.5, "right": 37.5, "top": 0, "bottom": 20}, {"width": 2.5, "height": 20}),
    ({"left": 0, "right": 20, "top": 18, "bottom": 24}, None),
    ({"left": 0, "right": 20, "top": 17.5, "bottom": 23.5}, {"width": 20, "height": 2.5}),
    ({"left": 0, "right": 20, "top": 21, "bottom": 41}, None),
])
def test_overlap_ignores_tight_leading_but_keeps_substantial_intersections(rect, expected):
    result = javascript("return overlap({left: 0, right: 20, top: 0, bottom: 20}, data);", rect)
    assert result == (pytest.approx(expected) if expected else None)


@pytest.mark.parametrize("dx, dy, expected", [
    (0, 0, False), (0, .5, False), (0, -.5, False), (.5, 0, False), (-.5, 0, False),
    (0, .51, True), (0, -.51, True), (.51, 0, True), (-.51, 0, True), (0, -200, True),
])
def test_sticky_layer_uses_displacement_from_unstuck_rect(dx, dy, expected):
    assert javascript("""
      const unstuck = {left: 100, right: 300, top: 500, bottom: 530};
      const rect = {left: unstuck.left + data.dx, right: unstuck.right + data.dx,
        top: unstuck.top + data.dy, bottom: unstuck.bottom + data.dy};
      return ownLayer('sticky', rect, unstuck);
    """, {"dx": dx, "dy": dy}) is expected


def test_other_positions_do_not_need_a_sticky_probe():
    assert javascript("return data.map(position => ownLayer(position));",
                      ["fixed", "absolute", "relative", "static"]) == [True, True, False, False]


def test_line_boxes_keep_pitch_inputs_and_report_tallest_fragment():
    result = javascript("const lines = lineBoxes(data); return {lines, evidence: lineEvidence(lines), original: data};", [
        {"top": 36, "bottom": 56, "lh": 24, "element": "span.second"},
        {"top": 10, "bottom": 30, "lh": 24, "element": "span.tall"},
        {"top": 18, "bottom": 32, "lh": 28, "element": "a.short"},
    ])
    assert result["lines"] == [
        {"top": 10, "bottom": 32, "lh": 28, "element": "span.tall", "tallest": "span.tall", "height": 20},
        {"top": 36, "bottom": 56, "lh": 24, "element": "span.second", "tallest": "span.second", "height": 20},
    ]
    assert result["original"][0]["element"] == "span.second"
    assert result["evidence"] == (
        "1: top 10.0px, bottom 32.0px, tallest span.tall (20.0px); "
        "2: top 36.0px, bottom 56.0px, tallest span.second (20.0px)"
    )


def test_hit_height_includes_offset_translated_pseudo_and_label_without_label_gap():
    result = javascript("""
      const target = {top: 100, bottom: 120};
      const pseudo = pseudoBounds(target, {top: '-10px', bottom: 'auto', height: '28px', minHeight: '0px', boxSizing: 'border-box'}, -6);
      const label = {top: 200, bottom: 244};
      return {pseudo, targetHeight: unionHeight([target, pseudo]),
        labelledHeight: Math.max(unionHeight([target, pseudo]), unionHeight([label]))};
    """)
    assert result == {"pseudo": {"top": 84, "bottom": 112}, "targetHeight": 36, "labelledHeight": 44}


def test_pseudo_height_counts_content_box_edges_minimum_and_bottom_offset():
    assert javascript("""
      return pseudoBounds({top: 100, bottom: 120}, {top: 'auto', bottom: '-5px',
        height: '10px', minHeight: '20px', boxSizing: 'content-box',
        paddingTop: '2px', paddingBottom: '2px', borderTopWidth: '1px', borderBottomWidth: '1px'}, 0);
    """) == {"top": 99, "bottom": 125}


COLOURS = {
    "white": [255, 255, 255, 1], "black": [0, 0, 0, 1],
    "transparent": [0, 0, 0, 0], "rgb(119, 119, 119)": [119, 119, 119, 1],
    "rgba(0, 0, 0, 0.5)": [0, 0, 0, 0.5], "color(srgb 1 1 1 / 0.5)": [255, 255, 255, 0.5],
}


def test_gradient_stops_nested_colours_positions_and_multiple_layers():
    result = javascript("""
      const colour = s => data[s], valid = s => Object.hasOwn(data, s);
      const images = 'linear-gradient(to bottom in srgb, rgba(0, 0, 0, 0.5) 0% 10%, 30%, transparent calc(100% - 1px)), linear-gradient(white, white)';
      return {layers: splitLayers(images), grounds: backgroundPaints(data.transparent, images, colour, valid),
        modern: stopColour('color(srgb 1 1 1 / 0.5) calc(10% + 2px) 100%', valid)};
    """, COLOURS)
    assert len(result["layers"]) == 2
    assert result["grounds"] == [[127.5, 127.5, 127.5, 1], [255, 255, 255, 1]]
    assert result["modern"] == "color(srgb 1 1 1 / 0.5)"


def test_contrast_takes_worst_stop_including_nonuniform_and_translucent_gradients():
    result = javascript("""
      const grounds = backgroundPaints(data.white,
        'linear-gradient(white 0%, rgb(119, 119, 119) 50%, rgba(0, 0, 0, 0.5) 100%)',
        s => data[s], s => Object.hasOwn(data, s));
      const pairs = compositePairs([{ink: data.white, ground: data.transparent}], grounds, 1);
      return {ratios: pairs.map(contrast), worst: Math.min(...pairs.map(contrast))};
    """, COLOURS)
    assert result["ratios"] == pytest.approx([1, 4.4780894536, 3.9766530249])
    assert result["worst"] == 1


def test_paired_compositing_preserves_ancestor_opacity_and_deduplicates():
    result = javascript("""
      const pairs = compositePairs([{ink: data['rgba(0, 0, 0, 0.5)'], ground: data.transparent}], [data.white, data.white], .5);
      const final = compositePairs(pairs, [data.black], 1);
      return {pairs, final, transparent: over(data.transparent, data.transparent), black: luminance(data.black), white: luminance(data.white)};
    """, COLOURS)
    assert result == {
        "pairs": [{"ink": [127.5, 127.5, 127.5, .5], "ground": [255, 255, 255, .5]}],
        "final": [{"ink": [63.75, 63.75, 63.75, 1], "ground": [127.5, 127.5, 127.5, 1]}],
        "transparent": [0, 0, 0, 0], "black": 0, "white": 1,
    }


def test_url_background_remains_unresolved_and_plain_background_resolves():
    assert javascript("""
      return ['none', 'url("photo.png")', 'linear-gradient(black, white), url("photo.png")']
        .map(image => backgroundPaints(data.white, image, s => data[s], s => Object.hasOwn(data, s)));
    """, COLOURS) == [[[255, 255, 255, 1]], None, None]


def test_none_layer_does_not_discard_resolvable_gradient_stops():
    assert javascript("""
      return backgroundPaints(data.transparent, 'none, linear-gradient(black, white), none',
        s => data[s], s => Object.hasOwn(data, s));
    """, COLOURS) == [[0, 0, 0, 1], [255, 255, 255, 1]]


def test_embedded_browser_programs_parse_without_a_browser():
    assert javascript("for (const source of data) new Function('return (' + source + ')'); return true;", [INSTALL, MEASURE])


def test_control_actions_and_revisits_use_visible_openers_and_restore_typed_value():
    result = javascript(r"""
      const actions = [], nodes = [];
      class Element {
        constructor(tag, id, attrs = {}) {
          Object.assign(this, {localName: tag, id, attrs, classList: [], parentElement: null,
            isConnected: true, shown: true, textContent: id, open: false});
          nodes.push(this);
        }
        getAttribute(name) { return this.attrs[name] ?? null; }
        hasAttribute(name) { return Object.hasOwn(this.attrs, name); }
        checkVisibility() {
          for (let a = this; a.parentElement; a = a.parentElement)
            if (a.parentElement.localName === 'details' && !a.parentElement.open && a !== a.parentElement.summary) return false;
          return this.shown;
        }
        getBoundingClientRect() { return {width: 30, height: 30}; }
        getClientRects() { return this.shown ? [this.getBoundingClientRect()] : []; }
        matches() { return false; }
        closest() { return null; }
        querySelector() { return this.summary || null; }
        scrollIntoView() { actions.push(['scroll', this.id]); }
        focus() { actions.push(['focus', this.id]); }
        dispatchEvent(event) {
          actions.push([event.type, this.id, event.bubbles, this.value ?? null]);
          this.handle?.(event); return true;
        }
      }
      globalThis.window = globalThis;
      globalThis.getComputedStyle = () => ({pointerEvents: 'auto', overflow: 'visible'});
      globalThis.document = {
        fonts: {ready: Promise.resolve()}, documentElement: {},
        querySelectorAll: selector => selector === '*' ? nodes : nodes.filter(n => n.hasAttribute('aria-controls')),
        getElementById: id => nodes.find(n => n.id === id)
      };
      globalThis.MutationObserver = class { observe() {} disconnect() {} };
      globalThis.requestAnimationFrame = fn => fn();
      globalThis.MouseEvent = globalThis.InputEvent = class {
        constructor(type, options) { this.type = type; Object.assign(this, options); }
      };
      const svg = new Element('circle', 'svg', {'aria-expanded': 'false'});
      svg.handle = event => { if (event.type === 'click') svg.attrs['aria-expanded'] = 'true'; };
      const combo = new Element('input', 'combo', {role: 'combobox', 'aria-expanded': 'false'});
      combo.value = 'original';
      combo.handle = event => { if (event.type === 'input') combo.attrs['aria-expanded'] = String(combo.value === 'a'); };
      const detail = new Element('details', 'native'), summary = new Element('summary', 'summary');
      detail.summary = summary;
      summary.click = () => { actions.push(['summary-click']); detail.open = !detail.open; };
      const hidden = new Element('details', 'remote'), hiddenSummary = new Element('summary', 'hidden-summary');
      hidden.summary = hiddenSummary; hidden.shown = hiddenSummary.shown = false;
      hiddenSummary.click = () => { throw new Error('ineligible summary clicked'); };
      const anonymous = new Element('details', ''); anonymous.shown = false;
      new Element('button', 'empty-controls', {'aria-controls': ' '});
      const button = new Element('button', 'opener', {'aria-expanded': 'false', 'aria-controls': 'elsewhere remote'});
      button.click = () => {
        actions.push(['opener-click']); hidden.open = hidden.shown = hiddenSummary.shown = true;
        button.attrs['aria-expanded'] = 'true';
      };
      const nested = (name, parentElement = null) => {
        const e = new Element('details', name), summary = new Element('summary', name + '-summary');
        e.parentElement = parentElement; e.summary = summary; summary.parentElement = e;
        summary.click = () => {
          if (!summary.checkVisibility()) throw new Error('closed ancestor hides summary');
          actions.push(['summary-click', name]); e.open = !e.open;
        };
        return e;
      };
      const outer = nested('outer'), inner = nested('inner', outer), member = nested('member', inner);
      const install = new Function('return (' + data + ')')(); install();
      const w = window.__layoutWalk;
      const initial = w.snapshot(), remoteEligible = initial.find(c => c.id === w.id(hidden)).eligible;
      const anonymousEligible = initial.find(c => c.id === w.id(anonymous)).eligible;
      return (async () => {
        for (const e of [svg, detail, hidden, combo]) await w.open(w.id(e));
        const opened = w.snapshot().filter(c => [svg, detail, hidden, combo].some(e => w.id(e) === c.id)).every(c => c.open);
        const typedValue = combo.value;
        await w.open(w.id(member), true);
        const revisited = outer.open && inner.open && member.open;
        await w.restore();
        return {remoteEligible, anonymousEligible, opened, revisited, typedValue, restoredValue: combo.value,
          restoredExpanded: combo.getAttribute('aria-expanded'), actions};
      })();
    """, INSTALL, source="")
    assert result == {
        "remoteEligible": True, "anonymousEligible": False, "opened": True, "revisited": True, "typedValue": "a", "restoredValue": "original",
        "restoredExpanded": "false",
        "actions": [["click", "svg", True, None], ["summary-click"], ["opener-click"],
                    ["focus", "combo"], ["input", "combo", True, "a"], ["summary-click", "outer"],
                    ["summary-click", "inner"], ["scroll", "member-summary"], ["summary-click", "member"],
                    ["input", "combo", True, "original"]],
    }


def test_signature_ignores_id_text_and_class_order():
    element = {"tag": "button", "classes": ["small", "action", "small"], "id": "one",
               "parent": {"tag": "section", "classes": ["panel"]}}
    assert signature(element) == "section.panel > button.action.small"
    assert signature({**element, "id": "two", "classes": ["action", "small"]}) == signature(element)
    assert signature({"tag": "html"}) == "html"


def test_grouping_counts_nodes_once_per_view_and_preserves_evidence():
    finding = {"rule": "hit-size", "element": {"tag": "button"}, "node": 1,
               "label": "Open", "message": "20px < 28px", "view": "320-light", "state": "as loaded"}
    grouped = group_findings([finding, {**finding, "state": "all open"},
                              {**finding, "node": 2, "label": "Close"},
                              {**finding, "view": "1440-dark"}, {**finding, "rule": "contrast"}])
    assert [g["rule"] for g in grouped] == ["contrast", "hit-size"]
    hit = next(g for g in grouped if g["rule"] == "hit-size")
    assert hit["count"] == 3
    assert hit["views"] == ["1440-dark", "320-light"]
    assert hit["states"] == ["all open", "as loaded"]
    assert hit["examples"] == ["Close: 20px < 28px", "Open: 20px < 28px"]


def test_report_and_files_distinguish_pass_failure_and_not_run(tmp_path):
    assert report({"status": "PASS", "findings": [], "views": []}) == "PASS: 0 finding groups; 0 views.\n"
    missing = write_result(tmp_path, [], "Chromium missing")
    assert missing["status"] == "NOT RUN"
    assert "Chromium missing" in (tmp_path / "report.txt").read_text()
    logs = [{"view": "320-light", "states": 2, "opened": 0, "discovered": 1, "findings": [
        {"rule": "control-completeness", "element": {"tag": "button"}, "node": 1, "label": "Unopened",
         "message": "control was never seen open", "view": "320-light", "state": "walk"}]}]
    failed = write_result(tmp_path, logs)
    assert failed["status"] == "FAIL"
    assert json.loads((tmp_path / "findings.json").read_text()) == failed
    text = (tmp_path / "report.txt").read_text()
    assert "0/1 eligible controls seen open" in text
    assert "Unopened: control was never seen open" in text
