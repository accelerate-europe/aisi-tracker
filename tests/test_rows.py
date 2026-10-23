"""Head, reporting line and national partners: schema, gate checks and generated outputs, on synthetic data only.

Every gate check is held by a mutation: the fixture passes, one guarded behaviour is broken, and the
specific message appears. Nothing here reads the real dataset.
"""

import copy

import pytest
from bs4 import BeautifulSoup
from jsonschema import Draft202012Validator

from scripts import agent_files, build, check_page, row_checks
from scripts.published import publish
from scripts.references import authored_schema, pointer_ancestors
from scripts.row_checks import check_rows, partner_order

VOCABULARIES = {
    "partner_relation": {
        "member": {"label": "member", "definition": "Synthetic."},
        "works_with": {"label": "works with", "definition": "Synthetic."},
        "planned": {"label": "planned", "definition": "Synthetic."},
    },
    "partner_cyber_none_published": "cyber agency not published",
}
BASE = "/institutes/0"


def entry(key, relation="works_with", **extra):
    return {
        "id": f"np-test-{key}",
        "name": f"Synthetic body {key}",
        "acronym": key.upper(),
        "what": "synthetic public body",
        "relation": relation,
        "how": f"Works with the institute on synthetic evaluation task {key}.",
        "source_ids": ["gb-source"],
        **extra,
    }


@pytest.fixture
def rows(dataset):
    d = copy.deepcopy(dataset)
    d["dataset"]["vocabularies"].update(copy.deepcopy(VOCABULARIES))
    institute = d["institutes"][0]
    institute["head"] = {
        "state": "stated",
        "person_id": "test-lead",
        "source_ids": ["gb-source"],
    }
    institute["reports_to"] = {
        "state": "stated",
        "name": "Test ministry",
        "source_ids": ["gb-source"],
    }
    institute["national_partners"] = {
        "state": "stated",
        "entries": [
            entry("lab", "planned"),
            entry("sca", cyber_agency=True),
        ],
    }
    d["sources"][0]["excerpts"][0]["supports"] += [
        f"{BASE}/head/person_id",
        f"{BASE}/reports_to/name",
        f"{BASE}/national_partners/entries/1/cyber_agency",
        *(
            f"{BASE}/national_partners/entries/{k}/{f}"
            for k in (0, 1)
            for f in ("name", "relation", "how", "what")
        ),
    ]
    return d


def prune(d):
    """Drop excerpt pointers that no longer resolve after a test replaced the claims they supported."""
    for source in d["sources"]:
        for excerpt in source.get("excerpts", []):
            keep = []
            for pointer in excerpt["supports"]:
                try:
                    pointer_ancestors(d, pointer)
                    keep.append(pointer)
                except (ValueError, KeyError, IndexError):
                    pass
            excerpt["supports"] = keep
    return d


def problems(d):
    """Schema errors of the authored contract, then every consistency error."""
    schema = build.json.loads(build.SCHEMA.read_text())
    errors = [
        e.message
        for e in Draft202012Validator(
            authored_schema(schema), format_checker=Draft202012Validator.FORMAT_CHECKER
        ).iter_errors(d)
    ]
    return errors or build.check_consistency(d, require_roster=True, complete=True)


def test_fixture_passes(rows):
    assert problems(rows) == []


def drop_support(pointer):
    def mutate(d):
        d["sources"][0]["excerpts"][0]["supports"].remove(pointer)

    return mutate


def partners(d):
    return d["institutes"][0]["national_partners"]


