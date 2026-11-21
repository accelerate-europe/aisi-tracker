# /// script
# requires-python = ">=3.11"
# dependencies = ["playwright==1.63.0"]
# ///
"""Walk a built site's disclosures and measure generic layout rules (G1a).

Exit 0 when clear, 1 on findings, 3 (NOT RUN) without Chromium. No build or data
imports: old built sites can be replayed in the browser gate's environment.
"""

import argparse
import json
import os
import sys
import threading
import time
from contextlib import contextmanager
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from browser_lock import browser_slot  # noqa: E402

NOT_RUN = 3
VIEWS = ["320-light", "390-light", "641-light", "1000-light", "1440-light", "1440-dark"]

# DOM identities survive state changes without adding attributes to the tested page.
INSTALL = r"""() => {
  const ids = new WeakMap(), controls = new Map(), originalValues = new Map(); let serial = 0;
  const id = e => { if (!ids.has(e)) ids.set(e, ++serial); return ids.get(e); };
  const describe = e => ({tag: e.localName, classes: [...e.classList],
    parent: e.parentElement ? {tag: e.parentElement.localName, classes: [...e.parentElement.classList]} : {}});
  const label = e => e.id || e.getAttribute('aria-label') || e.textContent.trim().replace(/\s+/g, ' ').slice(0, 100) || e.localName;
  const visible = e => {
    if (!e.isConnected || !e.checkVisibility({checkOpacity: true, checkVisibilityCSS: true})) return false;
    for (let a = e; a; a = a.parentElement) {
      const s = getComputedStyle(a), r = a.getBoundingClientRect();
      if (r.width <= 2 && r.height <= 2 &&
          (s.overflow !== 'visible' || s.clip !== 'auto' || s.clipPath !== 'none')) return false;
    }
    return [...e.getClientRects()].some(r => r.width > .5 && r.height > .5);
  };
  const disabled = e => e.matches(':disabled') || !!e.closest('[inert]') ||
    !!e.closest('[aria-disabled="true"]') || getComputedStyle(e).pointerEvents === 'none';
  const eligible = e => e && visible(e) && !disabled(e);
  const click = e => typeof e.click === 'function' ? e.click() :
    e.dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true, view: window}));
  const opener = e => {
    if (!e.isConnected) return null;
    if (e.localName !== 'details') return eligible(e) ? e : null;
    const summary = e.querySelector(':scope > summary');
    if (eligible(summary)) return summary;
    if (!e.id) return null;
    return [...document.querySelectorAll('[aria-controls]')].find(control =>
      eligible(control) && (control.getAttribute('aria-controls') || '').split(/\s+/).includes(e.id));
  };
  const isOpen = e => e.localName === 'details' ? e.open : e.getAttribute('aria-expanded') === 'true';
  const regions = e => e.localName === 'details' ? [e] :
    (e.getAttribute('aria-controls') || '').split(/\s+/).filter(Boolean).map(x => document.getElementById(x)).filter(Boolean);
  const snapshot = () => {
    for (const e of document.querySelectorAll('*')) {
      if (e.localName === 'details' || e.hasAttribute('aria-expanded')) controls.set(id(e), e);
    }
    return [...controls].map(([key, e]) => ({id: key, element: describe(e), label: label(e),
      eligible: !!opener(e), open: e.isConnected && isOpen(e), connected: e.isConnected}));
  };
  // Toggle handlers and lazy DOM writes must finish before a snapshot. A bounded
  // quiet period also catches asynchronous content without any page-specific selectors.
  const settle = async () => {
    await document.fonts.ready;
    await new Promise((resolve, reject) => {
      let quiet;
      const finish = error => { clearTimeout(quiet); clearTimeout(limit); observer.disconnect(); error ? reject(error) : resolve(); };
      const changed = () => { clearTimeout(quiet); quiet = setTimeout(() => finish(), 80); };
      const observer = new MutationObserver(changed);
      const limit = setTimeout(() => finish(new Error('DOM did not settle within 5 s')), 5000);
      observer.observe(document.documentElement, {subtree: true, childList: true, attributes: true, characterData: true});
      changed();
    });
    await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
  };
  window.__layoutWalk = {id, describe, label, visible, disabled, regions, controls, snapshot, settle,
    async open(key, revisit = false) {
      const e = controls.get(key);
      if (revisit && e?.isConnected) {
        const target = opener(e) || (e.localName === 'details' ? e.querySelector(':scope > summary') : e);
        const ancestors = [];
        for (let a = target?.parentElement; a; a = a.parentElement)
          if (a !== e && a.localName === 'details') ancestors.unshift(a);
        for (const ancestor of ancestors) {
          if (ancestor.open) continue;
          const summary = opener(ancestor);
          if (summary) { click(summary); await settle(); }
        }
        opener(e)?.scrollIntoView({block: 'center', inline: 'nearest', behavior: 'instant'});
        await settle();
      }
      const target = e && opener(e);
      if (target && !isOpen(e)) {
        if (target.getAttribute('role') === 'combobox') {
          if (!originalValues.has(target)) originalValues.set(target, target.value);
          target.focus(); target.value = 'a';
          target.dispatchEvent(new InputEvent('input', {bubbles: true, data: 'a', inputType: 'insertText'}));
        } else click(target);
      }
      await settle(); return snapshot();
    },
    async restore() {
      if (!originalValues.size) return;
      // Keep typed controls open through measurement and single-open revisits.
      for (const [e, value] of originalValues) {
        e.value = value; e.dispatchEvent(new Event('input', {bubbles: true}));
      }
      originalValues.clear(); await settle();
    }};
}"""

