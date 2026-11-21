"""News acceptance: fixed next-data expectations and small independent rule examples."""

import copy
import csv
import json
import re
from pathlib import Path
import subprocess

import pytest
from bs4 import BeautifulSoup
from jsonschema import Draft202012Validator

from scripts import agent_files, build, check_page, display, method_checks, reader_text, shared_claims
from scripts.references import authored_schema
from scripts.published import publish


ROOT = Path(__file__).resolve().parents[1]
STATUS_KINDS = ("commitment", "proposed", "designated", "launched", "first_output", "exploration_started", "withdrawn", "closed")


def union(*groups):
    return list(dict.fromkeys(s for group in groups for s in group))


def collect(data, *, limit=10):
    return display.recent_developments(data["jurisdictions"], union, data["institutes"], data["dataset"].get("shared_claims", []), limit=limit)


@pytest.fixture(scope="module")
def next_data():
    return publish(json.loads((ROOT / "data/aisi.json").read_text()), rereads={})


def sample():
    return {"dataset": {"as_of": "2026-11-17"}, "sources": [], "changelog": [],
            "jurisdictions": [{"id": "GB", "name": "United Kingdom", "timeline": []}],
            "institutes": [{"id": "gb-test", "jurisdiction_id": "GB", "name": "Full institute name", "short_name": "UK Test", "funding": [], "agreements": []}]}


def event(identity="status", day="2026-10-01", kind="designated", **extra):
    return {"id": identity, "date": day, "event": kind, "short": "Institute designated.", "description": "Institute designated. More detail.", "source_ids": [], **extra}


def budget(identity="budget", **extra):
    return {"id": identity, "decision_date": "2026-10-01", "amount": 120_000_000, "currency": "GBP", "commitment": "announced", "coverage": "component", "kind": "multi_year", "description": "A budget.", "source_ids": [], **extra}


def agreement(identity="agreement", **extra):
    return {"id": identity, "date": "2026-10-01", "state": "signed", "participation": "institute_party", "counterparty": "Developer", "source_ids": [], **extra}


def test_news_status_milestones_unchanged():
    data = sample()
    data["jurisdictions"][0]["timeline"] = [event(kind, f"2026-10-{n:02d}", kind) for n, kind in enumerate(STATUS_KINDS, 1)]
    assert {g["events"][0] for g in collect(data)} == set(STATUS_KINDS)
    assert {r["news_kind"] for r in build.event_rows(data)} == {"status"}


def test_news_rename_and_mandate_change():
    data = sample()
    data["jurisdictions"][0]["timeline"] = [event("rename", kind="named", short="New institute name adopted."), event("mandate", "2026-10-02", "mandate_changed", short="Institute mandate extended.")]
    assert [g["texts"] for g in collect(data)] == [["Institute mandate extended."], ["New institute name adopted."]]
    assert {r["news_kind"] for r in build.event_rows(data)} == {"mandate_or_name"}
    data["jurisdictions"][0]["timeline"][0]["short"] = ""
    assert collect(data)[1]["texts"] == ["Institute designated."]


@pytest.mark.parametrize("commitment", ["announced", "adopted", "proposed", "not_applicable"])
@pytest.mark.parametrize("coverage", ["whole_institute", "component", "not_institute_specific"])
@pytest.mark.parametrize("day", ["2026-10-01", None])
def test_news_budget_rule(commitment, coverage, day):
    data = sample()
    data["institutes"][0]["funding"] = [budget(commitment=commitment, coverage=coverage, decision_date=day)]
    groups = collect(data)
    selected = day and commitment in {"announced", "adopted"} and coverage in {"whole_institute", "component"}
    assert len(groups) == (1 if selected else 0)
    if selected:
        assert groups[0]["texts"] == [f"GBP 120m for the institute {commitment}."]
        assert groups[0]["events"] == []
    # Missing date and a null date both exclude the budget.
    del data["institutes"][0]["funding"][0]["decision_date"]
    assert collect(data) == []


def test_news_budget_keeps_card_amount_qualifiers_and_native_currency():
    data = sample()
    data["institutes"][0]["funding"] = [budget(amount_qualifier="at_least")]
    assert collect(data)[0]["texts"] == ["at least GBP 120m for the institute announced."]
    data["institutes"][0]["funding"][0].update(amount=125_000_000, amount_qualifier="about")
    assert collect(data)[0]["texts"] == ["about GBP 130m for the institute announced."]


@pytest.mark.parametrize("state", ["signed", "announced", "pledged"])
def test_news_agreement_rule(state):
    data = sample()
    data["institutes"][0]["agreements"] = [agreement(state=state, participation="recipient" if state == "pledged" else "institute_party")]
    expected = "Pledge announced by Developer." if state == "pledged" else f"Agreement {state} with Developer."
    assert collect(data)[0]["texts"] == [expected]
    data["institutes"][0]["agreements"][0].pop("date")
    assert collect(data) == []


@pytest.mark.parametrize("state", ["signed", "announced"])
def test_news_named_implementer_single_and_pair(state):
    data = paired_sample()
    for institute in data["institutes"]:
        institute["agreements"][0]["state"] = state
    verb = {"signed": "sign", "announced": "announce"}[state]
    expected = f"The two governments {verb} a text on cooperation naming their institutes."
    assert [g["texts"] for g in collect(data)] == [[expected]] * 2
    # Institute array order does not change registered party order or row order.
    data["institutes"].reverse()
    assert [g["texts"] for g in collect(data)] == [[expected]] * 2
    data["dataset"]["shared_claims"] = []
    assert [g["texts"] for g in collect(data)] == [[f"Governments {verb} a text on cooperation naming the institute."]] * 2
    for institute in data["institutes"]:
        institute["agreements"][0].pop("date")
    assert collect(data) == []