def not_published(d, field="head"):
    d["institutes"][0][field] = {
        "state": "not_published",
        "notes": "Searched the leadership page on 20 October 2026, in English.",
    }
    return prune(d)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        # valid enums: three states, three relations, nothing else
        (lambda d: d["institutes"][0]["head"].update(state="maybe"), "is not one of"),
        (lambda d: d["institutes"][0]["head"].update(state="unclear"), "is not one of"),
        (
            lambda d: partners(d)["entries"][1].update(relation="friend"),
            "is not one of",
        ),
        (
            lambda d: partners(d)["entries"][1].update(relation="formal_partnership"),
            "is not one of",
        ),
        # dropped fields are rejected, not ignored
        (
            lambda d: partners(d)["entries"][1].update(domain="cyber_security"),
            "Additional properties are not allowed ('domain'",
        ),
        (
            lambda d: d["institutes"][0]["reports_to"].update(verb="piloté par"),
            "Additional properties are not allowed ('verb'",
        ),
        (
            lambda d: d["institutes"][0]["reports_to"].update(joint=False),
            "Additional properties are not allowed ('joint'",
        ),
        # the head resolves to a person of this institute, and is not a former appointment
        (
            lambda d: d["institutes"][0]["head"].update(person_id="somebody-else"),
            "is not a person of this institute",
        ),
        (
            lambda d: d["institutes"][0]["people"][0].update(until="2025-01"),
            "former appointment",
        ),
        # every stated value, relation and how has a bound excerpt
        (
            drop_support(f"{BASE}/head/person_id"),
            f"required claim has no excerpt: {BASE}/head/person_id",
        ),
        (
            drop_support(f"{BASE}/reports_to/name"),
            f"required claim has no excerpt: {BASE}/reports_to/name",
        ),
        (
            drop_support(f"{BASE}/national_partners/entries/1/relation"),
            "entries/1/relation",
        ),
        (
            drop_support(f"{BASE}/national_partners/entries/0/how"),
            "entries/0/how",
        ),
        (
            drop_support(f"{BASE}/national_partners/entries/0/name"),
            "entries/0/name",
        ),
        (
            lambda d: d["dataset"]["vocabularies"].pop("partner_cyber_none_published"),
            "partner_cyber_none_published is required",
        ),
        # no cyber agency means notes that name the search
        (
            lambda d: partners(d)["entries"][1].pop("cyber_agency"),
            "no partner entry has cyber_agency",
        ),
        (
            lambda d: partners(d).update(
                entries=[entry("lab")],
                notes="Searched, nothing found.",
            ),
            "no partner entry has cyber_agency",
        ),
        (
            lambda d: partners(d)["entries"][1].update(cyber_agency=False),
            "True was expected",
        ),
        # ids and sources resolve and stay unique
        (
            lambda d: partners(d)["entries"][1].update(
                id=partners(d)["entries"][0]["id"]
            ),
            "duplicate stable record identity",
        ),
        (
            lambda d: d["institutes"][0]["head"].update(source_ids=["no-such-source"]),
            "unknown source id",
        ),
        (
            lambda d: partners(d)["entries"][1].update(source_ids=["no-such-source"]),
            "unknown source id",
        ),
        # what is two to six words, how at most 25
        (lambda d: partners(d)["entries"][1].update(what="agency"), "does not match"),
        (
            lambda d: partners(d)["entries"][1].update(what="a b c d e f g"),
            "does not match",
        ),
        (
            lambda d: partners(d)["entries"][1].update(how=" ".join(["word"] * 26)),
            "how has 26 words",
        ),
        (
            lambda d: partners(d)["entries"][1].pop("how"),
            "'how' is a required property",
        ),
        (
            lambda d: partners(d)["entries"][1].update(
                how="Works with it. It also helps."
            ),
            "how is one sentence",
        ),
        (
            lambda d: d["institutes"][0].update(
                head={"state": "not_applicable", "source_ids": ["gb-source"]}
            ),
            "False schema does not allow",
        ),
        # member only for a federation of existing bodies
        (
            lambda d: partners(d)["entries"][1].update(relation="member"),
            "member is reserved for an institute whose legal_form is federation_of_existing_bodies",
        ),
        (
            lambda d: relation_vocabulary(d).pop("planned"),
            "partner_relation must define exactly",
        ),
        # the host is never a partner
        (
            lambda d: partners(d)["entries"][1].update(name="Test ministry"),
            "the host is never a partner entry",
        ),
        # nor is the body it reports to
        (
            lambda d: (
                d["institutes"][0]["reports_to"].update(
                    name="Other ministry; Third body"
                ),
                partners(d)["entries"][1].update(name="third BODY "),
            ),
            "the body the institute reports to is never a partner entry",
        ),
        # states
        (
            lambda d: d["institutes"][0]["head"].update(state="not_published"),
            "False schema does not allow",
        ),
        (
            lambda d: d["institutes"][0]["reports_to"].update(
                state="not_published", name="x"
            ),
            "False schema does not allow",
        ),
        (
            lambda d: d["institutes"][0].update(head={"state": "not_published"}),
            "'notes' is a required property",
        ),
        (
            lambda d: d["institutes"][0].update(
                head={
                    "state": "not_published",
                    "notes": "Searched the leadership page.",
                }
            ),
            "must say what was searched and when",
        ),
        (
            lambda d: d["institutes"][0].update(
                reports_to={
                    "state": "stated",
                    "name": "A;B",
                    "source_ids": ["gb-source"],
                }
            ),
            "use '; '",
        ),
        (lambda d: partners(d)["entries"][1].update(name="人工智能"), "needs name_en"),
    ],
)
def test_gate_mutations(rows, mutation, message):
    assert problems(rows) == []
    mutation(rows)
    assert any(message in e for e in problems(rows)), problems(rows)


def relation_vocabulary(d):
    return d["dataset"]["vocabularies"]["partner_relation"]