# Shared with the browser-free tests so geometry and colour boundaries are tested
# against the code that the page actually executes.
RULE_HELPERS = r"""
  const overlap = (a, b) => {
    const width = Math.min(a.right, b.right) - Math.max(a.left, b.left);
    const height = Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top);
    const minimum = Math.max(2, .25 * Math.min(a.bottom - a.top, b.bottom - b.top));
    return width > 2 && height > minimum ? {width, height} : null;
  };
  const ownLayer = (position, rect, unstuck) => ['absolute', 'fixed'].includes(position) ||
    (position === 'sticky' && ['left', 'right', 'top', 'bottom'].some(edge => Math.abs(rect[edge] - unstuck[edge]) > .5));
  const lineBoxes = fragments => {
    const lines = [];
    for (const f of [...fragments].sort((a, b) => a.bottom - b.bottom)) {
      const last = lines.at(-1);
      if (!last || f.bottom - last.bottom > 6) lines.push({...f, tallest: f.element, height: f.bottom - f.top});
      else {
        if (f.bottom - f.top > last.height) { last.tallest = f.element; last.height = f.bottom - f.top; }
        last.top = Math.min(last.top, f.top); last.bottom = Math.max(last.bottom, f.bottom);
        last.lh = Math.max(last.lh, f.lh);
      }
    }
    return lines;
  };
  const lineEvidence = lines => lines.map((line, i) =>
    `${i + 1}: top ${line.top.toFixed(1)}px, bottom ${line.bottom.toFixed(1)}px, tallest ${line.tallest} (${line.height.toFixed(1)}px)`).join('; ');
  const unionHeight = boxes => Math.max(...boxes.map(r => r.bottom)) - Math.min(...boxes.map(r => r.top));
  const pseudoBounds = (r, s, translateY) => {
    const number = v => parseFloat(v) || 0;
    const extra = s.boxSizing === 'border-box' ? 0 :
      ['paddingTop', 'paddingBottom', 'borderTopWidth', 'borderBottomWidth'].reduce((n, k) => n + number(s[k]), 0);
    const height = Math.max(number(s.height), number(s.minHeight)) + extra;
    const top = (s.top !== 'auto' ? r.top + number(s.top) :
      s.bottom !== 'auto' ? r.bottom - number(s.bottom) - height : r.top) + translateY;
    return {top, bottom: top + height};
  };
  const over = (front, back) => {
    const alpha = front[3] + back[3] * (1 - front[3]);
    return alpha ? [...front.slice(0, 3).map((c, i) => (c * front[3] + back[i] * back[3] * (1 - front[3])) / alpha), alpha] : [0, 0, 0, 0];
  };
  const splitLayers = value => {
    const parts = []; let depth = 0, start = 0;
    for (let i = 0; i < value.length; i++) {
      if (value[i] === '(') depth++;
      if (value[i] === ')') depth--;
      if (value[i] === ',' && !depth) { parts.push(value.slice(start, i).trim()); start = i + 1; }
    }
    parts.push(value.slice(start).trim()); return parts;
  };
  const stopColour = (stop, isColour) => {
    // Stop positions may contain spaces and nested calc(); keep the whole colour function.
    let depth = 0;
    for (let i = 0; i <= stop.length; i++) {
      if (i === stop.length || (!depth && /\s/.test(stop[i]))) {
        const candidate = stop.slice(0, i);
        if (isColour(candidate)) return candidate;
      }
      if (stop[i] === '(') depth++;
      if (stop[i] === ')') depth--;
    }
    return null;
  };
  const distinct = values => [...new Map(values.map(v => [JSON.stringify(v), v])).values()];
  const backgroundPaints = (base, images, colour, isColour) => {
    let grounds = [base];
    if (images === 'none') return grounds;
    if (/url\(/i.test(images)) return null;
    for (const image of splitLayers(images).reverse()) {
      if (image === 'none') continue;
      const paints = splitLayers(image.slice(image.indexOf('(') + 1, -1))
        .map(stop => stopColour(stop, isColour)).filter(Boolean).map(colour);
      if (!paints.length) throw new Error(`cannot parse background colour stops: ${image}`);
      grounds = distinct(grounds.flatMap(bg => paints.map(paint => over(paint, bg))));
    }
    return grounds;
  };
  const compositePairs = (pairs, paints, opacity) => distinct(pairs.flatMap(pair => paints.map(bg => {
    const ink = over(pair.ink, bg), ground = over(pair.ground, bg);
    ink[3] *= opacity; ground[3] *= opacity;
    return {ink, ground};
  })));
  const luminance = c => c.slice(0, 3).map(v => v / 255).map(v => v <= .04045 ? v / 12.92 : ((v + .055) / 1.055) ** 2.4)
    .reduce((sum, v, i) => sum + v * [.2126, .7152, .0722][i], 0);
  const contrast = ({ink, ground}) => {
    const a = luminance(over(ink, [255, 255, 255, 1])), b = luminance(over(ground, [255, 255, 255, 1]));
    return (Math.max(a, b) + .05) / (Math.min(a, b) + .05);
  };
"""

