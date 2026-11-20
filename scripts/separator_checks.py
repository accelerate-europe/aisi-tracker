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
    "footer": "#foot > p",
    "method source": "#method-sec .inline-links",
}

PAGE_SEPARATORS_JS = r"""kinds => {
  const counts = Object.fromEntries(Object.keys(kinds).map(k => [k, {lines:0, separators:0}]));
  counts['re-read tails'] = {lines:0, separators:0};
  counts['archived-copy tails'] = {lines:0, separators:0};
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
  // Find all separators, including raw text that has bypassed both builders. A middle
  // dot within a word is not a separator. Hidden line-end dots still count as coverage.
  const walker = document.createTreeWalker(document.body,NodeFilter.SHOW_TEXT);
  while(walker.nextNode()) {
    const node=walker.currentNode, parent=node.parentElement;
    if(!parent || parent.closest('script,style,template,.vh')) continue;
    for(let i=0;i<node.length;i++) {
      if(node.data[i]!=='·' || (/\S/.test(node.data[i-1]||' ') && /\S/.test(node.data[i+1]||' '))) continue;
      const hiddenMark=parent.closest('.meta-dot.line-end, .own-line .qualifier-separator');
      if(!visible(parent) && !(hiddenMark && visible(hiddenMark.parentElement))) continue;
      const assigned=lineOf(parent);
      if(!assigned) {
        const line=parent.closest('p, li, td, th, summary, div') || parent;
        errors.push('unclassified separator: '+line.textContent.trim()); continue;
      }
      const [kind,line]=assigned;
      counts[kind].separators++;
      for(const [tail,selector] of [['re-read tails','.reread-tail'],['archived-copy tails','.archive-copy']]) {
        if(parent.closest(selector)) { counts[tail].lines++; counts[tail].separators++; }
      }
      if(!hiddenMark && !parent.closest('.meta-dot, .qualifier-separator')) errors.push(kind+': unbound separator: '+line.textContent.trim());
      if(!visible(parent)) continue;
      const dot=glyph(node,i); if(!dot.width || !dot.height) continue;
      const text=document.createTreeWalker(line,NodeFilter.SHOW_TEXT); let before=false;
      while(text.nextNode() && !before) {
        const t=text.currentNode;
        if(!visible(t.parentElement) || t.parentElement.closest('.cites') ||
           (!line.closest('.quote-body') && t.parentElement.closest('.quote-body'))) continue;
        for(let j=0;j<t.length;j++) {
          if(/\s/.test(t.data[j]) || t===node && j===i) continue;
          const r=glyph(t,j);
          if(r.width && r.left<dot.left-.5 && Math.abs((r.top+r.bottom-dot.top-dot.bottom)/2)<3) { before=true; break; }
        }
      }
      if(!before) errors.push(kind+': visible separator begins a line: '+line.textContent.trim());
    }
  }
  return {counts, errors:[...new Set(errors)]};
}"""


def separator_coverage_errors(counts: dict, required) -> list[str]:
    """A missing kind is a failure; lines with no separating content report zero dots."""
    return [f"no separator coverage: {kind}" for kind in required
            if not counts.get(kind, {}).get("lines")]
