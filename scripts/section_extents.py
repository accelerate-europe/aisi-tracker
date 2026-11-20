"""The page's width contract: explicit roles, independent edges and visible-state coverage.

The browser collects boxes and semantic relationships. These pure oracles decide
which edge a box owes; a container's measured width never defines its own target.
Local table/chart tracks use a named reference row, then compare every other row
with that row. Only intrinsic controls/labels and viewport overlays use containment.
"""

from collections import Counter
from math import isfinite
import sys
from time import monotonic


WIDTHS = (1000, 1440, 2000)
EXTENT_GROUPS = ("page", "funding", "grouped", "grid")
SECTION_LABEL_BREAKPOINT = 1280
# At 16px/rem the content cap is (80 - 10.75 - 2) * 16 = 1076px.
# With 40px of page gutters it binds above 1116px. Use the last whole CSS
# pixel below the label breakpoint, where the page has 1239px available.
STACKED_WIDTH = SECTION_LABEL_BREAKPOINT - 1
EXTENT_JOBS = tuple((width, group) for width in WIDTHS for group in EXTENT_GROUPS) + ((STACKED_WIDTH, "page"),)

BLOCK_KINDS = {
    "sections": ("main section", 10),
    "content columns": (".section-body", 9),
    "Functions caption": (".fn-summary", 1),
    "grouped Functions": (".fn-phone", 1),
    "grid Functions": (".fn-scroll", 1),
    "Funding charts": (".fund-list", 2),
    "group bands": (".group-block", 1),
    "group rows": (".group-list > li", 1),
    "foot rows": (".section-foot", 1),
    "source shells": (".srcs", 1),
    "card grids": (".cards", 1),
    "full cards": (".card:not(.mini)", 1),
    "mini cards": (".card.mini", 1),
    "running text": (".running-text", 1),
    "explanation shells": ("details.more", 1),
    "map": (".worldmap", 1),
    "map record": ("#record", 1),
    "download rows": (".dlist > li > a", 1),
    "worldwide table viewport": (".table-scroll", 1),
    "Funding record groups": (".fund-record-group", 1),
    "quotation bodies": (".quote-body", 1),
    "tooltip": ("#tip", 1),
}

# Every former mandatory kind belongs to at least one independently failing job.
# Evidence jobs also report all the roles and kinds their scoped snapshots see.
GROUP_KINDS = {
    "page": tuple(kind for kind in BLOCK_KINDS if kind not in {
        "grid Functions", "Funding record groups", "quotation bodies",
    }),
    "funding": ("Funding record groups", "source shells", "running text"),
    "grouped": ("grouped Functions", "quotation bodies", "running text"),
    "grid": ("grid Functions", "quotation bodies", "running text"),
}


def required_blocks(group: str) -> dict[str, int]:
    return {kind: BLOCK_KINDS[kind][1] for kind in GROUP_KINDS[group]}

