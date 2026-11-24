"""Functions table gate mutations, without starting a browser."""

import copy
from pathlib import Path

import pytest

from scripts import check_page


@pytest.fixture
def functions():
    data = {
        "dataset": {"vocabularies": {"function": {
            "testing": {"label": "Testing", "short": "testing", "definition": "Tests models.", "group": "Science"},
            "old": {"label": "Retired", "definition": "Old test.", "group": "Retired", "deprecated": True},
            "advice": {"label": "Advice", "short": "advice", "definition": "Advises government.", "group": "Policy"},
            "research": {"label": "Research", "short": "research", "definition": "Does research.", "group": "Science"},
            "foresight": {"label": "Foresight", "short": "foresight", "definition": "Forecasts.", "group": "Policy"},
            "incidents": {"label": "Incidents", "short": "incidents", "definition": "Responds.", "group": "Policy"},
        }}},
        "jurisdictions": [{"id": "AA", "name": "Alpha", "status": "proposed", "status_since": "2020-01-01"},
                          {"id": "ZZ", "name": "Zulu", "status": "established", "status_since": "2024"}],
        "institutes": [
            {"id": "a", "jurisdiction_id": "AA", "name": "Alpha Institute", "acronym": "AI",
             "functions": [{"code": "testing", "value": "both", "claim_ids": []},
                           {"code": "advice", "value": "not_coded", "claim_ids": []},
                           {"code": "research", "value": "activity", "claim_ids": []},
                           {"code": "foresight", "value": "not_found", "claim_ids": []},
                           {"code": "incidents", "value": "not_found", "claim_ids": []}], "claims": []},
            {"id": "z", "jurisdiction_id": "ZZ", "name": "Zulu Institute", "short_name": "ZI",
             "functions": [{"code": "testing", "value": "activity", "claim_ids": ["c"]},
                           {"code": "advice", "value": "mandate", "claim_ids": []},
                           {"code": "research", "value": "not_found", "claim_ids": []},
                           {"code": "foresight", "value": "not_found", "claim_ids": []},
                           {"code": "incidents", "value": "not_found", "claim_ids": []}],
             "claims": [{"id": "fact", "kind": "fact", "text": "A separate fact."},
                        {"id": "c", "kind": "function", "basis": "activity", "text": "It tested a model.", "source_ids": ["s"]}]},
        ],
        "sources": [{"id": "s", "url": "https://example.org/source", "excerpts": [
            {"text": "We tested model X.", "supports": ["/institutes/1/claims/1/text"]}]}],
    }
    html = '''<nav class="secnav"><a href="#functions" data-sections="functions testing set-up funding">Compare</a></nav>
    <div class="group"><nav class="group-nav"><a href="#functions">Functions</a></nav>
    <section aria-labelledby="functions"><h2 id="functions">Functions</h2><div id="functions-table">
    <p class="fn-summary">Most common: testing (2 of 2 institutes), advice (1) and research (1).</p>
    <span id="hint" class="vh">Scrolls sideways</span>
    <div class="fn-scroll" role="region" tabindex="0" aria-labelledby="functions" aria-describedby="hint">
    <table class="fn-table"><caption class="vh">Functions by institute</caption><colgroup></colgroup><colgroup span="2"></colgroup><colgroup span="3"></colgroup><colgroup></colgroup><thead><tr>
    <th scope="col" rowspan="2">Institute</th><th scope="colgroup" colspan="2">Science</th><th scope="colgroup" colspan="3">Policy</th><td class="fn-pad" aria-hidden="true"></td>
    </tr><tr>
    <th scope="col" data-code="testing"><abbr class="g" tabindex="0" aria-description="Testing: Tests models.">Testing</abbr></th>
    <th scope="col" data-code="research"><abbr class="g" tabindex="0" aria-description="Research: Does research.">Research</abbr></th>
    <th scope="col" data-code="advice" class="fn-group-start"><abbr class="g" tabindex="0" aria-description="Advice: Advises government.">Advice</abbr></th>
    <th scope="col" data-code="foresight"><abbr class="g" tabindex="0" aria-description="Foresight: Forecasts.">Foresight</abbr></th>
    <th scope="col" data-code="incidents"><abbr class="g" tabindex="0" aria-description="Incidents: Responds.">Incidents</abbr></th><td class="fn-pad" aria-hidden="true"></td>
    </tr></thead>
    <tbody><tr class="fn-group"><th scope="rowgroup">Established</th><td class="fn-column fn-group-first" data-code="testing" aria-hidden="true"></td><td class="fn-column" data-code="research" aria-hidden="true"></td><td class="fn-column fn-group-start" data-code="advice" aria-hidden="true"></td><td class="fn-column" data-code="foresight" aria-hidden="true"></td><td class="fn-column" data-code="incidents" aria-hidden="true"></td><td class="fn-pad" aria-hidden="true"></td></tr>
    <tr data-institute="z"><th scope="row"><abbr class="g" tabindex="0" aria-description="Zulu Institute">Zulu</abbr></th>
    <td data-code="testing" data-value="activity"><button type="button" class="fn-cell" aria-label="Zulu, Testing: Active" aria-expanded="false" aria-controls="detail-z"><span class="fn-mark fn-activity" aria-hidden="true"></span></button></td>
    <td data-code="research" data-value="not_found"><span class="vh">not found</span></td>
    <td data-code="advice" data-value="mandate" class="fn-group-start"><button type="button" class="fn-cell" aria-label="Zulu, Advice: Assigned" aria-expanded="false" aria-controls="detail-z"><span class="fn-mark fn-mandate" aria-hidden="true"></span></button></td>
    <td data-code="foresight" data-value="not_found"><span class="vh">not found</span></td>
    <td data-code="incidents" data-value="not_found"><span class="vh">not found</span></td><td class="fn-pad" aria-hidden="true"></td></tr>
    </tbody><tbody><tr class="fn-group"><th scope="rowgroup">Not yet established</th><td class="fn-column fn-group-first" data-code="testing" aria-hidden="true"></td><td class="fn-column" data-code="research" aria-hidden="true"></td><td class="fn-column fn-group-start" data-code="advice" aria-hidden="true"></td><td class="fn-column" data-code="foresight" aria-hidden="true"></td><td class="fn-column" data-code="incidents" aria-hidden="true"></td><td class="fn-pad" aria-hidden="true"></td></tr>
    <tr data-institute="a"><th scope="row"><abbr class="g" tabindex="0" aria-description="Alpha Institute">Alpha</abbr></th>
    <td data-code="testing" data-value="both"><button type="button" class="fn-cell" aria-label="Alpha, Testing: Assigned and active" aria-expanded="false" aria-controls="detail-a"><span class="fn-mark fn-activity" aria-hidden="true"></span></button></td>
    <td data-code="research" data-value="activity"><button type="button" class="fn-cell" aria-label="Alpha, Research: Active" aria-expanded="false" aria-controls="detail-a"><span class="fn-mark fn-activity" aria-hidden="true"></span></button></td>
    <td data-code="advice" data-value="not_coded" class="fn-group-start"><span class="vh">not yet coded</span></td>
    <td data-code="foresight" data-value="not_found"><span class="vh">not found</span></td>
    <td data-code="incidents" data-value="not_found"><span class="vh">not found</span></td><td class="fn-pad" aria-hidden="true"></td></tr>
    </tbody></table></div>
    <ul class="fn-legend fn-desktop-only" role="list"><li data-value="activity">Active</li><li data-value="mandate">Assigned</li><li data-value="not_found">Blank: no public text found; it may still be done</li><li data-value="not_coded">not yet coded</li></ul>
    <div class="section-foot"><details class="more quiet prose-panel fn-how"><summary>How this is coded</summary>
    <p class="small fn-use fn-desktop-only">Select a mark to see the quote and source.</p>
    <p class="small fn-use fn-phone-only">Select a country to see its functions, then a function for its quote and source.</p>
    <p class="small">We record quoted public texts.</p>
    <p class="small fn-order">Rows: by the year the institute's current status began, oldest first.</p>
    <p class="small fn-provisional">The list of functions is provisional.</p></details><span class="section-foot-actions"><a class="standalone section-dl ico i-dl" href="data/functions.csv" download="functions.csv"><span class="vh">Download functions (CSV)</span></a></span></div>
    </div></section></div>'''
    return data, html