def test_news_grouping_and_ids():
    data = sample()
    data["jurisdictions"][0]["timeline"] = [event("z-status")]
    before = collect(data)[0]
    data["institutes"][0]["funding"] = [budget("a-budget")]
    data["institutes"][0]["agreements"] = [agreement("b-agreement")]
    group, = collect(data)
    assert group["order"] == "z-status"
    assert agent_files.feed_news_id(group) == agent_files.feed_news_id(before)
    assert group["events"] == ["z-status"]
    assert group["texts"] == ["Institute designated.", "GBP 120m for the institute announced.", "Agreement signed with Developer."]
    assert group["items"] == ["timeline:GB:z-status", "funding:gb-test:a-budget", "agreements:gb-test:b-agreement"]
    for day in ("2026-10", "2026"):
        data["jurisdictions"][0]["timeline"][0]["date"] = day
        data["institutes"][0]["funding"][0]["decision_date"] = day
        data["institutes"][0]["agreements"][0]["date"] = day
        groups = collect(data)
        # Scoping feed identities must not change the bare-record tie-break.
        assert [g["order"] for g in groups] == ["funding:gb-test:a-budget", "agreements:gb-test:b-agreement", "z-status"]
        assert [agent_files.feed_news_id(g).split("/")[-1] for g in groups] == ["funding:gb-test:a-budget", "agreements:gb-test:b-agreement", "z-status"]
    data = sample()
    data["jurisdictions"][0]["timeline"] = [event(str(n), f"2026-10-{n:02d}") for n in range(1, 13)]
    data["jurisdictions"].append({"id": "CA", "name": "Canada", "timeline": [event("canada", "2026-10-12")]})
    groups = collect(data)
    assert len(groups) == 10
    assert [(g["jurisdiction"], g["date"]) for g in groups[:2]] == [("CA", "2026-10-12"), ("GB", "2026-10-12")]
    assert groups[-1]["date"] == "2026-10-04"
    data["jurisdictions"].reverse()
    for jurisdiction in data["jurisdictions"]:
        jurisdiction["timeline"].reverse()
    assert collect(data) == groups


@pytest.mark.parametrize("day", ["2026-10", "2026"])
def test_partial_date_feed_ids_distinguish_origins_and_owners(day):
    data = sample()
    data["jurisdictions"][0]["timeline"] = [event("shared", day)]
    timeline_id = agent_files.feed_news_id(collect(data)[0])
    assert timeline_id == f"tag:aisi.accelerate-europe.org,2026:news/GB/{day}/shared"
    data["institutes"].append({"id": "gb-other", "jurisdiction_id": "GB", "short_name": "UK Other"})
    for institute in data["institutes"]:
        institute["funding"] = [budget("shared", decision_date=day, display={"native_value": "GBP 120m"})]
        institute["agreements"] = [agreement("shared", date=day)]
    with_news(data)
    groups = data["dataset"]["display"]["developments"]
    assert [g["items"] for g in groups] == [
        ["agreements:gb-other:shared"], ["agreements:gb-test:shared"],
        ["funding:gb-other:shared"], ["funding:gb-test:shared"], ["timeline:GB:shared"],
    ]
    expected_ids = {
        f"tag:aisi.accelerate-europe.org,2026:news/GB/{day}/{identity}"
        for identity in ("agreements:gb-other:shared", "agreements:gb-test:shared", "funding:gb-other:shared", "funding:gb-test:shared", "shared")
    }
    ids = [e["id"] for e in agent_files.feed_entries(data) if e["kind"] == "news"]
    assert len(ids) == len(set(ids)) == 5
    assert set(ids) == expected_ids
    timeline, = (g for g in groups if g["events"])
    assert timeline["order"] == "shared"
    assert agent_files.feed_news_id(timeline) == timeline_id
    assert check_page.check_news(BeautifulSoup(news_html(data), "html.parser"), data) == []
    data["institutes"].reverse()
    assert collect(data) == groups


def events_csv(data, tmp_path):
    """Exercise the export rows and declared columns without invoking a site build."""
    path = tmp_path / "data" / "events.csv"
    path.parent.mkdir(exist_ok=True)
    with path.open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=build.EMPTY_COLUMNS["events"])
        writer.writeheader()
        writer.writerows(build.event_rows(data))
    with path.open(newline="") as stream:
        reader = csv.DictReader(stream)
        return reader.fieldnames, list(reader)


NEWS_COLUMNS = ["date", "jurisdiction", "institute", "news_kind", "headline", "source_urls", "id"]


def test_news_table_matches_published_items_and_feed(next_data):
    tables = build.tables(next_data)
    rows = tables["news"]
    groups = next_data["dataset"]["display"]["developments"]
    events = {row["id"]: row for row in tables["events"]}
    feed = [entry for entry in agent_files.feed_entries(next_data) if entry["kind"] == "news"]
    by_feed_id = {entry["id"]: entry for entry in feed}
    sources = {source["id"]: source["url"] for source in next_data["sources"]}
    schema = json.loads((ROOT / "schema/aisi.schema.json").read_text())["$defs"]["news_export"]
    validator = Draft202012Validator(schema)

    assert rows and [row["id"] for row in rows] == [identity for group in groups for identity in group["items"]]
    assert {agent_files.feed_news_id(group) for group in groups} == set(by_feed_id)
    offset = 0
    for group in groups:
        group_rows = rows[offset:offset + len(group["items"])]
        offset += len(group_rows)
        entry = by_feed_id[agent_files.feed_news_id(group)]
        assert " ".join(row["headline"] for row in group_rows) == entry["summary"]
        assert [row["source_urls"] for row in group_rows] == [
            "; ".join(sources[sid] for sid in ids) for ids in group["line_source_ids"]
        ]
    # The feed reverses same-date state ties; the CSV keeps the published collector order.
    assert [agent_files.feed_day(group["date"]) for group in groups] == [entry["updated"] for entry in feed]
    for row in rows:
        assert list(row) == NEWS_COLUMNS
        validator.validate(row)
        event = events[row["id"]]
        assert all(row[field] == event[field] for field in ("date", "jurisdiction", "institute", "news_kind"))


