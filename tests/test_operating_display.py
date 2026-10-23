"""Independent expected labels and deliberate failures of the rendered-page gate."""

import json
import subprocess
from pathlib import Path

import pytest
from bs4 import BeautifulSoup

from scripts import check_page


from scripts.check_page import (
    check_operating_output,
    expected_operating,
    expected_operating_lines,
    sort_errors_for,
)


@pytest.fixture(autouse=True)
def isolated_credit_expectations(monkeypatch):
    monkeypatch.setattr(check_page, "ACTIVITY_CREDIT_SOURCES", dict(check_page.ACTIVITY_CREDIT_SOURCES))


def page_operating_text(operating):
    source = (Path(__file__).resolve().parents[1] / "site/index.html").read_text()
    dates = source[
        source.index("  var MON =") : source.index("  function saveText(")
    ]
    helpers = source[
        source.index("  function operatingClause") : source.index(
            "  function operatingMark"
        )
    ]
    code = (
        'var MISSING = "Not published";\n'
        + dates
        + helpers
        + "\nconsole.log(JSON.stringify(operatingText("
        + json.dumps(None if operating is None else {"items": operating})
        + ")));"
    )
    return json.loads(subprocess.check_output(["node", "-e", code], text=True))


NA = '<span class="missing na"><span aria-hidden="true">\u2013</span><span class="vh">Not applicable</span></span>'


def output(
    kind="published_work", day="2025", lineage="test-work", source="test-source"
):
    return {
        "qualifies_as": kind,
        "date": day,
        "credit": {"institute_id": "test", "date": day},
        "lineage": lineage,
        "source_ids": [source],
    }


def display(status="established", outputs=None, lines=()):
    """Hand-written rendering (layout A): the kind and since lines are typed by the test, never computed."""
    outputs = outputs or []
    op = [
        {
            "kind": o["qualifies_as"],
            **{k: o[k] for k in ("date", "lineage", "source_ids")},
        }
        for o in outputs
    ]
    j = {"id": "GB", "status": status, "institute_id": "test"}
    inst = {"id": "test", "outputs": outputs, "operating": op}
    # Each synthetic output declares its expected evidence separately from the producer.
    for item in outputs:
        check_page.ACTIVITY_CREDIT_SOURCES[("test", item["lineage"])] = list(item["source_ids"])
    established = status == "established"
    missing = "Not published" if established else NA

    def cites(o):
        links = "".join(f'<a class="cn" data-source-id="{sid}">1</a>' for sid in o["source_ids"])
        return f'<span class="cites">{links}</span>'

    caveat = "Absence is not evidence of inactivity. 'Since' gives the date of the earliest known item of that kind."
    if outputs:
        # Text only, no glyph; each kind line cites its own item; its since line sits beneath, uncited.
        kinds = [
            f'<span class="qualified activity-kind"><span class="value-line activity-clause" data-kind="{o["qualifies_as"]}">{kind}{cites(o)}</span> '
            f'<span class="small qualifiers activity-since">{since}</span></span>'
            for kind, since, o in zip(lines[0::2], lines[1::2], outputs)
        ]
        mark = (
            f'<span class="smark value activity" data-state="confirmed" data-tip="{caveat}">'
            f'<span class="activity-kinds">{"".join(kinds)}</span></span>'
        )
    else:
        mark = f'<span class="smark value" data-state="not_confirmed" data-tip="{caveat}"><span>None found yet</span></span>'
    if not established:
        mark = NA
    values = [
        ("Known activity", "op", mark),
        ("Budget", "budget", missing),
        ("Staff", "staff", missing),
    ]
    card = "".join(
        f'<div class="row"><div class="k">{name}</div><div class="v">{text}</div></div>'
        for name, _, text in values
        if established
    )
    cells = "".join(f'<td data-col="{key}">{text}</td>' for _, key, text in values)
    rule = "A published evaluation or joint testing exercise; absence is not evidence of inactivity."
    html = f'<article class="card" data-j="GB">{card}</article><table class="world"><thead><tr><th data-k="op"><button data-tip="{check_page.OP_HEADER_TIP}">Known activity</button></th></tr></thead><tbody id="world-rows"><tr data-j="GB">{cells}</tr></tbody></table><ul id="rules"><li>{rule}</li></ul>'
    sources = [{"id": sid} for sid in dict.fromkeys(sid for item in outputs for sid in item["source_ids"])]
    return html, {"jurisdictions": [j], "institutes": [inst], "sources": sources}


