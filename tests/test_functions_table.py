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
    <p class="fn-summary">Most common: testing (2 of 2 institutes). Least common: research and advice (1 each).</p>
    <span id="hint" class="vh">Scrolls sideways</span>
    <div class="fn-scroll" role="region" tabindex="0" aria-labelledby="functions" aria-describedby="hint">
    <table class="fn-table"><caption class="vh">Functions by institute</caption><colgroup></colgroup><colgroup span="2"></colgroup><colgroup></colgroup><colgroup></colgroup><thead><tr>
    <th scope="col" rowspan="2">Institute</th><th scope="colgroup" colspan="2">Science</th><th scope="colgroup" colspan="1">Policy</th><td class="fn-pad" aria-hidden="true"></td>
    </tr><tr>
    <th scope="col" data-code="testing"><abbr class="g" tabindex="0" aria-description="Testing: Tests models.">Testing</abbr></th>
    <th scope="col" data-code="research"><abbr class="g" tabindex="0" aria-description="Research: Does research.">Research</abbr></th>
    <th scope="col" data-code="advice" class="fn-group-start"><abbr class="g" tabindex="0" aria-description="Advice: Advises government.">Advice</abbr></th><td class="fn-pad" aria-hidden="true"></td>
    </tr></thead>
    <tbody><tr class="fn-group"><th scope="rowgroup" colspan="5">Established</th></tr>
    <tr data-institute="z"><th scope="row"><abbr class="g" tabindex="0" aria-description="Zulu Institute">Zulu</abbr></th>
    <td data-code="testing" data-value="activity"><button type="button" class="fn-cell" aria-label="Zulu, Testing: Active" aria-expanded="false" aria-controls="detail-z"><span class="fn-mark fn-activity" aria-hidden="true"></span></button></td>
    <td data-code="research" data-value="not_found"><span class="vh">not found</span></td>
    <td data-code="advice" data-value="mandate" class="fn-group-start"><button type="button" class="fn-cell" aria-label="Zulu, Advice: Assigned" aria-expanded="false" aria-controls="detail-z"><span class="fn-mark fn-mandate" aria-hidden="true"></span></button></td><td class="fn-pad" aria-hidden="true"></td></tr>
    </tbody><tbody><tr class="fn-group"><th scope="rowgroup" colspan="5">Not yet established</th></tr>
    <tr data-institute="a"><th scope="row"><abbr class="g" tabindex="0" aria-description="Alpha Institute">Alpha</abbr></th>
    <td data-code="testing" data-value="both"><button type="button" class="fn-cell" aria-label="Alpha, Testing: Assigned and active" aria-expanded="false" aria-controls="detail-a"><span class="fn-mark fn-activity" aria-hidden="true"></span></button></td>
    <td data-code="research" data-value="activity"><button type="button" class="fn-cell" aria-label="Alpha, Research: Active" aria-expanded="false" aria-controls="detail-a"><span class="fn-mark fn-activity" aria-hidden="true"></span></button></td>
    <td data-code="advice" data-value="not_coded" class="fn-group-start"><span class="vh">not yet coded</span></td><td class="fn-pad" aria-hidden="true"></td></tr>
    </tbody><tfoot><tr><th scope="row">Institutes with it</th><td data-code="testing">2</td><td data-code="research">1</td><td data-code="advice">1</td><td class="fn-pad" aria-hidden="true"></td></tr></tfoot></table></div>
    <p class="fn-empty">No public text found at any institute yet for: foresight and incidents.</p>
    <ul class="fn-legend" role="list"><li data-value="activity">Active</li><li data-value="mandate">Assigned</li><li data-value="not_found">Blank: not found</li><li data-value="not_coded">not yet coded</li></ul>
    <p class="fn-blank">A blank cell means we found no public text for it, not that the institute does not do it.</p>
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
    ('Most common: testing (2 of 2 institutes).', 'Most common: Zulu Institute (2 of 2 institutes).', "summary facts"),
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


def test_function_jobs_are_registered_for_shards(functions):
    jobs = check_page.browser_jobs(Path("dist"), functions[0], None)
    names = [check_page.job_id(*job) for job in jobs]
    for name in ("check_functions_table", "check_functions_detail", "check_functions_layout"):
        assert names.count("interactions/" + name) == 1


def test_functions_structure_keeps_a_group_coded_nowhere(functions):
    """A function no institute has been coded against stays a column (it is not folded as not found), its group
    stays a group, and it is not named as least common."""
    data, html = functions
    data = copy.deepcopy(data)
    next(r for r in data["institutes"][1]["functions"] if r["code"] == "advice")["value"] = "not_coded"
    mandate = '<td data-code="advice" data-value="mandate" class="fn-group-start"><button type="button" class="fn-cell" aria-label="Zulu, Advice: Assigned" aria-expanded="false" aria-controls="detail-z"><span class="fn-mark fn-mandate" aria-hidden="true"></span></button></td>'
    assert mandate in html
    html = (html.replace(mandate, '<td data-code="advice" data-value="not_coded" class="fn-group-start"><span class="vh">not yet coded</span></td>')
            .replace('<td data-code="advice">1</td>', '<td data-code="advice">0</td>')
            .replace("Least common: research and advice (1 each).", "Least common: research (1)."))
    assert check_page.functions_structure(html, data) == []


def test_function_rows_put_an_undated_status_last_in_its_block(functions):
    """A record without status_since (an edge the card-grid gate exercises) sorts after the dated ones, never fails."""
    data, _ = functions
    data = copy.deepcopy(data)
    for j in data["jurisdictions"]:
        j.setdefault("status_since", "2024-01-01")
    data["jurisdictions"][0].pop("status_since")
    assert [i["id"] for i in check_page.function_rows(data)] == ["z", "a"]