@pytest.mark.parametrize("authored", [False, True])
def test_news_table_pair_has_one_row_per_state(authored):
    data = paired_sample()
    for institute in data["institutes"]:
        if institute["id"] == "ca-test":
            institute["name"] = "Canadian institute"
        if authored:
            institute["agreements"][0]["news"] = "The governments agree to cooperate."
    with_news(data)
    rows = build.news_rows(data, build.event_rows(data))
    expected = "The governments agree to cooperate." if authored else "The two governments sign a text on cooperation naming their institutes."
    assert [(row["jurisdiction"], row["institute"], row["id"], row["headline"]) for row in rows] == [
        ("Canada", "Canadian institute", "agreements:ca-test:mirror", expected),
        ("United Kingdom", "Full institute name", "agreements:gb-test:agreement", expected),
    ]


def test_news_table_same_day_items_keep_their_lines_and_sources():
    data = sample()
    data["sources"] = [{"id": sid, "url": f"https://example.org/{sid}"} for sid in ("a", "b", "c")]
    data["jurisdictions"][0].update(institute_id="gb-test", timeline=[
        event("z-status", news="The government designates the institute.", source_ids=["a"], citations={"news": ["a"]}),
    ])
    data["institutes"][0]["funding"] = [budget("a-budget", source_ids=["c", "b"], citations={"amount": ["c", "b"]})]
    with_news(data)
    assert len(data["dataset"]["display"]["developments"]) == 1
    rows = build.news_rows(data, build.event_rows(data))
    assert [(row["id"], row["headline"], row["source_urls"]) for row in rows] == [
        ("timeline:GB:z-status", "The government designates the institute.", "https://example.org/a"),
        ("funding:gb-test:a-budget", "GBP 120m for the institute announced.", "https://example.org/c; https://example.org/b"),
    ]
    assert [row["institute"] for row in rows] == ["Full institute name"] * 2


@pytest.mark.parametrize("day", ["2026", "2026-10", "2026-10-01"])
def test_news_table_keeps_date_precision_and_empty_institute(day):
    data = sample()
    data["institutes"] = []
    data["jurisdictions"][0]["timeline"] = [event(day=day)]
    with_news(data)
    row, = build.news_rows(data, build.event_rows(data))
    assert row == dict(zip(NEWS_COLUMNS, [day, "United Kingdom", "", "status", "Institute designated.", "", "timeline:GB:status"], strict=True))


def test_news_items_field(next_data, tmp_path):
    _, rows = events_csv(next_data, tmp_path)
    by_id = {r["id"]: r for r in rows}
    for group in collect(next_data, limit=None):
        assert len(group["items"]) == len(group["texts"])
        assert len(group["pairs"]) == len(group["texts"])
        assert len(group["line_source_ids"]) == len(group["texts"])
        assert union(*group["line_source_ids"]) == group["source_ids"] == group["citations"]["display"]
        assert all(identity in by_id for identity in group["items"])
        assert group["events"] == [by_id[identity]["origin_id"].split(":", 1)[1] for identity in group["items"] if by_id[identity]["origin_kind"] == "timeline"]
        assert all(by_id[identity]["jurisdiction_id"] == group["jurisdiction"] and by_id[identity]["date"] == group["date"] for identity in group["items"])
    schema = json.loads((ROOT / "schema/aisi.schema.json").read_text())
    validator = Draft202012Validator(schema)
    validator.validate(next_data)
    legacy = copy.deepcopy(next_data)
    for group in legacy["dataset"]["display"]["developments"]:
        del group["items"]
        del group["pairs"]
        del group["line_source_ids"]
    validator.validate(legacy)  # additive, optional fields


def test_events_news_kind_column(next_data, tmp_path):
    columns, rows = events_csv(next_data, tmp_path)
    assert "news_kind" in columns
    expected = {}
    for row in rows:
        record = next_data
        for component in row["source_pointer"].strip("/").split("/"):
            record = record[int(component)] if isinstance(record, list) else record[component]
        kind = ""
        if row["origin_kind"] == "timeline":
            kind = "status" if record["event"] in STATUS_KINDS else "mandate_or_name" if record["event"] in {"named", "mandate_changed"} else ""
        elif row["origin_kind"] == "agreements":
            kind = "agreement"
        elif row["origin_kind"] == "funding" and record["commitment"] in {"announced", "adopted"} and record["coverage"] in {"whole_institute", "component"}:
            kind = "budget"
        expected[row["id"]] = kind
        assert row["news_kind"] == kind
    selected = {identity for identity, kind in expected.items() if kind}
    assert {identity for g in collect(next_data, limit=None) for identity in g["items"]} == selected
    schema = json.loads((ROOT / "schema/aisi.schema.json").read_text())
    fields = build.table_schema(rows, build.table_descriptions(schema, "events"))["fields"]
    field = next(field for field in fields if field["name"] == "news_kind")
    assert field["type"] == "string" and "newest ten" in field["description"]