def test_functions_structure_keeps_unanimous_not_found_columns(functions):
    data, html = functions
    assert check_page.functions_structure(html, data) == []
    assert [i["id"] for i in check_page.function_rows(data)] == ["z", "a"]


@pytest.mark.parametrize("day, label", [("2024", "2024"), ("2024-11", "November 2024"), ("2024-11-20", "November 2024")])
def test_grid_control_names_since_at_month_precision(functions, day, label):
    data, html = functions
    data = copy.deepcopy(data)
    institute = next(i for i in data["institutes"] if i["id"] == "z")
    next(row for row in institute["functions"] if row["code"] == "testing")["since"] = day
    assert check_page.function_month(day) == label
    assert any("coded control/name differs" in e for e in check_page.functions_structure(html, data))
    html = html.replace('aria-label="Zulu, Testing: Active"', 'aria-label="Zulu, Testing: Active, since ' + label + '"')
    assert check_page.functions_structure(html, data) == []


def test_functions_structure_rejects_a_missing_vocabulary_column(functions):
    from bs4 import BeautifulSoup

    data, html = functions
    soup = BeautifulSoup(html, "html.parser")
    for cell in soup.select('.fn-table [data-code="incidents"]'):
        cell.decompose()
    soup.select_one('th[scope="colgroup"][colspan="3"]')["colspan"] = "2"
    soup.select_one('colgroup[span="3"]')["span"] = "2"
    assert "Functions columns/order differ" in check_page.functions_structure(str(soup), data)