@pytest.mark.parametrize(
    "how",
    (
        "Works with NCSC. It evaluates models.",
        "Works with AI! It evaluates models.",
        "Works with NCSC? It evaluates models.",
        'Works with "NCSC." It evaluates models.',
        "Works with the agency (NCSC.) It evaluates models.",
        "Works with the agency (NCSC). It evaluates models.",
        'Works with "NCSC". It evaluates models.',
        'Works with "the agency." It evaluates models.',
        "Works with G7. It evaluates models.",
        'Works with NCSC. "It evaluates models."',
        "Works with NCSC. (It evaluates models.)",
    ),
)
def test_how_rejects_sentence_break_after_acronym(rows, how):
    partners(rows)["entries"][1]["how"] = how
    assert any("how is one sentence" in error for error in problems(rows))


@pytest.mark.parametrize(
    "how",
    (
        "Works with the U.S. AI Safety Institute on testing.",
        "Works with the U.K. AI Safety Institute on testing.",
        "Works with Dr. Smith on testing.",
        "Works with NCSC on testing.",
        "Works with the (U.S.) AI Safety Institute on testing.",
    ),
)
def test_how_preserves_abbreviations_and_acronyms(rows, how):
    partners(rows)["entries"][1]["how"] = how
    assert problems(rows) == []


def test_member_passes_for_a_federation(rows):
    rows["institutes"][0]["legal_form"] = "federation_of_existing_bodies"
    partners(rows)["entries"][1]["relation"] = "member"
    assert problems(rows) == []


def test_more_than_six_partners_stay_in_the_data(rows):
    partners(rows)["entries"] += [entry(f"x{k}") for k in range(8)]
    rows["sources"][0]["excerpts"][0]["supports"] += [
        f"{BASE}/national_partners/entries/{k}/{f}"
        for k in range(2, 10)
        for f in ("name", "relation", "how", "what")
    ]
    assert (
        problems(rows) == []
    )  # no schema or check cap: the card caps, the data keeps all
    published = publish(rows)
    assert len(published["institutes"][0]["national_partners"]["entries"]) == 10
    shown = published["institutes"][0]["display"]["rows"]["partners"]["items"]
    assert len(shown) == 6
    # cyber agency, then the works_with entries in authored order; planned LAB falls beyond the cap
    assert [i["name"] for i in shown] == ["SCA", "X0", "X1", "X2", "X3", "X4"]
    folded = published["institutes"][0]["display"]["rows"]["partners"]["fold_items"]
    assert [i["name"] for i in folded] == [
        "SCA", "X0", "X1", "X2", "X3", "X4", "X5", "X6", "X7", "LAB"
    ]
    Draft202012Validator(build.json.loads(build.SCHEMA.read_text())).validate(published)
    tables = build.tables(build.export_dataset(rows))
    assert len(tables["national_partners"]) == 10


def test_other_states_pass_with_their_required_fields(rows):
    note = "Searched the leadership page and the ministry site on 20 October 2026, in English."
    rows["institutes"][0]["head"] = {"state": "not_published", "notes": note}
    rows["institutes"][0]["reports_to"] = {"state": "not_published", "notes": note}
    rows["institutes"][0]["national_partners"] = {
        "state": "not_published",
        "notes": note,
    }
    assert problems(prune(rows)) == []


@pytest.mark.parametrize("field", ("head", "reports_to", "national_partners"))
def test_not_applicable_rejected_for_established_institute(rows, field):
    rows["institutes"][0][field] = {"state": "not_applicable"}
    assert any(
        f"{field}: not_applicable requires no established institute" in error
        for error in problems(prune(rows))
    )


@pytest.mark.parametrize("status", ("exploring", "planned", "none_identified"))
def test_not_applicable_passes_without_established_institute(rows, status):
    # Kenya is exploring: a proposal does not establish an institute.
    institute = rows["institutes"][0]
    institute["status"] = status
    for field in ("head", "reports_to", "national_partners"):
        institute[field] = {"state": "not_applicable"}
    assert check_rows(rows) == []


def test_not_applicable_uses_primary_timeline_status(rows):
    rows["institutes"][0].pop("status")
    rows["jurisdictions"][0].pop("status")
    rows["jurisdictions"][0].pop("status_since")
    rows["institutes"][0]["head"] = {"state": "not_applicable"}
    assert any(
        "head: not_applicable requires no established institute" in error
        for error in problems(prune(rows))
    )


def test_rows_arrive_for_every_institute_together(rows):
    second = copy.deepcopy(rows["institutes"][0])
    second["id"] = "gb-other"
    for key in ("head", "reports_to", "national_partners"):
        del second[key]
    rows["institutes"].append(second)
    errors = check_rows(rows)
    assert any(
        "rows are added to every institute together; missing head, reports_to, national_partners"
        in e
        for e in errors
    )
    for key in ("head", "reports_to", "national_partners"):
        del rows["institutes"][0][key]
    assert check_rows(rows) == []  # none authored: the fields stay optional


