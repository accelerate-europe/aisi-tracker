"""Functions table gate mutations, without starting a browser."""

import copy
from pathlib import Path

import pytest

from scripts import check_page


@pytest.fixture
def functions():
    data = {
        "dataset": {"vocabularies": {"function": {
            "testing": {"label": "Testing", "definition": "Tests models.", "group": "Science"},
            "old": {"label": "Retired", "definition": "Old test.", "group": "Retired", "deprecated": True},
            "advice": {"label": "Advice", "definition": "Advises government.", "group": "Policy"},
        }}},
        "jurisdictions": [{"id": "AA", "name": "Alpha", "status": "proposed"},
                          {"id": "ZZ", "name": "Zulu", "status": "established"}],
        "institutes": [
            {"id": "a", "jurisdiction_id": "AA", "name": "Alpha Institute", "acronym": "AI",
             "functions": [{"code": "testing", "value": "not_found", "claim_ids": []},
                           {"code": "advice", "value": "not_coded", "claim_ids": []}], "claims": []},
            {"id": "z", "jurisdiction_id": "ZZ", "name": "Zulu Institute", "short_name": "ZI",
             "functions": [{"code": "testing", "value": "activity", "claim_ids": ["c"]},
                           {"code": "advice", "value": "not_found", "claim_ids": []}],
             "claims": [{"id": "fact", "kind": "fact", "text": "A separate fact."},
                        {"id": "c", "kind": "function", "basis": "activity", "text": "It tested a model.", "source_ids": ["s"]}]},
        ],
        "sources": [{"id": "s", "url": "https://example.org/source", "excerpts": [
            {"text": "We tested model X.", "supports": ["/institutes/1/claims/1/text"]}]}],
    }
    html = '''<nav class="secnav"><a href="#functions" data-sections="functions testing set-up funding">Compare</a></nav>
    <div class="group"><nav class="group-nav"><a href="#functions">Functions</a></nav>
    <section aria-labelledby="functions"><h2 id="functions">Functions</h2><div id="functions-table">
    <span id="hint" class="vh">Scrolls sideways</span>
    <div class="fn-scroll" role="region" tabindex="0" aria-labelledby="functions" aria-describedby="hint">
    <table class="fn-table"><caption class="vh">Functions by institute</caption><thead><tr>
    <th scope="col">Function</th><th scope="col" data-institute="z"><abbr class="g" tabindex="0" aria-description="Zulu Institute">Zulu</abbr></th>
    <th scope="col" data-institute="a" class="fn-pending"><abbr class="g" tabindex="0" aria-description="Alpha Institute">Alpha</abbr><span class="fn-nyo">not yet established</span></th>
    </tr></thead>
    <tbody><tr class="fn-group"><th scope="rowgroup" colspan="3">Science</th></tr>
    <tr data-code="testing"><th scope="row"><abbr class="g" aria-description="Tests models.">Testing</abbr></th>
    <td data-value="activity"><button type="button" class="fn-cell" aria-label="ZI, Testing: active" aria-expanded="false" aria-controls="detail-testing"><span class="fn-mark fn-activity" aria-hidden="true"></span></button></td>
    <td data-value="not_found" class="fn-pending"><span class="vh">not found</span></td></tr>
    </tbody><tbody><tr class="fn-group"><th scope="rowgroup" colspan="3">Policy</th></tr>
    <tr data-code="advice"><th scope="row"><abbr class="g" aria-description="Advises government.">Advice</abbr></th>
    <td data-value="not_found"><span class="vh">not found</span></td>
    <td data-value="not_coded" class="fn-pending"><span class="fn-mark fn-not_coded" aria-hidden="true"></span><span class="vh">not yet coded</span></td></tr>
    </tbody></table></div>
    <ul class="fn-legend" role="list"><li data-value="activity">Active</li><li data-value="not_found">Not found</li><li data-value="not_coded">Not yet coded</li><li><span class="fn-nyo-sample"></span><span>Not yet established: its government has not yet set it up.</span></li></ul>
    <p class="fn-provisional">The list of functions is provisional.</p>
    </div></section></div>'''
    return data, html


def test_functions_structure_accepts_operating_order_and_retired_code(functions):
    data, html = functions
    assert check_page.functions_structure(html, data) == []
    assert [i["id"] for i in check_page.function_columns(data)] == ["z", "a"]


@pytest.mark.parametrize(("old", "new", "finding"), [
    ('data-value="activity"', 'data-value="mandate"', "value counts"),
    ('aria-label="ZI, Testing: active"', 'aria-label="ZI, Testing: assigned"', "coded control/name"),
    ('class="fn-cell"', 'class="not-a-button"', "coded control/name"),
    ('aria-description="Tests models."', 'aria-description="Wrong test."', "function label/test"),
    ('class="fn-nyo"', 'class="missing-note"', "not-yet-established"),
    ('not yet established</span>', 'proposed</span>', "status word"),
    ('<li data-value="not_coded">Not yet coded</li>', '', "legend differs"),
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