def test_functions_desktop_missing_function_uses_uncoded_value(functions):
    data, html = functions
    data = copy.deepcopy(data)
    institute = next(i for i in data["institutes"] if i["id"] == "a")
    institute["functions"] = [row for row in institute["functions"] if row["code"] != "advice"]
    assert check_page.functions_structure(html, data) == []


def test_function_rows_order_by_year_then_name_then_id(functions):
    data, _ = functions
    data = copy.deepcopy(data)
    data["jurisdictions"] += [
        {"id": "BB", "name": "Beta", "status": "established", "status_since": "2024-12-31"},
        {"id": "CC", "name": "Later", "status": "established", "status_since": "2025"},
    ]
    data["institutes"] += [{"id": "b2", "jurisdiction_id": "BB"}, {"id": "b1", "jurisdiction_id": "BB"}, {"id": "c", "jurisdiction_id": "CC"}]
    assert [i["id"] for i in check_page.function_rows(data)] == ["b1", "b2", "z", "c", "a"]


@pytest.mark.parametrize(("old", "new", "finding"), [
    ('data-value="activity"', 'data-value="mandate"', "value counts"),
    ('<colgroup span="2"></colgroup>', '<colgroup span="3"></colgroup>', "column groups"),
    ('aria-label="Zulu, Testing: Active"', 'aria-label="ZI, Testing: Active"', "coded control/name"),
    ('aria-label="Alpha, Testing: Assigned and active"', 'aria-label="Alpha, Testing: Active"', "coded control/name"),
    ('aria-description="Testing: Tests models."', 'aria-description="Wrong test."', "function label/test"),
    ('aria-description="Zulu Institute"', 'aria-description="Wrong institute"', "row name/institute tip"),
    ('aria-description="Zulu Institute"', 'aria-description="Zulu Institute (planned)"', "status word in row tip"),
    ('<th scope="rowgroup">', '<th scope="rowgroup" colspan="5">', "group scope/span"),
    ('<td class="fn-column" data-code="research" aria-hidden="true"></td>', '', "block band columns/order"),
    ('>Zulu</abbr>', '>Zulu (2 of 5)</abbr>', "row name/institute tip"),
    ('Not yet established</th>', 'Pending</th>', "not-yet-established"),
    ('Not yet established</th>', 'proposed</th>', "status word"),
    ('<li data-value="not_coded">not yet coded</li>', '', "legend differs"),
    ('<tr data-institute="z">', '<tr data-institute="a">', "rows/order"),
    ('fn-mark fn-activity', 'fn-mark fn-both', "mark differs"),
    ('Most common: testing (2 of 2 institutes),', 'Most common: Zulu Institute (2 of 2 institutes),', "summary facts"),
    ('<div id="functions-table">', '<div id="functions-table"><p>It does this (active).</p>', "status word alone in parentheses"),
    ('<div id="functions-table">', '<div id="functions-table"><p class="fn-blank">Old caveat.</p>', "obsolete lede or blank"),
    ('Blank: no public text found; it may still be done', 'Blank: no public text found', "legend differs"),
    ('Blank: no public text found; it may still be done', 'Blank: it may still be done', "legend differs"),
    ('The list of functions is provisional.', '', "provisional line"),
    ('<tbody>', '<tbody><tr class="fn-detail-row"><td>We tested model X.</td></tr>', "quote was pre-rendered"),
])
def test_functions_structure_rejects_mutations(functions, old, new, finding):
    data, html = functions
    assert old in html
    assert any(finding in e for e in check_page.functions_structure(html.replace(old, new), data))


@pytest.mark.parametrize("suffix", ["planned", "proposed", "exploring", "announced", "PLANNED"])
def test_functions_status_suffix_is_hidden_in_both_forms(functions, functions_phone, suffix):
    data, desktop = functions
    data = copy.deepcopy(data)
    for institute in data["institutes"]:
        institute["name"] += f" ({suffix})"
    # The hand-written displays still contain the full institute name, without the recorded suffix.
    assert check_page.functions_structure(desktop, data) == []
    assert check_page.functions_phone_structure(functions_phone[1], data) == []