# First matching selector wins. The registry is passed to the browser, and the
# Python role oracle consumes its matches too; there is no CSS-based role fallback.
ROLE_SELECTORS = {
    "page": "main > .measure, main > .measure > .group, main section, .group-nav, .figures, #headline, .explore, #top > .measure, #foot",
    "content": "main section > .section-body",
    "section-label": ".section-label",
    "map-tools": ".maptools",
    "map-column": ".map-col",
    "map-stack": ".worldmap, .map-aside",
    "map-side": ".side",
    "region-list": "#regions",
    "record-panel": "#record",
    "region": ".region, .region-world",
    "card": ".card",
    "card-key": ".card .row > .k, .record-rows > dt",
    "card-value": ".card .row > .v, .record-rows > dd",
    "figure": ".figures > .figure",
    "function-panel": ".fn-detail",
    "funding-panel": ".fund-detail",
    "function-claims": ".fn-phone-claims",
    "function-summary": ".fn-function-summary",
    "funding-names": ".fund-missing-line, .fund-other-line, .fund-name-list, .fund-name-item",
    "quote-body": ".fn-sources .quote-body",
    "function-table": "table.fn-table",
    "table": "table.world",
    "table-row": "table > thead, table > tbody, table tr",
    "table-cell": "table th, table td",
    "function-track": ".fn-phone-head > span, .fn-phone-band, .fn-phone-country, .fn-phone-block > ul > li > .fn-pips",
    "funding-identity": ".fund-identity, .fund-axis > h3",
    "funding-measure": ".fund-measure, .fund-scale, .fund-axis > .fund-key",
    "funding-bar": ".fund-bar, .fund-bar-head",
    "funding-figure": ".fund-measure > .fund-value, .fund-figure-head",
    "funding-fill": ".bar-value",
    "download-row": ".dlist > li > a",
    "download-title": ".dlist .dl-title",
    "download-file": ".dlist .dl-file",
    "download-description": ".dlist .d",
    "function-label-overlay": ".fn-head .g, .fn-column-group > span",
    "toolbar": ".toolbar-actions",
    "map-search": ".map-search",
    "region-chips": ".region-chips",
    "inline-quote": ".fn-sources details.quote-inline",
    "record-figure": ".budget-amount",
    "region-row-label": ".region-rows > li > .small",
    "intrinsic-text": ".group-note, .fn-function-label, .fn-function-status, .card.mini > summary .find",
    "intrinsic": ".section-foot-actions, .det.status-phrase, .sort-label, .control-label, .summary-label, #changes-latest, .group-links > .meta-item, .fn-legend > li > span, .trailing-qualifier, .fn-label-full, .fn-label-short, .panel-identity, .card .top > .name, .card.mini > summary .name, .legend-entry, .legend-copy, .fn-legend > li, .fund-key > .key-item, .activity-line, .activity-kinds > li, .region-head > *, .group-label, .group-links, .view-switch .meta-item, .tbar > .search, .toolbar-actions > *, .marks > *, .figure > *, .source-count, .record-more, .back-map, .correction, .fn-phone-band-label, .fund-other .fund-names-toggle:not(button), .fund-name-row .panel-identity, .dl-title, .dl-file, .dlist .d, .close-control, .smark, .info, .head-line, .term, .g",
    "overlay": ".popover, #tip, .suggest, .palette",
    "text": ".running-text",
    "fill": ".fn, .map-search > .search, .fn-phone-row, .fn-cell, .fund-row, .fund-name-row, button.fund-names-toggle, .region-head, table th > button, .partner-how > p, .updated, #tip .tip-sub, .fn-claim, .fn-sources, .fn-sources > li, .gl, .region-brief, .region-sub, #world-count, .hl-version, .hl-sentence, .qualified, .activity-kind, .value-line, .activity-interval, .card.mini > summary, .fund-detail > summary, .fn-summary, .fn-view-switch, .fn-legend, .fn-scroll, .fn-phone, .fn-phone-head, .fn-phone-block, .fn-phone-block > ul, .fn-phone-block > ul > li, .fn-phone-functions, .fn-phone-functions > li, .fn-detail-top, .fn-function-head, .fn-basis, .access, .setup, .groups-short, .groups-long, .group-block, .marks, .group-list, .group-list > li, #fund-rows, .fund-list, .fund-axis, .fund-chart-rows, .fund-item, .fund-other, .fund-other > div, .fund-name-list, .fund-name-item, #fund-records, .fund-detail-body, .fund-detail-body > div, .funding-record, .fund-value, .section-foot, .foot, .entries, .statusgroup, .eu-level, .eu-head, .cards, .card-row, .card > .top, .card > .row, .facts, .body-group, .body-group ul, .body-group li, .how, .partner-how, .partner-how > ul, .member-list, .members, .member-names, .srcs, .srcs > ol, .prose-panel, .claim, .quote-body, .quote-excerpt, .quote-anchor, .quote-translation, .reread, .record-rows, .record-top, .record-notes, .record-status, .region-elsewhere, .region-rows, .region-rows > li, .region-divider, .legend, .development-list, .development-row, .development-label, .table-scroll, .scroll, .tbar, .downloads, .dlgroup, .dlist, .dlist > li, .dlist > li > a, .two, #status-defs, #rules, #limits, #limits ul, #changelog, #changelog > li, .change-head, .qualifiers, .partner-how li, .body-roles, .activity-kinds, .activity-text, .activity-since, .partners-none, .related, .source-statement",
    "fold": "details.more, details.srcs, details.quote, details.member-details, details.partner-how, details.members",
    "summary": "summary",
    "control": "button, input, select, textarea",
    "heading": "h1, h2, h3, h4, h5, h6",
}

CONTAINMENT_ROLES = frozenset({"intrinsic", "intrinsic-text", "control", "summary", "toolbar", "overlay", "funding-fill", "inline-quote", "function-label-overlay", "region-row-label"})
LEFT_ANCHORED_ROLES = frozenset({"heading", "section-label", "function-summary"})