def test_existing_data_without_rows_is_unchanged(dataset):
    published = publish(copy.deepcopy(dataset))
    assert "rows" not in published["institutes"][0]["display"]
    assert not {"head", "reports_to", "national_partners"} & set(
        published["institutes"][0]
    )


def test_display_rows_order_words_and_tail(rows):
    rows["institutes"][0]["legal_form"] = "federation_of_existing_bodies"
    partners(rows)["entries"].append(entry("reg", "member"))
    partners(rows)["entries"].append(entry("pla", "planned"))
    rows["sources"][0]["excerpts"][0]["supports"] += [
        f"{BASE}/national_partners/entries/{k}/{f}"
        for k in (2, 3)
        for f in ("name", "relation", "how", "what")
    ]
    shown = publish(rows)["institutes"][0]["display"]["rows"]
    assert shown["head"]["text"] == "Synthetic lead, Director"
    assert shown["reports_to"]["text"] == "Test ministry"
    assert (
        [i["text"] for i in shown["partners"]["items"]]
        == [
            "SCA (synthetic public body, works with)",  # cyber agency first, whatever its relation
            "REG (synthetic public body, member)",
            "LAB (synthetic public body, planned)",  # authored order breaks the tie between the two planned
            "PLA (synthetic public body, planned)",
        ]
    )
    assert shown["partners"]["items"][0]["how"].startswith("Works with the institute")
    assert shown["partners"]["tail"] is None
    partners(rows)["entries"] = [
        e for e in partners(rows)["entries"] if not e.get("cyber_agency")
    ]
    partners(rows)["notes"] = "Cyber agency searched on 20 October 2026 (en)."
    tail = publish(prune(rows))["institutes"][0]["display"]["rows"]["partners"]["tail"]
    assert tail == "cyber agency not published"
    rows["dataset"]["vocabularies"]["partner_cyber_none_published"] = (
        "digital security agency none published"
    )
    assert (
        publish(prune(rows))["institutes"][0]["display"]["rows"]["partners"]["tail"]
        == "digital security agency none published"
    )


def test_labels_come_only_from_the_vocabulary(rows):
    relation_vocabulary(rows)["works_with"]["label"] = "cooperates with"
    items = publish(rows)["institutes"][0]["display"]["rows"]["partners"]["items"]
    assert items[0]["text"] == "SCA (synthetic public body, cooperates with)"


def test_partner_order_cyber_then_relation_then_authored_order():
    entries = [
        entry("a", "planned"),
        entry("b", "works_with"),
        entry("c", "member"),
        entry("d", "planned", cyber_agency=True),
        entry("e", "works_with"),
        entry("f", "member"),
    ]
    assert [e["id"][-1] for e in partner_order(entries)] == list("dcfbea")


def test_citations_follow_the_bound_excerpts(rows):
    shown = publish(rows)["institutes"][0]["display"]["rows"]
    assert shown["head"]["cited"] == ["gb-source"]
    assert shown["partners"]["cited"] == ["gb-source"]
    rows["sources"][0]["excerpts"][0]["supports"].remove(f"{BASE}/head/person_id")
    assert publish(rows)["institutes"][0]["display"]["rows"]["head"]["cited"] == []


def detail_source(d, field, index=1):
    """A source supporting only one partner detail, distinct from its relation source."""
    pointer = f"{BASE}/national_partners/entries/{index}/{field}"
    for existing in d["sources"]:
        for excerpt in existing.get("excerpts", []):
            excerpt["supports"] = [p for p in excerpt["supports"] if p != pointer]
    source = copy.deepcopy(d["sources"][0])
    source.update(id="detail-source", url="https://example.test/detail")
    source.pop("output_lineage", None)
    source.pop("output_role", None)
    source["excerpts"] = [
        {
            "text": "Synthetic body details.",
            "supports": [f"{BASE}/national_partners/entries/{index}/{field}"],
        }
    ]
    d["sources"].append(source)
    partners(d)["entries"][index]["source_ids"].append("detail-source")


def test_cyber_requires_exact_binding_in_strict_mode(rows, monkeypatch):
    monkeypatch.setattr(row_checks, "STRICT_PARTNER_DETAILS", True)
    pointer = f"{BASE}/national_partners/entries/1/cyber_agency"
    detail_source(rows, "cyber_agency")
    assert problems(rows) == []
    rows["sources"][-1]["excerpts"][0]["supports"] = [
        f"{BASE}/national_partners/entries/1"
    ]
    assert any(
        f"required claim has no excerpt: {pointer}" in error
        for error in problems(rows)
    )