MEASURE = "key => {\n" + RULE_HELPERS + r"""
  const w = window.__layoutWalk, eps = .5, findings = [], reported = new Map();
  const styles = new WeakMap(), style = e => {
    if (!styles.has(e)) styles.set(e, getComputedStyle(e)); return styles.get(e);
  };
  const add = (rule, e, message) => {
    const identity = rule + ':' + w.id(e);
    if (reported.has(identity)) return;
    reported.set(identity, true);
    findings.push({rule, element: w.describe(e), node: w.id(e), label: w.label(e), message});
  };
  const doc = document.documentElement;
  if (Math.abs(doc.scrollWidth - doc.clientWidth) > eps)
    add('horizontal-overflow', doc, `document ${doc.scrollWidth}px, viewport ${doc.clientWidth}px`);
  let roots = [document.body];
  if (key !== null) {
    const control = w.controls.get(key);
    roots = control ? w.regions(control).filter(w.visible) : [];
    if (!roots.length) {
      add('controlled-region', control || doc, 'open control has no visible controlled region (aria-controls or details)');
      return findings;
    }
  }
  const within = e => roots.some(root => root === e || root.contains(e));
  const ownLayers = new Set();
  // Compare in the same scroll container, restoring each element before probing the next.
  for (const e of document.querySelectorAll('*')) {
    const position = style(e).position;
    if (position === 'sticky') {
      const rect = e.getBoundingClientRect(), inline = e.getAttribute('style');
      try {
        // Static keeps the flow position without the sticky offset and ignores the insets a relative probe would apply.
        e.style.setProperty('position', 'static', 'important');
        if (ownLayer(position, rect, e.getBoundingClientRect())) ownLayers.add(e);
      } finally {
        if (inline === null) e.removeAttribute('style'); else e.setAttribute('style', inline);
      }
    } else if (ownLayer(position)) ownLayers.add(e);
  }
  const layerOf = e => {
    for (let a = e; a; a = a.parentElement)
      if (ownLayers.has(a)) return a;
    return doc;
  };
  const blockOf = e => {
    for (let a = e; a; a = a.parentElement) {
      const s = style(a);
      if (!/^(inline|contents)/.test(s.display) || ['absolute', 'fixed'].includes(s.position)) return a;
    }
    return doc;
  };
  const box = r => ({left: r.left, right: r.right, top: r.top, bottom: r.bottom});
  const normalHeights = new Map();
  const lineHeight = s => {
    if (s.lineHeight !== 'normal') return parseFloat(s.lineHeight);
    if (!normalHeights.has(s.font)) {
      const probe = document.createElement('span');
      probe.style.cssText = 'all:initial;position:fixed;visibility:hidden;white-space:pre;';
      probe.style.font = s.font; probe.style.lineHeight = 'normal'; probe.textContent = 'M\nM';
      document.body.append(probe); normalHeights.set(s.font, probe.getBoundingClientRect().height / 2); probe.remove();
    }
    return normalHeights.get(s.font);
  };
  const clip = (r, e, diagnose) => {
    let shown = {...r}, base = {...r};
    for (let a = e; a; a = a.parentElement) {
      // Paint containment clips like overflow does, without an overflow property (off-screen sections use it).
      const s = style(a), paint = /\b(paint|content|strict)\b/.test(s.contain);
      const x = paint || s.overflowX !== 'visible', y = paint || s.overflowY !== 'visible';
      if (!x && !y) continue;
      const clipOnly = ['visible', 'clip'].includes(s.overflowX) && ['visible', 'clip'].includes(s.overflowY);  // hidden, auto and scroll ignore the margin
      const margin = clipOnly && (paint || s.overflowX === 'clip' || s.overflowY === 'clip') ? Number(/-?[\d.]+(?=px)/.exec(s.overflowClipMargin)?.[0] || 0) : 0;  // the length; a reference box keyword is not modelled
      const b = a.getBoundingClientRect(), left = b.left + a.clientLeft - margin, top = b.top + a.clientTop - margin;
      const right = b.left + a.clientLeft + a.clientWidth + margin, bottom = b.top + a.clientTop + a.clientHeight + margin;
      const scroll = [s.overflowX, s.overflowY].some(v => v === 'auto' || v === 'scroll');
      const intentional = s.textOverflow === 'ellipsis' || parseInt(s.webkitLineClamp) > 0;
      // Loss counts from the text's own rect, or from what an exempt ancestor (scroll, ellipsis) left visible.
      const loss = Math.max(x ? left - base.left : 0, x ? base.right - right : 0,
                            y ? top - base.top : 0, y ? base.bottom - bottom : 0);
      if (diagnose && !scroll && !intentional && loss > 2 + eps)
        add('clipped-text', e, `text clipped by ${a.localName} by ${loss.toFixed(1)}px`);
      if (x) { shown.left = Math.max(shown.left, left); shown.right = Math.min(shown.right, right); }
      if (y) { shown.top = Math.max(shown.top, top); shown.bottom = Math.min(shown.bottom, bottom); }
      if (scroll || intentional) base = {...shown};
    }
    return shown.right - shown.left > eps && shown.bottom - shown.top > eps ? shown : null;
  };
  const texts = [], range = document.createRange();
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  while (walker.nextNode()) {
    const node = walker.currentNode, e = node.parentElement;
    if (!node.data.trim() || !within(e) || e.closest('svg') || !w.visible(e)) continue;
    range.setStart(node, node.data.search(/\S/)); range.setEnd(node, node.data.trimEnd().length);
    const rects = [...range.getClientRects()].map(box).map(r => clip(r, e, true)).filter(Boolean);
    if (rects.length) texts.push({node, e, rects, layer: layerOf(e), block: blockOf(e), lh: lineHeight(style(e))});
  }
  // A vertical sweep keeps long pages linear in the number of nearby text fragments.
  const layers = new Map(), blocks = new Map();
  for (const text of texts) {
    if (!layers.has(text.layer)) layers.set(text.layer, []);
    if (!blocks.has(text.block)) blocks.set(text.block, []);
    for (const r of text.rects) {
      layers.get(text.layer).push({text, r});
      blocks.get(text.block).push({top: r.top, bottom: r.bottom, lh: text.lh,
        element: text.e.localName + (text.e.id ? '#' + text.e.id : '') + [...text.e.classList].map(c => '.' + c).join('')});
    }
  }
  for (const entries of layers.values()) {
    entries.sort((a, b) => a.r.top - b.r.top); let active = [];
    for (const entry of entries) {
      active = active.filter(a => a.r.bottom - entry.r.top > eps);
      for (const other of active) {
        if (other.text.node === entry.text.node) continue;
        const intersection = overlap(other.r, entry.r);
        if (intersection)
          add('text-overlap', entry.text.e, `text intersects ${w.label(other.text.e)} (${intersection.width.toFixed(1)} × ${intersection.height.toFixed(1)}px)`);
      }
      active.push(entry);
    }
  }
  for (const [block, fragments] of blocks) {
    // The block's own line height sets a floor for every line box (its strut), also for smaller inline text.
    const lines = lineBoxes(fragments), strut = block === doc ? 0 : lineHeight(style(block));
    for (let i = 1; i < lines.length; i++) {
      const pitch = lines[i].bottom - lines[i - 1].bottom, limit = Math.max(lines[i].lh, lines[i - 1].lh, strut) + 2;
      if (pitch > limit + eps) add('line-pitch', block,
        `line pitch ${pitch.toFixed(1)}px exceeds ${limit.toFixed(1)}px; lines: ${lineEvidence(lines)}`);
    }
  }
  const canvas = document.createElement('canvas'); canvas.width = canvas.height = 1;
  const ctx = canvas.getContext('2d', {willReadFrequently: true}), colours = new Map();
  const colour = value => {
    if (!colours.has(value)) {
      ctx.clearRect(0, 0, 1, 1); ctx.fillStyle = value; ctx.fillRect(0, 0, 1, 1);
      const c = [...ctx.getImageData(0, 0, 1, 1).data]; c[3] /= 255; colours.set(value, c);
    }
    return colours.get(value);
  };
  // Each stop remains paired with its composited ink through ancestor opacity.
  const backgrounds = new Map();
  const background = s => {
    const key = s.backgroundColor + '|' + s.backgroundImage;
    if (!backgrounds.has(key)) backgrounds.set(key,
      backgroundPaints(colour(s.backgroundColor), s.backgroundImage, colour, v => CSS.supports('color', v)));
    return backgrounds.get(key);
  };
  const checked = new Set();
  for (const {e} of texts) {
    if (checked.has(e)) continue; checked.add(e);
    const s = style(e); let pairs = [{ink: colour(s.color), ground: [0, 0, 0, 0]}], unknown = false;
    for (let a = e; a; a = a.parentElement) {
      const cs = style(a), paints = background(cs);
      if (!paints && pairs.some(pair => pair.ground[3] < 1)) unknown = true;
      pairs = compositePairs(pairs, paints || [colour(cs.backgroundColor)], Number(cs.opacity));
    }
    if (unknown) { add('contrast', e, 'cannot resolve text ground containing a url() image'); continue; }
    const ratio = Math.min(...pairs.map(contrast));
    const large = parseFloat(s.fontSize) >= 24 || (parseFloat(s.fontSize) >= 18.6667 && Number(s.fontWeight) >= 700);
    const minimum = large ? 3 : 4.5;
    if (ratio < minimum) add('contrast', e, `${ratio.toFixed(2)}:1, needs ${minimum}:1`);
  }
  for (const e of document.querySelectorAll('*')) {
    if (!within(e) || !w.visible(e) || w.disabled(e)) continue;
    const interactive = ['button', 'input', 'select', 'textarea', 'summary'].includes(e.localName) ||
      (e.localName === 'a' && e.hasAttribute('href')) || e.tabIndex >= 0 || e.hasAttribute('aria-expanded') ||
      ['button', 'link', 'checkbox', 'radio', 'switch', 'tab', 'menuitem', 'option', 'slider', 'spinbutton', 'combobox'].includes(e.getAttribute('role'));
    if (!interactive) continue;
    const s = style(e);
    if ((e.localName === 'a' || e.getAttribute('role') === 'link') && s.display === 'inline') {
      let prose = e.parentElement;
      while (prose && !['p', 'li'].includes(prose.localName) && /^(inline|contents)/.test(style(prose).display)) prose = prose.parentElement;
      if (prose) {
        const walker = document.createTreeWalker(prose, NodeFilter.SHOW_TEXT); let runningText = false;
        while (walker.nextNode())
          if (!e.contains(walker.currentNode) && walker.currentNode.textContent.trim()) { runningText = true; break; }
        if (runningText) continue;
      }
    }
    const token = s.getPropertyValue('--hit').trim(), probe = document.createElement('span');
    probe.style.cssText = 'all:initial;position:fixed;visibility:hidden;height:0;';
    probe.style.fontSize = s.fontSize; probe.style.width = token;
    document.body.append(probe); const minimum = probe.getBoundingClientRect().width; probe.remove();
    if (!token || !minimum || !probe.style.width) { add('hit-size', e, 'missing or invalid computed --hit'); continue; }
    const height = Math.max(...[e, ...Array.from(e.labels || [])].filter(w.visible).map(target => {
      const r = target.getBoundingClientRect(), before = getComputedStyle(target, '::before'), boxes = [r];
      if (before.position === 'absolute' && !['none', 'normal'].includes(before.content) &&
          before.display !== 'none' && before.visibility === 'visible' && before.pointerEvents !== 'none') {
        const transform = before.transform === 'none' ? new DOMMatrix() : new DOMMatrix(before.transform);
        boxes.push(pseudoBounds(r, before, transform.f));
      }
      return unionHeight(boxes);
    }));
    if (height + eps < minimum)
      add('hit-size', e, `${height.toFixed(1)}px hit height, --hit ${minimum.toFixed(1)}px`);
  }
  return findings;
}"""