def assign_role(matches: list[str]) -> str | None:
    """A role comes from the semantic selector registry, never computed CSS."""
    return next((role for role in ROLE_SELECTORS if role in matches), None)


def page_bounds(viewport: float, rem: float = 16) -> tuple[float, float]:
    width = min(80 * rem, viewport - 2 * 1.25 * rem)
    left = (viewport - width) / 2
    return left, left + width


def content_bounds(viewport: float, rem: float = 16) -> tuple[float, float]:
    left, right = page_bounds(viewport, rem)
    if viewport >= SECTION_LABEL_BREAKPOINT:
        left += (10.75 + 2) * rem
    return left, min(right, left + (80 - 10.75 - 2) * rem)


def expected_bounds(role: str, parent: tuple[float, float], *, viewport: float, rem: float = 16,
                    index: int = 0, count: int = 1, lone: bool = False, mini: bool = False,
                    track: tuple[float, float] | None = None, indented: bool = False,
                    columns: int = 0, overflow_width: float = 0,
                    text_left: float | None = None) -> tuple[float, float]:
    """The border-box target for a declared role; local tracks are explicit inputs.

    Values here express the design contract, independent of the implementation's
    custom properties. A CSS token accidentally redefined on a child cannot move
    the oracle with it. Padding is handled separately by content_insets().
    """
    left, right = parent
    page_left, page_right = page_bounds(viewport, rem)
    if role == "page":
        return page_left, page_right
    if role == "content":
        return content_bounds(viewport, rem)
    if role == "section-label":
        return page_left, page_left + 10.75 * rem if viewport >= SECTION_LABEL_BREAKPOINT else page_right
    if role == "function-table":
        # At these desktop widths all function tracks fit the content column.
        return left, right
    if role == "table":
        # Only a table that does not fit may exceed its assigned column. Painted
        # overflow (the selected row's 8px tint) never changes the table's width.
        return left, max(right, left + overflow_width)
    if role in {"map-tools", "map-stack"}:
        return left, min(right, left + 52 * rem) if viewport <= 1100 else right
    if role == "map-column":
        return left, right if viewport <= 1100 else left + (right - left - 2 * rem) * 2 / 3
    if role == "map-side":
        return (left, min(right, left + 52 * rem)) if viewport <= 1100 else (left + (right - left - 2 * rem) * 2 / 3 + 2 * rem, right)
    if role == "region-list":
        return left, min(right, left + 30 * rem) if viewport <= 1100 else right
    if role == "map-search":
        return track if track is not None else (left, right)
    if role == "region-chips":
        return track if track is not None else (left, right)
    if role == "card":
        cols = 1 if mini else (columns or (3 if viewport > 1100 else 2 if viewport > 640 else 1))
        gap = 2.5 * rem
        width = (right - left - (cols - 1) * gap) / cols
        start = left + (index % cols) * (width + gap)
        return start, start + (2 * width + gap if lone and cols == 3 else width)
    if role in {"card-key", "card-value"}:
        return (left, min(right, left + 7.5 * rem)) if role == "card-key" else (left + (7.5 + .75) * rem, right)
    if role == "figure":
        width = (right - left - (count - 1) * 2.5 * rem) / count
        start = left + index * (width + 2.5 * rem)
        return start, start + width
    if role == "text":
        return left, min(right, left + 49 * rem)
    if role == "download-title":
        return left + 1.5 * rem, left + (1.5 + 14) * rem
    if role == "download-file":
        return left + (1.5 + 14 + .75) * rem, left + (1.5 + 14 + .75 + 13) * rem
    if role == "download-description":
        return left + (1.5 + 14 + .75 + 13 + .75) * rem, right
    if role == "mini-finding":
        left += (8.5 + .75) * rem
        return left, min(right, left + 49 * rem)
    if role == "function-summary":
        # The reference is the sibling name's text, never the summary's box.
        if text_left is not None:
            left = text_left - 1.5 * rem
        return left, min(right, left + 49 * rem)
    if role == "function-claims":
        left += (.25 + .5 + (.75 if viewport > 640 else .5)) * rem
        return left, right
    if role == "quote-body":
        left += rem
        return left, min(right, left + 49 * rem)
    if track is not None:
        return track
    if indented:
        left += rem
    return left, right