def test_news_exclusions():
    data = sample()
    data["institutes"][0].update(people=[{"id": "head", "name": "New head", "role": "Director", "since": "2026-10-01", "source_ids": []}], outputs=[{"lineage": "report", "date": "2026-10-02", "title": "New report", "source_ids": []}])
    data["dataset"]["network_milestones"] = [{"id": "network", "date": "2026-10-03", "type": "founding", "description": "Network founded.", "source_ids": []}]
    data["jurisdictions"][0]["memberships"] = [{"body": "Network", "since": "2026-10-04", "source_ids": []}]
    assert collect(data) == []
    assert all(row["news_kind"] == "" for row in build.event_rows(data))
    data["jurisdictions"][0]["timeline"] = [event("first", "2026-10-02", "first_output", short="First published work credited to the institute.")]
    assert collect(data)[0]["events"] == ["first"]


def news_html(data):
    """Execute the actual News rendering and helpers, with a small DOM and no browser."""
    orders = check_page.card_source_orders(BeautifulSoup("", "html.parser"), data)
    payload = {"groups": data["dataset"]["display"]["developments"], "orders": orders,
               "jurisdictions": [{"id": j["id"], "name": j["name"]} for j in data["jurisdictions"]],
               "sources": [{k: s[k] for k in ("id", "url", "title", "title_en", "publisher") if k in s} for s in data.get("sources", [])]}
    source = (ROOT / "site/index.html").read_text()
    helpers = source[source.index("  var MON ="):source.index("  function saveText(")]
    helpers += source[source.index("  function sepGap("):source.index("  // One footer for full records")]
    helpers += source[source.index("  function fieldCitations("):source.index("  // The Status row value")]
    helpers += source[source.index("  function citations("):source.index("  // None-identified entries")]
    helpers += next(line for line in source.splitlines() if line.startswith("  function andJoin("))
    rendering = source[source.index("    var newsPairs ="):source.index('    $("developments").hidden =')]
    script = r'''
function el(tag, cls, text) {
  return {tag, tagName:tag.toUpperCase(), className:cls||'', dataset:{}, attrs:{}, children:text == null ? [] : [{text}],
    setAttribute(k,v){this.attrs[k]=v;}, appendChild(n){this.children.push(n);return n;},
    get childNodes(){return this.children;},
    set textContent(text){this.children=[{text}];},
    get classList(){return {add:c=>{this.className+=' '+c;}};}};
}
const document={createTextNode:text=>({text}),createDocumentFragment:()=>el('#fragment')};
function esc(x){return String(x).replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('"','&quot;');}
function html(n){if('text' in n)return esc(n.text);if(n.tag==='#fragment')return n.children.map(html).join('');
  return '<'+n.tag+(n.className?' class="'+esc(n.className)+'"':'')+(n.hidden?' hidden':'')+
  Object.entries(n.attrs).map(([k,v])=>' '+k+'="'+esc(v)+'"').join('')+
  Object.entries(n.dataset).map(([k,v])=>' data-'+k.replace(/[A-Z]/g,c=>'-'+c.toLowerCase())+'="'+esc(v)+'"').join('')+
  '>'+n.children.map(html).join('')+'</'+n.tag+'>';}
const payload=JSON.parse(require('fs').readFileSync(0,'utf8'));
const m={display:{developments:payload.groups}}, J=Object.fromEntries(payload.jurisdictions.map(j=>[j.id,j]));
const S=Object.fromEntries(payload.sources.map(s=>[s.id,s])), SOURCE_ORDERS=payload.orders;
const list=el('ul');list.setAttribute('id','development-list');
function $(id){if(id!=='development-list')throw new Error(id);return list;}
function recordSources(){return [];}
function hostOf(url){return new URL(url).host;}
''' + helpers + rendering + "\nconsole.log(html(list));"
    result = subprocess.run(["node", "-e", script], input=json.dumps(payload), text=True, capture_output=True, check=True)
    return result.stdout + '<a id="news-download" download="news.csv" href="data/news.csv"></a>'


def page_entries(data):
    soup = BeautifulSoup(news_html(data), "html.parser")
    for cites in soup.select(".cites"):
        cites.decompose()
    return [tuple(row.select_one(selector).get_text() for selector in (".development-date", ".development-name", ".development-text")) for row in soup.select(".development-row")]


def test_news_gate_independent_oracle(monkeypatch):
    def check(data, markup=None):
        return check_page.check_news(BeautifulSoup(markup or news_html(data), "html.parser"), data)

    data = paired_sample()
    data["sources"] = [{"id": "fixture-source", "url": "https://example.org/agreement", "title": "Synthetic agreement", "publisher": "Test publisher"}]
    for jurisdiction, institute in zip(data["jurisdictions"], data["institutes"], strict=True):
        jurisdiction["institute_id"] = institute["id"]
        agreement, = institute["agreements"]
        agreement["source_ids"] = ["fixture-source"]
        agreement["citations"] = {"date": ["fixture-source"]}
    with_news(data)
    assert check(data) == []
    # Even a matching page and corrupted collector output must fail the oracle.
    for field, value in (("texts", ["Wrong sentence."]), ("order", "wrong-id"), ("events", ["not-a-timeline-event"]), ("items", ["funding:wrong-id"]), ("pairs", [None]), ("pairs", []), ("line_source_ids", [[]])):
        broken = copy.deepcopy(data)
        broken["dataset"]["display"]["developments"][0][field] = value
        assert any("published identities" in e for e in check(broken)), field
    original = news_html(data)
    for mutation in ("state", "date", "sentence", "row-class"):
        soup = BeautifulSoup(original, "html.parser")
        row = soup.select_one(".development-row")
        if mutation == "state":
            row["data-j"] = "XX"
        elif mutation == "date":
            row.select_one(".development-date")["data-date"] = "1900-01-01"
        elif mutation == "sentence":
            row.select_one(".development-text").string = "Wrong fixture sentence."
        else:
            row["class"].append("earlier")
        assert check(data, str(soup)), mutation
    broken = copy.deepcopy(data)
    monkeypatch.setattr(display, "news_matches", lambda rule, record: False)
    broken["dataset"]["display"]["developments"] = collect(broken)
    assert any("published identities" in e for e in check(broken))