def test_unbound_cyber_flag_rejected_in_strict_mode(rows, monkeypatch):
    monkeypatch.setattr(row_checks, "STRICT_PARTNER_DETAILS", True)
    rows["sources"][0]["excerpts"][0]["supports"].remove(
        f"{BASE}/national_partners/entries/1/cyber_agency"
    )
    assert problems(rows) == [
        f"required claim has no excerpt: {BASE}/national_partners/entries/1/cyber_agency"
    ]


def test_at_most_one_cyber_agency_in_strict_mode(rows, monkeypatch):
    monkeypatch.setattr(row_checks, "STRICT_PARTNER_DETAILS", True)
    detail_source(rows, "cyber_agency")
    assert problems(rows) == []
    partners(rows)["entries"][0]["cyber_agency"] = True
    rows["sources"][0]["excerpts"][0]["supports"].append(
        f"{BASE}/national_partners/entries/0/cyber_agency"
    )
    assert problems(rows) == [
        "gb-test: national_partners allows at most one cyber_agency entry"
    ]


def test_cyber_compatibility_mode_allows_pending_data(rows, monkeypatch):
    monkeypatch.setattr(row_checks, "STRICT_PARTNER_DETAILS", False)
    # Legacy fixtures may have multiple flags without exact bindings.
    partners(rows)["entries"][0]["cyber_agency"] = True
    assert problems(rows) == []


def test_cyber_source_is_cited_even_in_compatibility_mode(rows, monkeypatch):
    monkeypatch.setattr(row_checks, "STRICT_PARTNER_DETAILS", False)
    detail_source(rows, "cyber_agency")
    assert problems(rows) == []
    cited = publish(rows)["institutes"][0]["display"]["rows"]["partners"]["cited"]
    assert "detail-source" in cited


def test_cyber_source_required_by_page_oracle(rows):
    detail_source(rows, "cyber_agency")
    assert any(
        "Partners: citations" in error
        for error in page_errors(rows, card(good_rows()))
    )
    fixed = good_rows().replace(
        cite() + "</span>", cite("gb-source", "detail-source") + "</span>", 1
    )
    assert page_errors(rows, card(fixed)) == []


def test_what_requires_exact_binding_in_strict_mode(rows, monkeypatch):
    monkeypatch.setattr(row_checks, "STRICT_PARTNER_DETAILS", True)
    detail_source(rows, "cyber_agency")
    assert problems(rows) == []
    pointer = f"{BASE}/national_partners/entries/1/what"
    rows["sources"][0]["excerpts"][0]["supports"].remove(pointer)
    rows["sources"][0]["excerpts"][0]["supports"].append(
        f"{BASE}/national_partners/entries/1"
    )
    assert any(
        f"required claim has no excerpt: {pointer}" in error
        for error in problems(rows)
    )
    monkeypatch.setattr(row_checks, "STRICT_PARTNER_DETAILS", False)
    assert problems(rows) == []


@pytest.mark.parametrize("strict", (False, True))
def test_what_only_source_is_cited(rows, monkeypatch, strict):
    monkeypatch.setattr(row_checks, "STRICT_PARTNER_DETAILS", strict)
    rows["sources"][0]["excerpts"][0]["supports"].remove(
        f"{BASE}/national_partners/entries/1/what"
    )
    detail_source(rows, "what")
    assert problems(rows) == []
    published = publish(rows)["institutes"][0]
    assert published["national_partners"]["entries"][1]["citations"]["what"] == [
        "detail-source"
    ]
    assert "detail-source" in published["display"]["rows"]["partners"]["cited"]


def test_what_only_source_required_by_page_oracle(rows):
    rows["sources"][0]["excerpts"][0]["supports"].remove(
        f"{BASE}/national_partners/entries/1/what"
    )
    detail_source(rows, "what")
    assert any(
        "Partners: citations" in error
        for error in page_errors(rows, card(good_rows()))
    )
    fixed = good_rows().replace(
        cite() + "</span>", cite("gb-source", "detail-source") + "</span>", 1
    )
    assert page_errors(rows, card(fixed)) == []


def test_csv_columns(rows):
    tables = build.tables(build.export_dataset(rows))
    assert (
        list(tables["national_partners"][0]) == build.EMPTY_COLUMNS["national_partners"]
    )
    institute = tables["institutes"][0]
    assert institute["head_person_id"] == "test-lead"
    assert institute["reports_to"] == "Test ministry"
    assert "reports_to_joint" not in institute
    by_id = {r["id"]: r for r in tables["national_partners"]}
    assert by_id["np-test-sca"]["cyber_agency"] is True
    assert by_id["np-test-lab"]["cyber_agency"] in ("", None, False)
    assert by_id["np-test-lab"]["relation"] == "planned"
    assert by_id["np-test-lab"]["how"].startswith("Works with the institute")