def content_insets(node: dict, rem: float = 16) -> tuple[float, float]:
    """Named horizontal padding/border geometry, not a reading of computed CSS."""
    role = node["role"]
    if role == "record-panel":
        return rem + 1, rem + 1
    if role in {"function-panel", "funding-panel"}:
        return rem, rem
    if role == "region":
        return .75 * rem, .75 * rem
    if node.get("cite"):
        return rem + 2, 0
    if role == "function-summary":
        return 1.5 * rem, 0
    if role == "download-row":
        return .5 * rem, .5 * rem
    if role == "funding-identity":
        return .5 * rem, 0
    if node.get("world_cell"):
        return .75 * rem, .75 * rem
    if node.get("tooltip"):
        return .5 * rem + 1, .5 * rem + 1
    if node.get("opened_summary"):
        return 0, max(44, 2.5 * rem, 1.75 * rem) + .75 * rem
    if node.get("list_indent"):
        return 1.25 * rem, 0
    return 0, 0


def extent_errors(snapshot: dict) -> list[str]:
    """Check each box against its one role edge, then compare same-edge siblings."""
    errors, expected, siblings = [], {}, {}
    viewport, rem = snapshot["viewport"], snapshot.get("rem", 16)
    for node in snapshot["nodes"]:
        role = assign_role(node["matches"])
        name = node["name"]
        parent = expected.get(node.get("parent"), page_bounds(viewport, rem))
        if role is None:
            errors.append(f"unclassified block: {name}")
            # Keep the nearest enclosing shell for descendants, even when an
            # intervening wrapper is missing from the registry. It still fails.
            expected[node["id"]] = parent
            continue
        node = {**node, "role": role}
        target = expected_bounds(role, parent, viewport=viewport, rem=rem,
                                 **{key: node[key] for key in ("index", "count", "lone", "mini", "track", "indented", "columns", "overflow_width", "text_left") if key in node})
        actual = node["left"], node["right"]
        if not all(isfinite(value) for value in (*actual, *target)):
            errors.append(f"{name}: non-finite geometry")
            continue
        # Text roles must carry a glyph edge; any other block measured as running text (a function summary) is held to it too.
        if role in {"text", "intrinsic-text"} or "text_right" in node:
            text_right = node["text_right"]
            if not isfinite(text_right):
                errors.append(f"{name}: non-finite text geometry")
            elif text_right > actual[1] + 1:
                errors.append(f"{name}: text runs past its block: right {text_right:.2f}, maximum {actual[1]:.2f}")
        if role in LEFT_ANCHORED_ROLES:
            if role == "function-summary" and node.get("text_left") is None:
                errors.append(f"{name}: missing function name text edge")
            if abs(actual[0] - target[0]) > 1 or actual[1] > target[1] + 1:
                errors.append(f"{name}: {role} left {actual[0]:.2f}, expected {target[0]:.2f}; right {actual[1]:.2f}, maximum {target[1]:.2f}")
            if role == "function-summary" and (actual[0] < parent[0] - 1 or actual[1] > parent[1] + 1):
                errors.append(f"{name}: function-summary outside its panel content")
            target = actual
        elif role in CONTAINMENT_ROLES:
            # The complete popover may protrude from its tiny opener; its named
            # overlay track is the viewport, never an ordinary section shell.
            limit = (0, viewport) if role in {"overlay", "function-label-overlay"} else target
            if actual[0] < limit[0] - 1 or actual[1] > limit[1] + 1:
                errors.append(f"{name}: {role} outside its named track {limit}")
            if role == "intrinsic-text" and actual[1] - actual[0] > 49 * rem + 1:
                errors.append(f"{name}: intrinsic text exceeds the text measure")
            if node.get("tooltip") and actual[1] - actual[0] > min(18 * rem, viewport - 2 * rem) + 1:
                errors.append(f"{name}: tooltip exceeds its named width")
            target = parent if role == "inline-quote" else actual
        elif any(abs(got - want) > 1 for got, want in zip(actual, target)):
            errors.append(f"{name}: {role} edges {actual[0]:.2f}..{actual[1]:.2f}, expected {target[0]:.2f}..{target[1]:.2f}")
        # Direct sibling comparison catches two opposite sub-pixel errors that
        # individually fall within tolerance but visibly separate common rules.
        if role not in CONTAINMENT_ROLES | LEFT_ANCHORED_ROLES:
            key = node.get("parent"), round(target[0], 5), round(target[1], 5)
            previous = siblings.get(key)
            if previous and any(abs(a - b) > 1 for a, b in zip(actual, previous[1])):
                errors.append(f"{name}: edge differs from sibling {previous[0]}")
            else:
                siblings[key] = name, actual
        inset_left, inset_right = content_insets(node, rem)
        expected[node["id"]] = target[0] + inset_left, target[1] - inset_right
    for group in snapshot.get("track_groups", []):
        owner = expected.get(group["owner"])
        tracks = group["tracks"]
        if not owner or not tracks:
            errors.append(group['name'] + ': no reference tracks')
            continue
        if abs(tracks[0][0] - owner[0]) > 1 or abs(tracks[-1][1] - owner[1]) > 1:
            errors.append(group['name'] + ': reference tracks do not span their assigned owner')
        if any(abs(right[0] - left[1] - group['gap']) > 1 for left, right in zip(tracks, tracks[1:])):
            errors.append(group['name'] + ': reference track gaps differ from their named spacing')
    return errors