def paired_sample():
    data = sample()
    data["jurisdictions"].append({"id": "CA", "name": "Canada", "timeline": []})
    data["institutes"][0]["agreements"] = [agreement(counterparty="Canadian counterparty", participation="named_implementer")]
    data["institutes"].append({"id": "ca-test", "jurisdiction_id": "CA", "short_name": "CA Test", "agreements": [agreement("mirror", counterparty="British counterparty", participation="named_implementer")]})
    data["dataset"]["shared_claims"] = [{"id": "uk-canada", "kind": "agreement", "targets": [
        ["institutes", {"id": "gb-test"}, "agreements", {"id": "agreement"}],
        ["institutes", {"id": "ca-test"}, "agreements", {"id": "mirror"}],
    ]}]
    return data


def with_news(data, *, limit=10):
    data["dataset"]["display"] = {"developments": collect(data, limit=limit)}
    return data


def test_page_pair_once_both_states_in_registration_order():
    data = with_news(paired_sample())
    groups = data["dataset"]["display"]["developments"]
    assert [g["jurisdiction"] for g in groups] == ["CA", "GB"]
    assert [g["pairs"] for g in groups] == [[{"id": "uk-canada", "states": ["GB", "CA"], "names": ["United Kingdom", "Canada"]}]] * 2
    assert page_entries(data) == [("1 Oct 2026", "United Kingdom and Canada", "The two governments sign a text on cooperation naming their institutes.")]
    assert check_page.check_news(BeautifulSoup(news_html(data), "html.parser"), data) == []
    # Registration order, not institute, jurisdiction or alphabetical order.
    data["dataset"]["shared_claims"][0]["targets"].reverse()
    data["institutes"].reverse()
    assert page_entries(with_news(data)) == [("1 Oct 2026", "Canada and United Kingdom", "The two governments sign a text on cooperation naming their institutes.")]


def test_page_pair_counts_as_one_of_ten_and_keeps_cutoff_mirror():
    data = paired_sample()
    data["jurisdictions"][0]["timeline"] = [event(f"status-{n}", f"2026-10-{n:02d}") for n in range(2, 11)]
    # At the cutoff: CA (pair, tenth), DE (eleventh), GB (pair mirror).
    data["jurisdictions"].append({"id": "DE", "name": "Germany", "timeline": [event("excluded")]})
    with_news(data)
    groups = data["dataset"]["display"]["developments"]
    assert len(groups) == 11
    assert [g["jurisdiction"] for g in groups[-2:]] == ["CA", "GB"]
    entries = page_entries(data)
    assert len(entries) == 10
    assert entries[-1] == ("1 Oct 2026", "United Kingdom and Canada", "The two governments sign a text on cooperation naming their institutes.")
    assert all("Germany" not in states for _, states, _ in entries)
    assert check_page.check_news(BeautifulSoup(news_html(data), "html.parser"), data) == []
    feed = [e for e in agent_files.feed_entries(data) if e["kind"] == "news"]
    assert len(feed) == 11
    assert {e["id"].rsplit("news/", 1)[-1] for e in feed if "naming their institutes" in e["summary"]} == {"CA/2026-10-01", "GB/2026-10-01"}
    # The page also caps an unlimited audit collection, independently of the producer.
    assert page_entries(with_news(data, limit=None)) == entries


def test_page_skips_only_duplicate_line_and_keeps_other_news_in_its_item():
    data = paired_sample()
    data["sources"] = [{"id": sid, "url": f"https://example.org/{sid}", "title": sid, "publisher": "Test publisher"}
                       for sid in ("status-source", "gb-agreement-source", "ca-agreement-source")]
    data["jurisdictions"][0]["timeline"] = [event(source_ids=["status-source"], citations={"short": ["status-source"]})]
    for jurisdiction, institute in zip(data["jurisdictions"], data["institutes"], strict=True):
        jurisdiction["institute_id"] = institute["id"]
        sid = jurisdiction["id"].lower() + "-agreement-source"
        institute["agreements"][0].update(source_ids=[sid], citations={"state": [sid]})
    with_news(data)
    assert page_entries(data) == [
        ("1 Oct 2026", "United Kingdom and Canada", "The two governments sign a text on cooperation naming their institutes."),
        ("1 Oct 2026", "United Kingdom", "Institute designated."),
    ]
    soup = BeautifulSoup(news_html(data), "html.parser")
    rows = soup.select(".development-row")
    assert [[a["data-source-id"] for a in row.select(".cn")] for row in rows] == [["ca-agreement-source"], ["status-source"]]
    assert check_page.check_news(soup, data) == []
    # Both download and feed keep the sources of every line in the per-state item.
    groups = {g["jurisdiction"]: g for g in data["dataset"]["display"]["developments"]}
    assert groups["GB"]["line_source_ids"] == [["status-source"], ["gb-agreement-source"]]
    assert groups["GB"]["source_ids"] == groups["GB"]["citations"]["display"] == ["status-source", "gb-agreement-source"]
    assert groups["CA"]["source_ids"] == groups["CA"]["citations"]["display"] == ["ca-agreement-source"]
    feed = {e["id"]: e for e in agent_files.feed_entries(data) if e["kind"] == "news"}
    for group in groups.values():
        content = BeautifulSoup(feed[agent_files.feed_news_id(group)]["content"], "html.parser")
        assert [a["href"] for a in content.select("a")] == [f"https://example.org/{sid}" for sid in group["source_ids"]]
    # A page matching bad per-line metadata must still fail the canonical oracle.
    groups["GB"]["line_source_ids"][0] = groups["GB"]["source_ids"]
    errors = check_page.check_news(BeautifulSoup(news_html(data), "html.parser"), data)
    assert any("published identities" in error for error in errors)
    assert any("citations differ" in error for error in errors)