@pytest.mark.parametrize(
    ("kind", "day", "since", "label", "clauses"),
    [
        ("published_work", "2025-03-07", "since 7 Mar 2025", "Published work", "Published work since 7 Mar 2025"),
        ("joint_testing", "2025-03", "since Mar 2025", "Joint testing", "Joint testing since Mar 2025"),
        ("published_work", "2025", "since 2025", "Published work", "Published work since 2025"),
    ],
)
def test_findings_show_kind_and_since_recorded_date(kind, day, since, label, clauses):
    html, data = display(outputs=[output(kind, day)], lines=(label, since))
    assert page_operating_text(data["institutes"][0]["operating"]) == clauses
    assert expected_operating_lines(data["jurisdictions"][0], data["institutes"][0]) == [label, since]
    assert check_operating_output(html, data) == []
    for broken, error in (
        (html.replace(since, "Confirmed"), "Known activity differs"),
        (html.replace(f">{label}<", "><"), "Known activity differs"),
        (html.replace("earliest known item of that kind", "first item"), "lacks the definition of since"),
        (html.replace('data-state="confirmed" data-tip', 'data-state="confirmed" data-x="" data-tip').replace('<span class="activity-kinds">', '<i class="sw st-established"></i><span class="activity-kinds">'), "carries a glyph"),
    ):
        assert any(error in e for e in check_operating_output(broken, data)), error


def test_no_qualifying_output():
    html, data = display()
    assert check_operating_output(html, data) == []
    assert page_operating_text([]) == "None found yet"
    assert (
        page_operating_text(None) == "Not published"
    )  # Missing generated data is a defensive fallback.
    assert any(
        "Known activity differs" in e
        for e in check_operating_output(
            html.replace("None found yet", "Not confirmed"), data
        )
    )


@pytest.mark.parametrize(
    "status", ["planned", "proposed", "exploring", "none_identified"]
)
def test_all_non_established_states_are_inapplicable_even_with_outputs(status):
    html, data = display(status, [output()])
    rows = [
        {
            "id": "GB",
            "operational": "Not applicable",
            "budget": "Not applicable",
            "staff": "Not applicable",
        }
    ]
    assert check_operating_output(html, data, rows) == []
    errors = check_operating_output(html.replace(NA, "Not published"), data)
    assert any("Known activity differs from structural absence" in e for e in errors)
    assert any("must be a structural absence" in e for e in errors)
    rows[0]["budget"] = "Not published"
    assert any(
        "CSV applicability" in e for e in check_operating_output(html, data, rows)
    )


@pytest.mark.parametrize(
    ("label", "value"),
    [("Known activity", NA), ("Budget", "Not applicable"), ("Staff", NA)],
)
def test_cards_omit_structurally_absent_rows(label, value):
    html, data = display("planned")
    row = f'<div class="row"><div class="k">{label}</div><div class="v">{value}</div></div>'
    html = html.replace('data-j="GB">', 'data-j="GB">' + row, 1)
    errors = check_operating_output(html, data)
    assert any(f"card shows {label} before establishment" in e for e in errors)
    assert any("card shows Not applicable text" in e for e in errors)


def test_structural_dash_needs_screen_reader_text():
    html, data = display("planned")
    broken = html.replace('<span class="vh">Not applicable</span>', "")
    assert any(
        "must be a structural absence" in e
        for e in check_operating_output(broken, data)
    )