@pytest.mark.parametrize(("name", "shown"), [
    ("Institute (ISI) (Proposed)", "Institute (ISI)"),
    ("Institute (ISI)", "Institute (ISI)"),
    ("Institute (planned research)", "Institute (planned research)"),
    ("Institute (planned) branch", "Institute (planned) branch"),
    ("Institute announced", "Institute announced"),
])
def test_function_display_name_preserves_everything_except_trailing_status(name, shown):
    assert check_page.function_display_name({"name": name, "short_name": "Short", "acronym": "I"}) == shown


def test_function_quote_oracle_uses_original_indices_and_requires_one_binding(functions):
    data, _ = functions
    assert check_page.function_quotes(data)["c"]["excerpt"]["text"] == "We tested model X."
    broken = copy.deepcopy(data)
    broken["sources"][0]["excerpts"][0]["supports"] = ["/institutes/1/claims/0/text"]
    with pytest.raises(ValueError, match="expected one bound quote, got 0"):
        check_page.function_quotes(broken)
    broken = copy.deepcopy(data)
    broken["sources"][0]["excerpts"] *= 2
    with pytest.raises(ValueError, match="expected one bound quote, got 2"):
        check_page.function_quotes(broken)


@pytest.mark.parametrize(("values", "expected"), [
    ([15, 13, 12, 11], "Most common: a (15 of 15 institutes), b (13) and c (12)."),
    ([15, 13, 12, 12], "Most common: a (15 of 15 institutes) and b (13)."),
    ([15, 12, 12, 12], "Most common: a (15 of 15 institutes)."),
    ([12, 12, 12, 12], "Most common: a, b, c and d (12 of 15 institutes each)."),
    ([12, 12, 12, 0], "Most common: a (12 of 15 institutes), b (12) and c (12)."),
    ([0, 1, 1, 2], "Most common: d (2 of 15 institutes), b (1) and c (1)."),
    ([0, 0, 0, 0], ""),
])
def test_function_summary_keeps_vocabulary_order_and_whole_ties(values, expected):
    vocab = {code: {"short": code} for code in "abcd"}
    assert check_page.function_summary(vocab, dict(zip(vocab, values)), 15) == expected


# B. Hand-worked group endpoints: a 60px lean over 60*sqrt(3) height, with a 1px head rule.
def test_functions_structure_keeps_a_group_coded_nowhere(functions):
    """A group with uncoded and not-found functions stays visible and is absent from the summary."""
    data, html = functions
    data = copy.deepcopy(data)
    next(r for r in data["institutes"][1]["functions"] if r["code"] == "advice")["value"] = "not_coded"
    mandate = '<td data-code="advice" data-value="mandate" class="fn-group-start"><button type="button" class="fn-cell" aria-label="Zulu, Advice: Assigned" aria-expanded="false" aria-controls="detail-z"><span class="fn-mark fn-mandate" aria-hidden="true"></span></button></td>'
    assert mandate in html
    html = (html.replace(mandate, '<td data-code="advice" data-value="not_coded" class="fn-group-start"><span class="vh">not yet coded</span></td>')
            .replace('<td data-code="advice">1</td>', '<td data-code="advice">0</td>')
            .replace("testing (2 of 2 institutes), advice (1) and research (1).", "testing (2 of 2 institutes) and research (1)."))
    assert check_page.functions_structure(html, data) == []


def test_function_rows_put_an_undated_status_last_in_its_block(functions):
    """A record without status_since (an edge the card-grid gate exercises) sorts after the dated ones, never fails."""
    data, _ = functions
    data = copy.deepcopy(data)
    for j in data["jurisdictions"]:
        j.setdefault("status_since", "2024-01-01")
    data["jurisdictions"][0].pop("status_since")
    assert [i["id"] for i in check_page.function_rows(data)] == ["z", "a"]


@pytest.mark.parametrize(("text", "expected"), [
    ("The institute is doing it (active).", ["(active)"]),
    ("The government gives it the function (ASSIGNED).", ["(ASSIGNED)"]),
    ("It does it ( active ) and is tasked with it (assigned).", ["( active )", "(assigned)"]),
    ("Active: it does the work. Assigned: the law gives it the function.", []),
    ("It has a role (assigned by law) and staff (active since 2024).", []),
    ("(active)", []),
    ("A heading. (Assigned).", []),
    ("(active)\nA separate sentence.", []),
])
def test_function_copy_parenthesised_status_rule(text, expected):
    from scripts.reader_text import status_parentheses

    assert status_parentheses(text) == expected


