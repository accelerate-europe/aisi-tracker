"""The browser renders supplied choices and field evidence, including empty bindings."""

import json
import re
import subprocess
from pathlib import Path

import pytest

from scripts.check_page import expected_activity

PAGE = Path(__file__).resolve().parents[1] / "site/index.html"


def test_partner_fold_renders_every_item_with_its_citations():
    source = PAGE.read_text()
    helper = source[
        source.index("  function partnersFold(") : source.index("  function recordSources(")
    ]
    # Hand-worked eight-item order: the first six also appear on the card.
    items = [
        {"name": f"Body {n}", "how": f"Does task {n}.", "cited": [f"source-{n}"]}
        for n in range(8)
    ]
    data = {"state": "stated", "items": items[:6], "fold_items": items}
    stub = """
function el(tag, className, text) {
  return {tag, className, text, children: [], appendChild(child) { this.children.push(child); }};
}
const document = {createTextNode: text => el("text", "", text)};
const cite = (ids, label) => ({className: "cites", ids, label});
"""
    script = stub + helper + "\nconst p = " + json.dumps(data) + """;
const fold = partnersFold(p, cite);
const assert = require("node:assert/strict");
assert.equal(partnersFold({state: "stated"}, cite), null);
assert.equal(partnersFold({state: "stated", fold_items: []}, cite), null);
for (const state of ["not_published", "not_applicable"]) {
  assert.equal(partnersFold({state, fold_items: p.fold_items}, cite), null);
}
const items = fold.children.find(child => child.tag === "ul").children;
console.log(JSON.stringify(items.map(item => ({
  name: item.children.find(child => child.className === "pname").text,
  how: item.children.find(child => child.className === "phow").text,
  cited: item.children.find(child => child.className === "phow").children.find(child => child.className === "cites").ids
}))));
"""
    result = subprocess.run(
        ["node", "-e", script], check=True, capture_output=True, text=True
    )
    assert json.loads(result.stdout) == items


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
        "id": "gb-aisi",
        "outputs": [
            {
                "lineage": "approach-to-evaluations",
                "date": "1900",
                "credit": {"date": "2024-02-09"},
                "qualifies_as": "published_work",
                "source_ids": ["bibliography-only"],
                "citations": {"display": ["wrong"]},
            }
        ],
    }
    actual = expected_activity(institute)
    assert actual[0]["date"] == "2024-02-09"
    assert actual[0]["source_ids"] == ["gb-approach-2024"]


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
    assert events, "production first-output oracle must exercise credited events"
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
    institute["id"] = "gb-aisi"
    institute["outputs"][0]["lineage"] = "approach-to-evaluations"
    del institute["outputs"][0]["credit"]
    with pytest.raises(KeyError, match="credit"):
        expected_activity(institute)
    from scripts.check_page import expected_event_sources

    with pytest.raises(ValueError, match="credited catalogue item"):
        expected_event_sources(
            {"institutes": []}, {"id": "XX"}, {"event": "first_output"}
        )