def test_api_record_carries_the_rows_and_their_excerpts(rows):
    published = publish(rows)
    record = agent_files.record_file(published, "institutes", 0)
    assert record["record"]["head"]["person_id"] == "test-lead"
    assert record["record"]["national_partners"]["entries"][0]["how"]
    pointers = [
        p for s in record["sources"] for e in s["excerpts"] for p in e["supports"]
    ]
    assert "/record/head/person_id" in pointers
    assert "/record/national_partners/entries/0/how" in pointers
    assert agent_files.api_root(published) == "api/v1"


def test_orientation_names_the_new_rows(rows):
    text = agent_files.orientation_markdown(publish(rows))
    assert "national_partners.csv" in text and "`cyber_agency`" in text


# The rendered-page gate: hand-written HTML, expected words worked out by the oracle from the data.


def card(rows_html):
    return f"""<article class="card" data-j="GB" id="c-gb"><div class="row"><div class="k">Form</div><div class="v">Unit</div></div>{rows_html}
    <div class="row"><div class="k">Mandate</div><div class="v">Evaluate</div></div></article>"""


def cite(n="gb-source", *more):
    anchors = ",".join(
        f'<a class="cn" data-source-id="{sid}" href="u">{number}</a>'
        for number, sid in enumerate((n, *more), 1)
    )
    return f'<span class="cites">{anchors}</span>'


HOW_SCA = "Works with the institute on synthetic evaluation task sca."
HOW_LAB = "Works with the institute on synthetic evaluation task lab."


def fold(*pairs):
    items = "".join(
        f'<li><span class="pname">{n}</span> <span class="phow">{h}{cite()}</span></li>'
        for n, h in pairs
    )
    return f'<details class="more quiet partner-how"><summary>How they work together</summary><ul>{items}</ul></details>'


def good_rows():
    return (
        f'<div class="row"><div class="k">Head</div><div class="v">Synthetic lead, Director{cite()}</div></div>'
        f'<div class="row"><div class="k">Reports to</div><div class="v">Test ministry{cite()}</div></div>'
        '<div class="row"><div class="k">Partners</div><div class="v">'
        '<span class="partners"><span class="partner">SCA (synthetic public body, works with)</span> '
        f'<span class="partner">LAB (synthetic public body, planned)</span>{cite()}</span>'
        + fold(("SCA", HOW_SCA), ("LAB", HOW_LAB))
        + "</div></div>"
    )


def oracle_data(rows):
    published = publish(rows)
    published["jurisdictions"][0]["id"] = "GB"
    return published


def page_errors(rows, html):
    return check_page.check_leadership_rows(html, oracle_data(rows))


def test_page_oracle_accepts_the_expected_card(rows):
    assert page_errors(rows, card(good_rows())) == []


@pytest.mark.parametrize(
    ("old", "new", "message"),
    [
        ("Synthetic lead, Director", "Synthetic lead", "Head: shows"),
        ("Test ministry", "Other ministry", "Reports to: shows"),
        (
            "SCA (synthetic public body, works with)",
            "SCA (synthetic public body, member)",
            "Partners: shows",
        ),
        (
            "SCA (synthetic public body, works with)",
            "SCA (works with)",
            "Partners: shows",
        ),
        (
            '<span class="partner">SCA (synthetic public body, works with)',
            '<span class="partner">LAB (synthetic public body, planned)',
            "Partners: shows",
        ),
        ('<a class="cn" data-source-id="gb-source" href="u">1</a>', "", "citations"),
        (
            '<div class="k">Head</div>',
            '<div class="k">Leader</div>',
            "differ from the data",
        ),
        # the how sentence belongs in the fold-out, not on the line
        (
            "SCA (synthetic public body, works with)",
            "SCA (synthetic public body, works with) " + HOW_SCA,
            "Partners: shows",
        ),
        (HOW_LAB, "Another sentence.", "fold-out shows"),
        ("How they work together", "More", "summary must read"),
        (
            '<details class="more quiet partner-how">',
            '<details class="more quiet partner-how" open>',
            "must start closed",
        ),
        ('<details class="more quiet partner-how">', "<details>", "fold-out shows"),
    ],
)
def test_page_oracle_mutations(rows, old, new, message):
    html = card(good_rows()).replace(old, new, 1)
    assert any(message in e for e in page_errors(rows, html)), page_errors(rows, html)