def test_function_copy_reader_rule_covers_prose_and_masks_evidence():
    from scripts.reader_text import check_reader_text

    data = {
        "dataset": {
            "definition": "It does the work (active).",
            "vocabularies": {"function": {"testing": {"definition": "The law assigns it (assigned)."}}},
        },
        "jurisdictions": [{"id": "AA", "claims": [
            {"text": 'It reports "We do it (active)." and is tasked with it (assigned).'},
        ]}],
        "sources": [{"title": "Doing it (active)", "excerpts": [
            {"text": "We do it (assigned).", "gloss": "A translated description (active)."},
        ]}],
    }
    errors = [error for error in check_reader_text(data) if "parenthesised status" in error]
    assert errors == [
        "/dataset/definition: reword parenthesised status '(active)' as a plain sentence",
        "/dataset/vocabularies/function/testing/definition: reword parenthesised status '(assigned)' as a plain sentence",
        "/jurisdictions/0/claims/0/text: reword parenthesised status '(assigned)' as a plain sentence",
        "/sources/0/excerpts/0/gloss: reword parenthesised status '(active)' as a plain sentence",
    ]


def test_function_copy_status_rule_reads_only_fn_copy_strings():
    from scripts.reader_text import functions_copy_errors

    html = r'''<script>
    var other = "Elsewhere it does work (active).";
    var FN_COPY = {
      intro: "Which of {count} functions it does (active).",
      how: 'The institute does it (assigned).',
      escaped: "It does work \u0028active\u0029.",
      label: "(assigned)",
      plain: "Active: a public text reports the institute doing it."
    };
    </script>'''
    assert functions_copy_errors(html) == [
        "FN_COPY.intro: reword parenthesised status '(active)' as a plain sentence",
        "FN_COPY.how: reword parenthesised status '(assigned)' as a plain sentence",
        "FN_COPY.escaped: reword parenthesised status '(active)' as a plain sentence",
    ]
    assert functions_copy_errors('<p>Elsewhere it does work (active).</p>') == []


@pytest.fixture
def functions_phone(functions):
    """Hand-written phone projection: all five values, two unanimous not-found codes and two status blocks."""
    data, _ = functions
    html = '''<section aria-labelledby="functions"><h2 id="functions">Functions</h2><div id="functions-table">
    <div class="fn-phone"><div class="fn-phone-head"><span></span><span><span class="fn-group-label fn-label-full">Science</span><span class="fn-group-label fn-label-short">Science</span></span><span><span class="fn-group-label fn-label-full">Policy</span><span class="fn-group-label fn-label-short">Policy</span></span></div>
    <div class="fn-phone-block"><h3>Established</h3><ul role="list"><li data-institute="z">
    <button type="button" class="fn-phone-row" aria-label="Zulu, Zulu Institute: show functions" aria-expanded="false" aria-controls="fn-phone-detail-z"></button><span class="fn-phone-country" aria-hidden="true">Zulu</span><span class="fn-pips" aria-hidden="true"><span class="fn-mark fn-activity fn-pip" aria-hidden="true"></span><span class="fn-mark fn-not_found fn-pip" aria-hidden="true"></span></span><span class="fn-pips" aria-hidden="true"><span class="fn-mark fn-mandate fn-pip" aria-hidden="true"></span><span class="fn-mark fn-not_found fn-pip" aria-hidden="true"></span><span class="fn-mark fn-not_found fn-pip" aria-hidden="true"></span></span>
    </li></ul></div>
    <div class="fn-phone-block"><h3>Not yet established</h3><ul role="list"><li data-institute="a">
    <button type="button" class="fn-phone-row" aria-label="Alpha, Alpha Institute: show functions" aria-expanded="false" aria-controls="fn-phone-detail-a"></button><span class="fn-phone-country" aria-hidden="true">Alpha</span><span class="fn-pips" aria-hidden="true"><span class="fn-mark fn-activity fn-pip" aria-hidden="true"></span><span class="fn-mark fn-activity fn-pip" aria-hidden="true"></span></span><span class="fn-pips" aria-hidden="true"><span class="fn-mark fn-not_coded fn-pip" aria-hidden="true"></span><span class="fn-mark fn-not_found fn-pip" aria-hidden="true"></span><span class="fn-mark fn-not_found fn-pip" aria-hidden="true"></span></span>
    </li></ul></div></div>
    <ul class="fn-legend fn-phone-only" role="list"><li data-value="activity"><span class="fn-mark fn-activity fn-pip" aria-hidden="true"></span>Active</li><li data-value="mandate"><span class="fn-mark fn-mandate fn-pip" aria-hidden="true"></span>Assigned</li><li data-value="not_found"><span class="fn-mark fn-not_found fn-pip" aria-hidden="true"></span>No public text found; it may still be done</li><li data-value="not_coded"><span class="fn-mark fn-not_coded fn-pip" aria-hidden="true"></span>not yet coded</li></ul>
    </div></section>'''
    return data, html