def signature(element: dict) -> str:
    """Classes are a set; IDs and text identify examples, never finding groups."""
    def part(value):
        return value.get("tag", "document") + "".join(f".{c}" for c in sorted(set(value.get("classes", []))))

    parent = element.get("parent")
    return f"{part(parent)} > {part(element)}" if parent else part(element)


def group_findings(findings: list[dict]) -> list[dict]:
    """Count distinct failing nodes per view, not repeated measurements of a node."""
    groups = {}
    for finding in findings:
        key = finding["rule"], signature(finding["element"])
        group = groups.setdefault(key, {"rule": key[0], "signature": key[1], "nodes": set(), "views": set(), "states": set(), "examples": set()})
        group["nodes"].add((finding["view"], finding["node"]))
        group["views"].add(finding["view"])
        group["states"].add(finding["state"])
        group["examples"].add(f"{finding['label']}: {finding['message']}")
    return [{"rule": g["rule"], "signature": g["signature"], "count": len(g["nodes"]),
             "views": sorted(g["views"]), "states": sorted(g["states"]), "examples": sorted(g["examples"])}
            for _, g in sorted(groups.items())]


def report(result: dict) -> str:
    lines = [f"{result['status']}: {len(result['findings'])} finding groups; {len(result['views'])} views."]
    if result.get("reason"):
        lines.append(result["reason"])
    for view in result["views"]:
        lines.append(f"{view['view']}: {view['states']} states, {view['opened']}/{view['discovered']} eligible controls seen open.")
    for finding in result["findings"]:
        lines.append(f"- {finding['rule']}: {finding['signature']} ({finding['count']}; {', '.join(finding['views'])})")
        lines.extend(f"  {example}" for example in finding["examples"])
    return "\n".join(lines) + "\n"