def test_page_oracle_fold_out_follows_card_order(rows):
    html = card(
        good_rows().replace(
            fold(("SCA", HOW_SCA), ("LAB", HOW_LAB)),
            fold(("LAB", HOW_LAB), ("SCA", HOW_SCA)),
        )
    )
    assert any("fold-out shows" in e for e in page_errors(rows, html))


def test_page_oracle_requires_the_order_after_form(rows):
    html = card("").replace("</article>", "") + good_rows() + "</article>"
    assert any("must follow Form directly" in e for e in page_errors(rows, html))


def test_page_oracle_card_cap_is_six(rows):
    partners(rows)["entries"] += [entry(f"x{k}") for k in range(6)]
    rows["sources"][0]["excerpts"][0]["supports"] += [
        f"{BASE}/national_partners/entries/{k}/{f}"
        for k in range(2, 8)
        for f in ("name", "relation", "how", "what")
    ]
    names = ["SCA", "X0", "X1", "X2", "X3", "X4", "X5", "LAB"]
    line = "".join(
        f'<span class="partner">{n} (synthetic public body, {"planned" if n == "LAB" else "works with"})</span> '
        for n in names
    )
    html = card(
        good_rows()
        .replace(
            '<span class="partner">SCA (synthetic public body, works with)</span> '
            '<span class="partner">LAB (synthetic public body, planned)</span>',
            line,
        )
        .replace(
            fold(("SCA", HOW_SCA), ("LAB", HOW_LAB)),
            fold(
                *[
                    (
                        n,
                        f"Works with the institute on synthetic evaluation task {n.lower()}.",
                    )
                    for n in names
                ]
            ),
        )
    )
    assert any(
        "Partners: shows" in e for e in page_errors(rows, html)
    )  # eight on the line: two too many
    kept = names[:6]
    line = "".join(
        f'<span class="partner">{n} (synthetic public body, works with)</span> '
        for n in kept
    )
    ok = card(
        good_rows()
        .replace(
            '<span class="partner">SCA (synthetic public body, works with)</span> '
            '<span class="partner">LAB (synthetic public body, planned)</span>',
            line,
        )
        .replace(
            fold(("SCA", HOW_SCA), ("LAB", HOW_LAB)),
            fold(
                *[
                    (
                        n,
                        f"Works with the institute on synthetic evaluation task {n.lower()}.",
                    )
                    for n in names
                ]
            ),
        )
    )
    assert page_errors(rows, ok) == []
    truncated = BeautifulSoup(ok, "html.parser")
    truncated.select("details.partner-how li")[-1].decompose()
    assert any("fold-out shows" in e for e in page_errors(rows, str(truncated)))

    # X5 is seventh in display order: its how source must occur only in the fold.
    rows["sources"][0]["excerpts"][0]["supports"].remove(
        f"{BASE}/national_partners/entries/7/how"
    )
    detail_source(rows, "how", index=7)
    assert problems(rows) == []
    shown = publish(rows)["institutes"][0]["display"]["rows"]["partners"]
    assert shown["cited"] == ["gb-source"]
    folded = {item["id"]: item for item in shown["fold_items"]}
    assert folded["np-test-x5"]["cited"] == ["gb-source", "detail-source"]
    assert any("fold-out citations" in e for e in page_errors(rows, ok))
    cited = ok.replace(
        f"task x5.{cite()}</span>",
        f"task x5.{cite('gb-source', 'detail-source')}</span>",
    )
    assert page_errors(rows, cited) == []


def test_page_oracle_tail_and_dash(rows):
    partners(rows)["entries"] = [
        e for e in partners(rows)["entries"] if not e.get("cyber_agency")
    ]
    partners(rows)["notes"] = "Cyber agency searched on 20 October 2026 (en)."
    prune(rows)
    html = card(
        good_rows()
        .replace(
            '<span class="partner">SCA (synthetic public body, works with)</span> ', ""
        )
        .replace(fold(("SCA", HOW_SCA), ("LAB", HOW_LAB)), fold(("LAB", HOW_LAB)))
    )
    assert any(
        "expected" in e and "cyber agency not published" in e
        for e in page_errors(rows, html)
    )
    html = html.replace(
        cite() + "</span>",
        ' <span class="partners-none">cyber agency not published</span>'
        + cite()
        + "</span>",
        1,
    )
    assert page_errors(rows, html) == []
    rows["institutes"][0]["reports_to"] = {"state": "not_applicable"}
    prune(rows)
    assert any(
        "not applicable must be the muted dash" in e for e in page_errors(rows, html)
    )
    na = '<span class="missing na"><span aria-hidden="true">–</span><span class="vh">Not applicable</span></span>'
    fixed = html.replace(f"Test ministry{cite()}", na)
    assert page_errors(rows, fixed) == []