def coverage_errors(counts: dict, required: dict) -> list[str]:
    """Expected counts come from controls before opening, not from passing boxes."""
    return [f"{kind}: inspected {counts.get(kind, 0)} of {minimum} required states"
            for kind, minimum in required.items() if counts.get(kind, 0) < minimum]


# Computed display alone is insufficient: flex/grid items are blockified even
# when they are just a mark or a label. Only filling items owe a column role;
# named intrinsic items and controls retain their separate containment checks.
BLOCK_PARTICIPATION_JS = r"""(style, parent, named) => {
  const display=style.display;
  if(['none','contents','table-column','table-column-group'].includes(display) || display.includes('ruby')) return false;
  if(['absolute','fixed'].includes(style.position)) return named;
  const layout=parent.display;
  if(['flex','inline-flex','grid','inline-grid'].includes(layout)) {
    if(named) return true;
    if(layout.endsWith('grid')) {
      const align=style.justifySelf==='auto'?parent.justifyItems:style.justifySelf;
      return ['normal','stretch'].includes(align);
    }
    if(parent.flexDirection.startsWith('column')) {
      const align=style.alignSelf==='auto'?parent.alignItems:style.alignSelf;
      return ['normal','stretch'].includes(align);
    }
    return parseFloat(style.flexGrow)>0;
  }
  return ['block','flow-root','flex','grid','list-item','table','table-caption',
    'table-row-group','table-header-group','table-footer-group','table-row','table-cell'].includes(display) || display.startsWith('block ');
}"""