def run(browser, base_url: str, view: str) -> dict:
    """One fresh context per view; discovery is independent of page classes/data."""
    from playwright.sync_api import Error, TimeoutError as PlaywrightTimeout

    width, theme = view.split("-")
    context = browser.new_context(viewport={"width": int(width), "height": 900}, color_scheme=theme, locale="en-GB", reduced_motion="reduce")
    page = context.new_page()
    findings, observed, seen, groups = [], {}, set(), set()
    states = 0
    installed = False

    def issue(rule, message, control=None):
        control = control or {"element": {"tag": "html"}, "id": "document", "label": "document"}
        findings.append({"rule": rule, "element": control["element"], "node": control["id"],
                         "label": control["label"], "message": message, "view": view, "state": "walk"})

    page.on("console", lambda m: issue("console-error", m.text) if m.type == "error" else None)
    page.on("pageerror", lambda e: issue("page-error", str(e)))

    def remember(snapshot):
        for control in snapshot:
            if control["eligible"]:
                observed[control["id"]] = control
                if control["open"]:
                    seen.add(control["id"])
        return {c["id"]: c for c in snapshot}

    def measure(state, key=None):
        nonlocal states
        findings.extend({**f, "view": view, "state": state} for f in page.evaluate(MEASURE, key))
        states += 1

    deadline = time.monotonic() + 20 * 60

    def open_control(key, snapshot, revisit=False):
        if time.monotonic() > deadline:
            raise RuntimeError("walk exceeded 20 minutes before completeness")
        after = remember(page.evaluate("([key, revisit]) => window.__layoutWalk.open(key, revisit)", [key, revisit]))
        if after.get(key, {}).get("open"):
            for other, before in snapshot.items():
                # Only controls a reader can use join a group (a hidden twin can mirror the state: both maps' dots).
                if other != key and before["open"] and before["eligible"] and after.get(other, {}).get("connected") and not after[other]["open"]:
                    groups.update((key, other))
        return after

    def explore(snapshot, root=None):
        attempted = set()
        pending = list(snapshot)
        while pending:
            key = pending.pop(0)
            control = snapshot.get(key)
            if key in attempted or not control or not control["eligible"]:
                continue
            if root is not None and not page.evaluate("([root, key]) => { const w = window.__layoutWalk; return w.regions(w.controls.get(root)).some(r => r.contains(w.controls.get(key))); }", [root, key]):
                continue
            attempted.add(key)
            if control["open"]:
                continue
            before = snapshot
            snapshot = open_control(key, before)
            newly_visible = [k for k, c in snapshot.items() if c["eligible"] and not before.get(k, {}).get("eligible")]
            pending = newly_visible + pending
        return snapshot

    try:
        started = time.monotonic()
        page.goto(base_url, wait_until="domcontentloaded", timeout=30000)
        try:
            page.wait_for_function("!document.documentElement.hasAttribute('data-prerendered')",
                                   timeout=max(1, 30000 - (time.monotonic() - started) * 1000))
        except PlaywrightTimeout:
            issue("take-over", "html[data-prerendered] remained after 30 s")
            return {"view": view, "states": states, "discovered": 0, "opened": 0, "findings": findings}
        page.wait_for_load_state("networkidle", timeout=30000)
        page.add_style_tag(content="*, *::before, *::after { content-visibility: visible !important; transition: none !important; animation: none !important; }")
        page.mouse.move(-1, -1)
        page.evaluate(INSTALL)
        installed = True
        page.evaluate("() => window.__layoutWalk.settle()")
        snapshot = remember(page.evaluate("() => window.__layoutWalk.snapshot()"))
        measure("as loaded")
        snapshot = explore(snapshot)
        measure("independent controls open")
        measured = set()
        while remaining := groups - measured:
            key = min(remaining)
            measured.add(key)
            control = snapshot[key]
            snapshot = open_control(key, snapshot, revisit=True)
            if snapshot.get(key, {}).get("eligible") and snapshot[key]["open"]:
                snapshot = explore(snapshot, key)
                measure(f"single-open {snapshot[key]['label']}", key)
            else:
                state = snapshot.get(key, {})
                issue("controlled-region", "single-open member could not be revisited "
                      f"(connected {state.get('connected')}, eligible {state.get('eligible')}, open {state.get('open')})", control)
    except (Error, RuntimeError) as exc:
        issue("walk-error", str(exc))
    finally:
        try:
            if installed:
                page.evaluate("() => window.__layoutWalk.restore()")
        except Error as exc:
            issue("walk-error", f"could not restore typed controls: {exc}")
        finally:
            context.close()
    for key in observed.keys() - seen:
        issue("control-completeness", "control was never seen open", observed[key])
    return {"view": view, "states": states, "discovered": len(observed), "opened": len(seen), "findings": findings}


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *args) -> None:
        pass