def test_functions_phone_structure_accepts_groups_values_and_row_order(functions_phone):
    data, html = functions_phone
    assert check_page.functions_phone_structure(html, data) == []


def test_function_group_labels_keep_unknown_names():
    import json
    import subprocess

    expected = {"Testing and science": "Testing and science", "Rules": "Rules and standards", "Informing government": "Informing government", "Ecosystem": "Ecosystem and adoption",
                "Other and new": "Other and new", "constructor": "constructor", "__proto__": "__proto__"}
    source = (Path(__file__).resolve().parents[1] / "site/index.html").read_text()
    helpers = source[source.index("      var GROUP_LABELS ="):source.index("      function columnClass(")]
    result = subprocess.run(["node", "-e", helpers + "console.log(JSON.stringify(" + json.dumps(list(expected)) + ".map(groupName)));"],
                            capture_output=True, text=True, check=True)
    assert json.loads(result.stdout) == list(expected.values())
    assert [check_page.fn_group_name(group) for group in expected] == list(expected.values())


@pytest.mark.parametrize("first, second, first_label, second_label", [
    ("Testing and science", "Rules", "Testing and science", "Rules and standards"),
    ("Informing government", "Ecosystem", "Informing government", "Ecosystem and adoption"),
])
def test_function_group_labels_in_desktop_and_phone_heads(functions, functions_phone, first, second, first_label, second_label):
    for (data, html), check in ((functions, check_page.functions_structure), (functions_phone, check_page.functions_phone_structure)):
        data = copy.deepcopy(data)
        for entry in data["dataset"]["vocabularies"]["function"].values():
            entry["group"] = {"Science": first, "Policy": second}.get(entry["group"], entry["group"])
        html = html.replace(">Science<", ">" + first_label + "<").replace(">Policy<", ">" + second_label + "<")
        for label in (first_label, second_label):  # the phone head's short form prints "&" for "and"
            html = html.replace('fn-label-short">' + label + "<", 'fn-label-short">' + label.replace(" and ", "\u00a0& ") + "<")
        assert check(html, data) == []
        assert any("groups/order" in error or "group heads/order" in error for error in check(html.replace(">" + first_label + "<", ">Wrong group<"), data))


