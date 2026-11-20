"""Page-wide separator inventory and glyph oracle, independent of the layout pass."""

# These are reader-facing kinds, not implementation classes for the dots themselves.
# Narrower source and record kinds take precedence over their enclosing card.
SEPARATOR_LINES = {
    "function sources": ".fn-sources > li",
    "sources": "li[data-source-id]",
    "card meta": ".card .source-after",
    "source footer": ".source-after",
    "funding records": ".fund-context, .funding-native, .funding-metadata, .fund-detail-body .qualifiers",
    "card records": ".card .qualifiers, .card .partners, .card .partner-how li, .card .member-names",
    "map record": "#record .qualifiers, #record .partners",
    # budgetValue() and staffValue() also render outside cards and map records.
    "worldwide qualifiers": "table.world .qualifiers",
    "quotation tails": ".quote .reread, .claim > .reread",
    "news labels": ".development-label",
    "news text": ".development-text",
    "hero update": ".updated",
    "group navigation": ".group-links",
    "functions switch": ".fn-view-switch",
    "function status": ".fn-phone-functions > li, .fn-function-head, .fn-detail-context",
    "tooltip sub-line": "#tip .tip-sub",
    "world count": "#world-count",
    "version history": "#changes > summary, .change-head",
    "footer": "#foot > p:has(.foot-feed)",
    "method source": "#method-sec .inline-links",
}

PAGE_SEPARATORS_JS = r"""kinds => {
  const counts = Object.fromEntries(Object.keys(kinds).map(k => [k, {lines:0, separators:0, visible:0}]));
  counts['re-read tails'] = {lines:0, separators:0, visible:0};
  counts['archived-copy tails'] = {lines:0, separators:0, visible:0};
  const errors = [], range = document.createRange();
  const visible = n => n.checkVisibility({contentVisibilityAuto:true, visibilityProperty:true}) &&
    !n.closest('.vh, [hidden], script, style, template') && n.getClientRects().length;
  const entries = Object.entries(kinds);
  for(const [kind,selector] of entries) counts[kind].lines = [...document.querySelectorAll(selector)].filter(visible).length;
  const lineOf = parent => {
    for (const [kind, selector] of entries) { const line = parent.closest(selector); if (line) return [kind,line]; }
    return null;
  };
  const glyph = (node,i) => { range.setStart(node,i); range.setEnd(node,i+1); return range.getBoundingClientRect(); };
  const sameLine = (a,b) => a && b && a.width && b.width && a.height && b.height &&
    Math.min(a.bottom,b.bottom)-Math.max(a.top,b.top) > Math.min(a.height,b.height)/2;
  const readable = (t,line) => visible(t.parentElement) && !t.parentElement.closest('.cites') &&
    (line.closest('.quote-body') || !t.parentElement.closest('.quote-body'));
  // At 400px and narrower the section links show no dots by rule, so the row keeps to one line (principle: Separator dots).
  const spacedNav = kind => kind==='group navigation' && globalThis.innerWidth<=400;
  // Walk the reader-facing line, independently of the page's item/container lookup.
  // Neighbouring ink also works when the separator itself has display:none.
  const neighbours = (line,node,i) => {
    const text=document.createTreeWalker(line,NodeFilter.SHOW_TEXT); let before=null, after=false;
    while(text.nextNode()) {
      const t=text.currentNode, shown=readable(t,line);
      for(let j=0;j<t.length;j++) {
        if(t===node && j===i) { after=true; continue; }
        if(/[\s·]/.test(t.data[j]) || !shown) continue;
        const r=glyph(t,j); if(!r.width || !r.height) continue;
        if(after) return [before,r];
        before=r;
      }
    }
    return [before,null];
  };
  const visibleByLine = new Map();
  // Find all separators, including raw text that has bypassed both builders. A middle
  // dot within a word is not a separator. Hidden line-end dots still count as coverage.
  const walker = document.createTreeWalker(document.body,NodeFilter.SHOW_TEXT);
  while(walker.nextNode()) {
    const node=walker.currentNode, parent=node.parentElement;
    if(!parent || parent.closest('script,style,template,.vh')) continue;
    for(let i=0;i<node.length;i++) {
      if(node.data[i]!=='·' || (/\S/.test(node.data[i-1]||' ') && /\S/.test(node.data[i+1]||' '))) continue;
      const marker=parent.closest('.meta-dot, .qualifier-separator');
      if(!visible(parent) && !(marker && visible(marker.parentElement))) continue;
      const assigned=lineOf(parent);
      if(!assigned) {
        const line=parent.closest('p, li, td, th, summary, div') || parent;
        errors.push('unclassified separator: '+line.textContent.trim()); continue;
      }
      const [kind,line]=assigned;
      counts[kind].separators++;
      const dot=glyph(node,i), shown=!!(visible(parent) && dot.width && dot.height);
      if(shown) {
        counts[kind].visible++;
        visibleByLine.set(line,(visibleByLine.get(line)||0)+1);
      }
      for(const [tail,selector] of [['re-read tails','.reread-tail'],['archived-copy tails','.archive-copy']]) {
        if(parent.closest(selector)) { counts[tail].lines++; counts[tail].separators++; if(shown) counts[tail].visible++; }
      }
      if(!marker) errors.push(kind+': unbound separator: '+line.textContent.trim());
      const [before,next]=neighbours(line,node,i);
      if(!shown && sameLine(before,next) && !spacedNav(kind)) errors.push(kind+': separator hidden mid-line: '+line.textContent.trim());
      if(shown && (!sameLine(before,dot) || before.left>=dot.left-.5)) errors.push(kind+': visible separator begins a line: '+line.textContent.trim());
      if(shown && marker?.matches('.meta-dot') && !sameLine(dot,next)) errors.push(kind+': visible separator ends a line: '+line.textContent.trim());
    }
  }
  // These rows are defined by their separators. Count adjacent items even if
  // every dot was removed, and check each row so another row cannot mask it.
  const required = {'group navigation':'a', 'functions switch':'button',
    'hero update':':scope > .meta-item', 'footer':':scope > .meta-item'};
  const edge = (item,last) => {
    const text=document.createTreeWalker(item,NodeFilter.SHOW_TEXT); let found=null;
    while(text.nextNode()) {
      const t=text.currentNode;
      if(!readable(t,item) || t.parentElement.closest('.meta-dot, .qualifier-separator')) continue;
      for(let j=0;j<t.length;j++) {
        if(/\s/.test(t.data[j])) continue;
        const r=glyph(t,j); if(!r.width || !r.height) continue;
        found=r; if(!last) return found;
      }
    }
    return found;
  };
  for(const [kind,items] of Object.entries(required)) {
    if(!kinds[kind]) continue;
    for(const line of [...document.querySelectorAll(kinds[kind])].filter(visible)) {
      const nodes=[...line.querySelectorAll(items)].filter(visible);
      if(nodes.length<2) errors.push(kind+': missing separator items: '+line.textContent.trim());
      const sharesLine=nodes.some((item,i)=>i && sameLine(edge(nodes[i-1],true),edge(item,false)));
      if(sharesLine && !visibleByLine.get(line) && !spacedNav(kind)) errors.push(kind+': zero visible separators between items sharing a line: '+line.textContent.trim());
    }
  }
  return {counts, errors:[...new Set(errors)]};
}"""


def separator_coverage_errors(counts: dict, required) -> list[str]:
    """A missing kind is a failure; lines with no separating content report zero dots."""
    return [f"no separator coverage: {kind}" for kind in required
            if not counts.get(kind, {}).get("lines")]
