"""The browser renders supplied choices and field evidence, including empty bindings."""

import copy
import json
import re
import subprocess
from pathlib import Path

import pytest
from bs4 import BeautifulSoup

from scripts.check_page import GENERIC_PARTNER_HOW, PLURAL_PARTNER_HOW, expected_activity

PAGE = Path(__file__).resolve().parents[1] / "site/index.html"


def test_staff_bars_require_a_dated_total_and_keep_bounds_and_zero():
    source = PAGE.read_text()
    helpers = source[source.index("  function selectedRecord("):source.index("  // Staff counts arrive")]
    helpers += source[source.index("    function comparedBudget("):source.index("    var fundItems =")]
    script = """
const assert=require('node:assert/strict'), BAR_TIP='Minimum positive width';
function el(tag,className){return {tag,className,dataset:{},style:{},children:[],setAttribute(k,v){this[k]=v;},appendChild(n){this.children.push(n);}};}
""" + helpers + """
const institute=record=>({display:{total_staff:record?'staff':null,staff_records:record?{staff:record}:{}}});
for (const record of [null,{total:25},{total:25,as_of:''},{target:50,as_of:'2026-10'},{technical:20,as_of:'2026-10'},
  {total:'25',as_of:'2026-10'},{total:NaN,as_of:'2026-10'},{total:Infinity,as_of:'2026-10'}]) assert.equal(comparedStaff(institute(record)),null);
for (const qualifier of ['exact','about','at_least','at_most','more_than','less_than','nearly']) {
  const record={total:140,total_qualifier:qualifier,as_of:'2026-10',display:{total:'140'}};
  assert.equal(comparedStaff(institute(record)),record);
  const words={exact:null,about:'about',at_least:'at least',at_most:'up to',more_than:'more than',less_than:'less than',nearly:'nearly'};
  assert.equal(staffBound(record),words[qualifier]);
  assert.equal(staffBound({technical_qualifier:qualifier,display:{technical:'30'}},'technical'),words[qualifier]);
}
const zero={total:0,as_of:'2026-10',display:{total:'0'}};
assert.equal(comparedStaff(institute(zero)),zero);
assert.equal(staffBound({display:{total:'~240'}}),'about');
for (const [value,scale,wanted] of [[140,250,'56.00000000000001%'],[50,100,'50%'],[0,250,'0%'],[1,250,'0.4%']]) {
  const track=measureBar(value,scale,false);
  assert.equal(track.dataset.value,value);assert.equal(track.dataset.scale,scale);
  assert.equal(track.children.length,1);assert.ok(Math.abs(parseFloat(track.children[0].style.width)-parseFloat(wanted))<1e-10);
  assert.equal(track.children[0].className,'bar-value');
}
assert.equal(measureBar(null,250,false).children.length,0);
assert.equal(measureBar(5,10,true).children[0].className,'bar-value pending');
"""
    subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True)


def test_empty_staff_records_do_not_become_a_target_and_dated_targets_win():
    source = PAGE.read_text()
    helper = source[source.index("  function selectedRecord("):source.index("  // Staff counts arrive")]
    choice = source[source.index('      var latest = selectedRecord(i, "staff", "total_staff")'):source.index("      function staffSummary()")]
    script = """
const assert=require('node:assert/strict'), comparedStaff=()=>null;
""" + helper + "\nfunction choose(i){\n" + choice + "\nreturn described;\n}\n" + """
const empty={id:'empty',display:{target:null}}, older={id:'older',target:100,as_of:'2025'}, newer={id:'newer',target:50,as_of:'2026-10'};
const institute=records=>({display:{total_staff:null,technical_staff:null,staff:'empty',staff_ids:records.map(r=>r.id),staff_records:Object.fromEntries(records.map(r=>[r.id,r]))}});
assert.equal(choose(institute([empty])),null);
assert.equal(choose(institute([empty,newer,older])),newer);
const a={id:'a',target:40,as_of:'2026-10'}, z={id:'z',target:60,as_of:'2026-10'};
assert.equal(choose(institute([empty,z,a])),a);
"""
    subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True)


def test_funding_detail_keeps_each_record_in_its_own_group():
    source = PAGE.read_text()
    helper = source[source.index("      function detailsBody() {"):source.index('      var det = el("details", "more quiet fund-detail"')]
    script = """
const assert=require('node:assert/strict');
function el(tag,className,text){return {tag,className:className||'',text:text||'',dataset:{},children:[],
  appendChild(n){this.children.push(n);return n;},insertBefore(n,before){this.children.splice(this.children.indexOf(before),0,n);},
  get firstChild(){return this.children[0];},get lastChild(){return this.children[this.children.length-1];}};}
const document={createTextNode:text=>el('text','',text)}, S={}, m={}, j={};
const funding=[
  {id:'own',coverage:'whole_institute',commitment:'adopted',amount:100},
  {id:'component',coverage:'component',commitment:'adopted',amount:50,description:'Shared computing programme'},
  {id:'proposal',coverage:'whole_institute',commitment:'proposed',amount:200},
  {id:'related',coverage:'not_institute_specific',commitment:'adopted',amount:300},
];
const staff=[{id:'total',total:25,display:{total:'25'},as_of:'2026-10',notes:'Staffing qualification'},
  {id:'target',target:50,display:{target:'50'},as_of:'2027'}];
const i={staffing_profile:'Staff are seconded.',display:{funding_ids:funding.map(r=>r.id),funding_records:Object.fromEntries(funding.map(r=>[r.id,r])),
  staff_ids:staff.map(r=>r.id),staff_records:Object.fromEntries(staff.map(r=>[r.id,r]))}};
const fundOrder=['own-source','staff-source'];
const staffSummary=()=>el('div','fund-staff-summary'), fu=null;
const fundingRecord=item=>{const n=el('div','funding-record',item.id);n.dataset.record='funding';n.dataset.recordId=item.id;n.appendChild(el('p','',item.id));return n;};
const relatedFunding=()=>el('div','related-funding','Related amounts');
const publishedFunding=item=>item.description;
const staffText=item=>item.display.total, withPeriod=text=>text, asOf=text=>'as of '+text, confTag=()=>null;
const qualified=(value,parts)=>{const n=el('span','qualified');n.appendChild(el('span','value-line',value));n.appendChild(el('span','qualifiers',parts.filter(Boolean).join(' · ')));return n;};
const fieldCitations=()=>[], staffCitations=()=>[], fundingCites=()=>el('span','cites');
const sourcesBlock=order=>el('div','srcs',order.join(',')), missing=()=>el('span','missing','Not published');
""" + helper + """
const body=detailsBody(), groups=body.children.filter(n=>n.dataset.group);
assert.deepEqual(groups.map(n=>n.dataset.group),['Own funding','Proposals','Related funding','Staff records']);
const ids=group=>group.children.filter(n=>n.dataset.record).map(n=>n.dataset.recordId);
assert.deepEqual(groups.map(ids),[['own','component'],['proposal'],['related'],['total','target']]);
assert.equal(groups[0].children.find(n=>n.dataset.recordId==='component').firstChild.text,'Component: Shared computing programme');
assert.ok(groups[2].children.some(n=>n.className==='related-funding'));
assert.equal(body.children.find(n=>n.className.split(' ').includes('staffing-note')).text,'Staff are seconded.');
assert.equal(body.children.find(n=>n.className==='srcs').text,'own-source,staff-source');
const all=node=>[node,...node.children.flatMap(all)];
assert.ok(all(body).some(n=>n.text==='Staffing qualification'));
assert.ok(all(body).some(n=>n.text==='target 50'));
assert.ok(all(body).some(n=>n.text==='as of 2026-10'));
"""
    subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True)