def test_function_claims_keep_grid_status_and_mixed_basis_labels():
    """Exercise the real claim renderer with single/mixed bases, inline and in grid panels."""
    import subprocess

    source = (Path(__file__).resolve().parents[1] / 'site/index.html').read_text()
    copy = source[source.index('      var FN_COPY ='):source.index('      var names =', source.index('      var FN_COPY ='))]
    renderer = source[source.index('      function fnSince('):source.index('      function build()', source.index('      function appendClaims('))]
    dates = source[source.index('  var MON ='):source.index('  // A date inside running text')]
    script = """
const assert=require('node:assert/strict');
function el(tag,cls='',text='') {
  return {tag,className:cls,text,children:[],dataset:{},open:false,classList:{add(){}},
    get textContent(){return this.text+this.children.map(n=>n.textContent).join('');},
    appendChild(n){this.children.push(n);return n;}, querySelectorAll:()=>[],querySelector:()=>null,insertBefore(){}};
}
const document={createTextNode:text=>el('#text','',text)}, S={}, SOURCE_ORDERS={};
const citations=()=>{}, fieldCitations=()=>[], fnSources=()=>el('ul'), claimBlock=()=>el('div','fn-claim');
const quoteOnSourceLine=()=>{}, glueCitations=()=>{}, externalLinks=()=>{};
const instShort=i=>el('span','',i.short_name), names={mandate:'Assigned',activity:'Active',both:'Assigned and active'};
const i={id:'sample',jurisdiction_id:'XX',short_name:'UK AISI'}, rows={sample:{test:{}}};
const CLAIM_INDEX={a:{claim:{basis:'activity',text:'It ran tests.'},excerpts:[{source:{published:'2024-11-20'}}]},m:{claim:{basis:'mandate'},excerpts:[]},m2:{claim:{basis:'mandate'},excerpts:[]}};
COPY
DATES
RENDERER
for(const [value,ids,since,sentence,status] of [
  ['mandate',['m'],null,'Assigned to UK AISI','assigned'],
  ['activity',['a'],null,'UK AISI is active, without an assignment','active, without an assignment'],
  ['both',['m','m2','a'],null,'Assigned to UK AISI, and active','assigned and active'],
  ['activity',['a'],'2024-11-20','UK AISI is active since November 2024, without an assignment','active since November 2024, without an assignment'],
  ['both',['m','m2','a'],'2024-11','Assigned to UK AISI, and active since November 2024','assigned and active since November 2024'],
  ['activity',['a'],'2024','UK AISI is active since 2024, without an assignment','active since 2024, without an assignment']
]) for(const inline of [false,true]) {
  rows.sample.test={value,claim_ids:ids,since};
  assert.equal(fnStatus(rows.sample.test),status);
  const detail=el('div'); appendClaims(detail,i,'test',inline);
  const contexts=detail.children.filter(n=>n.className.split(' ').includes('fn-detail-context'));
  assert.deepEqual(contexts.map(n=>n.textContent),inline?[]:[sentence]);
  const evidence=detail.children.filter(n=>!n.className.split(' ').includes('fn-detail-context'));
  assert.deepEqual(evidence.map(n=>n.className==='fn-basis'?n.textContent:n.dataset.claim),
    value==='both'?['Assignment','m','m2','Activity','a']:ids);
}
assert.equal(fnNewest({newest:'a'}),'Newest: It ran tests. (November 2024)');
CLAIM_INDEX.a.excerpts[0].source.published='2024';
assert.equal(fnNewest({newest:'a'}),'Newest: It ran tests. (2024)');
delete CLAIM_INDEX.a.excerpts[0].source.published;
assert.equal(fnNewest({newest:'a'}),'Newest: It ran tests.');
assert.equal(fnNewest({newest:null}),'');
for(const count of [1,3,4,6]) for(const value of ['activity','both']) for(const inline of [false,true]) {
  // Generated order is authoritative, even when IDs do not sort in date order.
  const activityIds=['z','a','q','b','expired-z','expired-a'].slice(0,count);
  activityIds.forEach(id=>CLAIM_INDEX[id]={claim:{basis:'activity',expired:id.startsWith('expired')},excerpts:[]});
  const assignments=value==='both'?['m','m2']:[], ids=[...assignments,...activityIds];
  rows.sample.test={value,claim_ids:ids,since:null};
  const detail=el('div'); appendClaims(detail,i,'test',inline);
  assert.deepEqual(detail.children.filter(n=>n.className==='fn-claim').map(n=>n.dataset.claim),[...assignments,...activityIds.slice(0,3)]);
  const folds=detail.children.filter(n=>n.className.split(' ').includes('fn-earlier'));
  assert.equal(folds.length,count>3?1:0);
  if(count>3) {
    const fold=folds[0]; assert.equal(fold.tag,'details'); assert.equal(fold.open,false);
    assert.equal(fold.children[0].tag,'summary'); assert.equal(fold.children[0].textContent,(count-3)+' earlier');
    assert.deepEqual(fold.children.slice(1).map(n=>n.dataset.claim),activityIds.slice(3));
    assert.equal(detail.children.at(-1),fold);
    assert.deepEqual(fold.className.split(' '),['more','quiet','fn-earlier']);
  }
  assert.deepEqual(detail.children.filter(n=>n.className==='fn-basis').map(n=>n.textContent),value==='both'?['Assignment','Activity']:[]);
}
""".replace('COPY', copy).replace('DATES', dates).replace('RENDERER', renderer)
    subprocess.run(['node', '-'], input=script, text=True, capture_output=True, check=True)


@pytest.mark.parametrize(("old", "new", "finding"), [
    ('fn-label-full">Science<', 'fn-label-full">Policy<', "group heads/order"),
    ('<h3>Not yet established</h3>', '<h3>proposed</h3>', "blocks/order"),
    ('data-institute="z"', 'data-institute="a"', "rows/order"),
    ('Zulu, Zulu Institute: show functions', 'Zulu: 2 functions with a public text', "row control/name"),
    ('Zulu, Zulu Institute: show functions', 'Zulu, Zulu Institute: proposed', "status words"),
    ('>Zulu</span>', '>Wrong country</span>', "country label"),
    ('>Zulu</span>', '>Zulu (2)</span>', "counts or status words"),
    ('class="fn-mark fn-not_found fn-pip"', 'class="fn-mark fn-activity fn-pip"', "pip mark differs"),
    ('class="fn-mark fn-activity fn-pip"', 'class="fn-mark fn-both fn-pip"', "pip mark differs"),
    ('class="fn-mark fn-activity fn-pip"', 'class="fn-mark fn-activity fn-mandate fn-pip"', "pip mark differs"),
    ('No public text found; it may still be done</li>', 'No public text found</li>', "legend label/mark"),
    ('No public text found; it may still be done</li>', 'It may still be done</li>', "legend label/mark"),
])
def test_functions_phone_structure_rejects_mutations(functions_phone, old, new, finding):
    data, html = functions_phone
    assert old in html
    assert any(finding in error for error in check_page.functions_phone_structure(html.replace(old, new), data))