def test_page_oracle_every_card_needs_the_rows_once_any_has_them(rows):
    second = copy.deepcopy(rows["institutes"][0])
    second.update(id="gb-other", jurisdiction_id="FR")
    for key in ("head", "reports_to", "national_partners"):
        del second[key]
    published = oracle_data(rows)
    published["institutes"].append(second)
    html = (
        card(good_rows())
        + '<article class="card" data-j="FR" id="c-fr"><div class="row"><div class="k">Form</div><div class="v">x</div></div></article>'
    )
    assert any(
        "every institute needs all three rows" in e
        for e in check_page.check_leadership_rows(html, published)
    )


def test_page_without_rows_is_accepted_when_the_data_has_none(dataset):
    published = publish(copy.deepcopy(dataset))
    assert check_page.check_leadership_rows(card(""), published) == []
    soup = BeautifulSoup(card(""), "html.parser")
    assert soup.select_one("#c-gb")


def test_rule_tables_report_the_rows(rows):
    from pathlib import Path

    from scripts.rule_tables import Report

    report = Report(publish(rows), Path("work/reports/rule-tables.md"), [], None)
    text = report.rows()
    assert "stated: Synthetic lead" in text
    assert "stated: Test ministry" in text
    assert "stated: 2 entries (planned 1; works_with 1); cyber agency present" in text
    partners(rows)["entries"].pop(1)
    partners(rows)["notes"] = "Cyber agency searched on 20 October 2026 (en)."
    assert (
        "no cyber agency" in Report(publish(prune(rows)), Path("x.md"), [], None).rows()
    )


def test_rule_tables_without_rows_say_not_recorded(dataset):
    from pathlib import Path

    from scripts.rule_tables import Report

    text = Report(publish(copy.deepcopy(dataset)), Path("x.md"), [], None).rows()
    assert text.count("not recorded") == 3


def test_head_must_be_a_person_of_the_same_institute(rows):
    other = copy.deepcopy(rows["institutes"][0])
    other["id"] = "gb-other"
    other["people"] = [
        {
            "id": "other-lead",
            "name": "Other lead",
            "role": "Director",
            "title_status": "published",
            "source_ids": ["gb-source"],
        }
    ]
    other["head"]["person_id"] = "other-lead"
    rows["institutes"].append(other)
    assert not any(e.startswith("gb-other: head") for e in check_rows(rows))
    rows["institutes"][0]["head"]["person_id"] = (
        "other-lead"  # a person, but of another institute
    )
    assert any(
        "gb-test: head.person_id 'other-lead' is not a person of this institute" in e
        for e in check_rows(rows)
    )


def test_page_oracle_notices_a_dropped_partners_citation(rows):
    html = BeautifulSoup(card(good_rows()), "html.parser")
    html.select_one(".partners .cites").decompose()
    assert any("Partners: citations []" in e for e in page_errors(rows, str(html)))


@pytest.mark.parametrize("anchor", ("</summary>", "</ul>"))
def test_page_oracle_rejects_fold_citation_outside_item(rows, anchor):
    html = card(good_rows()).replace(anchor, anchor + cite())
    assert any(
        "fold-out citation outside a partner item" in error
        for error in page_errors(rows, html)
    )


def test_partners_without_acronym_sort_ties_by_authored_order():
    zentral = entry("z", "member")
    zentral.pop("acronym")
    zentral.update(name="Zentralstelle", name_en="Agency for A")
    plain = entry("bb", "member")
    assert [e["id"] for e in partner_order([zentral, plain])] == [
        "np-test-z",
        "np-test-bb",
    ]
    assert [e["id"] for e in partner_order([plain, zentral])] == [
        "np-test-bb",
        "np-test-z",
    ]


def test_citation_must_end_its_value_but_not_before_the_partner_fold_out(rows):
    value = '<div class="v">Text{}<span class="cites"><a class="cn" data-source-id="gb-source" href="u">1</a></span>{}</div>'

    def ends_value(tail):
        errors = check_page.check_citation_ends(
            BeautifulSoup(value.format("", tail), "html.parser")
        )
        return not errors

    assert ends_value("")
    assert ends_value(
        fold(("SCA", HOW_SCA))
    )  # a collapsed fold-out after the value is not part of it
    assert not ends_value(
        " trailing words"
    )  # the mutation: text after the citation still fails
    assert not ends_value(
        '<details class="more"><summary>Other</summary></details>'
    )  # only the partner fold-out is exempt


def test_fold_citation_must_end_its_own_item():
    html = fold(("SCA", HOW_SCA))
    assert check_page.check_citation_ends(BeautifulSoup(html, "html.parser")) == []
    broken = html.replace("</li>", " trailing words</li>")
    assert "citation group is not at the end of its value" in check_page.check_citation_ends(
        BeautifulSoup(broken, "html.parser")
    )