def test_detail_oracle_rejects_reordered_and_duplicate_citations():
    from scripts.check_page import check_source_alignment

    sources = [
        {"id": sid, "url": "https://example.org/" + sid}
        for sid in ("description", "period", "confidence")
    ]
    data = {
        "sources": sources,
        "jurisdictions": [{"id": "XX"}],
        "institutes": [
            {
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
        html = f'<article class="card" data-j="XX">{source_list}</article><article class="fund-item" data-j="XX">{source_list}<p data-record="funding" data-record-id="budget">Published budget{cites}</p></article>'
        return [
            e
            for e in check_source_alignment(html, data)
            if "record citations differ" in e
        ]

    assert errors(["description", "period", "confidence"]) == []
    assert errors(["period", "description", "confidence"])
    assert errors(["description", "period", "confidence", "confidence"])


def test_recent_development_sample_is_independent_and_order_sensitive():
    import copy
    from scripts.published import publish
    from scripts.check_page import check_summary_data, expected_event_sources

    data = publish(
        json.loads((PAGE.parents[1] / "data/aisi.json").read_text()), strict=True
    )
    jurisdiction = next(j for j in data["jurisdictions"] if j["id"] == "ES")
    event = next(e for e in jurisdiction["timeline"] if e["id"] == "es-1")
    assert expected_event_sources(data, jurisdiction, event) == [
        "es-plan",
        "es-moncloa",
    ]
    event["citations"] = {"description": ["wrong"]}
    event["source_ids"] = ["wrong"]
    assert expected_event_sources(data, jurisdiction, event) == [
        "es-plan",
        "es-moncloa",
    ]
    fixture = copy.deepcopy(data)
    fixture["jurisdictions"] = [jurisdiction]
    jurisdiction["timeline"] = [event]

    def errors(ids):
        cites = "".join(f'<a class="cn" data-source-id="{sid}">1</a>' for sid in ids)
        html = f'<ul id="development-list"><li class="development-row" data-j="ES" data-events="es-1"><time class="development-date" data-date="{event["date"]}"></time><span class="development-name">{jurisdiction["name"]}</span><p class="development-text">{event["short"]}<span class="cites">{cites}</span></p></li></ul>'
        return [
            e
            for e in check_summary_data(html, fixture)
            if "development citations differ" in e
        ]

    assert errors(["es-plan", "es-moncloa"]) == []
    assert errors(["es-moncloa", "es-plan"])
    assert errors(["es-plan", "es-moncloa", "es-moncloa"])


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


def test_other_overview_links_are_preserved_without_note_citations():
    from scripts.published import publish

    data = publish(
        json.loads((PAGE.parents[1] / "data/aisi.json").read_text()), strict=True
    )
    expected = [
        ["network-2025", "network-nist-2026"],
        ["ov-wikipedia"],
        ["ov-casrai"],
        ["ai-act-tracker"],
    ]
    assert [
        o["display"]["source_links"] for o in data["dataset"]["other_overviews"]
    ] == expected
    assert all(not o["citations"]["note"] for o in data["dataset"]["other_overviews"])
    assert "sourceLinks(o.display.source_links, S)" in PAGE.read_text()


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
        source.index("  // One display name per body") : source.index("  function recordSources(")
    ]
    card_start = source.index("  function instituteCard(")
    card_rows = source[
        source.index("    if (inst) {", card_start) : source.index(
            "    var rest = recordSources(j)", card_start
        )
    ]
    table_rows = source[
        source.index("      var instLabel =") : source.index(
            "      var net =", source.index("      var instLabel =")
        )
    ]
    stub = r"""
function el(tag,cls,text){return {tag, className:cls||'', dataset:{}, children:[],
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
        + "\nconst d="
        + json.dumps(data)
        + ";\n"
        + """
const I=Object.fromEntries(d.institutes.map(i=>[i.id,i]));
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
    # Head, Reports to and Partners appear only for institutes that have the fields (tests/test_rows.py).
    assert {"Institute", "Status since", "Form", "Mandate", "Finding", "Budget", "Staff"} <= fields <= {
        "Institute", "Status since", "Form", "Mandate", "Finding", "Budget", "Staff", "Head", "Reports to", "Partners",
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
    body = source[source.index("  function publishedFunding("):source.index("  function nativeFundingCitations(")]
    assert "withPeriod" not in body
    assert 'text += " ("' not in body


def funding_record_tree(record):
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
    result = subprocess.run(["node", "-e", stub + helpers + "\nconsole.log(JSON.stringify(fundingRecord(" + json.dumps(record) + ",cite)));"], capture_output=True, text=True, check=True)
    return json.loads(result.stdout)


def tree_text(node):
    return node["text"] + "".join(tree_text(child) for child in node["children"] if child["className"] != "cites")


def test_all_real_funding_disclosures_separate_native_figures_from_source_statements():
    from scripts.published import publish
    from scripts.check_page import expected_native_funding_value

    data = publish(json.loads((PAGE.parents[1] / "data/aisi.json").read_text()), strict=True)
    records = {record["id"]: record for institute in data["institutes"] for record in institute.get("funding", []) if record.get("amount") is not None}
    assert len(records) >= 15
    for record in records.values():
        tree = funding_record_tree(record)
        native = next(child for child in tree["children"] if child["className"] == "funding-native")
        assert native["children"][0]["text"] == expected_native_funding_value(record)
        expected_citations = list(dict.fromkeys(sid for field in ("amount", "currency", "amount_qualifier", "period", "commitment", "kind", "coverage") for sid in record.get("citations", {}).get(field, [])))
        assert native["children"][-1]["children"][-1]["text"] == ",".join(expected_citations)
        assert tree_text(native) == record["display"]["native_value"] + " " + " · ".join(record["display"]["native_qualifiers"])
        metadata = next(child for child in tree["children"] if child["className"] == "funding-metadata")
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
    # Independent regressions for former semantic duplicates, identified by permanent IDs.
    expected = {
        "funding-f9ea264762c8593293364a58539295d9": ("CAD 50m", ["one-off", "announced"]),
        "funding-8f4ea7ceaf9d5be69600d6df1c6fa215": ("JPY 380m", ["one-off", "FY2024 supplementary", "adopted"]),
        "funding-7d0b36a48cc65315865a6d6ec83ddeb6": ("JPY 8.8bn", ["one-off", "FY2025 supplementary", "adopted"]),
        "funding-0475119a71cc5e2099e293d3a48356b0": ("NOK 30m", ["a year", "2027 budget proposal"]),
        "funding-443aa6a31d205192966af69e21022fb1": ("Up to USD 10m", ["a year", "FY2026 enacted"]),
        "funding-a4935c40a4e750e8844af6de22176e13": ("~KRW 4.0bn", ["a year", "2026 R&D component", "adopted"]),
        "funding-d0f61d15548153b9830f74f6e14ec324": ("~KRW 13bn", ["a year", "2026 total", "adopted"]),
        "funding-0d5ffdbe6b0c59dabb6edfc221aa4956": ("AUD 7.9m", ["a year", "From 2029-30 (ongoing)", "announced"]),
    }
    assert {identity: (records[identity]["display"]["native_value"], records[identity]["display"]["native_qualifiers"]) for identity in expected} == expected


@pytest.mark.parametrize(
    "amount,qualifier,currency,label",
    [
        (10, None, "EUR", "EUR 10"),
        (9_150_000, "about", "EUR", "About EUR 9.2m"),
        (9_150_000, None, "EUR", "~EUR 9.2m"),
        (10, None, "USD", "USD 10"),
        (9_150_000, "about", "USD", "About USD 9.2m"),
    ],
)
def test_native_funding_gate_rejects_wrong_amount_and_wrong_field_evidence(
    amount, qualifier, currency, label
):
    from scripts.check_page import check_source_alignment
    record = {"id": "budget", "amount": amount, "amount_qualifier": qualifier, "currency": currency, "description": "A sourced statement.", "source_ids": ["amount", "statement"], "citations": {"amount": ["amount"], "description": ["statement"]}}
    data = {"sources": [{"id": identity, "url": "https://example.org/" + identity} for identity in ("amount", "statement")], "jurisdictions": [{"id": "XX", "source_ids": []}], "institutes": [{"id": "institute", "jurisdiction_id": "XX", "funding": [record], "staff": []}]}
    listing = '<div class="srcs"><ol><li data-source-id="amount"><a href="https://example.org/amount">Amount</a></li><li data-source-id="statement"><a href="https://example.org/statement">Statement</a></li></ol></div>'
    html = f'<article class="card" data-j="XX">{listing}</article><article class="fund-item" data-j="XX">{listing}<div data-record="funding" data-record-id="budget"><p class="funding-native"><span class="budget-amount">{label}</span><a class="cn" data-source-id="amount" href="https://example.org/amount">1</a></p><details class="source-statement"><summary>Source statement</summary><p>A sourced statement.<a class="cn" data-source-id="statement" href="https://example.org/statement">2</a></p></details><p class="funding-metadata"></p></div></article>'
    def errors(text):
        return [error for error in check_source_alignment(text, data) if "native funding amount" in error or "funding ." in error]
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


def test_unselected_canadian_envelope_remains_in_disclosures_and_downloads():
    from scripts.published import publish
    from scripts.build import tables
    data = publish(json.loads((PAGE.parents[1] / "data/aisi.json").read_text()), strict=True)
    canada = next(institute for institute in data["institutes"] if institute["id"] == "ca-caisi")
    additional = "funding-f9ea264762c8593293364a58539295d9"
    assert canada["display"]["budget"] == "funding-360a59a37fde5cc8ba0a03ce3a00ea5f"
    assert additional in canada["display"]["funding_ids"]
    record = canada["display"]["funding_records"][additional]
    assert "additional CAD 50m" in tree_text(funding_record_tree(record))
    exported = {row["id"]: row for row in tables(data)["funding"]}
    assert exported[additional]["amount"] == 50_000_000
    assert exported[additional]["currency"] == "CAD"
    assert "additional" in exported[additional]["description"]


def test_format_fixture_is_independent_of_live_exchange_rates():
    from scripts.check_page import make_format_fixture
    from scripts.published import publish

    data = publish(
        json.loads((PAGE.parents[1] / "data/aisi.json").read_text()), strict=True
    )
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
    assert record["display"]["value"] == case["figure"] == "Up to ~EUR 11m (GBP 9.0m)"