def test_page_legacy_news_without_line_sources_keeps_group_citations():
    data = sample()
    data["sources"] = [{"id": "status-source", "url": "https://example.org/status", "title": "Status", "publisher": "Test publisher"}]
    data["jurisdictions"][0]["timeline"] = [event(source_ids=["status-source"], citations={"short": ["status-source"]})]
    with_news(data)
    original = news_html(data)
    for group in data["dataset"]["display"]["developments"]:
        del group["line_source_ids"]
    assert news_html(data) == original


def test_page_unregistered_mirror_stays_under_each_state():
    data = paired_sample()
    data["dataset"]["shared_claims"] = []
    with_news(data)
    assert page_entries(data) == [
        ("1 Oct 2026", "Canada", "Governments sign a text on cooperation naming the institute."),
        ("1 Oct 2026", "United Kingdom", "Governments sign a text on cooperation naming the institute."),
    ]
    assert [g["pairs"] for g in collect(data)] == [[None], [None]]
    assert check_page.check_news(BeautifulSoup(news_html(data), "html.parser"), data) == []
    # Optional pairs preserves legacy rendering for an unpaired collection.
    for group in data["dataset"]["display"]["developments"]:
        del group["pairs"]
    assert len(page_entries(data)) == 2


def test_news_kinds_follow_method_weight_before_record_ids():
    data = sample()
    data["jurisdictions"][0]["timeline"] = [event("z-status"), event("a-name", kind="named", short="New name adopted.")]
    data["institutes"][0]["funding"] = [budget("z-budget")]
    data["institutes"][0]["agreements"] = [agreement("a-agreement")]
    group, = collect(data)
    assert group["order"] == "z-status"
    assert group["texts"] == ["Institute designated.", "New name adopted.", "GBP 120m for the institute announced.", "Agreement signed with Developer."]
    assert group["pairs"] == [None] * 4
    assert [rule["kind"] for rule in display.NEWS_KINDS] == ["status", "mandate_or_name", "budget", "agreement"]
    # Owner IDs sort as IDs, not as prefixes of a concatenated events-row ID.
    data["institutes"].append({"id": "gb-test-a", "jurisdiction_id": "GB", "short_name": "UK Other", "funding": [budget("a-budget", amount=10_000_000, display={"native_value": "GBP 10m"})]})
    data["institutes"][0]["funding"][0]["display"] = {"native_value": "GBP 120m"}
    with_news(data)
    assert collect(data)[0]["items"] == ["timeline:GB:z-status", "timeline:GB:a-name", "funding:gb-test:z-budget", "funding:gb-test-a:a-budget", "agreements:gb-test:a-agreement"]
    assert check_page.check_news(BeautifulSoup(news_html(data), "html.parser"), data) == []


@pytest.mark.parametrize("field,value", [
    ("id", "wrong-pair"), ("states", ["CA", "GB"]), ("names", ["Canada", "United Kingdom"]),
])
def test_gate_rejects_corrupt_pair_metadata_even_when_page_matches(field, value):
    data = paired_sample()
    with_news(data)
    for group in data["dataset"]["display"]["developments"]:
        group["pairs"][0][field] = value
    errors = check_page.check_news(BeautifulSoup(news_html(data), "html.parser"), data)
    assert any("published identities" in error for error in errors)


def test_gate_rejects_duplicate_pair_missing_row_and_incomplete_state_label():
    data = with_news(paired_sample())
    original = news_html(data)
    assert check_page.check_news(BeautifulSoup(original, "html.parser"), data) == []
    for mutation, expected in (("duplicate", "exactly once"), ("missing", "ten entries"), ("name", "both states")):
        soup = BeautifulSoup(original, "html.parser")
        pair = soup.select_one('.development-row[data-pairs="uk-canada"]')
        if mutation == "duplicate":
            pair.insert_after(copy.deepcopy(pair))
        elif mutation == "missing":
            pair.decompose()
        else:
            pair.select_one(".development-name").string = data["jurisdictions"][0]["name"]
        assert any(expected in error for error in check_page.check_news(soup, data))


def test_page_empty_news_and_gate():
    data = with_news(sample())
    assert page_entries(data) == []
    assert check_page.check_news(BeautifulSoup(news_html(data), "html.parser"), data) == []


@pytest.mark.parametrize("origin", ["timeline", "agreements", "funding"])
def test_authored_news_overrides_fallback_with_its_own_citations(origin):
    data = sample()
    data["jurisdictions"][0]["institute_id"] = "gb-test"
    owner = data["jurisdictions"][0] if origin == "timeline" else data["institutes"][0]
    record = {"timeline": event, "agreements": agreement, "funding": budget}[origin]()
    record["display"] = {"native_value": "GBP 120m"}
    owner[origin] = [record]
    before, = collect(data)
    text = "The government announces additional support for the institute to evaluate advanced models over the next four years."
    record.update(news=text, citations={"news": ["news-source"], "description": ["old-source"]})
    data["sources"] = [{"id": sid, "url": f"https://example.org/{sid}", "title": sid, "publisher": "Test"} for sid in ("news-source", "old-source")]
    with_news(data)
    after, = collect(data)
    assert after["texts"] == [text]
    assert after["line_source_ids"] == [["news-source"]]
    assert after["items"] == before["items"]
    assert agent_files.feed_news_id(after) == agent_files.feed_news_id(before)
    assert agent_files.feed_entries(data)[0]["summary"] == text
    assert check_page.check_news(BeautifulSoup(news_html(data), "html.parser"), data) == []
    assert display.check_news_lines(data) == []  # no 12-word cap on news