def test_two_kinds_have_separate_citations():
    clauses = "Published work since 2024; joint testing since Mar 2025"
    lines = ("Published work", "since 2024", "Joint testing", "since Mar 2025")
    html, data = display(
        outputs=[
            output(day="2024", source="report"),
            output("joint_testing", "2025-03", "exercise", "joint"),
        ],
        lines=lines,
    )
    assert check_operating_output(html, data) == []
    assert page_operating_text(data["institutes"][0]["operating"]) == clauses
    swapped = html.replace('data-source-id="report"', 'data-source-id="joint"')
    assert any("clause citations differ" in e for e in check_operating_output(swapped, data))
    # Wrong generated values must not influence the independent oracle.
    data["institutes"][0]["operating"] = []
    assert expected_operating(data["jurisdictions"][0], data["institutes"][0]) == clauses
    assert expected_operating_lines(data["jurisdictions"][0], data["institutes"][0]) == list(lines)


@pytest.mark.parametrize(
    ("old", "new", "error"),
    [
        ('data-state="not_confirmed"', 'data-state="confirmed"', "text mark missing"),
        (
            'data-tip="Absence is not evidence of inactivity.',
            'data-tip="Old wording',
            "tooltip lacks finding wording",
        ),
    ],
)
def test_swatch_and_tooltip_regressions_fail(old, new, error):
    html, data = display()
    assert any(error in e for e in check_operating_output(html.replace(old, new), data))


def test_mini_cards_have_no_field_rows():
    html, data = display(status="none_identified")
    html = html.replace('<article class="card"', '<article class="card mini"')
    soup = BeautifulSoup(html, "html.parser")
    soup.select_one("article").clear()
    assert check_operating_output(str(soup), data) == []
    soup.select_one('[data-col="op"]').string = "Not published"
    assert any(
        "table Known activity differs" in e
        for e in check_operating_output(str(soup), data)
    )


def test_real_activity_method_is_rendered_from_dataset():
    root = Path(__file__).resolve().parents[1]
    data = json.loads((root / "data/aisi.json").read_text())
    source = (root / "site/index.html").read_text()
    assert 'm.method_summaries.forEach(function (s)' in source
    assert sum(t["id"] == "known-activity" for t in data["dataset"]["method"]) == 1



def test_real_table_sort_keeps_gaps_last_in_both_directions():
    """The page's own sort helpers and comparator: values by meaning, then gaps, then structural absence."""
    source = (Path(__file__).resolve().parents[1] / "site/index.html").read_text()
    helpers = source[
        source.index("    function sortKey(") : source.index("    var regionOf =")
    ]
    start = source.index("      shown.sort(function ")
    comparator = source[start : source.index("\n", start)]
    rows = (
        "[{name:'Zulu', s:{op: sortKey('2025-03', false, false)}}, {name:'Beta', s:{op: sortKey(null, false, false)}},"
        " {name:'Alpha', s:{op: sortKey('2025-03', false, false)}}, {name:'Dash', s:{op: sortKey(null, false, true)}},"
        " {name:'Early', s:{op: sortKey('2024-11-19', false, false)}}, {name:'Year', s:{op: sortKey('2025', false, false)}},"
        " {name:'Aardvark', s:{op: sortKey(null, false, false)}}]"
    )
    for direction, names in (
        (1, ["Early", "Year", "Alpha", "Zulu", "Aardvark", "Beta", "Dash"]),
        (-1, ["Alpha", "Zulu", "Year", "Early", "Aardvark", "Beta", "Dash"]),
    ):
        code = (
            helpers
            + 'var sortK="op", sortDir='
            + str(direction)
            + ", shown="
            + rows
            + ";"
            + comparator
            + ";console.log(JSON.stringify(shown.map(r=>r.name)))"
        )
        assert (
            json.loads(subprocess.check_output(["node", "-e", code], text=True))
            == names
        )