SECTION_EXTENTS_JS = r"""({selectors, scope, kinds}) => {
  const participates = """ + BLOCK_PARTICIPATION_JS + r""";
  const entries = Object.entries(selectors), nodes = [], ids = new Map(), tracks = {};
  const visible = n => !n.closest('.vh, [hidden], svg, script, style, template') &&
    n.checkVisibility({visibilityProperty:true, contentVisibilityAuto:true}) && n.getClientRects().length;
  const box = n => {const r=n.getBoundingClientRect();return [r.left,r.right];};
  const textRight = (n, left) => {
    const walker=document.createTreeWalker(n,NodeFilter.SHOW_TEXT), range=document.createRange();
    let text, right=left;
    while((text=walker.nextNode())) {
      if(!text.data.trim() || !visible(text.parentElement)) continue;
      // Range rectangles include text clipped by an ellipsis. Exclude that
      // text, including inline descendants of the box doing the clipping.
      let clipped=false;
      for(let p=text.parentElement;p;p=p.parentElement) {
        const s=getComputedStyle(p);
        if(s.textOverflow==='ellipsis' && s.overflowX!=='visible' && s.display!=='inline') {clipped=true;break;} // an inline box clips nothing
        if(p===n) break;
      }
      if(clipped) continue;
      range.selectNodeContents(text);
      for(const r of range.getClientRects()) if(r.width && r.height) right=Math.max(right,r.right);
    }
    return right;
  };
  const name = n => n.tagName.toLowerCase()+(n.id?'#'+n.id:'')+(n.classList.length?'.'+[...n.classList].join('.'):'');
  const matches = n => {const found=entries.find(([,s])=>n.matches(s));return found?[found[0]]:[];};
  const candidate = (n, roles) => {
    if(n.matches('details.quote-inline, .smark.activity, button, input, select, textarea')) return true;
    let parent=n.parentElement;
    while(parent && getComputedStyle(parent).display==='contents') parent=parent.parentElement;
    return participates(getComputedStyle(n), parent?getComputedStyle(parent):{}, roles.length>0);
  };
  const roots=[...document.querySelectorAll('main > .measure, #top > .measure, #foot, #tip')];
  const focus=scope?document.querySelector(scope):null;
  if(scope && (!focus || !visible(focus))) return {missing:scope};
  // A panel needs its ancestors for edge propagation, but no unrelated subtree.
  const ancestors=[];
  if(focus) for(let p=focus.parentElement;p;p=p.parentElement) if(roots.some(root=>root===p || root.contains(p))) ancestors.unshift(p);
  const candidates=focus?[...ancestors,focus,...focus.querySelectorAll('*')]:roots.flatMap(root=>[root,...root.querySelectorAll('*')]);
  for(const n of candidates) {
    if(!visible(n)) continue;
    const roles = matches(n), role=roles[0];
    if(!candidate(n, roles)) continue;
    const r=box(n);
    let p=n.parentElement; while(p && !ids.has(p)) p=p.parentElement;
    const item={id:nodes.length,name:name(n),matches:roles,parent:ids.get(p),left:r[0],right:r[1],
      cite:n.matches('.cite'),world_cell:n.matches('table.world td'),
      tooltip:n.matches('#tip'),opened_summary:n.matches('.card.mini[open] > summary, .fund-detail[open] > summary'),
      list_indent:n.matches('#changelog, #limits ul')};
    if(role==='text' || role==='intrinsic-text' || n.matches('.running-text')) item.text_right=textRight(n,r[0]);
    if(role==='card') {
      item.index=[...n.parentElement.children].filter(e=>e.matches('.card')).indexOf(n);
      item.mini=n.matches('.mini');
      item.lone=n.parentElement.matches('.card-row:only-child') && n.matches(':only-child');
    }
    if(role==='figure') { item.index=[...n.parentElement.children].indexOf(n); item.count=n.parentElement.children.length; }
    if(role==='map-search' || role==='region-chips') {
      const owner=n.closest('.maptools'), b=box(owner), gap=32;
      // The stack uses a wrapping search row with intrinsic chips. The named
      // map-tools tracks are separate from ordinary section columns.
      if(innerWidth>1100) item.track=role==='map-search'?[b[0]+(b[1]-b[0]-gap)*2/3+gap,b[1]]:[b[0],b[0]+(b[1]-b[0]-gap)*2/3];
      else item.matches=['intrinsic'];
    }
    if(role==='table') {
      const scroller=n.closest('.fn-scroll, .scroll');
      // A fitting table ends at the independent shell edge. A scrolling table
      // may be intrinsically wider, but its painted overflow is never width.
      if(!scroller.classList.contains('fits')) item.overflow_width=r[1]-r[0];
    }
    if(role==='function-summary') {
      const label=n.parentElement.querySelector(':scope > .fn-function-label > span:not(.fn-mark)');
      item.text_left=label?box(label)[0]:null;
    }
    if(role==='table-row') item.track=box(n.closest('table'));
    if(role==='table-cell') {
      const table=n.closest('table'), row=n.parentElement;
      if(n.colSpan>1 || row.closest('thead')) item.matches=['intrinsic'];
      else {
        const reference=[...table.querySelectorAll('thead tr')].find(r=>r.children.length===row.children.length);
        const index=[...row.children].indexOf(n), head=reference?.children[index];
        item.track=head?box(head):box(n); // row/track counts are held by the table structure jobs
      }
    }
    if(role==='function-panel' && !n.matches('.fn-phone-detail')) {
      const scroll=n.closest('.fn-scroll'), r=box(scroll); item.track=[r[0],r[0]+scroll.clientWidth];
    }
    if(role==='function-track') {
      const list=n.closest('.fn-phone'), heads=[...list.querySelectorAll('.fn-phone-head > span')];
      let index=n.matches('.fn-phone-country')?0:n.matches('.fn-phone-band')?1+[...n.parentElement.querySelectorAll(':scope > .fn-phone-band')].indexOf(n):n.matches('.fn-pips')?1+[...n.parentElement.querySelectorAll(':scope > .fn-pips')].indexOf(n):heads.indexOf(n);
      item.track=box(heads[index]);
    }
    if(['funding-identity','funding-measure','funding-bar','funding-figure'].includes(role)) {
      const chart=n.closest('.fund-list'), first=chart?.querySelector('.fund-item');
      const identity=first?.querySelector('.fund-identity'), measure=first?.querySelector('.fund-measure');
      if(identity && measure) {
        const byRole={'funding-identity':identity,'funding-measure':measure,'funding-bar':first.querySelector('.fund-bar'),'funding-figure':measure.querySelector('.fund-value')};
        item.track=box(byRole[role]);
      }
    }
    // A text node's declared indentation belongs to its owner, never to a
    // computed max-width. Nested prose lists retain the normal list inset.
    ids.set(n,item.id);nodes.push(item);
  }
  for(const item of nodes) tracks[item.matches[0] || 'unclassified']=(tracks[item.matches[0] || 'unclassified']||0)+1;
  const coverage=Object.fromEntries(Object.entries(kinds).map(([kind,[selector]])=>[kind,[...ids.keys()].filter(n=>n.matches(selector)).length]));
  const track_groups=[];
  for(const owner of ids.keys()) if(owner.matches('.fn-phone')) {
    track_groups.push({name:'grouped Functions columns',owner:ids.get(owner),gap:0,tracks:[...owner.querySelectorAll('.fn-phone-head > span')].map(box)});
  }
  for(const owner of ids.keys()) if(owner.matches('.fund-list')) {
    const first=owner.querySelector('.fund-item');
    if(first) track_groups.push({name:'Funding '+owner.dataset.kind+' tracks',owner:ids.get(owner),gap:8,
      tracks:[first.querySelector('.fund-identity'),first.querySelector('.fund-bar'),first.querySelector('.fund-measure > .fund-value')].map(box)});
  }
  return {viewport:innerWidth,rem:parseFloat(getComputedStyle(document.documentElement).fontSize),nodes,counts:tracks,coverage,track_groups};
}"""


