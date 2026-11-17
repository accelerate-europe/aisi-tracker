"""News acceptance: fixed next-data expectations and small independent rule examples."""

import copy
import csv
import json
from pathlib import Path
import subprocess

import pytest
from bs4 import BeautifulSoup
from jsonschema import Draft202012Validator

from scripts import agent_files, build, check_page, display
from scripts.published import publish


ROOT = Path(__file__).resolve().parents[1]
STATUS_KINDS = ("commitment", "proposed", "designated", "launched", "first_output", "exploration_started", "withdrawn", "closed")
SEPTEMBER = [
    ("2026-11-12", "United Kingdom", "Agreement signed between UK AISI and KI Norge."),
    ("2026-11-12", "Norway", "Agreement signed between UK AISI and KI Norge."),
    ("2026-10-24", "United Kingdom", "GBP 120m for UK AISI announced."),
    ("2026-10-13", "Canada", "Agreement announced between UK AISI and CAISI."),
    ("2026-10-13", "United Kingdom", "Agreement announced between UK AISI and CAISI."),
    ("2026-10-09", "United Kingdom", "Agreement announced between UK AISI and J-AISI."),
    ("2026-10-09", "Japan", "Agreement announced between UK AISI and J-AISI."),
    ("2026-10-07", "Norway", "Budget proposes NOK 30m and an AI safety mandate for KI Norge."),
    ("2026-10-06", "Australia", "Agreement announced with Anthropic."),
    ("2026-09-30", "United States", "Earliest archived NIST page found uses the new name CAISSI."),
    ("2026-09-28", "South Korea", "Agreement signed with Kakao."),
    ("2026-09-25", "Netherlands", "Cabinet says AI safety institute exploration under way; report due year end."),
    ("2026-09-21", "Spain", "IA360 plan commits to an AI Security Institute within AESIA."),
]
PAGE_SEPTEMBER = [
    ("12 Nov 2026", "United Kingdom and Norway", "Agreement signed between UK AISI and KI Norge."),
    ("24 Oct 2026", "United Kingdom", "GBP 120m for UK AISI announced."),
    ("13 Oct 2026", "United Kingdom and Canada", "Agreement announced between UK AISI and CAISI."),
    ("9 Oct 2026", "United Kingdom and Japan", "Agreement announced between UK AISI and J-AISI."),
    ("7 Oct 2026", "Norway", "Budget proposes NOK 30m and an AI safety mandate for KI Norge."),
    ("6 Oct 2026", "Australia", "Agreement announced with Anthropic."),
    ("30 Sep 2026", "United States", "Earliest archived NIST page found uses the new name CAISSI."),
    ("28 Sep 2026", "South Korea", "Agreement signed with Kakao."),
    ("25 Sep 2026", "Netherlands", "Cabinet says AI safety institute exploration under way; report due year end."),
    ("21 Sep 2026", "Spain", "IA360 plan commits to an AI Security Institute within AESIA."),
]
# Hand-checked against the old eight-kind rule on next 04e90fc. These are historical
# identities, even when the ten-item window now pushes them out of the page/feed.
OLD_NEWS = [
    ("NO", "2026-10-07", ["no-1"], [SEPTEMBER[7][2]]),
    ("NL", "2026-09-25", ["nl-2"], [SEPTEMBER[11][2]]),
    ("ES", "2026-09-21", ["es-1"], [SEPTEMBER[12][2]]),
    ("DE", "2026-08-31", ["de-2", "de-3"], ["Ministries establish AISI Deutschland, initially within BSI and BNetzA.", "AISI Deutschland starts work."]),
    ("AU", "2026-08-10", ["au-first-output"], ["First published work credited to the institute."]),
    ("MY", "2026-07-28", ["my-2026-07-28-designated"], ["Ministry confirms Malaysia AI Safety Institute established."]),
    ("NL", "2026-07-03", ["nl-4"], ["International AI Strategy reaffirms exploration of a Dutch AI safety institute."]),
    ("KE", "2026-07", ["ke-2026-draft-policy"], ["Draft policy proposes Kenya AI Safety Institute; consultation opens."]),
    ("NL", "2026-06-10", ["nl-0"], ["Ministry-commissioned TNO study of an AI safety institute option is published."]),
    ("DE", "2026-06-08", ["de-1"], ["National Security Council decides to found an AI security institute."]),
]


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
    return {"id": identity, "date": "2026-10-01", "state": "signed", "counterparty": "Developer", "source_ids": [], **extra}