def test_sort_gate_rejects_gaps_first_and_wrong_order():
    def rows(*items):
        return [dict(name=n, n=i, **item) for i, (n, item) in enumerate(items)]

    good = rows(("A", {"tier": 0, "v": "2024"}), ("B", {"tier": 0, "v": "2025"}), ("C", {"tier": 2}), ("D", {"tier": 3}))
    assert sort_errors_for(good, "op", 1) == []
    assert any("out of order" in e for e in sort_errors_for(good, "op", -1))
    gaps_first = rows(("C", {"tier": 2}), ("A", {"tier": 0, "v": "2025"}), ("D", {"tier": 3}))
    assert any("gaps must stay last" in e for e in sort_errors_for(gaps_first, "op", -1))
    ties = rows(("B", {"tier": 2}), ("A", {"tier": 2}))
    ties[0]["n"], ties[1]["n"] = 1, 0
    assert any("not alphabetical" in e for e in sort_errors_for(ties, "op", 1))


@pytest.mark.parametrize("shared_source", [False, True])
def test_actual_page_renderer_attaches_each_clause_citation(shared_source):
    """Execute the real renderer with a minimal DOM surface, not a copied renderer."""
    source = (Path(__file__).resolve().parents[1] / "site/index.html").read_text()
    dates = source[
        source.index("  var MON =") : source.index("  function saveText(")
    ]
    renderers = source[
        source.index("  function statusMark") : source.index("  function statusGroup")
    ]
    tooltip = next(
        line for line in source.splitlines() if line.startswith("  var OPHELP =")
    )
    code = (
        r"""
    function el(tag, cls, text) {
      const node = {tag, className: cls || "", dataset: {}, children: [], value: text || "", attrs: {},
        classList: {add(c) { this.owner.className += " " + c; }},
        appendChild(child) { this.children.push(child); return child; },
        setAttribute(key, value) { this.attrs[key] = value; },
        get lastElementChild() { return this.children[this.children.length - 1]; },
        get textContent() { return this.value + this.children.map(c => c.textContent).join(""); },
        set textContent(value) { this.value = value; this.children = []; }
      };
      node.classList.owner = node;
      return node;
    }
    var document = {createTextNode: text => el("#text", null, text), createDocumentFragment: () => el("#fragment")};
    var LABEL = {}, MISSING = "Not published", calls = [];
    function cite(ids, role) { calls.push({ids, role}); var node = el("span", "cites", ""); node.sourceIds = ids; return node; }
    """
        + dates
        + source[source.index("  function fieldCitations("):source.index("  function selectedRecord(")]
        + renderers
        + tooltip
    )
    report_source = "joint" if shared_source else "report"
    op = [
        {
            "kind": "joint_testing",
            "date": "2024",
            "lineage": "exercise",
            "source_ids": ["joint"],
        },
        {
            "kind": "published_work",
            "date": "2025-03",
            "lineage": "report",
            "source_ids": [report_source],
        },
    ]
    for evidence in op:
        evidence["citations"] = {"date": evidence["source_ids"]}
    code += "var mark = operatingMark(" + json.dumps({"items": op, "since": "2024", "first_lineage": "exercise"}) + ", cite), kinds = mark.children[0];"
    code += """console.log(JSON.stringify({calls, markClass: mark.className, kindsClass: kinds.className,
      kinds: kinds.children.map(item => { const clause = item.children[0], since = item.children[2];
        return {kind: clause.dataset.kind, clauseClass: clause.className, text: clause.value, sources: clause.children.filter(c => c.className === "cites").map(c => c.sourceIds),
                since: since.textContent, sinceClass: since.className}; }),
      swatches: mark.children.filter(c => c.tag === "i").map(c => c.className), tooltip: mark.title}));"""
    actual = json.loads(subprocess.check_output(["node", "-e", code], text=True))
    assert "activity" in actual["markClass"].split()
    # Each kind line cites its own item, once; the since lines are uncited qualifier lines.
    assert actual["calls"] == [
        {"ids": ["joint"], "role": "Known activity"},
        {"ids": [report_source], "role": "Known activity"},
    ]
    assert actual["kinds"] == [
        {"kind": "joint_testing", "clauseClass": "value-line activity-clause", "text": "Joint testing", "sources": [["joint"]], "since": "since 2024", "sinceClass": "small qualifiers activity-since"},
        {"kind": "published_work", "clauseClass": "value-line activity-clause", "text": "Published work", "sources": [[report_source]], "since": "since Mar 2025", "sinceClass": "small qualifiers activity-since"},
    ]
    assert actual["swatches"] == []  # Known activity is text: squares are for status only
    assert "'Since' gives the date of the earliest known item of that kind." in actual["tooltip"]