@pytest.mark.parametrize("origin", ["timeline", "agreements", "funding"])
def test_news_requires_exact_excerpt_and_is_authored_reader_text(dataset, origin):
    data = copy.deepcopy(dataset)
    collection = "jurisdictions" if origin == "timeline" else "institutes"
    record = data[collection][0][origin][0]
    record["news"] = "The institute receives additional support."
    pointer = f"/{collection}/0/{origin}/0/news"
    supports = data["sources"][0]["excerpts"][0]["supports"]
    supports[:] = [p for p in supports if p != pointer]  # the shared fixture binds its own News lines
    errors = method_checks.check_method_rules(data)
    assert f"required claim has no excerpt: {pointer}" in errors
    data["sources"][0]["excerpts"][0]["supports"].append(pointer)
    assert method_checks.check_method_rules(data) == []
    schema = authored_schema(json.loads((ROOT / "schema/aisi.schema.json").read_text()))
    props = schema["properties"][collection]["items"]["properties"]
    node = props[origin]["items"]
    Draft202012Validator(schema).evolve(schema=node).validate(record)
    assert "bound to a quote" in schema["$defs"]["news"]["description"]
    record["news"] = "The institute receives support\u2014and evaluates models."
    assert any(pointer in error and "U+2014" in error for error in reader_text.check_reader_text(data))
    record["news"] = "The institute  receives support."
    assert any(pointer in error and "double spaces" in error for error in reader_text.check_reader_text(data))


@pytest.mark.parametrize("left,right,invalid", [(None, None, False), ("The governments cooperate.", None, True), (None, "The governments cooperate.", True), ("The governments cooperate.", "The governments sign a statement.", True), ("The governments cooperate.", "The governments cooperate.", False)])
def test_paired_news_both_or_neither_and_identical(left, right, invalid):
    data = paired_sample()
    for institute, text in zip(data["institutes"], (left, right), strict=True):
        if text is not None:
            institute["agreements"][0]["news"] = text
    errors = shared_claims.check_shared_claims(data)
    assert any("news must be present on both sides" in error for error in errors) == invalid
    if left is not None and not invalid:
        with_news(data)
        assert page_entries(data) == [("1 Oct 2026", "United Kingdom and Canada", left)]
        assert check_page.check_news(BeautifulSoup(news_html(data), "html.parser"), data) == []


@pytest.mark.parametrize("name", ["United Kingdom", "UK Test"])
def test_generated_news_rejects_its_state_or_institute_name(name):
    data = sample()
    data["institutes"][0]["agreements"] = [agreement(counterparty=name)]
    assert any("generated News line repeats" in error and name in error for error in display.check_news_lines(data))
    data["institutes"][0]["agreements"][0]["news"] = f"The institute adopts the name {name}."
    assert display.check_news_lines(data) == []  # an authored rename is a review point


@pytest.mark.parametrize("kind", ["budget", "agreement"])
def test_generated_name_check_catches_template_regressions(monkeypatch, kind):
    data = paired_sample()
    data["institutes"][0]["funding"] = [budget()]
    name = "Canada" if kind == "agreement" else "UK Test"
    rules = tuple({**rule, "text": lambda *args: f"Support for {name}."} if rule["kind"] == kind else rule for rule in display.NEWS_KINDS)
    monkeypatch.setattr(display, "NEWS_KINDS", rules)
    errors = display.check_news_lines(data)
    assert any("gb-test" in error and "generated News line repeats" in error for error in errors)


@pytest.mark.parametrize("text,glossary,valid", [
    ("An XYZ initiative begins.", {}, False),
    ("Unrelated words (XYZ) begin an initiative.", {}, False),
    ("The ABC-based programme begins.", {"ABC": "Advanced Benchmarking Centre"}, False),
    ("EuroHPC begins work.", {"HPC": "High Performance Computing"}, False),
    ("Department for Science, Innovation and Technology (DSIT) announces support.", {}, True),
    ("Artificial-Intelligence Safety Institute (AISI) begins work.", {}, True),
    ("Center for Advancing Innovation and Standards for Super Intelligence (CAISSI) is the new name.", {}, True),
    ("AI Safety Institute (AISI) begins work.", {}, True),
    ("The institute joins the ABC programme.", {"ABC": "Advanced Benchmarking Centre"}, True),
    ("The institute joins the ABC programme.", {"ABC": {"GB": "Advanced Benchmarking Centre"}}, True),
    ("The institute joins the ABC programme.", {"ABC": {"CA": "Advanced Benchmarking Centre"}}, False),
    ("Xylophone Yearly Zone (XYZ) joins an XYZ programme.", {}, False),
    ("The institute receives GBP 120m.", {}, True),
    ("The institute receives ZZZ 120m.", {}, False),
    ("The institute joins KRiBSI.", {"KRiBSI": "Korea Research institute for Better Software Institutions"}, True),
    ("The institute joins KRiBSIx.", {"KRiBSI": "Korea Research institute for Better Software Institutions"}, False),
])
def test_news_abbreviation_rule(text, glossary, valid):
    data = sample()
    data["dataset"].update(glossary=glossary, exchange_rates=[{"currency": "GBP"}])
    data["jurisdictions"][0]["timeline"] = [event(news=text)]
    assert (display.check_news_lines(data) == []) == valid


def test_news_checks_historical_lines_and_timeline_fallback():
    data = sample()
    data["jurisdictions"][0]["timeline"] = [event(str(n), f"2026-10-{n:02d}") for n in range(1, 13)]
    data["jurisdictions"][0]["timeline"][0]["short"] = "IA360 begins."
    assert len(collect(data)) == 10
    assert display.check_news_lines(data) == ["timeline:GB:1: unexplained News abbreviation 'IA360'"]