OPEN_FOLDS_JS = r"""async scope => {
  const root=scope?document.querySelector(scope):document.querySelector('main');
  if(!root) return {expected:{},counts:{},remaining:['missing scope '+scope]};
  const visible=d=>d.checkVisibility({visibilityProperty:true}) && d.getClientRects().length;
  const folds=()=>[...root.querySelectorAll('details')].filter(d=>!d.matches('.cols,.download-menu') && visible(d));
  for(let pass=0;pass<8;pass++) {
    const closed=folds().filter(d=>!d.open);
    if(!closed.length) break;
    await Promise.all(closed.map(d=>new Promise(resolve=>{
      d.addEventListener('toggle',resolve,{once:true});d.open=true;
    })));
  }
  await document.fonts.ready;await new Promise(requestAnimationFrame);await new Promise(requestAnimationFrame);
  const selectors={'explanation folds':'details.more:not(.fund-detail)','source folds':'details.srcs','quote folds':'details.quote','mini cards':'details.card.mini'};
  const expected={},counts={};
  for(const [kind,selector] of Object.entries(selectors)) {
    const found=folds().filter(d=>d.matches(selector));expected[kind]=found.length;counts[kind]=found.filter(d=>d.open).length;
  }
  return {expected,counts,remaining:folds().filter(d=>!d.open).map(d=>d.id||d.className)};
}"""