def test_oracle_rejects_dropped_date_swapped_kinds_and_missing_citation():
    html, data = display(
        outputs=[
            output("joint_testing", "2024", "exercise", "joint"),
            output(day="2025-03", lineage="report", source="report"),
        ],
        lines=("Joint testing", "since 2024", "Published work", "since Mar 2025"),
    )
    assert check_operating_output(html, data) == []
    mutations = [
        html.replace('data-source-id="joint"', 'data-source-id="temporary"')
        .replace('data-source-id="report"', 'data-source-id="joint"')
        .replace('data-source-id="temporary"', 'data-source-id="report"'),
        html.replace(">since 2024<", ">since 2023<"),
        html.replace(">since 2024<", ">since Mar 2025<"),
        html.replace(">Joint testing<", ">Published work<"),
        html.replace('<a class="cn" data-source-id="joint">1</a>', ""),
        html.replace('<a class="cn" data-source-id="report">1</a>', ""),
        html.replace('data-kind="joint_testing"', 'data-kind="published_work"'),
        html.replace('>since 2024</span>', '>since 2024<span class="cites"><a class="cn" data-source-id="joint">1</a></span></span>'),
    ]
    for n, broken in enumerate(mutations):
        assert check_operating_output(broken, data), n


def test_oracle_rejects_extra_text_anywhere_in_the_value():
    html, data = display(outputs=[output()], lines=("Published work", "since 2025"))
    assert check_operating_output(html, data) == []
    extra = "<span>Confirmed operational since 2020</span>"
    for broken in (
        html.replace("</div></div><div class=\"row\"><div class=\"k\">Budget", extra + "</div></div><div class=\"row\"><div class=\"k\">Budget"),
        html.replace("</span></span></span></span></td>", "</span></span></span></span>" + extra + "</td>", 1),
        html.replace("since 2025</span></span></span>", "since 2025</span></span></span>" + extra, 1),
    ):
        assert broken != html
        assert any("Known activity differs" in e for e in check_operating_output(broken, data))


def test_activity_oracle_uses_catalogue_order_without_relaxing_citation_checks():
    item = output()
    item["source_ids"] = ["second", "first"]
    html, data = display(outputs=[item], lines=("Published work", "since 2025"))
    # Independent membership has its old review order; current catalogue order wins.
    check_page.ACTIVITY_CREDIT_SOURCES[("test", "test-work")] = ["first", "second"]
    data["sources"] = [{"id": sid} for sid in ("second", "first", "unrelated")]
    assert check_operating_output(html, data) == []
    for ids in (["first", "second"], ["second", "first", "first"], ["second", "unrelated"]):
        soup = BeautifulSoup(html, "html.parser")
        for group in soup.select(".activity-clause .cites"):
            group.clear()
            for sid in ids:
                link = soup.new_tag("a", attrs={"class": "cn", "data-source-id": sid})
                link.string = "1"
                group.append(link)
        assert any("clause citations differ" in error for error in check_operating_output(str(soup), data)), ids