@contextmanager
def serve(dist: Path):
    handler = partial(QuietHandler, directory=str(dist.resolve()))
    with ThreadingHTTPServer(("127.0.0.1", 0), handler) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_address[1]}/"
        finally:
            server.shutdown()
            thread.join()


def write_result(out: Path, logs: list[dict], reason: str = "") -> dict:
    findings = group_findings([finding for log in logs for finding in log["findings"]])
    result = {"status": "NOT RUN" if reason else "FAIL" if findings else "PASS", "reason": reason,
              "views": [{k: v for k, v in log.items() if k != "findings"} for log in logs], "findings": findings}
    out.mkdir(parents=True, exist_ok=True)
    (out / "findings.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    (out / "report.txt").write_text(report(result), encoding="utf-8")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dist", type=Path, required=True, help="built site with index.html")
    parser.add_argument("--out", type=Path, required=True, help="findings.json and report.txt directory")
    parser.add_argument("--views", default=",".join(VIEWS), help="comma-separated <width>-<theme>, default all six")
    args = parser.parse_args()
    views = args.views.split(",")
    if any(view not in VIEWS for view in views):
        parser.error(f"choose views from {', '.join(VIEWS)}")
    if not (args.dist / "index.html").is_file():
        parser.error(f"no index.html in {args.dist}")
    missing = "Chromium missing; uv run --locked scripts/check_page.py --install-chrome"
    try:
        from playwright.sync_api import Error, sync_playwright
    except ImportError:
        print(report(write_result(args.out, [], missing)), end="", file=sys.stderr)
        return NOT_RUN
    logs = []
    with sync_playwright() as playwright:
        chrome = playwright.chromium.executable_path
        if not (os.path.isfile(chrome) and os.access(chrome, os.X_OK)):
            print(report(write_result(args.out, [], missing)), end="", file=sys.stderr)
            return NOT_RUN
        with browser_slot("walk.py"), serve(args.dist) as url:
            try:
                browser = playwright.chromium.launch(executable_path=chrome, headless=True,
                                                     env={**os.environ, "DBUS_SESSION_BUS_ADDRESS": "disabled:"})
            except Error as exc:
                print(report(write_result(args.out, [], f"Chromium could not start: {exc}")), end="", file=sys.stderr)
                return NOT_RUN
            try:
                for view in views:
                    logs.append(run(browser, url, view))
                    result = write_result(args.out, logs)
                    print(f"{view}: {len(logs[-1]['findings'])} observations", flush=True)
            finally:
                browser.close()
    print(report(result), end="")
    return 1 if result["findings"] else 0


if __name__ == "__main__":
    sys.exit(main())