def partner_fold(items, label="Partners", unfolded=False):
    source = PAGE.read_text()
    helper = source[
        source.index("  function partnersFold(") : source.index("  function recordSources(")
    ]
    data = {"state": "stated", "items": items if label == "Members" else items[:6], "fold_items": items}
    stub = """
function el(tag, className, text) {
  return {tag, className, text, children: [], appendChild(child) { this.children.push(child); }};
}
const document = {createTextNode: text => el("text", "", text)};
const cite = (ids, label) => ({className: "cites", ids, label});
"""
    script = stub + helper + "\nconst label = " + json.dumps(label) + ";\nconst unfolded = " + json.dumps(unfolded) + ";\nconst p = " + json.dumps(data) + """;
const fold = partnersFold(p, cite, label, unfolded);
const assert = require("node:assert/strict");
assert.equal(fold.tag, unfolded ? "div" : "details");
assert.equal(fold.children[0].tag, unfolded ? "p" : "summary");
assert.equal(partnersFold({state: "stated"}, cite, label), null);
assert.equal(partnersFold({state: "stated", fold_items: []}, cite, label), null);
for (const state of ["not_published", "not_applicable"]) {
  assert.equal(partnersFold({state, fold_items: p.fold_items}, cite, label), null);
}
const items = fold.children.find(child => child.tag === "ul").children;
for (const item of items) {
  for (const name of item.children.filter(child => child.className === "pname")) {
    assert.equal(name.children.find(child => child.className === "cites").label, label);
  }
}
console.log(JSON.stringify(items.map(item => ({
  names: item.children.filter(child => child.className === "pname").map(name => ({
    name: name.text, cited: name.children.find(child => child.className === "cites").ids
  })),
  how: item.children.find(child => child.className === "phow")?.text || ""
}))));
"""
    result = subprocess.run(
        ["node", "-e", script], check=True, capture_output=True, text=True
    )
    return json.loads(result.stdout)


@pytest.mark.parametrize("label", ["Members", "Partners"])
@pytest.mark.parametrize("unfolded", [False, True])
def test_partner_fold_renders_every_item_with_its_citations(label, unfolded):
    # Hand-worked eight-item order: the first six also appear on the card.
    items = [
        {"name": f"Body {n}", "how": f"Does task {n}.", "cited": [f"source-{n}"]}
        for n in range(8)
    ]
    assert partner_fold(items, label, unfolded) == [
        {"names": [{"name": f"Body {n}", "cited": [f"source-{n}"]}], "how": f"does task {n}."}
        for n in range(8)
    ]


@pytest.mark.parametrize(
    ("singular", "plural"),
    [
        ("Is to be tied to the institute by agreement and can contribute staff, computing capacity and specialist knowledge.", "are to be tied to the institute by agreement and can contribute staff, computing capacity and specialist knowledge."),
        ("Is named among the institute’s partners whose agreements enable evaluations of the latest AI capabilities.", "are named among the institute’s partners whose agreements enable evaluations of the latest AI capabilities."),
        ("Is one of the national evaluation and security bodies the institute federates without a new legal structure.", "are among the national evaluation and security bodies the institute federates without a new legal structure."),
        ("Takes part in the institute as a participating organisation of the AISI Partnership Agreement.", "take part in the institute as participating organisations of the AISI Partnership Agreement."),
        ("Has initial representation on the TRAINS Taskforce chaired by the then US AI Safety Institute.", "have initial representation on the TRAINS Taskforce chaired by the then US AI Safety Institute."),
        ("Is listed among the institute's partners.", ""),
        ("Is named as a technical partner that the institute works with.", ""),
    ],
)
@pytest.mark.parametrize("label", ["Members", "Partners"])
def test_partner_fold_groups_without_losing_name_citations(singular, plural, label):
    items = [
        {"name": "A", "how": singular, "cited": ["a-name", "a-how"]},
        {"name": "B", "how": "Does another task.", "cited": ["b"]},
        {"name": "C", "how": singular, "cited": ["c-name", "c-how"]},
    ]
    assert partner_fold(items, label) == [
        {"names": [{"name": "A", "cited": ["a-name", "a-how"]}, {"name": "C", "cited": ["c-name", "c-how"]}], "how": plural},
        {"names": [{"name": "B", "cited": ["b"]}], "how": "does another task."},
    ]
    # A singleton must retain the singular wording; generic claims remain names only.
    assert partner_fold(items[:1], label)[0]["how"] == (singular[:1].lower() + singular[1:] if plural else "")


def page_partner_maps():
    """The page's GENERIC_HOW list and PLURAL_HOW keys, read from its source."""
    source = PAGE.read_text()
    fold = source[source.index("  function partnersFold(") : source.index("  function recordSources(")]
    generic = fold[fold.index("var GENERIC_HOW = [") : fold.index("];", fold.index("var GENERIC_HOW = ["))]
    plural = fold[fold.index("var PLURAL_HOW = {") : fold.index("};", fold.index("var PLURAL_HOW = {"))]
    return (
        set(re.findall(r'"([^"]+)"', generic)),
        set(re.findall(r'^\s*"([^"]+)":', plural, re.M)),
    )


def test_partner_how_maps_match_current_data():
    # The page keys these maps on exact data sentences (moving them into the producer is planned for 1.1.0);
    # a mapped sentence that no longer occurs means an edit silently stopped the grouping.
    data = json.loads((PAGE.parents[1] / "data/aisi.json").read_text())
    shared, used = set(), set()
    for institute in data["institutes"]:
        hows = [e["how"] for e in (institute.get("national_partners") or {}).get("entries", [])]
        used.update(hows)
        shared.update(h for h in hows if hows.count(h) > 1)
    generic, plural = page_partner_maps()
    assert generic and plural
    assert generic == GENERIC_PARTNER_HOW, "page and gate disagree on the generic sentences"
    assert plural == set(PLURAL_PARTNER_HOW), "page and gate disagree on the plural sentences"
    assert sorted(generic - used) == [], "generic sentence matches no partner"
    assert sorted(plural - shared) == [], "plural sentence is shared by no two partners of one institute"