def test_news_status_milestones_unchanged(next_data):
    groups = collect(next_data, limit=None)
    by_key = {(g["jurisdiction"], g["date"], g["order"]): g for g in groups}
    for jid, day, events, texts in OLD_NEWS:
        group = by_key[jid, day, events[0]]
        assert group["events"] == events
        assert group["texts"][:len(texts)] == texts
        tail = day if len(day) == 10 else day + "/" + events[0]
        assert agent_files.feed_news_id(group) == f"tag:aisi.accelerate-europe.org,2026:news/{jid}/{tail}"
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
        assert groups[0]["texts"] == [f"GBP 120m for UK Test {commitment}."]
        assert groups[0]["events"] == []
    # Missing date and a null date both exclude the budget.
    del data["institutes"][0]["funding"][0]["decision_date"]
    assert collect(data) == []


def test_news_budget_keeps_card_amount_qualifiers_and_native_currency():
    data = sample()
    data["institutes"][0]["funding"] = [budget(amount_qualifier="at_least")]
    assert collect(data)[0]["texts"] == ["at least GBP 120m for UK Test announced."]
    data["institutes"][0]["funding"][0].update(amount=125_000_000, amount_qualifier="about")
    assert collect(data)[0]["texts"] == ["about GBP 130m for UK Test announced."]


@pytest.mark.parametrize("state", ["signed", "announced", "pledged"])
def test_news_agreement_rule(state):
    data = sample()
    data["institutes"][0]["agreements"] = [agreement(state=state, counterparty="Full Canadian counterparty")]
    data["jurisdictions"].append({"id": "CA", "name": "Canada", "timeline": []})
    data["institutes"].append({"id": "ca-test", "jurisdiction_id": "CA", "name": "Canadian full name", "short_name": "CA Test", "agreements": [agreement("mirror", state=state, counterparty="Full British counterparty")]})
    assert [g["texts"] for g in collect(data)] == [[f"Agreement {state} with Full British counterparty."], [f"Agreement {state} with Full Canadian counterparty."]]
    data["dataset"]["shared_claims"] = [{"id": "uk-canada", "kind": "agreement", "targets": [["institutes", {"id": "gb-test"}, "agreements", {"id": "agreement"}], ["institutes", {"id": "ca-test"}, "agreements", {"id": "mirror"}]]}]
    assert [g["texts"] for g in collect(data)] == [[f"Agreement {state} between UK Test and CA Test."]] * 2
    # Institute array order does not change registered party order or row order.
    data["institutes"].reverse()
    assert [g["texts"] for g in collect(data)] == [[f"Agreement {state} between UK Test and CA Test."]] * 2
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
    assert group["texts"] == ["Institute designated.", "GBP 120m for UK Test announced.", "Agreement signed with Developer."]
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
    assert {identity for g in collect(next_data) for identity in g["items"]} < selected
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


def test_news_since_september(next_data):
    all_groups = collect(next_data, limit=None)
    assert [(g["date"], g["name"], text) for g in all_groups if g["date"] >= "2026-09-01" for text in g["texts"]] == SEPTEMBER
    groups = next_data["dataset"]["display"]["developments"]
    assert [(g["date"], g["name"], text) for g in groups for text in g["texts"]] == SEPTEMBER
    assert page_entries(next_data) == PAGE_SEPTEMBER
    feed = [e for e in agent_files.feed_entries(next_data) if e["kind"] == "news"]
    assert {e["id"] for e in feed} == {agent_files.feed_news_id(g) for g in groups}
    assert {e["summary"] for e in feed} == {" ".join(g["texts"]) for g in groups}


def news_html(data):
    """Execute the actual News rendering and helpers, with a small DOM and no browser."""
    orders = check_page.card_source_orders(BeautifulSoup("", "html.parser"), data)
    payload = {"groups": data["dataset"]["display"]["developments"], "orders": orders,
               "jurisdictions": [{"id": j["id"], "name": j["name"]} for j in data["jurisdictions"]],
               "sources": [{k: s[k] for k in ("id", "url", "title", "title_en", "publisher") if k in s} for s in data.get("sources", [])]}
    source = (ROOT / "site/index.html").read_text()
    helpers = source[source.index("  var MON ="):source.index("  function saveText(")]
    helpers += source[source.index("  function separated("):source.index("  // One footer for full records")]
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
    return result.stdout + '<a id="events-download" download="events.csv" href="data/events.csv"></a>'