def check_section_extents(browser, base_url: str, width: int, group: str) -> list[str]:
    """One width and state family per job; one hydrated page for all its states."""
    from check_page import REVEAL_FUNDING_LIST_JS, hydrated, layout_settled, printed_report_line, report_line

    if (width, group) not in EXTENT_JOBS:
        raise ValueError(f"unknown section extent job: {width}/{group}")
    errors, required = [], {}
    counts, role_counts, block_counts = Counter(), Counter(), Counter()
    prefix = f"{width}px {group}"
    started = monotonic()

    def progress(label):
        line = report_line(f"{prefix} section extents: {label} ({monotonic() - started:.1f}s)")
        errors.append(line)
        # Survives a watchdog exit before the job returns its normal report.
        print(printed_report_line(line), file=sys.stderr, flush=True)

    page = browser.new_page(viewport={"width": width, "height": 1000})
    try:
        hydrated(page, base_url, fn_view="default")
        page.add_style_tag(content=".offscreen, .card-row {content-visibility:visible !important;}")

        def inspect(label: str, scope: str | None = None, *, open_folds=True):
            if open_folds:
                opened = page.evaluate(OPEN_FOLDS_JS, scope)
                errors.extend(f"{prefix} {label}: unopened fold {name}" for name in opened['remaining'])
                errors.extend(f"{prefix} {label}: {finding}" for finding in coverage_errors(opened['counts'], opened['expected']))
                for kind, number in opened['expected'].items():
                    required[kind] = max(required.get(kind, 0), number)
                    counts[kind] = max(counts[kind], opened['counts'][kind])
            else:
                layout_settled(page)
            # One evaluate reads every visible block, text edge, role, track
            # and kind. Scoped states keep ancestors but skip other subtrees.
            snapshot = page.evaluate(SECTION_EXTENTS_JS, {"selectors": ROLE_SELECTORS, "scope": scope, "kinds": BLOCK_KINDS})
            if snapshot.get("missing"):
                errors.append(f"{prefix} {label}: missing visible scope {snapshot['missing']}")
                return False
            errors.extend(f"{prefix} {label}: {finding}" for finding in extent_errors(snapshot))
            for role, number in snapshot["counts"].items():
                role_counts[role] = max(role_counts[role], number)
            for kind, number in snapshot["coverage"].items():
                block_counts[kind] = max(block_counts[kind], number)
            return True

        if group == "page":
            required.update({"map record": 1, "earlier News": 1, "explanation folds": 1,
                             "source folds": 1, "quote folds": 1, "mini cards": 1})
            inspect("initial closed folds", open_folds=False)
            progress("initial closed folds complete")
            earlier = page.locator('#developments-more')
            if earlier.count() and earlier.is_visible() and earlier.get_attribute('aria-expanded') == 'false':
                earlier.click()
                counts['earlier News'] += 1
            inspect("all native folds and earlier News")
            progress("native folds and earlier News complete")
            page.locator('#map-search').fill('United Kingdom')
            page.locator('#map-search').press('Enter')
            counts['map record'] += int(inspect("map record", '#record'))
            tip = page.locator('#record [data-tip-sub], #record [aria-description]').first
            if tip.count():
                tip.dispatch_event('mouseover')
                inspect('tooltip', '#tip', open_folds=False)
                page.keyboard.press('Escape')
            progress("map record and tooltip complete")
        elif group == "funding":
            targets = page.locator('#fund-rows [aria-controls^="fund-detail-"]').evaluate_all("ns=>[...new Set(ns.map(n=>n.getAttribute('aria-controls')))]")
            required['Funding details'] = max(1, len(targets))
            for index, target in enumerate(targets):
                button = page.locator(f'#fund-rows [aria-controls="{target}"]').first
                button.evaluate(REVEAL_FUNDING_LIST_JS)
                button.evaluate("n=>{if(n.getAttribute('aria-expanded')!=='true') n.click();}")
                counts['Funding details'] += int(inspect(target, f"#{target}[open]"))
                button.evaluate('n=>n.click()')
                progress(f"Funding details {index + 1}/{len(targets)} complete")
        else:
            view = group
            required[view + ' view'] = 1
            page.evaluate("v=>document.getElementById('functions-table').fnView(v)", view)
            scope = '.fn-phone' if view == 'grouped' else '.fn-scroll'
            counts[view + ' view'] += int(inspect(view + ' view', scope))
            progress(view + " view complete")
            controls = page.locator('.fn-phone-row' if view == 'grouped' else '.fn-cell')
            indices = range(controls.count())
            required[view + ' evidence'] = max(1, len(indices))
            for done, index in enumerate(indices, 1):
                control = controls.nth(index)
                control.evaluate("n=>{if(n.getAttribute('aria-expanded')!=='true') n.click();}")
                if view == 'grouped':
                    page.locator('.fn-phone-detail button.fn-function-label[aria-expanded="false"]').evaluate_all('ns=>ns.forEach(n=>n.click())')
                counts[view + ' evidence'] += int(inspect(f'{view} evidence {index + 1}', '.fn-detail'))
                control.evaluate('n=>n.click()')
                if done % 5 == 0 or done == len(indices):
                    progress(f"{view} evidence {done}/{len(indices)} complete")
        errors.extend(f"{prefix}: {finding}" for finding in coverage_errors(counts, required))
        errors.extend(f"{prefix} blocks: {finding}" for finding in coverage_errors(block_counts, required_blocks(group)))
        progress('states: ' + ', '.join(f'{k}={counts[k]}/{v}' for k, v in required.items()))
        progress('roles (maximum visible): ' + ', '.join(f'{k}={v}' for k, v in sorted(role_counts.items())))
        progress('kinds (maximum visible): ' + ', '.join(f'{k}={v}' for k, v in sorted(block_counts.items())))
    finally:
        page.close()
    return list(dict.fromkeys(errors))