@pytest.mark.parametrize(("selector", "change", "finding"), [
    (':scope > .fn-pips', "reverse", "pip count/order"),
    (':scope > .fn-pips', "remove", "pip groups/order"),
    (':scope > .fn-pips', "duplicate", "pip groups/order"),
    (':scope > .fn-pips > .fn-pip', "reverse", "pip mark differs"),
    (':scope > .fn-pips > .fn-pip', "remove", "pip count/order"),
    (':scope > .fn-pips > .fn-pip', "duplicate", "pip count/order"),
])
def test_functions_phone_structure_checks_pip_positions_and_counts(functions_phone, selector, change, finding):
    from bs4 import BeautifulSoup

    data, html = functions_phone
    soup = BeautifulSoup(html, "html.parser")
    row = soup.select_one('li[data-institute="z"]')
    # First two groups, or the two Science pips: their positions and shapes differ in this fixture.
    first, second, *_ = row.select(selector)
    if change == "reverse":
        first.insert_before(second.extract())
    elif change == "remove":
        first.decompose()
    else:
        first.insert_after(copy.deepcopy(first))
    assert any(finding in error for error in check_page.functions_phone_structure(str(soup), data))


@pytest.fixture
def functions_both(functions, functions_phone):
    """The saved page contains both hand-written projections with one shared set of caveats."""
    from bs4 import BeautifulSoup

    data, html = functions
    soup = BeautifulSoup(html, "html.parser")
    phone = BeautifulSoup(functions_phone[1], "html.parser")
    box = soup.select_one('#functions-table')
    for selector in ('.fn-phone', '.fn-legend'):
        box.select_one('.section-foot').insert_before(phone.select_one(selector).extract())
    return data, str(soup)


def test_functions_saved_forms_accept_both_and_scope_legends(functions_both):
    data, html = functions_both
    for check in (check_page.functions_forms_structure, check_page.functions_structure, check_page.functions_phone_structure):
        assert check(html, data) == []


def test_functions_phone_structure_rejects_wrong_block_without_reordering(functions_phone):
    from bs4 import BeautifulSoup

    data, html = functions_phone
    soup = BeautifulSoup(html, "html.parser")
    row = soup.select_one('li[data-institute="z"]').extract()
    soup.select('.fn-phone-block > ul')[1].insert(0, row)
    assert any("block membership" in error for error in check_page.functions_phone_structure(str(soup), data))


def test_functions_phone_missing_function_uses_uncoded_value(functions_phone):
    data, html = functions_phone
    data = copy.deepcopy(data)
    institute = next(i for i in data["institutes"] if i["id"] == "a")
    institute["functions"] = [row for row in institute["functions"] if row["code"] != "advice"]
    assert check_page.functions_phone_structure(html, data) == []


def test_functions_phone_model_keeps_unanimous_not_found_columns_and_groups(functions_phone):
    data, _ = functions_phone
    data = copy.deepcopy(data)
    for institute in data["institutes"]:
        for row in institute["functions"]:
            if row["code"] == "advice":
                row["value"] = "not_found"
    groups = [
        {"name": "Science", "codes": ["testing", "research"]},
        {"name": "Policy", "codes": ["advice", "foresight", "incidents"]},
    ]
    model = check_page.function_phone_model(data)
    assert model["groups"] == groups
    for row in model["rows"]:
        assert [function["code"] for function in row["functions"]] == ["testing", "research", "advice", "foresight", "incidents"]
        assert all(function["value"] == "not_found" for function in row["functions"] if function["group"] == "Policy")
    next(row for row in data["institutes"][0]["functions"] if row["code"] == "advice")["value"] = "not_coded"
    assert check_page.function_phone_model(data)["groups"] == groups


def test_functions_phone_structure_rejects_a_missing_not_found_pip(functions_phone):
    from bs4 import BeautifulSoup

    data, html = functions_phone
    soup = BeautifulSoup(html, "html.parser")
    # The third Policy pip represents incidents, even though every institute is not-found for it.
    for row in soup.select('.fn-phone li[data-institute]'):
        row.select(':scope > .fn-pips')[1].select(':scope > .fn-pip')[2].decompose()
    assert any("phone pip count/order differs" in error for error in check_page.functions_phone_structure(str(soup), data))
