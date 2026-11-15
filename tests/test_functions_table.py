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
    <p class="fn-blank"><strong>A blank cell means we found no public text for it, not that the institute does not do it.</strong></p>
    <p class="fn-summary">Most common: testing (2 of 2 institutes), advice (1) and research (1).</p>
    <span id="hint" class="vh">Scrolls sideways</span>
    <div class="fn-scroll" role="region" tabindex="0" aria-labelledby="functions" aria-describedby="hint">
    <table class="fn-table"><caption class="vh">Functions by institute</caption><colgroup></colgroup><colgroup span="2"></colgroup><colgroup></colgroup><colgroup></colgroup><thead><tr>
    <th scope="col" rowspan="2">Institute</th><th scope="colgroup" colspan="2">Science</th><th scope="colgroup" colspan="1">Policy</th><td class="fn-pad" aria-hidden="true"></td>
    </tr><tr>
    <th scope="col" data-code="testing"><abbr class="g" tabindex="0" aria-description="Testing: Tests models.">Testing</abbr></th>
    <th scope="col" data-code="research"><abbr class="g" tabindex="0" aria-description="Research: Does research.">Research</abbr></th>
    <th scope="col" data-code="advice" class="fn-group-start"><abbr class="g" tabindex="0" aria-description="Advice: Advises government.">Advice</abbr></th><td class="fn-pad" aria-hidden="true"></td>
    </tr></thead>
    <tbody><tr class="fn-group"><th scope="rowgroup">Established</th><td class="fn-column fn-group-first" data-code="testing" aria-hidden="true"></td><td class="fn-column" data-code="research" aria-hidden="true"></td><td class="fn-column fn-group-start" data-code="advice" aria-hidden="true"></td><td class="fn-pad" aria-hidden="true"></td></tr>
    <tr data-institute="z"><th scope="row"><abbr class="g" tabindex="0" aria-description="Zulu Institute">Zulu</abbr></th>
    <td data-code="testing" data-value="activity"><button type="button" class="fn-cell" aria-label="Zulu, Testing: Active" aria-expanded="false" aria-controls="detail-z"><span class="fn-mark fn-activity" aria-hidden="true"></span></button></td>
    <td data-code="research" data-value="not_found"><span class="vh">not found</span></td>
    <td data-code="advice" data-value="mandate" class="fn-group-start"><button type="button" class="fn-cell" aria-label="Zulu, Advice: Assigned" aria-expanded="false" aria-controls="detail-z"><span class="fn-mark fn-mandate" aria-hidden="true"></span></button></td><td class="fn-pad" aria-hidden="true"></td></tr>
    </tbody><tbody><tr class="fn-group"><th scope="rowgroup">Not yet established</th><td class="fn-column fn-group-first" data-code="testing" aria-hidden="true"></td><td class="fn-column" data-code="research" aria-hidden="true"></td><td class="fn-column fn-group-start" data-code="advice" aria-hidden="true"></td><td class="fn-pad" aria-hidden="true"></td></tr>
    <tr data-institute="a"><th scope="row"><abbr class="g" tabindex="0" aria-description="Alpha Institute">Alpha</abbr></th>
    <td data-code="testing" data-value="both"><button type="button" class="fn-cell" aria-label="Alpha, Testing: Assigned and active" aria-expanded="false" aria-controls="detail-a"><span class="fn-mark fn-activity" aria-hidden="true"></span></button></td>
    <td data-code="research" data-value="activity"><button type="button" class="fn-cell" aria-label="Alpha, Research: Active" aria-expanded="false" aria-controls="detail-a"><span class="fn-mark fn-activity" aria-hidden="true"></span></button></td>
    <td data-code="advice" data-value="not_coded" class="fn-group-start"><span class="vh">not yet coded</span></td><td class="fn-pad" aria-hidden="true"></td></tr>
    </tbody><tfoot><tr><th scope="row">Institutes with it</th><td data-code="testing">2</td><td data-code="research">1</td><td data-code="advice">1</td><td class="fn-pad" aria-hidden="true"></td></tr></tfoot></table></div>
    <p class="fn-empty">No public text found at any institute yet for: foresight and incidents.</p>
    <ul class="fn-legend" role="list"><li data-value="activity">Active</li><li data-value="mandate">Assigned</li><li data-value="not_found">Blank: not found</li><li data-value="not_coded">not yet coded</li></ul>
    <p class="fn-order">Rows: by the year the institute's current status began, oldest first.</p>
    <p class="fn-provisional">The list of functions is provisional.</p>
    </div></section></div>'''
    return data, html


def test_functions_structure_accepts_row_blocks_and_folded_codes(functions):
    data, html = functions
    assert check_page.functions_structure(html, data) == []
    assert [i["id"] for i in check_page.function_rows(data)] == ["z", "a"]


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
    ('<td data-code="advice">1</td><td class="fn-pad" aria-hidden="true"></td>', '<td data-code="advice">1</td><td class="fn-pad"></td>', "spacer column"),
    ('<colgroup span="2"></colgroup>', '<colgroup span="3"></colgroup>', "column groups"),
    ('aria-label="Zulu, Testing: Active"', 'aria-label="ZI, Testing: Active"', "coded control/name"),
    ('aria-label="Alpha, Testing: Assigned and active"', 'aria-label="Alpha, Testing: Active"', "coded control/name"),
    ('class="fn-cell"', 'class="not-a-button"', "coded control/name"),
    ('aria-description="Testing: Tests models."', 'aria-description="Wrong test."', "function label/test"),
    ('aria-description="Zulu Institute"', 'aria-description="Wrong institute"', "row name/institute tip"),
    ('aria-description="Zulu Institute"', 'aria-description="Zulu Institute (planned)"', "status word in row tip"),
    ('<th scope="rowgroup">', '<th scope="rowgroup" colspan="5">', "group scope/span"),
    ('<td class="fn-column" data-code="research" aria-hidden="true"></td>', '', "block band columns/order"),
    ('class="fn-column fn-group-start" data-code="advice"', 'class="fn-column" data-code="advice"', "block band group gap"),
    ('>Zulu</abbr>', '>Zulu (2 of 5)</abbr>', "row name/institute tip"),
    ('Not yet established</th>', 'Pending</th>', "not-yet-established"),
    ('Not yet established</th>', 'proposed</th>', "status word"),
    ('<li data-value="not_coded">not yet coded</li>', '', "legend differs"),
    ('<tr data-institute="z">', '<tr data-institute="a">', "rows/order"),
    ('<td data-code="testing">2</td>', '<td data-code="testing">3</td>', "prevalence counts"),
    ('<td data-code="testing">2</td>', '<td data-code="foresight">2</td>', "prevalence counts"),
    ('for: foresight and incidents.', 'for: incidents.', "folded empty codes"),
    ('class="fn-group-start"', 'class="missing-gap"', "group gap"),
    ('fn-mark fn-activity', 'fn-mark fn-both', "mark differs"),
    ('Most common: testing (2 of 2 institutes),', 'Most common: Zulu Institute (2 of 2 institutes),', "summary facts"),
    ('<div id="functions-table">', '<div id="functions-table"><div class="fn-phone"></div>', "phone markup"),
    ('<div id="functions-table">', '<div id="functions-table"><p>It does this (active).</p>', "status word alone in parentheses"),
    ('<span class="vh">not found</span>', '<span>not found</span>', "absent value"),
    ('<p class="fn-blank"><strong>', '<p class="missing-blank"><strong>', "blank explanation"),
    ('The list of functions is provisional.', '', "provisional line"),
    ('<tbody>', '<tbody><tr class="fn-detail-row"><td>We tested model X.</td></tr>', "quote was pre-rendered"),
    ('aria-describedby="hint"', '', "scroll region"),
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


def test_function_jobs_are_registered_for_shards(functions):
    jobs = check_page.browser_jobs(Path("dist"), functions[0], None)
    names = [check_page.job_id(*job) for job in jobs]
    for name in ("check_functions_table", "check_functions_detail", "check_functions_layout", "check_functions_phone", "check_functions_sticky", "check_functions_tint"):
        assert names.count("interactions/" + name) == 1


def test_functions_structure_keeps_a_group_coded_nowhere(functions):
    """A function no institute has been coded against stays a column (it is not folded as not found), its group
    stays a group, and it is not named in the most common summary."""
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
    """Hand-written phone projection: all five values, one folded pair and two status blocks."""
    data, _ = functions
    html = '''<section aria-labelledby="functions"><h2 id="functions">Functions</h2><div id="functions-table">
    <p class="fn-blank"><strong>A blank cell means we found no public text for it, not that the institute does not do it.</strong></p>
    <div class="fn-phone"><div class="fn-phone-head"><span></span><span data-group="Science">Science</span><span data-group="Policy">Policy</span></div>
    <div class="fn-phone-block"><h3>Established</h3><ul role="list"><li data-institute="z">
    <button type="button" class="fn-phone-row" aria-label="Zulu, Zulu Institute: show functions" aria-expanded="false" aria-controls="fn-detail-z"><span class="fn-phone-country">Zulu</span><span class="fn-pips" data-group="Science" aria-hidden="true"><span class="fn-mark fn-activity fn-pip" data-code="testing" data-value="activity" aria-hidden="true"></span><span class="fn-mark fn-not_found fn-pip" data-code="research" data-value="not_found" aria-hidden="true"></span></span><span class="fn-pips" data-group="Policy" aria-hidden="true"><span class="fn-mark fn-mandate fn-pip" data-code="advice" data-value="mandate" aria-hidden="true"></span></span></button>
    </li></ul></div>
    <div class="fn-phone-block"><h3>Not yet established</h3><ul role="list"><li data-institute="a">
    <button type="button" class="fn-phone-row" aria-label="Alpha, Alpha Institute: show functions" aria-expanded="false" aria-controls="fn-detail-a"><span class="fn-phone-country">Alpha</span><span class="fn-pips" data-group="Science" aria-hidden="true"><span class="fn-mark fn-activity fn-pip" data-code="testing" data-value="both" aria-hidden="true"></span><span class="fn-mark fn-activity fn-pip" data-code="research" data-value="activity" aria-hidden="true"></span></span><span class="fn-pips" data-group="Policy" aria-hidden="true"><span class="fn-mark fn-not_coded fn-pip" data-code="advice" data-value="not_coded" aria-hidden="true"></span></span></button>
    </li></ul></div></div>
    <ul class="fn-legend" role="list"><li data-value="activity"><span class="fn-mark fn-activity fn-pip" aria-hidden="true"></span>Active</li><li data-value="mandate"><span class="fn-mark fn-mandate fn-pip" aria-hidden="true"></span>Assigned</li><li data-value="not_found"><span class="fn-mark fn-not_found fn-pip" aria-hidden="true"></span>No public text found</li><li data-value="not_coded"><span class="fn-mark fn-not_coded fn-pip" aria-hidden="true"></span>not yet coded</li></ul>
    <p class="fn-pip-note">One pip per function, in the order of the groups above; tap a row for the names.</p>
    </div></section>'''
    return data, html


def test_functions_phone_structure_accepts_groups_values_and_row_order(functions_phone):
    data, html = functions_phone
    assert check_page.functions_phone_structure(html, data) == []


@pytest.mark.parametrize(("old", "new", "finding"), [
    ('<div class="fn-phone">', '<table class="fn-table"></table><div class="fn-phone">', "replace the desktop table"),
    ('data-group="Science">Science</span>', 'data-group="Policy">Policy</span>', "group heads/order"),
    ('<h3>Not yet established</h3>', '<h3>proposed</h3>', "blocks/order"),
    ('data-institute="z"', 'data-institute="a"', "rows/order"),
    ('Zulu, Zulu Institute: show functions', 'Zulu: 2 functions with a public text', "row control/name"),
    ('Zulu, Zulu Institute: show functions', 'Zulu, Zulu Institute: proposed', "status words"),
    ('aria-expanded="false" aria-controls="fn-detail-z"', 'aria-expanded="true" aria-controls="fn-detail-z"', "row control/name"),
    ('aria-controls="fn-detail-a"', 'aria-controls="fn-detail-z"', "controls are not unique"),
    ('<span class="fn-phone-country">', '<span class="fn-phone-country" tabindex="0">', "nested focus targets"),
    ('<span class="fn-phone-country">', '<span class="fn-phone-country" aria-description="Zulu Institute">', "country has a tooltip"),
    ('>Zulu</span>', '>Wrong country</span>', "country label"),
    ('>Zulu</span>', '>Zulu (2)</span>', "counts or status words"),
    ('data-code="research" data-value="not_found"', 'data-code="research" data-value="activity"', "pip values/order"),
    ('data-code="testing" data-value="both"', 'data-code="foresight" data-value="both"', "pip values/order"),
    ('class="fn-mark fn-not_found fn-pip" data-code="research"', 'class="fn-mark fn-activity fn-pip" data-code="research"', "pip mark differs"),
    ('data-group="Science" aria-hidden="true"', 'data-group="Science" aria-hidden="false"', "decorative phone pip group"),
    ('No public text found</li>', 'Blank: not found</li>', "legend label/mark"),
    ('<p class="fn-pip-note">', '<p>', "pip note"),
    ('<div class="fn-phone">', '<div class="fn-phone-detail"></div><div class="fn-phone">', "detail is present"),
    ('<p class="fn-blank"><strong>', '<p><strong>', "blank caveat"),
])
def test_functions_phone_structure_rejects_mutations(functions_phone, old, new, finding):
    data, html = functions_phone
    assert old in html
    assert any(finding in error for error in check_page.functions_phone_structure(html.replace(old, new), data))


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


def test_functions_phone_model_folds_only_unanimous_not_found(functions_phone):
    data, _ = functions_phone
    data = copy.deepcopy(data)
    for institute in data["institutes"]:
        for row in institute["functions"]:
            if row["code"] == "advice":
                row["value"] = "not_found"
    assert check_page.function_phone_model(data)["groups"] == [{"name": "Science", "codes": ["testing", "research"]}]
    next(row for row in data["institutes"][0]["functions"] if row["code"] == "advice")["value"] = "not_coded"
    assert check_page.function_phone_model(data)["groups"] == [
        {"name": "Science", "codes": ["testing", "research"]}, {"name": "Policy", "codes": ["advice"]},
    ]