def page_entries(data):
    soup = BeautifulSoup(news_html(data), "html.parser")
    for cites in soup.select(".cites"):
        cites.decompose()
    return [tuple(row.select_one(selector).get_text() for selector in (".development-date", ".development-name", ".development-text")) for row in soup.select(".development-row")]


def test_news_gate_independent_oracle(next_data, monkeypatch):
    def check(data, markup=None):
        return check_page.check_news(BeautifulSoup(markup or news_html(data), "html.parser"), data)

    assert check(next_data) == []
    # Even a matching page and corrupted collector output must fail the oracle.
    for field, value in (("texts", ["Wrong sentence."]), ("order", "wrong-id"), ("events", ["not-a-timeline-event"]), ("items", ["funding:wrong-id"]), ("pairs", [None]), ("pairs", []), ("line_source_ids", [[]])):
        broken = copy.deepcopy(next_data)
        broken["dataset"]["display"]["developments"][0][field] = value
        assert any("published identities" in e for e in check(broken))
    for old, new in (('data-j="GB"', 'data-j="US"'), ('data-date="2026-11-12"', 'data-date="2026-11-11"'), ("Agreement signed", "Agreement pledged"), ('class="development-row earlier"', 'class="development-row"')):
        assert check(next_data, news_html(next_data).replace(old, new, 1))
    broken = copy.deepcopy(next_data)
    monkeypatch.setattr(display, "news_matches", lambda rule, record: False)
    broken["dataset"]["display"]["developments"] = collect(broken)
    assert any("published identities" in e for e in check(broken))


def paired_sample():
    data = sample()
    data["jurisdictions"].append({"id": "CA", "name": "Canada", "timeline": []})
    data["institutes"][0]["agreements"] = [agreement(counterparty="Canadian counterparty")]
    data["institutes"].append({"id": "ca-test", "jurisdiction_id": "CA", "short_name": "CA Test", "agreements": [agreement("mirror", counterparty="British counterparty")]})
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
    assert page_entries(data) == [("1 Oct 2026", "United Kingdom and Canada", "Agreement signed between UK Test and CA Test.")]
    assert check_page.check_news(BeautifulSoup(news_html(data), "html.parser"), data) == []
    # Registration order, not institute, jurisdiction or alphabetical order.
    data["dataset"]["shared_claims"][0]["targets"].reverse()
    data["institutes"].reverse()
    assert page_entries(with_news(data)) == [("1 Oct 2026", "Canada and United Kingdom", "Agreement signed between CA Test and UK Test.")]


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
    assert entries[-1] == ("1 Oct 2026", "United Kingdom and Canada", "Agreement signed between UK Test and CA Test.")
    assert all("Germany" not in states for _, states, _ in entries)
    assert check_page.check_news(BeautifulSoup(news_html(data), "html.parser"), data) == []
    feed = [e for e in agent_files.feed_entries(data) if e["kind"] == "news"]
    assert len(feed) == 11
    assert {e["id"].rsplit("news/", 1)[-1] for e in feed if "between" in e["summary"]} == {"CA/2026-10-01", "GB/2026-10-01"}
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
        ("1 Oct 2026", "United Kingdom and Canada", "Agreement signed between UK Test and CA Test."),
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
        ("1 Oct 2026", "Canada", "Agreement signed with British counterparty."),
        ("1 Oct 2026", "United Kingdom", "Agreement signed with Canadian counterparty."),
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
    assert group["texts"] == ["Institute designated.", "New name adopted.", "GBP 120m for UK Test announced.", "Agreement signed with Developer."]
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


def test_gate_rejects_duplicate_pair_missing_row_and_incomplete_state_label(next_data):
    original = news_html(next_data)
    for mutation, expected in (("duplicate", "exactly once"), ("missing", "ten entries"), ("name", "both states")):
        soup = BeautifulSoup(original, "html.parser")
        pair = soup.select_one('.development-row[data-pairs="agreement-gb-no-statement-2026"]')
        if mutation == "duplicate":
            pair.insert_after(copy.deepcopy(pair))
        elif mutation == "missing":
            soup.select(".development-row")[-1].decompose()
        else:
            pair.select_one(".development-name").string = "United Kingdom"
        assert any(expected in error for error in check_page.check_news(soup, next_data))


def test_page_empty_news_and_gate():
    data = with_news(sample())
    assert page_entries(data) == []
    assert check_page.check_news(BeautifulSoup(news_html(data), "html.parser"), data) == []