def page_call(expression):
    source = PAGE.read_text()
    helpers = source[
        source.index("  function fieldCitations(") : source.index(
            "  function annualPeriod("
        )
    ]
    result = subprocess.run(
        ["node", "-e", helpers + "\nconsole.log(JSON.stringify(" + expression + "));"],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(result.stdout)


def test_renderer_has_no_editorial_selection_or_source_fallback():
    source = PAGE.read_text()
    for token in (
        "IMPLIED",
        "DEVELOPMENT_EVENTS",
        "var SMALL",
        "DATA_AS_OF",
        "function comparableFunding",
        "function pickFunding",
        "function latestStaff",
        "function staffRecord",
        "function recentDevelopments",
        "function fieldSources",
    ):
        assert token not in source, f"renderer contains editorial selection: {token}"
    assert raw_array_reads(source) == []
    assert "m.display.developments.forEach" in source
    assert "return j.display.advisory" in source
    assert "Number.isFinite(fu.eur_per_year_est)" not in source


def raw_array_reads(source):
    script = source.split("<script>", 1)[-1].split("</script>", 1)[0]
    # r.x is the renderer's CSV row, not an authored institute record.
    script = script.replace("r.x.staff", "csvStaff").replace("na.staff", "staffAbsent")
    return re.findall(r'(?<!display)\.(?:funding|staff|timeline|source_ids|maintainers|operating|changelog)\b|\[\s*["\'](?:funding|staff|timeline|source_ids|maintainers|operating|changelog)["\']\s*\]|inst\[collection\]|op\[0\]', script)


@pytest.mark.parametrize("expression", ["inst.funding[0]", "inst.staff.find(f)", "j.timeline", "item.source_ids", "m.maintainers[0]", 'inst["funding"]', "inst[collection][index]", "op[0]", "d.changelog[0]", "inst.operating.items"])
def test_raw_array_guard_rejects_mutations(expression):
    assert raw_array_reads("<script>" + expression + "</script>")


def test_selected_record_uses_supplied_id_even_when_facts_suggest_another():
    institute = {"display": {
        "funding_records": {"new": {"amount": 100}, "selected": {"amount": 1}},
        "staff_records": {"new": {"total": 100}, "selected": {"total": 2}},
        "budget": "selected", "comparison_funding": None, "staff": "selected",
    }}
    encoded = json.dumps(institute)
    assert page_call(f'selectedRecord({encoded}, "funding", "budget")')["amount"] == 1
    assert page_call(f'selectedRecord({encoded}, "funding", "comparison_funding")') is None
    assert page_call(f'selectedRecord({encoded}, "staff", "staff")')["total"] == 2


def test_citations_use_only_named_fields_and_never_broad_sources():
    record = {
        "source_ids": ["decoy"],
        "citations": {
            "total": ["staff"],
            "as_of": ["date", "staff"],
            "display": ["unrelated"],
        },
        "total": 2,
    }
    encoded = json.dumps(record)
    assert page_call(f'fieldCitations({encoded}, ["total", "as_of"])') == [
        "staff",
        "date",
    ]
    assert page_call(f'fieldCitations({encoded}, ["missing"])') == []
    assert page_call(f"staffCitations({encoded})") == ["staff", "date"]


def test_known_activity_oracle_uses_reviewed_credit_not_bibliographic_sources():
    institute = {
        "id": "fixture-institute",
        "outputs": [
            {
                "lineage": "fixture-work",
                "date": "1900",
                "credit": {"date": "2024-02-09"},
                "qualifies_as": "published_work",
                "source_ids": ["bibliography-only"],
                "citations": {"display": ["wrong"]},
            }
        ],
    }
    reviewed = {("fixture-institute", "fixture-work"): ["fixture-credit"]}
    actual = expected_activity(institute, reviewed)
    assert actual[0]["date"] == "2024-02-09"
    assert actual[0]["source_ids"] == ["fixture-credit"]
    with pytest.raises(ValueError, match="needs reviewed sources"):
        expected_activity(institute)


def test_annual_format_uses_supplied_classification():
    source = PAGE.read_text()
    helpers = source[
        source.index("  function fieldCitations(") : source.index(
            "  // Citations follow the last word;"
        )
    ]
    # Explicit published display values deliberately disagree with the record kind.
    records = [
        {
            "kind": "one_off",
            "eur_per_year_est": 2000000,
            "period": "per year",
            "display": {"annual_estimate": True, "value": "~EUR 2m", "qualifiers": ["a year"]},
        },
        {
            "kind": "annual",
            "eur_per_year_est": 2000000,
            "description": "Published allocation",
            "display": {"annual_estimate": False, "value": "Published allocation", "qualifiers": []},
        },
    ]
    result = subprocess.run(
        [
            "node",
            "-e",
            helpers
            + "\nconsole.log(JSON.stringify("
            + json.dumps(records)
            + ".map(budgetText)));",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert json.loads(result.stdout) == ["~EUR 2m; a year", "Published allocation"]


@pytest.mark.parametrize("reverse_sources", [False, True])
def test_real_activity_matches_independent_credit_oracle(reverse_sources):
    from scripts.published import publish

    authored = json.loads((PAGE.parents[1] / "data/aisi.json").read_text())
    if reverse_sources:
        authored["sources"].reverse()
    data = publish(authored, strict=True)
    source_order = {source["id"]: n for n, source in enumerate(data["sources"])}
    for institute in data["institutes"]:
        expected = expected_activity(institute)
        for row in expected:
            row["source_ids"] = sorted(row["source_ids"], key=source_order.__getitem__)
        actual = [
            {key: row[key] for key in ("kind", "date", "lineage", "source_ids")}
            for row in institute["operating"]
        ]
        assert actual == expected, institute["id"]


def test_all_real_first_outputs_match_independent_credit_oracle():
    from scripts.check_page import expected_event_sources
    from scripts.published import publish

    data = publish(
        json.loads((PAGE.parents[1] / "data/aisi.json").read_text()), strict=True
    )
    source_order = {source["id"]: n for n, source in enumerate(data["sources"])}
    events = [
        (jurisdiction, event)
        for jurisdiction in data["jurisdictions"]
        for event in jurisdiction.get("timeline", [])
        if event["event"] == "first_output"
    ]
    for jurisdiction, event in events:
        expected = sorted(
            expected_event_sources(data, jurisdiction, event),
            key=source_order.__getitem__,
        )
        assert event["source_ids"] == expected, (jurisdiction["id"], event["id"])


def test_credit_oracle_rejects_unknown_work_and_missing_explicit_credit():
    import pytest

    institute = {
        "id": "new-institute",
        "outputs": [
            {
                "lineage": "new-work",
                "qualifies_as": "published_work",
                "date": "2025",
                "credit": {"date": "2025"},
                "source_ids": ["bibliography"],
            }
        ],
    }
    with pytest.raises(ValueError, match="needs reviewed sources"):
        expected_activity(institute)
    reviewed = {("new-institute", "new-work"): ["fixture-credit"]}
    assert expected_activity(institute, reviewed) == [
        {"kind": "published_work", "date": "2025", "lineage": "new-work", "source_ids": ["fixture-credit"]}
    ]
    del institute["outputs"][0]["credit"]
    with pytest.raises(KeyError, match="credit"):
        expected_activity(institute, reviewed)
    from scripts.check_page import expected_event_sources

    with pytest.raises(ValueError, match="credited catalogue item"):
        expected_event_sources(
            {"institutes": []}, {"id": "XX"}, {"event": "first_output"}
        )


def source_alignment_card(source_list):
    """One established state: a comparable card/table row and its status mark."""
    return (
        '<article class="card" data-j="XX"><div class="row" data-track="2"><div class="v">Institute</div></div>'
        + source_list
        + '</article><table><tbody id="world-rows"><tr data-j="XX"><td data-col="inst">Institute</td>'
        '<td data-col="status"><span class="smark"><i class="sw"></i>Established</span></td></tr></tbody></table>'
    )


def test_detail_oracle_rejects_reordered_and_duplicate_citations():
    from scripts.check_page import check_source_alignment

    sources = [
        {"id": sid, "url": "https://example.org/" + sid}
        for sid in ("description", "period", "confidence")
    ]
    data = {
        "sources": sources,
        "jurisdictions": [{"id": "XX", "kind": "state", "status": "established"}],
        "institutes": [
            {
                "id": "institute",
                "jurisdiction_id": "XX",
                "funding": [
                    {
                        "id": "budget",
                        "description": "Published budget",
                        "citations": {
                            "description": ["description"],
                            "period": ["period"],
                            "confidence": ["confidence"],
                        },
                    }
                ],
            }
        ],
    }
    source_list = (
        '<div class="srcs"><ol>'
        + "".join(
            f'<li data-source-id="{s["id"]}"><a href="{s["url"]}">Source</a></li>'
            for s in sources
        )
        + "</ol></div>"
    )

    def errors(ids):
        cites = "".join(
            f'<a class="cn" data-source-id="{sid}" href="https://example.org/{sid}">{[s["id"] for s in sources].index(sid) + 1}</a>'
            for sid in ids
        )
        html = source_alignment_card(source_list) + f'<li class="fund-detail" data-j="XX" data-institute="institute">{source_list}<p data-record="funding" data-record-id="budget">Published budget<span class="cites">{cites}</span></p></li>'
        findings = check_source_alignment(html, data)
        assert all("record citations differ" in e for e in findings), findings
        return findings

    assert errors(["description", "period", "confidence"]) == []
    assert errors(["period", "description", "confidence"])
    assert errors(["description", "period", "confidence", "confidence"])


def test_recent_development_sample_is_independent_and_order_sensitive(dataset, monkeypatch):
    from scripts.published import publish
    from scripts import check_page
    from scripts.check_page import check_summary_data, expected_event_sources

    data = publish(dataset, strict=False)
    jurisdiction, = data["jurisdictions"]
    jurisdiction.update(id="XX", name="Fixture state")
    event = {"id": "fixture-event", "event": "designated", "date": "2025-01-02",
             "short": "Fixture institute designated.", "description": "Fixture institute designated.",
             "source_ids": ["fixture-first", "fixture-second"],
             "citations": {"description": ["fixture-first", "fixture-second"]}}
    jurisdiction["timeline"] = [event]
    data["institutes"] = []
    data["sources"] = [{"id": sid, "url": f"https://example.org/{sid}"} for sid in ("fixture-first", "fixture-second")]
    monkeypatch.setitem(check_page.DEVELOPMENT_CREDIT_SOURCES, ("XX", "fixture-event"), ["fixture-first", "fixture-second"])
    assert expected_event_sources(data, jurisdiction, event) == [
        "fixture-first",
        "fixture-second",
    ]
    event["citations"] = {"description": ["wrong"]}
    event["source_ids"] = ["wrong"]
    assert expected_event_sources(data, jurisdiction, event) == [
        "fixture-first",
        "fixture-second",
    ]

    def errors(ids):
        cites = "".join(f'<a class="cn" data-source-id="{sid}">1</a>' for sid in ids)
        html = f'<ul id="development-list"><li class="development-row" data-j="XX" data-events="fixture-event"><time class="development-date" data-date="{event["date"]}"></time><span class="development-name">{jurisdiction["name"]}</span><p class="development-text">{event["short"]}<span class="cites">{cites}</span></p></li></ul>'
        return [
            e
            for e in check_summary_data(html, data)
            if "development citations differ" in e
        ]

    assert errors(["fixture-first", "fixture-second"]) == []
    assert errors(["fixture-second", "fixture-first"])
    assert errors(["fixture-first", "fixture-second", "fixture-second"])

    # A record's card numbers its sources in its own citation order, which may differ from the catalogue order.
    def card_errors(ids):
        card = '<article class="card" data-j="XX"><div class="srcs"><ol><li data-source-id="fixture-second"></li><li data-source-id="fixture-first"></li></ol></div></article>'
        cites = "".join(f'<a class="cn" data-source-id="{sid}">1</a>' for sid in ids)
        html = card + f'<ul id="development-list"><li class="development-row" data-j="XX" data-events="fixture-event"><time class="development-date" data-date="{event["date"]}"></time><span class="development-name">{jurisdiction["name"]}</span><p class="development-text">{event["short"]}<span class="cites">{cites}</span></p></li></ul>'
        return [e for e in check_summary_data(html, data) if "development citations differ" in e]

    assert card_errors(["fixture-second", "fixture-first"]) == []
    assert card_errors(["fixture-first", "fixture-second"])
    assert card_errors(["fixture-second"])
    assert card_errors(["fixture-second", "fixture-first", "fixture-first"])
    assert card_errors(["fixture-second", "fixture-first", "other"])


def test_card_source_orders_follow_the_page_without_a_card():
    from scripts.check_page import card_source_orders

    data = {
        "sources": [{"id": "a"}, {"id": "b"}, {"id": "c"}],
        "jurisdictions": [{"id": "XX", "institute_id": "xx-i", "display": {"source_links": ["b", "c"]}}],
        "institutes": [{"id": "xx-i", "display": {"source_links": ["a", "b"]}}],
    }
    # No card: the jurisdiction's links, then the institute's, first occurrence kept (the page's recordSources).
    assert card_source_orders(BeautifulSoup("", "html.parser"), data) == {"XX": ["b", "c", "a"]}
    # A card's source list wins; an empty one does not count.
    card = '<article class="card" data-j="XX"><div class="srcs"><ol><li data-source-id="c"></li><li data-source-id="a"></li></ol></div></article>'
    assert card_source_orders(BeautifulSoup(card, "html.parser"), data) == {"XX": ["c", "a"]}
    empty = '<article class="card" data-j="XX"></article>'
    assert card_source_orders(BeautifulSoup(empty, "html.parser"), data) == {"XX": ["b", "c", "a"]}


def test_fixture_refresh_rebinds_evidence_and_flags_at_current_positions():
    from scripts.published import publish
    from scripts.check_page import make_format_fixture, refresh_fixture_selections

    data = publish(
        json.loads((PAGE.parents[1] / "data/aisi.json").read_text()), strict=True
    )
    fixture, cases = make_format_fixture(data)
    first = next(j for j in fixture["jurisdictions"] if j["id"] == cases[0]["jid"])
    first["timeline"][0]["instrument"] = "advisory_report"
    first["population"]["value"] = 1
    first["display"] = {"advisory": False, "small_state": False}
    # Source order, rather than event or binding order, controls group citations.
    ids = [s["id"] for s in fixture["sources"][:2]]
    event = first["timeline"][0]
    event["citations"] = {"date": ids[::-1], "event": ids}
    event["source_ids"] = ids
    refresh_fixture_selections(fixture)
    first = next(j for j in fixture["jurisdictions"] if j["id"] == cases[0]["jid"])
    assert first["display"]["advisory"] is True
    assert first["display"]["small_state"] is True
    assert first["timeline"][0]["citations"]["date"] == ids
    assert first["citations"]["since"] == ids
    institute = next(
        i for i in fixture["institutes"] if i["jurisdiction_id"] == first["id"]
    )
    assert institute["funding"][0]["citations"]["eur_per_year_est"]
    assert institute["staff"][0]["citations"]["total"]
    groups = fixture["dataset"]["display"]["developments"]
    assert all(g["citations"]["display"] for g in groups)


def test_mandate_cites_the_short_text_that_is_rendered():
    source = PAGE.read_text()
    expression = re.search(
        r'row\("Mandate", mandate \|\| missing\(\), (.+)\);', source
    )[1]
    helpers = source[
        source.index("  function fieldCitations(") : source.index(
            "  function selectedRecord("
        )
    ]
    script = (
        helpers
        + '\nconst inst={mandate_short:"Short text", mandate:"Long text", citations:{mandate_short:["short"], mandate:["long", "short"]}}; console.log(JSON.stringify('
        + expression
        + "));"
    )
    result = subprocess.run(
        ["node", "-e", script], check=True, capture_output=True, text=True
    )
    assert json.loads(result.stdout) == ["short"]


def card_table_citation_gaps(data):
    """Execute page row expressions; exempt only actual missing values and linked names.

    This small element stub captures values/IDs without simulating layout or routing.
    The real citation renderer and numbering have separate tests.
    """
    source = PAGE.read_text()
    dates = source[
        source.index("  var MON =") : source.index("  function saveText(")
    ]
    helpers = source[
        source.index("  function fieldCitations(") : source.index(
            "  // Citations follow the last word;"
        )
    ]
    presentation = source[
        source.index("  var DISPLAY_NAME =") : source.index("  function recordSources(")
    ]
    card_start = source.index("  function instituteCard(")
    card_rows = source[
        source.index("    function bodiesRow(", card_start) : source.index(
            "    var rest = recordSources(j)", card_start
        )
    ]
    table_rows = source[
        source.index("      var instLabel =") : source.index(
            "      var net =", source.index("      var instLabel =")
        )
    ]
    label = source[source.index("  var LABEL = {") : source.index("\n", source.index("  var LABEL = {"))]
    stub = label + r"""
function el(tag,cls,text){return {tag, tagName:tag.toUpperCase(), className:cls||'', classList:{add(){}}, dataset:{}, children:[],
 textContent:text||'', setAttribute(){}, appendChild(child){this.children.push(child);return child;}};}
const document={createTextNode:text=>el('text','',text)};
function words(node){return typeof node==='string'?node:node.textContent+node.children.map(words).join('');}
function isMissing(node){return typeof node!=='string'&&(node.className.split(' ').includes('missing')||node.children.some(isMissing));}
function term(text){return document.createTextNode(text);}
function confidence(text){return document.createTextNode(text);}
function confTag(r){var c=r&&r.confidence;return c&&c!=='high'?document.createTextNode(c):null;}
function confTerms(qualifiers){return qualifiers;}
function statusConfidence(j){return j.status_confidence&&j.status_confidence!=='high'?document.createTextNode(j.status_confidence):null;}
function separated(parts){return document.createTextNode(parts.join(' '));}
function advisory(j){return j.display.advisory;}
function operatingMark(){return missing();} // Known activity has its own independent oracle.
function operatingText(){return '';}
function intervalQualifier(text){return document.createTextNode(text);}
function relatedFunding(){return el('span','','Related');}
function factClaims(){return [];} // the Facts row has its own content check (tests/content/test_claims.py)
function citeTarget(box){return box;} // citation placement has its own checks; this stub captures the IDs.
const cite=()=>el("span");
const rows=[];
function capture(j,inst,view,label,value,ids){
 if(label==='Known activity'||label==='Related funding')return;
 rows.push({jurisdiction:j.id,view,field:label,text:words(value),missing:isMissing(value),
   linkedName:label==='Institute'&&!!(inst&&inst.website),ids:ids||[]});
}
"""
    script = (
        stub
        + dates
        + presentation
        + helpers
        + "\nfunction activityLineNode(){return null;}\n"  # checked separately in test_activity_line.py
        + "\nconst d="
        + json.dumps(data)
        + ";\n"
        + """
const I=Object.fromEntries(d.institutes.map(i=>[i.id,i]));
const m=d.dataset;
nameInstitutes(d.institutes,Object.fromEntries(d.jurisdictions.map(j=>[j.id,j])));
for(const j of d.jurisdictions){const inst=I[j.institute_id]||null;
 if(j.status!=='none_identified'){
  const row=(label,value,ids)=>{capture(j,inst,'card',label,value,ids);return el('div','v');};
"""
        + card_rows
        + """
 }
 if(j.kind!=='supranational'){
  let column=0;const labels=['Institute','Status since','Known activity','Budget','Staff'];
  const cell=(value,ids)=>{capture(j,inst,'table',labels[column++],value,ids);return {dataset:{}};};
"""
        + table_rows
        + """
 }
}
console.log(JSON.stringify(rows));
"""
    )
    result = subprocess.run(["node"], input=script, text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    rows = json.loads(result.stdout)
    fields = {row["field"] for row in rows}
    # Members belong to Form; Head, Reports to and Partners follow their presence rules (tests/test_rows.py).
    # Cards label the row "Status"; the table's column is "Status since".
    assert {"Institute", "Status", "Status since", "Form", "Mandate", "Finding", "Budget", "Staff"} <= fields <= {
        "Institute", "Status", "Status since", "Form", "Mandate", "Finding", "Budget", "Staff", "Head", "Reports to", "Partners",
    }
    sources = {item["id"] for item in data["sources"]}
    return [
        f"{row['jurisdiction']}/{row['view']}/{row['field']}: {row['text']}"
        for row in rows
        if not row["missing"]
        and not row["linkedName"]
        and (not row["ids"] or any(sid not in sources for sid in row["ids"]))
    ]


def test_real_card_table_facts_have_field_citations():
    from scripts.published import publish

    data = publish(
        json.loads((PAGE.parents[1] / "data/aisi.json").read_text()), strict=True
    )
    assert card_table_citation_gaps(data) == []


@pytest.mark.parametrize("record,description", [
    ({"description": "¥380m supplementary budget", "currency": "JPY", "period": "FY2024 supplementary", "commitment": "adopted"}, "JPY 380m supplementary budget"),
    ({"description": "NOK 30m proposed for 2027", "currency": "NOK", "period": "2027 budget proposal", "commitment": "proposed"}, "NOK 30m proposed for 2027"),
])
def test_funding_descriptions_never_append_bracketed_periods(record, description):
    source = PAGE.read_text()
    helpers = source[source.index("  function annualPeriod("):source.index("  // Citations follow the last word;")]
    result = subprocess.run(["node", "-e", helpers + "\nvar record=" + json.dumps(record) + "; console.log(JSON.stringify(publishedFunding(record)));"], capture_output=True, text=True, check=True)
    assert json.loads(result.stdout) == description


def funding_record_tree(record, rates=()):
    source = PAGE.read_text()
    helpers = source[source.index("  function fieldCitations("):source.index("  // Citations follow the last word;")]
    stub = """
function el(tag, className, text) { return {tag, className:className||'', text:text||'', dataset:{}, children:[], appendChild(child){this.children.push(child); return child;}}; }
const document={createTextNode:text=>el('text','',text)};
function separated(parts){return el('span','',parts.join(' · '));}
function confTag(r){return el('span','confidence',r.confidence);}
function confTerms(qualifiers){return qualifiers;}
function fmtDate(text){return text;}
function cite(ids){return el('span','cites',ids.join(','));}
"""
    result = subprocess.run(["node", "-e", stub + helpers + "\nuseRates(" + json.dumps(list(rates)) + ");\nconsole.log(JSON.stringify(fundingRecord(" + json.dumps(record) + ",cite)));"], capture_output=True, text=True, check=True)
    return json.loads(result.stdout)


def tree_text(node):
    return node["text"] + "".join(tree_text(child) for child in node["children"] if child["className"] != "cites")


@pytest.mark.parametrize("bound", [False, True])
def test_undisclosed_funding_has_no_source_statement_and_keeps_evidence(bound):
    record = {
        "id": "undisclosed", "kind": "none_disclosed", "confidence": "high",
        "description": "No budget published for the institute as a whole.",
        "notes": "The answer refers to existing resources.",
        "display": {"native_value": "Not published", "native_qualifiers": [], "generated_description": True},
        "citations": {"description": ["evidence"] if bound else [], "notes": ["answer"]},
    }
    tree = funding_record_tree(record)
    assert all("source-statement" not in child["className"] for child in tree["children"])
    native = next(child for child in tree["children"] if "funding-native" in child["className"].split())
    assert tree_text(native).strip() == "Not published"
    assert native["children"][-1]["children"][-1]["text"] == ("evidence" if bound else "")
    metadata = next(child for child in tree["children"] if "funding-metadata" in child["className"].split())
    assert record["notes"] in tree_text(metadata)
    assert metadata["children"][-1]["text"] == "answer"


def test_legacy_undisclosed_funding_keeps_its_authored_source_statement():
    # Only the generated line is hidden: a legacy record's authored description (the EU AI Office's) still shows.
    record = {
        "id": "legacy", "kind": "none_disclosed", "confidence": "high",
        "description": "The decision assigns staff costs to programme appropriations.",
        "display": {"native_value": "Not published", "native_qualifiers": []},
        "citations": {"description": ["decision"]},
    }
    tree = funding_record_tree(record)
    statement = [child for child in tree["children"] if "source-statement" in child["className"]]
    assert len(statement) == 1 and record["description"] in tree_text(statement[0])


def test_funding_gate_rejects_a_source_statement_for_undisclosed_funding():
    from scripts.check_page import check_source_alignment

    record = {"id": "budget", "kind": "none_disclosed", "description": "No budget published for the institute as a whole.",
              "display": {"generated_description": True}}
    data = {"sources": [], "jurisdictions": [{"id": "XX", "kind": "state", "status": "established", "source_ids": []}],
            "institutes": [{"id": "institute", "jurisdiction_id": "XX", "funding": [record], "staff": []}]}
    listing = '<div class="srcs"><ol></ol></div>'
    statement = '<details class="source-statement"><summary>Source statement</summary><p>Generated negative.</p></details>'
    html = source_alignment_card(listing) + f'<li class="fund-detail" data-j="XX" data-institute="institute">{listing}<div data-record="funding" data-record-id="budget"><p>Not published</p>{statement}</div></li>'
    assert check_source_alignment(html.replace(statement, ""), data) == []
    assert check_source_alignment(html, data) == ["XX: a generated undisclosed-funding line is shown as a source statement"]
    record["display"] = {}  # a legacy record's authored description may show as a statement
    assert check_source_alignment(html, data) == []


@pytest.mark.parametrize("label", [
    'Component: GBP 240m for a shared programme',
    'Component: GBP 240m for a shared <span class="cite-glue">programme</span>',
    'Component: <abbr class="g">GBP</abbr> 240m for a shared pro<span>gramme</span>',
])
def test_component_name_check_accepts_published_currency_words_but_requires_the_name(label):
    from scripts.check_page import check_source_alignment

    record = {"id": "component", "coverage": "component", "currency": "GBP", "description": "£240m for a shared programme", "citations": {"description": ["source"]}}
    data = {"sources": [{"id": "source", "url": "https://example.org/source"}],
            "jurisdictions": [{"id": "XX", "kind": "state", "status": "established", "source_ids": []}],
            "institutes": [{"id": "institute", "jurisdiction_id": "XX", "funding": [record], "staff": []}]}
    listing = '<div class="srcs"><ol><li data-source-id="source"><a href="https://example.org/source">Source</a></li></ol></div>'
    html = source_alignment_card(listing) + f'<li class="fund-detail" data-j="XX" data-institute="institute">{listing}<div data-record="funding" data-record-id="component"><p class="fund-component">{label}<span class="cites"><a class="cn" data-source-id="source" href="https://example.org/source">1</a></span></p></div></li>'
    assert check_source_alignment(html, data) == []
    assert any("component" in error for error in check_source_alignment(html.replace(label, "Component"), data))
    assert any("component" in error for error in check_source_alignment(html.replace(label, "Component: GBP 240m"), data))
    assert any("component" in error for error in check_source_alignment(html.replace('class="fund-component"', 'class="source-statement"'), data))


def test_all_real_funding_disclosures_separate_native_figures_from_source_statements():
    from scripts.published import publish
    from scripts.check_page import expected_shown_funding_value, rate_sources

    data = publish(json.loads((PAGE.parents[1] / "data/aisi.json").read_text()), strict=True)
    records = {record["id"]: record for institute in data["institutes"] for record in institute.get("funding", []) if record.get("amount") is not None}
    for record in records.values():
        tree = funding_record_tree(record, data["dataset"]["exchange_rates"])
        native = next(child for child in tree["children"] if "funding-native" in child["className"].split())
        shown = expected_shown_funding_value(record, data)
        assert native["children"][0]["text"] == shown
        # The euro figure cites the rate's source after the amount's own (rule currency-rounding).
        expected_citations = list(dict.fromkeys([sid for field in ("amount", "currency", "amount_qualifier", "period", "commitment", "kind", "coverage") for sid in record.get("citations", {}).get(field, [])] + rate_sources(record, data)))
        assert native["children"][-1]["children"][-1]["text"] == ",".join(expected_citations)
        assert tree_text(native) == shown + " " + " · ".join(record["display"]["native_qualifiers"])
        metadata = next(child for child in tree["children"] if "funding-metadata" in child["className"].split())
        assert record.get("notes", "") in tree_text(metadata)
        statements = [child for child in tree["children"] if child["className"] == "more quiet source-statement"]
        normalised = record.get("description", "")
        for symbol, currency in (("£", "GBP"), ("¥", "JPY"), ("S$", "SGD"), ("€", "EUR"), ("₩", "KRW")):
            normalised = re.sub(re.escape(symbol) + r"\s*", currency + " ", normalised)
        assert len(statements) == int(bool(normalised) and normalised != record["display"]["native_value"])
        if statements:
            assert statements[0]["tag"] == "details"
            assert statements[0]["children"][0]["text"] == "Source statement"
            assert not statements[0].get("open")
            # Stored source wording survives; only currency symbols are normalised.
            statement = tree_text(statements[0]["children"][1])
            assert statement == normalised
            assert " (" + (record.get("period") or "") + ")" not in statement


@pytest.mark.parametrize(
    "amount,qualifier,currency,label",
    [
        (10, None, "EUR", "EUR 10"),
        (9_150_000, "about", "EUR", "about EUR 9.2m"),
        (9_150_000, None, "EUR", "~EUR 9.2m"),
        # Another currency is also shown in euros, here at 0.5 EUR per USD (rule currency-rounding).
        (10, None, "USD", "~EUR 5 (USD 10)"),
        (9_150_000, "about", "USD", "about EUR 4.6m (USD 9.2m)"),
    ],
)
def test_native_funding_gate_rejects_wrong_amount_and_wrong_field_evidence(
    amount, qualifier, currency, label
):
    from scripts.check_page import check_source_alignment
    record = {"id": "budget", "amount": amount, "amount_qualifier": qualifier, "currency": currency, "description": "A sourced statement.", "source_ids": ["amount", "statement"], "citations": {"amount": ["amount"], "description": ["statement"]}}
    data = {"dataset": {"exchange_rates": [{"currency": "USD", "eur_per_unit": 0.5}]}, "sources": [{"id": identity, "url": "https://example.org/" + identity} for identity in ("amount", "statement")], "jurisdictions": [{"id": "XX", "kind": "state", "status": "established", "source_ids": []}], "institutes": [{"id": "institute", "jurisdiction_id": "XX", "funding": [record], "staff": []}]}
    listing = '<div class="srcs"><ol><li data-source-id="amount"><a href="https://example.org/amount">Amount</a></li><li data-source-id="statement"><a href="https://example.org/statement">Statement</a></li></ol></div>'
    html = source_alignment_card(listing) + f'<li class="fund-detail" data-j="XX" data-institute="institute">{listing}<div data-record="funding" data-record-id="budget"><p class="funding-native"><span class="budget-amount">{label}</span><span class="cites"><a class="cn" data-source-id="amount" href="https://example.org/amount">1</a></span></p><details class="source-statement"><summary>Source statement</summary><p>A sourced statement.<span class="cites"><a class="cn" data-source-id="statement" href="https://example.org/statement">2</a></span></p></details><p class="funding-metadata"></p></div></li>'
    def errors(text):
        findings = check_source_alignment(text, data)
        assert not any("matched 0" in error for error in findings), findings
        return findings
    assert errors(html) == []
    if qualifier == "about":
        assert any(
            "native funding amount" in error
            for error in errors(html.replace(label, label.replace(currency, "~" + currency)))
        )
    elif "~" in label:
        assert any(
            "native funding amount" in error
            for error in errors(html.replace(label, label.replace("~", "")))
        )
    assert any("native funding amount" in error for error in errors(html.replace(label, currency + " 12")))
    assert any("funding .funding-native citations" in error for error in errors(html.replace('data-source-id="amount" href=', 'data-source-id="statement" href=')))


def test_format_fixture_is_independent_of_input_exchange_rates(dataset):
    from scripts.check_page import make_format_fixture
    from scripts.published import publish

    data = publish(dataset, strict=False)
    state, = data["jurisdictions"]
    institute, = data["institutes"]
    data["jurisdictions"] = []
    data["institutes"] = []
    for n in range(6):
        jid, iid = f"X{n}", f"fixture-{n}"
        data["jurisdictions"].append({**copy.deepcopy(state), "id": jid, "institute_id": iid})
        data["institutes"].append({**copy.deepcopy(institute), "id": iid, "jurisdiction_id": jid})
    data["jurisdictions"].append({"id": "XX", "name": "Fixture state", "kind": "state",
                                  "status": "none_identified", "europe_scope": False, "citations": {}})
    rate = next(r for r in data["dataset"]["exchange_rates"] if r["currency"] == "GBP")
    rate["eur_per_unit"] = 99
    fixture, cases = make_format_fixture(data)
    assert rate["eur_per_unit"] == 99  # The factory only changes its own copy.
    assert (
        next(r for r in fixture["dataset"]["exchange_rates"] if r["currency"] == "GBP")[
            "eur_per_unit"
        ]
        == 1.2
    )
    case = next(c for c in cases if c.get("date") == "2025-03")
    institute = next(
        i for i in fixture["institutes"] if i["jurisdiction_id"] == case["jid"]
    )
    record = institute["display"]["funding_records"][
        institute["display"]["comparison_funding"]
    ]
    assert record["eur_per_year_est"] == 10_800_000
    assert record["display"]["value"] == case["figure"] == "up to ~EUR 11m (GBP 9.0m)"


def figures_note(institutes):
    """Run the page's figures-row block in Node on a synthetic dataset; return the (i) text it builds."""
    source = PAGE.read_text()
    start = source.index("    // Figures below the map:")
    block = source[start : source.index("    m.display.developments.forEach", start)]
    stub = r"""
function el(tag,cls,text){return {children:[],textContent:text||'',appendChild(c){this.children.push(c);return c;},querySelector(){return this;},get lastChild(){return this.children[this.children.length-1]||this;}};}
const document={createTextNode:t=>el('text','',t)};
const box=el('div'); const $=()=>box; let note=null;
function info(text){note=text;return el('button');}
function infoAfterText(){} function term(t){return document.createTextNode(t);}
const MEDIAN_CEILING='x';
"""
    jurisdictions = {i["jurisdiction_id"]: {"id": i["jurisdiction_id"], "name": i.pop("name"), "kind": "state"} for i in institutes}
    script = (
        stub
        + "const J=" + json.dumps(jurisdictions) + ";\n"
        + "const d={institutes:" + json.dumps(institutes) + "};\n"
        + "const m={display:{counts:{}}};\n"
        + block
        + "\nconsole.log(JSON.stringify(note));"
    )
    result = subprocess.run(["node"], input=script, text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def leftover(jid, name, kind="one_off", commitment="adopted"):
    """An established institute whose card shows a budget that is not compared."""
    return {"jurisdiction_id": jid, "name": name, "status": "established", "display": {
        "budget": "f", "comparison_funding": None, "funding_records": {"f": {"kind": kind, "commitment": commitment}}}}


def compared(jid, name):
    return {"jurisdiction_id": jid, "name": name, "status": "established", "display": {
        "budget": "g", "comparison_funding": "g", "funding_records": {"g": {"kind": "annual", "commitment": "adopted"}}}}


@pytest.mark.parametrize(
    "institutes,note",
    [
        ([compared("AA", "Alpha"), compared("BB", "Beta")], "Counts cover the 2 established state institutes."),
        ([compared("AA", "Alpha"), leftover("JP", "Japan")], "Counts cover the 2 established state institutes. Japan's budget is a one-off amount, so it counts neither in the budget median nor among those with no budget."),
        ([leftover("GB", "United Kingdom", commitment="proposed")], "Counts cover the 1 established state institutes. UK's budget is a proposal, so it counts neither in the budget median nor among those with no budget."),
        ([leftover("JP", "Japan"), leftover("KR", "Korea"), leftover("NO", "Norway")],
         "Counts cover the 3 established state institutes. The budgets of Japan, Korea and Norway are one-off amounts, so they count neither in the budget median nor among those with no budget."),
        ([leftover("JP", "Japan"), leftover("SE", "Sweden", kind="annual", commitment="proposed")],
         "Counts cover the 2 established state institutes. The budgets of Japan and Sweden are not compared, so they count neither in the budget median nor among those with no budget."),
        ([leftover("JP", "Japan"), leftover("FI", "Finland", kind="annual")],
         "Counts cover the 2 established state institutes. The budgets of Japan and Finland are not compared, so they count neither in the budget median nor among those with no budget."),
    ],
)
def test_figures_note_names_the_institutes_outside_both_budget_numbers(institutes, note):
    assert figures_note(institutes) == note


def page_eur_rounded(triples):
    source = PAGE.read_text()
    helpers = source[source.index("  var RATED ="):source.index("  function publishedFunding(")]
    script = helpers + "\nconsole.log(JSON.stringify(" + json.dumps(triples) + ".map(function (p) { return eurRounded(p[0], p[1], p[2]); })));"
    return json.loads(subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True).stdout)


def test_page_rounding_matches_the_producer():
    """The page's euro figure rounds amount x rate with display.round2's rule (rule currency-rounding)."""
    from decimal import Decimal
    from scripts.display import round2
    from scripts.published import publish

    data = publish(json.loads((PAGE.parents[1] / "data/aisi.json").read_text()), strict=True)
    rates = [r["eur_per_unit"] for r in data["dataset"]["exchange_rates"]]
    amounts = [0, 5, 10, 999, 1_250, 9_150_000, 10_000_000, 15_000_000, 3_975_000_000, 8_800_000_000, 20_100_000_000]
    triples = [(a, r, q) for a in amounts for r in rates for q in (None, "at_most", "at_least", "nearly", "about")]
    triples += [(x, 1, q) for x in (0.455, 1.25, 2.65, 95, 99.5, 125, 135, 8.05e6, 1.15e9) for q in (None, "at_most", "at_least")]
    # A remainder past twelve digits still rounds a bound outward (float snapping used to drop it).
    triples += [(52_000_000_004, 0.001, q) for q in (None, "at_most", "at_least")]
    assert page_eur_rounded(triples) == [float(round2(Decimal(str(a)) * Decimal(str(r)), q)) for a, r, q in triples]


def test_source_lists_show_english_with_the_original_in_the_link_tooltip():
    """Naming: the source title and its original share one link, including its external tail."""
    from scripts.check_page import check_source_alignment

    data = {"sources": [{"id": "fr", "url": "https://example.org/fr", "title": "L’annonce", "title_en": "The announcement", "language": "fr"}, {"id": "en", "url": "https://example.org/en"}],
            "jurisdictions": [{"id": "XX", "kind": "state", "status": "established", "source_ids": ["fr", "en"]}], "institutes": []}

    def errors(fr_item):
        html = source_alignment_card(f'<div class="srcs"><ol>{fr_item}<li data-source-id="en"><a href="https://example.org/en">Report</a></li></ol></div>')
        findings = check_source_alignment(html, data)
        return findings

    item = '<li data-source-id="fr"><a class="original-text" href="https://example.org/fr" aria-description="L’annonce">The <span class="ext-tail">announcement<span class="vh"> (opens in a new tab)</span></span></a><span class="archive-copy"> (<a class="archive" href="x">archived copy</a>)</span></li>'
    assert errors(item) == []
    for broken in (
        item.replace(' aria-description="L’annonce"', ''),
        item.replace(' class="original-text"', ''),
        item.replace('aria-description="L’annonce"', 'aria-description="Wrong original"'),
        item.replace('aria-description="L’annonce"', 'aria-description="L’annonce" data-tip="Wrong tooltip"'),
        item.replace('aria-description=', 'lang="fr" aria-description='),
        item.replace('announcement<span', 'other title<span'),
        item.replace('</li>', '<span class="orig">L’annonce</span></li>'),
        item.replace('</li>', '<span class="title-en"> [The announcement]</span></li>'),
    ):
        assert errors(broken)
    data['sources'][0].pop('title_en')
    untranslated = '<li data-source-id="fr"><a href="https://example.org/fr" lang="fr">L’annonce</a></li>'
    assert errors(untranslated) == []
    assert errors(untranslated.replace(' lang="fr"', ''))


def test_bilingual_names_and_titles_render_through_the_same_tooltip_route():
    """Execute the renderers on supplied forms, including text that looks like markup."""
    source = PAGE.read_text()
    helpers = source[source.index('  function sourceList('):source.index('  // The producer owns evidence')]
    helpers += source[source.index('  function withOriginal('):source.index('  function instShort(')]
    helpers += next(line for line in source.splitlines() if line.startswith('  function rowValue('))
    helpers += '\n' + next(line for line in source.splitlines() if line.startswith('  function publisherInTitle('))
    helpers += '\n' + '\n'.join(next(line for line in source.splitlines() if line.startswith(f'  function {name}(')) for name in ('shortPublisher', 'shortTitle'))
    helpers += source[source.index('  function citations('):source.index('  // None-identified entries (principle:')]
    helpers += source[source.index('  function originalName('):source.index('  function bodyGroup(')]
    helpers += next(line for line in source.splitlines() if line.startswith('  function tipText('))
    stub = r"""
function el(tag, cls, text) {
  const n = {tag, tagName: tag.toUpperCase(), className: cls || '', attrs: {}, dataset: {}, children: text == null ? [] : [{text}],
    setAttribute(k, v) { this.attrs[k] = v; }, getAttribute(k) { return this.attrs[k]; },
    appendChild(n) { this.children.push(n); return n; }, get childNodes() { return this.children; }};
  n.classList = {add(c) { n.className = [n.className, c].filter(Boolean).join(' '); }};
  return n;
}
const document = {createTextNode: text => ({text})};
function missing(na) { return el('span', 'missing', na ? 'Not applicable' : 'Not published'); }
function dated(text) { return el('span', 'date-text', text); }
function rereadLine() { return el('span', 'reread', 'Source last re-read 15 Nov 2026.'); }
function archiveCopy(li, src) { if (src.archive_url) li.appendChild(el('span', 'archive-copy', ' · archived copy')); }
function rereadTail() { return false; }
function hostOf(url) { return new URL(url).hostname; }
"""
    bodies = [
        {"name": "Agence <nationale> (AN)", "name_en": "National Agency (AN)"},
        {"name": "Agence (locale)", "name_en": "Local Agency"},
        {"name": "原名"},
    ]
    sources = [
        {"id": "translated", "title": "L’annonce <originale>", "title_en": "The announcement", "language": "fr", "archive_url": "https://example.org/archive"},
        {"id": "original", "title": "原文", "language": "zh"},
        {"id": "english", "title": "Report", "language": "en"},
        {"id": "unknown", "title": "Title"},
    ]
    sources = [{**s, "url": "https://example.org/" + s["id"], "publisher": "Publisher", "published": "2026-11-15"} for s in sources]
    script = stub + helpers + '\nconst bodies=' + json.dumps(bodies) + ';\nconst sources=' + json.dumps(sources) + r""";
const S = Object.fromEntries(sources.map(s => [s.id, s])), order = sources.map(s => s.id);
const cite = citations(S, order);
const names = bodies.map(b => bodyItem(b.name_en || b.name, originalName(b), [], [], cite, 'authority role'));
const list = sourceList(order, S, {translated: ['Finding']}, true);
const DISPLAY_NAME = {translated: 'Institute', same: 'Same name', absent: 'No local name'};
const institutes = [
  {id: 'translated', name_local: 'Institut <local>'},
  {id: 'translated', name_local: 'Institut <local>', website: 'https://example.org/institute'},
  {id: 'same', name_local: 'Same name'},
  {id: 'absent', website: 'https://example.org/absent'}
].map(instName);
const rows = [
  {state: 'stated', text: 'Ministry', original: 'Ministère <local>'},
  {state: 'stated', text: 'Original ministry'},
  {state: 'not_published', text: 'Stale name', original: 'Stale original'},
  {state: 'not_applicable'}
].map(rowValue);
console.log(JSON.stringify({names, list, institutes, rows, marks: cite(order),
  nameTips: names.map(n => tipText(n.children[0].children[0])),
  titleTips: list.children.map(li => tipText(li.children[0]))}));
"""
    rendered = json.loads(subprocess.run(["node", "-"], input=script, text=True, capture_output=True, check=True).stdout)

    def text(node):
        return node.get('text', '') + ''.join(text(child) for child in node.get('children', []))

    assert rendered['nameTips'] == ['Agence <nationale>', 'Agence (locale)', '']
    for body, item in zip(bodies, rendered['names'], strict=True):
        line = item['children'][0]
        name, roles = line['children']
        assert text(name) == body.get('name_en', body['name'])
        assert name['tag'] == 'span' and 'authority role' in text(roles)
        assert 'aria-description' not in line['attrs'] and 'aria-description' not in roles['attrs']
        assert len(item['children']) == 1  # no original on a second line
        if body.get('name_en'):
            assert name['tabIndex'] == 0 and body['name'] not in text(item)
        else:
            assert name['attrs'] == {} and 'tabIndex' not in name
    assert rendered['titleTips'] == ['L’annonce <originale>', '', '', '']
    marks = [n for n in rendered['marks']['children'] if n.get('tag') == 'a']
    for src, item, mark in zip(sources, rendered['list']['children'], marks, strict=True):
        link = item['children'][0]
        shown = src.get('title_en', src['title'])
        assert text(link) == shown and link['href'] == src['url'] and 'tabIndex' not in link
        assert link.get('lang') == (None if src.get('title_en') or src.get('language') == 'en' else src.get('language'))
        assert mark['dataset']['tip'] == shown + ' (Publisher)'
        assert mark['dataset']['tipSub'] == (src['title'] + ' · ' if src.get('title_en') else '') + 'example.org'
        assert ', Publisher, 2026-11-15' in text(item)
        assert text(item).endswith('Source last re-read 15 Nov 2026.')
    translated = rendered['list']['children'][0]
    assert [c.get('className', '').split(' ')[0] for c in translated['children']][-2:] == ['archive-copy', 'reread']  # a record's list ends the line with its archived copy
    assert ', supports finding' in text(translated)
    assert sources[0]['title'] not in text(translated)
    for name in rendered['institutes'][:2]:
        assert text(name) == 'Institute'
        assert name['attrs']['aria-description'] == 'Institut <local>'
        assert 'original-text' in name['className']
    assert rendered['institutes'][0]['tabIndex'] == 0
    assert 'tabIndex' not in rendered['institutes'][1]
    assert rendered['institutes'][1]['href'] == 'https://example.org/institute'
    assert rendered['institutes'][2] == {'text': 'Same name'}
    assert rendered['institutes'][3]['attrs'] == {}
    translated_row = rendered['rows'][0]
    assert text(translated_row) == 'Ministry' and translated_row['tabIndex'] == 0
    assert translated_row['attrs']['aria-description'] == 'Ministère <local>'
    assert rendered['rows'][1] == 'Original ministry'
    assert text(rendered['rows'][2]) == 'Not published'
    assert text(rendered['rows'][3]) == 'Not applicable'
    assert all('aria-description' not in node['attrs'] for node in rendered['rows'][2:])