@pytest.mark.parametrize("text", ["", " Leading space.", "Two sentences. Another sentence.", "Two\nlines."])
def test_news_is_one_sentence(text):
    data = sample()
    data["jurisdictions"][0]["timeline"] = [event(news=text)]
    assert any("one nonblank sentence" in error for error in display.check_news_lines(data))


def test_events_news_column_and_api_records(dataset, tmp_path):
    data = copy.deepcopy(dataset)
    for owner in data["jurisdictions"] + data["institutes"]:
        for origin in ("timeline", "agreements", "funding"):
            for record in owner.get(origin, []):
                record.pop("news", None)  # only the lines this test sets
    supports = data["sources"][0]["excerpts"][0]["supports"]
    supports[:] = [p for p in supports if not p.endswith("/news")]
    for origin in ("timeline", "agreements", "funding"):
        owner = data["jurisdictions"][0] if origin == "timeline" else data["institutes"][0]
        owner[origin][0]["news"] = f"The institute reports a {origin} change."
    data["institutes"][0]["funding"][0]["decision_date"] = "2026-10-01"
    published = publish(data, strict=False, rereads={})
    columns, rows = events_csv(published, tmp_path)
    assert "news" in columns
    assert {row["origin_kind"]: row["news"] for row in rows if row["news"]} == {
        origin: f"The institute reports a {origin} change." for origin in ("timeline", "agreements", "funding")}
    assert all(row["news"] == "" for row in rows if row["origin_kind"] not in ("timeline", "agreements", "funding"))
    fields = build.table_schema(rows, build.table_descriptions(json.loads((ROOT / "schema/aisi.schema.json").read_text()), "events"))["fields"]
    field = next(field for field in fields if field["name"] == "news")
    assert field["type"] == "string" and "empty when absent" in field["description"]
    api = agent_files.api_files(published)
    root = agent_files.api_root(published)
    assert api[f"{root}/jurisdictions/GB.json"]["record"]["timeline"][0]["news"] == data["jurisdictions"][0]["timeline"][0]["news"]
    for origin in ("agreements", "funding"):
        assert api[f"{root}/institutes/gb-test.json"]["record"][origin][0]["news"] == data["institutes"][0][origin][0]["news"]


def test_news_required_for_shown_items_only():
    """Every item the page's News list shows needs an authored line; older items keep the template."""
    data = sample()
    data["jurisdictions"][0]["timeline"] = [event(str(n), f"2026-10-{n:02d}", news="The institute is designated.") for n in range(1, 12)]
    data["institutes"][0]["agreements"] = [agreement(date="2026-09-01")]
    assert display.check_news_required(data) == []  # the agreement is the twelfth item, past the ten rows
    data["jurisdictions"][0]["timeline"][10] = event("11", "2026-10-11", "first_output")
    assert display.check_news_required(data) == []  # generated from the output list, so no authored line
    data["jurisdictions"][0]["timeline"][5].pop("news")
    assert display.check_news_required(data) == [
        f"timeline:GB:6: shown in News without an authored news line bound to a quote; write one ({display.NEWS_RULE})"]
    data["jurisdictions"][0]["timeline"][5]["news"] = "The institute is designated."
    data["institutes"][0]["agreements"][0]["date"] = "2026-10-20"
    assert display.check_news_required(data) == [
        f"agreements:gb-test:agreement: shown in News without an authored news line bound to a quote; write one ({display.NEWS_RULE})"]
    data["institutes"][0]["agreements"][0]["date"] = "2026-09-01"
    data["jurisdictions"][0]["timeline"][2]["news"] = " ".join(["word"] * display.NEWS_MAX_WORDS)
    assert display.check_news_required(data) == []  # at the limit
    data["jurisdictions"][0]["timeline"][2]["news"] += " more"
    assert display.check_news_required(data) == [
        f"timeline:GB:3: News line has 41 words, over the limit of 40; shorten it ({display.NEWS_RULE})"]
    data["jurisdictions"][0]["timeline"][2]["date"] = "2026-01-03"  # past the ten rows: not shown, not counted
    assert display.check_news_required(data) == []


def test_news_required_holds_for_the_dataset():
    data = json.loads((ROOT / "data/aisi.json").read_text())
    assert display.check_news_required(data) == []
    doc = (ROOT / display.NEWS_RULE.split("#")[0]).read_text()
    assert doc.count("\n## News lines\n") == 1
    # The worked examples quote current lines; a reworded line updates its example.
    examples = re.findall(r'^- [^:\n]+: "(.+?)" Quotes?: ', doc, re.M)
    records = {(collection, owner["id"], origin, r["id"]): r for collection in ("jurisdictions", "institutes")
               for owner in data[collection] for origin in ("timeline", "funding", "agreements") for r in owner.get(origin, [])}
    bound = {}  # each authored line and the excerpts bound to it
    for source in data["sources"]:
        for excerpt in source.get("excerpts", []):
            for pointer in excerpt.get("supports", []):
                if pointer[-1] == "news" and len(pointer) == 5:
                    line = records[(pointer[0], pointer[1]["id"], pointer[2], pointer[3]["id"])]["news"]
                    bound.setdefault(line, []).append(excerpt["text"])
    assert set(examples) <= set(bound)
    section = doc.split("## News lines")[1]
    worked, pairs = section.split("Examples, with")[1].split("Before and after:")
    for item in re.findall(r'^- [^:\n]+: (".+)$', worked, re.M):
        line, *quotes = re.findall(r'"([^"]+)"', item)
        assert quotes and [q for q in quotes if not any(q in text for text in bound[line])] == [], line
    # Each before-and-after pair ends on a current line; its before line is no longer used.
    afters = re.findall(r'^- [^\n]*Before: "([^"]+)"[^\n]*After: "([^"]+)"$', pairs, re.M)
    assert all(after in bound and before not in bound for before, after in afters)
