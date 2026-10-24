# /// script
# requires-python = ">=3.11"
# dependencies = ["jsonschema[format-nongpl]>=4.23", "pyyaml>=6"]
# ///
"""Validate data/aisi.json and build dist/: the page plus JSON, CSV, datapackage.json and the per-record API.

Run from the repo root: uv run scripts/build.py [--out DIR]
Exits non-zero on any schema or consistency error, so a broken dataset never ships.
Outputs are written to a temporary folder beside the output directory first and only replace it
when everything succeeded. No mode writes a tracked file except --update-readme, which regenerates
the README citation, current-count, agents and definition blocks, CITATION.cff and .zenodo.json; --check writes nothing at all unless --report-dir is given.
"""

import argparse
import csv
import datetime as dt
import hashlib
import html
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from decimal import Decimal
from pathlib import Path

# Validation must not write into the checkout, including bytecode caches; set before any import below.
sys.dont_write_bytecode = True

from jsonschema import Draft202012Validator  # noqa: E402

if __package__:
    from . import published as fact_pipeline
    from .references import authored_schema, excerpt_identity, materialise_references, pointer_ancestors
    from .published import INTERVAL_KEYS, budget_ratios, comparable_funding, annual_eur_estimate as annual_eur_estimate, known_activity as known_activity, output_activity_kind as output_activity_kind, publish, prepared_facts, timeline_status as timeline_status, eur_per_unit as eur_per_unit, significant as significant
    from .shared_claims import check_shared_claims, shared_claim_coverage  # type: ignore[import-not-found]
    from .method_copy import full_method_page
    from .method_checks import check_method_rules  # type: ignore[import-not-found]
    from .source_origins import source_origin_report, write_source_origin_report
    from .agent_files import LICENSES, doi_url, api_files, api_landing_html, api_root, citation_cff, zenodo_json, head_links, json_ld_script, llms_txt, robots_txt, sitemap_xml, orientation_html, orientation_markdown, definition_markdown, public_url_errors
    from .release_checks import check_metadata, check_readme_licence, check_readme_doi, check_markdown, generated_block, generated_citation, generated_counts, check_launch_wording, write_citation, check_instrument_status_dates, instrument_status_warnings, dates_after_as_of
else:  # Direct script execution.
    import published as fact_pipeline
    from references import authored_schema, excerpt_identity, materialise_references, pointer_ancestors
    from published import INTERVAL_KEYS, budget_ratios, comparable_funding, annual_eur_estimate as annual_eur_estimate, known_activity as known_activity, output_activity_kind as output_activity_kind, publish, prepared_facts, timeline_status as timeline_status, eur_per_unit as eur_per_unit, significant as significant
    from shared_claims import check_shared_claims, shared_claim_coverage
    from method_copy import full_method_page
    from method_checks import check_method_rules
    from source_origins import source_origin_report, write_source_origin_report
    from agent_files import LICENSES, doi_url, api_files, api_landing_html, api_root, citation_cff, zenodo_json, head_links, json_ld_script, llms_txt, robots_txt, sitemap_xml, orientation_html, orientation_markdown, definition_markdown, public_url_errors
    from release_checks import check_metadata, check_readme_licence, check_readme_doi, check_markdown, generated_block, generated_citation, generated_counts, check_launch_wording, write_citation, check_instrument_status_dates, instrument_status_warnings, dates_after_as_of

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data" / "aisi.json"
SCHEMA = ROOT / "schema" / "aisi.schema.json"
SITE = ROOT / "site" / "index.html"
# Title and description of every published data file, in download order; the page lists files from datapackage.json.
FILES = {
    "jurisdictions": ("States and territories", "status, dates and region of every state and territory, and the EU"),
    "institutes": ("Institutes", "host, form, mandate and website of each institute"),
    "events": ("All events", "every dated government act, agreement, output and milestone in one table"),
    "timeline": ("Status timeline", "the dated government acts that set each status"),
    "funding": ("Funding", "published budgets, their period and a rough EUR figure a year"),
    "exchange_rates": ("Exchange rates", "the ECB rates used for the EUR figures"),
    "staff": ("Staff", "headcounts, technical staff and targets, dated"),
    "people": ("Leadership", "heads and leads of each institute"),
    "national_partners": ("National partners", "the national bodies each institute works with, what each is, how they work together and the kind of relation"),
    "outputs": ("Outputs", "publications, tools and datasets credited to each institute"),
    "agreements": ("Agreements", "commitments made with AI developers, governments and other institutes"),
    "known_activity": ("Known activity", "the earliest published work and joint testing of each institute"),
    "network_milestones": ("Network milestones", "founding, meetings and renaming of the international network"),
    "related_bodies": ("Related bodies", "AI Act authorities and other bodies not counted as institutes"),
    "closest_bodies": ("Closest candidates", "for states with none identified, the nearest body and what it lacks"),
    "sources": ("Sources", "every source with publisher, date and archive link"),
    "source_excerpts": ("Source excerpts", "the quoted passages and the values they support"),
    "aisi": ("Full dataset", "every record with its sources, confidence and check dates"),
    "aisi-schema": ("Schema", "defines every field"),
    "shared-claim-coverage": ("Consistency checks", "which repeated figures are checked against each other"),
}
DIST = ROOT / "dist"
ROSTER = ROOT / "data" / "reference" / "roster.csv"

# The 32 states the Europe page counts: EU-27, Iceland, Liechtenstein, Norway, UK, Switzerland.
EUROPE = set("AT BE BG HR CY CZ DK EE FI FR DE GR HU IE IT LV LT LU MT NL PL PT RO SK SI ES SE IS LI NO GB CH".split())



def fail(errors: list[str]) -> None:
    for e in errors:
        print(f"error: {e}", file=sys.stderr)
    sys.exit(1)


def export_dataset(data: dict, *, strict: bool | None = None) -> dict:
    """Materialise all facts, reference positions and display decisions once."""
    return publish(data, strict=strict)


def walk_dates(obj, path: str = ""):
    """Yield (path, value) for every string field whose name marks it as a date."""
    date_keys = {"as_of", "published", "accessed", "last_checked", "date", "status_since", "established", "announced", "from", "since", "until", "decision_date", "credited_from", "instrument_status_checked"}
    if isinstance(obj, dict):
        for k, v in obj.items():
            p = f"{path}/{k}"
            if k == "status_definitions":
                continue
            if k in date_keys and isinstance(v, str):
                yield p, v
            else:
                yield from walk_dates(v, p)
    elif isinstance(obj, list):
        for n, v in enumerate(obj):
            yield from walk_dates(v, f"{path}/{n}")


def valid_partial_date(s: str) -> bool:
    fmt = {4: "%Y", 7: "%Y-%m", 10: "%Y-%m-%d"}.get(len(s))
    try:
        return bool(fmt) and dt.datetime.strptime(s, fmt) is not None
    except ValueError:
        return False


def check_excerpts(d: dict) -> list[str]:
    errors = []
    for source in d["sources"]:
        for excerpt in source.get("excerpts", []):
            where = f"source {source['id']} excerpt {excerpt_identity(excerpt)}"
            if not source.get("language"):
                errors.append(f"{where}: source language is required for an excerpt")
            elif source["language"].split("-")[0] != "en" and not excerpt.get("gloss", "").strip():
                errors.append(f"{where}: non-English excerpt needs an English gloss")
            for pointer in excerpt["supports"]:
                try:
                    nodes = pointer_ancestors(d, pointer)
                except (ValueError, KeyError, IndexError) as exc:
                    errors.append(f"{where}: pointer {pointer!r} does not resolve ({exc})")
                    continue
                # Identifier citations are separate from evidence for status and other claims.
                tokens = pointer[1:].split("/")
                if (
                    len(tokens) == 3
                    and tokens[0] == "jurisdictions"
                    and tokens[2] == "related_bodies"
                ):
                    errors.append(
                        f"{where}: pointer {pointer!r} must identify an individual related body"
                    )
                    continue
                if len(tokens) >= 4 and tokens[0] == "jurisdictions" and tokens[2] == "related_bodies" and not nodes[4].get("source_ids"):
                    errors.append(f"{where}: pointer {pointer!r} related body needs its own source_ids")
                    continue
                field = tokens[2] if len(tokens) >= 3 and tokens[0] in {"jurisdictions", "institutes"} else ""
                citation_key = "identifier_source_ids" if field in {"iso3", "m49", "wikidata", "identifier_notes", "identifier_source_ids"} else "source_ids"
                cited = next((node for node in reversed(nodes) if isinstance(node, dict) and citation_key in node), None)
                if cited is None or source["id"] not in cited[citation_key]:
                    errors.append(f"{where}: pointer {pointer!r} nearest cited record does not cite this source")
    return errors


def check_funding_totals(d: dict, complete: bool = False) -> list[str]:
    """Validate comparison metadata and derived sums, not chart selection."""
    errors = []
    confidence = {"low": 0, "medium": 1, "high": 2}
    for institute in d["institutes"]:
        records = institute.get("funding", [])
        for index, total in enumerate(records):
            where = f"{institute['id']} funding[{index}]"
            annual = total["kind"] in {"annual", "multi_year"} and total.get("annual_amount") is not None
            if complete and annual and not total.get("coverage"):
                errors.append(f"{where}: annual comparison needs coverage")
            period = total.get("annual_period")
            if period and period["start"] > period["end"]:
                errors.append(f"{where}: invalid annual period")
            if annual and total.get("coverage") == "whole_institute" and not period:
                errors.append(f"{where}: whole-institute comparison needs annual_period")
            if "sum_of" not in total:
                continue
            try:
                indices = fact_pipeline.component_indices(records, index)
            except ValueError as exc:
                errors.append(f"{where}: {exc}")
                continue
            components = [records[n] for n in indices]
            if total.get("coverage") != "whole_institute" or any(c.get("coverage") != "component" for c in components):
                errors.append(f"{where}: sum needs whole-institute total and component records")
            if not period or any(c.get(key) != total.get(key) for c in components for key in ("currency", "annual_period", "commitment")):
                errors.append(f"{where}: sum components must share currency, period and commitment")
            if total["confidence"] != min((c["confidence"] for c in components), key=confidence.get):
                errors.append(f"{where}: total must retain the lowest component confidence")
            if set(total["source_ids"]) != {sid for c in components for sid in c["source_ids"]}:
                errors.append(f"{where}: total must cite the component source union")
    return errors


def check_figure_sources(d: dict) -> list[str]:
    """A low-confidence budget or staff figure names its source type; no other figure does; a calculated sum is our estimate."""
    errors = []
    for institute in d["institutes"]:
        for kind in ("funding", "staff"):
            for record in institute.get(kind, []):
                where = f"{institute['id']} {kind} {record.get('id', '?')}"
                if (record["confidence"] == "low") != ("figure_source" in record):
                    errors.append(f"{where}: figure_source is required for low confidence and only then")
                elif "sum_of" in record and record["confidence"] == "low" and record["figure_source"] != "our_estimate":
                    errors.append(f"{where}: a low-confidence sum of components is our_estimate")
    return errors


def check_exchange_rates(d: dict) -> list[str]:
    """One cited period and basis, covering funding currencies, with positive primary conversion inputs."""
    errors = []
    entries = d["dataset"].get("exchange_rates", [])
    rates = {r["currency"]: r for r in entries}
    if len(rates) != len(entries):
        errors.append("exchange_rates: duplicate currency")
    currencies = {f["currency"] for i in d["institutes"] for f in i.get("funding", []) if f.get("currency")}
    missing = (currencies - {"EUR"}) - rates.keys()
    if missing:
        errors.append(f"exchange_rates: missing currencies {sorted(missing)}")
    signatures = {(r["period"]["start"], r["period"]["end"], r["basis"], tuple(sorted(r["source_ids"]))) for r in entries}
    if len(signatures) > 1:
        errors.append("exchange_rates: all entries must use one period, basis and source set")
    sources = {s["id"]: s for s in d["sources"]}
    for r in entries:
        where = f"exchange_rates {r['currency']}"
        start, end = r["period"]["start"], r["period"]["end"]
        if not (len(start) == len(end) == 10 and valid_partial_date(start) and valid_partial_date(end) and start <= end <= d["dataset"]["as_of"]):
            errors.append(f"{where}: invalid or future period")
        if not r["basis"].strip():
            errors.append(f"{where}: basis must not be blank")
        if not all(sources.get(s, {}).get("type") == "primary" for s in r["source_ids"]):
            errors.append(f"{where}: rates need primary sources")
        if any(not Decimal(str(r[k])).is_finite() or r[k] <= 0 for k in ("units_per_eur",)):
            errors.append(f"{where}: rates must be finite and positive")
    as_of = d["dataset"]["as_of"]
    # The ECB publishes a year's annual average only in January, so even a 31 December as_of uses the year before.
    year = int(as_of[:4]) - 1
    if any(r["period"] != {"start": f"{year}-01-01", "end": f"{year}-12-31"} for r in entries):
        errors.append(f"exchange_rates: period must be calendar {year}, the latest complete year before as_of")
    for rule in d["dataset"].get("method", []):
        text = rule["text"]
        if "annual-average" in text and f"annual-average rates for {year}" not in text:
            errors.append(f"exchange_rates: dataset.method/{rule['id']} must say 'annual-average rates for {year}', the rate period")
    if "EUR" in rates:
        errors.append("exchange_rates: EUR is the base and has no row")
    return errors


def check_budget_ratio_inputs(d: dict) -> list[str]:
    """A state's compared (charted) budget must yield both ratios: the population and GDP it divides by are recorded and usable (rule budget-ratios)."""
    rates = {row["currency"]: eur_per_unit(row["units_per_eur"]) for row in d["dataset"].get("exchange_rates", [])}
    jurisdictions = {j["id"]: j for j in d["jurisdictions"]}
    errors = []
    for institute in sorted(d["institutes"], key=lambda i: i["id"]):
        jurisdiction = jurisdictions.get(institute["jurisdiction_id"])
        if not jurisdiction or jurisdiction.get("kind") != "state":
            continue
        n = comparable_funding(institute.get("funding", []), d["dataset"]["as_of"])
        if n is None:
            continue
        try:
            ratios = budget_ratios(institute["funding"][n], jurisdiction, rates)
        except ValueError:  # a budget currency without a rate: check_exchange_rates names it
            continue
        where = f"institute {institute['id']} compared funding {institute['funding'][n]['id']}"
        if ratios["eur_per_person"] is None:
            errors.append(f"{where}: no budget per person; jurisdiction {jurisdiction['id']} needs a positive population")
        if ratios["share_of_gdp"] is None:
            errors.append(f"{where}: no share of GDP; jurisdiction {jurisdiction['id']} needs a positive gdp in the budget currency, EUR or a currency in dataset.exchange_rates")
    return errors


def check_consistency(d: dict, require_roster: bool = False, complete: bool = False, *, strict: bool | None = None, materialised: bool = False, authored: dict | None = None) -> list[str]:
    """Checks JSON Schema cannot express: unique ids, references, links in both directions, scope."""
    errors = check_exchange_rates(d)
    if strict is None:
        strict = fact_pipeline.AUTHORING_STRICT
    authored = d if authored is None else authored
    legacy = not strict and all("status" in j for j in authored["jurisdictions"])
    try:
        if not materialised:
            d = materialise_references(d) if legacy else prepared_facts(d, strict=strict)
    except (ValueError, KeyError, IndexError, TypeError, ArithmeticError) as exc:
        return errors + [f"authored facts or references: {exc}"]
    src_ids = [s["id"] for s in d["sources"]]
    jur = {j["id"]: j for j in d["jurisdictions"]}
    inst = {i["id"]: i for i in d["institutes"]}
    for name, ids in (("source", src_ids), ("jurisdiction", [j["id"] for j in d["jurisdictions"]]), ("institute", [i["id"] for i in d["institutes"]])):
        dupes = sorted({x for x in ids if ids.count(x) > 1})
        if dupes:
            errors.append(f"duplicate {name} ids: {dupes}")
    known = set(src_ids)
    src_type = {s["id"]: s["type"] for s in d["sources"]}
    used = set()

    def refs(where: str, ids: list[str]) -> None:
        for sid in ids:
            used.add(sid)
            if sid not in known:
                errors.append(f"{where}: unknown source id '{sid}'")

    for p, v in walk_dates(d):
        if not valid_partial_date(v):
            errors.append(f"{p}: '{v}' is not a real date")

    jids = {j["id"] for j in d["jurisdictions"]}
    for term, ex in d["dataset"].get("glossary", {}).items():
        if isinstance(ex, dict) and set(ex) - jids:
            errors.append(f"glossary '{term}': unknown jurisdiction ids {sorted(set(ex) - jids)}")
    for n, ov in enumerate(d["dataset"].get("other_overviews", [])):
        refs(f"dataset.other_overviews[{n}]", ov["source_ids"])
    for n, rate in enumerate(d["dataset"].get("exchange_rates", [])):
        refs(f"dataset.exchange_rates[{n}]", rate["source_ids"])
    milestones = d["dataset"].get("network_milestones", [])
    if len({m["id"] for m in milestones}) != len(milestones):
        errors.append("duplicate network milestone ids")
    for milestone in milestones:
        refs(f"network milestone {milestone['id']}", milestone["source_ids"])
        if milestone["body"] not in d["dataset"]["vocabularies"]["membership_body"]:
            errors.append(f"network milestone {milestone['id']}: unknown membership body")
    wikidata = [
        record["wikidata"]
        for collection in ("jurisdictions", "institutes")
        for record in d[collection]
        if record.get("wikidata") is not None
    ]
    if len(wikidata) != len(set(wikidata)):
        errors.append("duplicate wikidata identifiers across jurisdictions and institutes")
    for collection in ("jurisdictions", "institutes"):
        for key in ("iso3", "m49"):
            values = [r[key] for r in d[collection] if r.get(key) is not None]
            if len(values) != len(set(values)):
                errors.append(f"{collection}: duplicate {key} identifiers")
        for record in d[collection]:
            refs(f"{collection} {record['id']} identifiers", record.get("identifier_source_ids", []))
            check = record["check"]
            refs(f"{collection} {record['id']} independent sources", check.get("independent_source_ids", []))
            refs(f"{collection} {record['id']} governance sources", check.get("governance_source_ids", []))
            for query in check.get("searches", []):
                refs(f"{collection} {record['id']} search", query.get("source_ids", []))
            if check.get("explicit_absence"):
                refs(f"{collection} {record['id']} explicit absence", [check["explicit_absence"]["source_id"]])
            if any(record.get(k) is not None for k in ("iso3", "m49", "wikidata")) and not record.get("identifier_source_ids"):
                errors.append(f"{collection} {record['id']}: non-null identifiers need identifier_source_ids")
    if not (legacy and not materialised):  # legacy input: checked on the prepared facts below
        errors += check_budget_ratio_inputs(d)
    for j in d["jurisdictions"]:
        refs(f"jurisdiction {j['id']}", j["source_ids"])
        if "closest_body" in j:
            refs(f"jurisdiction {j['id']} closest_body", j["closest_body"]["source_ids"])
        for n, body in enumerate(j.get("related_bodies", [])):
            refs(f"jurisdiction {j['id']} related_bodies[{n}]", body.get("source_ids", []))
        if "population" in j:
            refs(f"jurisdiction {j['id']} population", j["population"]["source_ids"])
        if "gdp" in j:
            refs(f"jurisdiction {j['id']} gdp", j["gdp"]["source_ids"])
        if "region" in j:
            refs(f"jurisdiction {j['id']} region", j["region"]["source_ids"])
        for n, ms in enumerate(j.get("memberships", [])):
            refs(f"jurisdiction {j['id']} memberships[{n}]", ms["source_ids"])
            if ms["body"] not in d["dataset"]["vocabularies"]["membership_body"]:
                errors.append(f"jurisdiction {j['id']}: membership body '{ms['body']}' is not in dataset.vocabularies.membership_body")
        iid = j.get("institute_id")
        if iid:
            i = inst.get(iid)
            if not i:
                errors.append(f"jurisdiction {j['id']}: unknown institute '{iid}'")
            elif i["jurisdiction_id"] != j["id"]:
                errors.append(f"jurisdiction {j['id']}: institute '{iid}' belongs to {i['jurisdiction_id']}")
        if j["status"] != "none_identified":
            if j["check"]["method"] != "individual":
                errors.append(f"jurisdiction {j['id']}: a positive status needs an individual check")
            if not any(src_type.get(s) == "primary" for s in j["source_ids"]):
                errors.append(f"jurisdiction {j['id']}: a positive status needs at least one primary source")
        if j["status"] in ("established", "planned", "proposed") and not iid:
            errors.append(f"jurisdiction {j['id']}: status '{j['status']}' needs an institute record")
    for i in d["institutes"]:
        refs(f"institute {i['id']}", i["source_ids"])
        j = jur.get(i["jurisdiction_id"])
        if not j:
            errors.append(f"institute {i['id']}: unknown jurisdiction '{i['jurisdiction_id']}'")
        elif j.get("institute_id") != i["id"]:
            errors.append(f"institute {i['id']}: jurisdiction {j['id']} does not link back to it")
        for k in ("funding", "staff", "people", "names", "outputs"):
            for n, item in enumerate(i.get(k, [])):
                refs(f"institute {i['id']} {k}[{n}]", item.get("source_ids", []))
        for n, funding in enumerate(i.get("funding", [])):
            for allocation in funding.get("annual_profile", []):
                refs(f"institute {i['id']} annual profile", allocation["source_ids"])
            if funding.get("annual_amount") is not None and (not funding.get("currency") or not funding.get("annual_basis", "").strip()):
                errors.append(f"institute {i['id']} funding[{n}]: annual_amount needs currency and a non-empty annual_basis")
        for n, name in enumerate(i.get("names", [])):
            for previous in i["names"][:n]:
                precision = min(len(name["from"]), len(previous["from"]))
                if name["from"][:precision] < previous["from"][:precision]:
                    errors.append(f"institute {i['id']}: names not in date order at index {n}")
                    break
    europe = {j["id"] for j in d["jurisdictions"] if j["europe_scope"]}
    if europe != EUROPE:
        errors.append(f"europe_scope set differs from the 32 states: missing {sorted(EUROPE - europe)}, extra {sorted(europe - EUROPE)}")
    for jid in europe & set(jur):
        j = jur[jid]
        if j["kind"] != "state" or "population" not in j or "eu_member" not in j:
            errors.append(f"jurisdiction {jid}: europe_scope states need kind 'state', a population and eu_member")
    if require_roster:
        rows = {r["id"]: r for r in csv.DictReader(ROSTER.open(encoding="utf-8"))}
        roster = set(rows)
        have = {j["id"] for j in d["jurisdictions"] if j["kind"] != "supranational"}
        for jid in roster & have:
            reg, r = jur[jid].get("region", {}), rows[jid]
            if (reg.get("id"), reg.get("subregion_id")) != (r["m49_region_id"], r["m49_subregion_id"]):
                errors.append(f"jurisdiction {jid}: region differs from the roster (M49 {r['m49_region_id']}/{r['m49_subregion_id']})")
        if roster - have:
            errors.append(f"roster jurisdictions missing: {sorted(roster - have)}")
        if have - roster:
            errors.append(f"jurisdictions not in the roster: {sorted(have - roster)}")
    events = set(d["dataset"]["vocabularies"].get("timeline_event", {}))
    for j in d["jurisdictions"]:
        tl = j.get("timeline", [])
        ids = [e["id"] for e in tl]
        if len(ids) != len(set(ids)):
            errors.append(f"jurisdiction {j['id']}: duplicate timeline event ids")
        for n, e in enumerate(tl):
            refs(f"jurisdiction {j['id']} timeline[{n}]", e["source_ids"])
            if e["event"] not in events:
                errors.append(f"jurisdiction {j['id']}: timeline event '{e['event']}' is not in dataset.vocabularies.timeline_event")
            if e["event"] == "withdrawn":
                target = next((x for x in tl[:n] if x["id"] == e.get("withdraws")), None)
                if target is None or target["event"] == "withdrawn":
                    errors.append(f"jurisdiction {j['id']}: withdrawn event {e['id']} must name an earlier event that is not itself a withdrawal")
            for prev in tl[:n]:
                k = min(len(e["date"]), len(prev["date"]))
                if e["date"][:k] < prev["date"][:k]:
                    errors.append(f"jurisdiction {j['id']}: timeline not in date order at {e['id']} (before {prev['id']})")
                    break
    for i in d["institutes"]:
        for k in ("legal_basis", "placement"):
            if i.get(k):
                refs(f"institute {i['id']} {k}", i[k]["source_ids"])
        for n, a in enumerate(i.get("agreements", [])):
            refs(f"institute {i['id']} agreements[{n}]", a["source_ids"])
        for k in ("head", "reports_to", "national_partners"):
            if i.get(k):
                refs(f"institute {i['id']} {k}", i[k].get("source_ids", []))
        for n, e in enumerate(i.get("national_partners", {}).get("entries", [])):
            refs(f"institute {i['id']} national_partners.entries[{n}]", e["source_ids"])
        for key, span in (i.get("intervals") or {}).items():
            if span is not None and span["days_min"] > span["days_max"]:
                errors.append(f"institute {i['id']}: intervals.{key} days_min exceeds days_max")
        testing, access = i.get("testing_capacity", {}), i.get("model_access", {})
        refs(f"institute {i['id']} testing_capacity", testing.get("source_ids", []))
        for mode in testing.get("modes", []):
            refs(f"institute {i['id']} testing_capacity.modes {mode['code']}", mode["source_ids"])
        for key in ("access_events", "developer_agreements"):
            for item in access.get(key, []):
                refs(f"institute {i['id']} model_access.{key} {item['id']}", item["source_ids"])
        if access.get("legal_power"):
            refs(f"institute {i['id']} model_access.legal_power", access["legal_power"]["source_ids"])
        refs(f"institute {i['id']} model_access", access.get("source_ids", []))
        refs(f"institute {i['id']} staffing_model", i.get("staffing_model", {}).get("source_ids", []))
    if complete:
        for j in d["jurisdictions"]:
            if "status_confidence" not in j:
                errors.append(f"jurisdiction {j['id']}: status_confidence missing")
            if j["status"] != "none_identified":
                if not j.get("timeline"):
                    errors.append(f"jurisdiction {j['id']}: timeline missing")
                inst = next((i for i in d["institutes"] if i["id"] == j.get("institute_id")), None)
                required_fields: tuple[str, ...] = ("mandate_short", "placement")
                if inst and inst["status"] == "established":
                    required_fields += ("legal_basis",)
                for k in required_fields:
                    if inst and not inst.get(k):
                        errors.append(f"institute {inst['id']}: {k} missing")
    seen_urls: dict[str, str] = {}
    for src in d["sources"]:
        u = src["url"].rstrip("/")
        if u in seen_urls:
            errors.append(f"sources {seen_urls[u]} and {src['id']} share the URL {src['url']}; cite one record")
        seen_urls.setdefault(u, src["id"])
    unused = sorted(known - used)
    if unused:
        errors.append(f"sources never cited: {unused}")
    errors.extend(check_metadata(d))
    errors.extend(check_instrument_status_dates(d))
    if d["changelog"] and d["changelog"][-1]["version"] != d["dataset"]["version"]:
        errors.append(f"latest changelog version {d['changelog'][-1]['version']} differs from dataset.version {d['dataset']['version']}")
    errors.extend(check_funding_totals(d, complete or require_roster))
    errors.extend(check_figure_sources(d))
    errors.extend(check_excerpts(materialise_references(authored) if legacy else d))
    origins = {"EU"} | {j["id"] for j in d["jurisdictions"]} | set(d["dataset"]["vocabularies"].get("source_origin", {}))
    for source in d["sources"]:
        if source.get("origin") is not None and source["origin"] not in origins:
            errors.append(f"{source['id']}: unknown source origin {source['origin']}")
    errors.extend(check_method_rules(d, authored=authored))
    errors.extend(check_shared_claims(materialise_references(authored) if legacy else d))
    if legacy and not materialised:
        # Transitional snapshots are validated before removing stored copies.
        # This adapter disappears from production when fact_pipeline.AUTHORING_STRICT is true.
        try:
            prepared = prepared_facts(authored, strict=False)
            errors += check_budget_ratio_inputs(prepared)
            for institute in prepared["institutes"]:
                for activity in institute.get("operating", []):
                    refs(f"{institute['id']} generated operating", activity["source_ids"])
            for jurisdiction in prepared["jurisdictions"]:
                for event in jurisdiction.get("timeline", []):
                    if event["event"] == "first_output":
                        refs(f"{jurisdiction['id']} generated first_output", event["source_ids"])
        except (ValueError, KeyError, IndexError, TypeError, ArithmeticError) as exc:
            errors.append(f"legacy fact migration: {exc}")
    return errors


def join(xs) -> str:
    return "; ".join(xs)


def val(x):
    return "" if x is None else x


def interval_bound(institute: dict, key: str, end: str):
    span = (institute.get("intervals") or {}).get(key)
    return "" if span is None else span[f"days_{end}"]


def flag_columns(i: dict) -> dict:
    """institutes.csv columns for testing_capacity, model_access and staffing_model; access events, agreements and the legal power stay in aisi.json."""
    testing, access, staffing = i.get("testing_capacity", {}), i.get("model_access", {}), i.get("staffing_model", {})
    modes = testing.get("modes", [])
    power = access.get("legal_power", {})
    parts = [*access.get("access_events", []), *([power] if power else []), *access.get("developer_agreements", [])]

    def searches(flag):
        return join([f"{s['date']} ({s['language']}): {s['query']}" for s in flag.get("searches", [])])

    return {
        "testing_capacity": join([m["code"] for m in modes]) if modes else testing.get("state", ""),
        "testing_capacity_source_ids": join(list(dict.fromkeys([*testing.get("source_ids", []), *(sid for m in modes for sid in m["source_ids"])]))),
        "testing_capacity_notes": testing.get("notes", ""),
        "testing_capacity_searches": searches(testing),
        "model_access": access.get("value", ""),
        "model_access_latest_event": access.get("latest_event", ""),
        "model_access_events": join([f"{e['date']} {e['developer']} {e['model']} ({e['kind']})" for e in access.get("access_events", [])]),
        "model_access_legal_power": f"{power['act']}, {power['provision']}" if power else "",
        "model_access_agreement_ids": join([a["agreement_id"] for a in access.get("developer_agreements", [])]),
        "model_access_source_ids": join(list(dict.fromkeys([*(sid for part in parts for sid in part["source_ids"]), *access.get("source_ids", [])]))),
        "model_access_notes": access.get("notes", ""),
        "model_access_searches": searches(access),
        "staffing_model": staffing.get("code", ""),
        "staffing_model_source_ids": join(staffing.get("source_ids", [])),
        "staffing_model_notes": staffing.get("notes", ""),
        "staffing_model_searches": searches(staffing),
    }


def event_rows(d: dict) -> list[dict]:
    """Merge the selected dated event categories with version-specific provenance."""
    jurisdictions = {j["id"]: j for j in d["jurisdictions"]}
    institutes = {i["id"]: i for i in d["institutes"]}
    urls = {s["id"]: s["url"] for s in d["sources"]}
    rows = []

    def add(record, date, kind, description, origin, pointer, jid="", iid=""):
        if not date:
            return
        identity = f"{jid}:{record['id']}" if origin == "timeline" else (
            f"{jid}:{record['body']}" if origin == "memberships" else (
                f"{iid}:{record['id']}" if iid and record.get("id") else record.get("id", "")
            )
        )
        if not identity or identity.startswith("/"):
            raise ValueError(f"events {origin}: record needs a stable origin_id")
        rows.append({
            "id": f"{origin}:{identity}", "jurisdiction_id": jid, "jurisdiction": jurisdictions.get(jid, {}).get("name", ""),
            "institute_id": iid, "institute": institutes.get(iid, {}).get("name", ""),
            "type": kind, "date": date, "description": description, "short": record.get("short", "") if origin in ("timeline", "network_milestones") else "",
            "origin_kind": origin, "origin_id": identity, "source_pointer": pointer,
            "withdraws": f"{jid}:{record['withdraws']}" if origin == "timeline" and record.get("withdraws") else "",
            "scheduled": date > d["dataset"]["as_of"][:len(date)],
            "source_ids": join(record["source_ids"]), "source_urls": join(urls[sid] for sid in record["source_ids"]),
        })

    for n, j in enumerate(d["jurisdictions"]):
        for k, event in enumerate(j.get("timeline", [])):
            add(event, event["date"], event["event"], event["description"], "timeline", f"/jurisdictions/{n}/timeline/{k}", j["id"], j.get("institute_id") or "")
        for k, membership in enumerate(j.get("memberships", [])):
            add(membership, membership.get("since"), "membership", f"Membership of {membership['body']}", "memberships", f"/jurisdictions/{n}/memberships/{k}", j["id"])
    for n, i in enumerate(d["institutes"]):
        # Names are not rows: the timeline event each name links to carries the name change once.
        for collection in ("people", "agreements", "funding"):
            for k, record in enumerate(i.get(collection, [])):
                if collection == "people":
                    date, kind, description = record.get("since"), "appointment", f"{record['name']}: {record['role']}"
                elif collection == "agreements":
                    date, kind = record.get("date"), "agreement"
                    description = f"{record['state'].capitalize()}: {record['counterparty']}" + (f". {record['scope']}" if record.get("scope") else "")
                else:
                    date, kind, description = record.get("decision_date"), "funding_decision", record["description"]
                add(record, date, kind, description, collection, f"/institutes/{n}/{collection}/{k}", i["jurisdiction_id"], i["id"])
    for n, milestone in enumerate(d["dataset"].get("network_milestones", [])):
        add(milestone, milestone["date"], f"network_{milestone['type']}", milestone["description"], "network_milestones", f"/dataset/network_milestones/{n}")
    return sorted(rows, key=lambda row: (row["date"], row["origin_kind"], row["id"]))


def ratio_columns(f: dict, j: dict) -> dict:
    """funding.csv budget ratios with the year and sources of the denominator behind each; empty off the compared record."""
    used = f.get("ratio_denominators", {})
    population = j.get("population", {}) if f.get("eur_per_person_est") is not None else {}
    gdp = j.get("gdp", {}) if f.get("share_of_gdp_est") is not None else {}
    return {
        "eur_per_person_est": val(f.get("eur_per_person_est")),
        "population_year": val(used.get("population", {}).get("year") if population else None),
        "population_source_ids": join(population.get("source_ids", [])),
        "share_of_gdp_est": val(f.get("share_of_gdp_est")),
        "gdp_year": val(used.get("gdp", {}).get("year") if gdp else None),
        "gdp_source_ids": join(gdp.get("source_ids", [])),
    }


def tables(d: dict) -> dict[str, list[dict]]:
    """Flatten an already published document; never derive facts in a consumer."""
    url = {s["id"]: s["url"] for s in d["sources"]}

    accessed = {s["id"]: s["accessed"] for s in d["sources"]}
    rate_units = {r["currency"]: r["units_per_eur"] for r in d["dataset"].get("exchange_rates", [])} | {"EUR": 1}

    def links(ids):
        return join(url[i] for i in ids)

    jurisdictions = {j["id"]: j for j in d["jurisdictions"]}

    jur = [{
        "id": j["id"], "name": j["name"], "kind": j["kind"], "europe_scope": j["europe_scope"],
        "iso3": val(j.get("iso3")), "m49": val(j.get("m49")), "wikidata": val(j.get("wikidata")),
        "identifier_source_ids": join(j.get("identifier_source_ids", [])), "identifier_notes": j.get("identifier_notes", ""),
        "eu_member": val(j.get("eu_member")), "region_id": j.get("region", {}).get("id", ""), "region": j.get("region", {}).get("name", ""),
        "subregion_id": j.get("region", {}).get("subregion_id", ""), "subregion": j.get("region", {}).get("subregion", ""),
        "memberships": join(ms["body"] for ms in j.get("memberships", [])),
        "memberships_source_ids": join(sorted({x for ms in j.get("memberships", []) for x in ms["source_ids"]})),
        "population": val(j.get("population", {}).get("value")),
        "population_year": val(j.get("population", {}).get("year")),
        "population_source_ids": join(j.get("population", {}).get("source_ids", [])),
        "gdp": val(j.get("gdp", {}).get("value")), "gdp_currency": j.get("gdp", {}).get("currency", ""),
        "gdp_year": val(j.get("gdp", {}).get("year")), "gdp_source_ids": join(j.get("gdp", {}).get("source_ids", [])),
        "status": j["status"], "status_since": j.get("status_since", ""), "status_confidence": j.get("status_confidence", ""), "institute_id": j.get("institute_id") or "",
        "summary": j["summary"],
        "related_bodies": join(f"{b['name']} ({b['role']})" for b in j.get("related_bodies", [])),
        "next_check": j.get("next_check", {}).get("date", ""), "next_check_reason": j.get("next_check", {}).get("reason", ""),
        "last_checked": j["check"]["last_checked"], "last_checked_basis": j["check"].get("last_checked_basis", ""), "check_method": j["check"]["method"],
        "source_ids": join(j["source_ids"]), "source_urls": links(j["source_ids"]), "notes": j.get("notes", ""),
    } for j in d["jurisdictions"]]
    inst = [{
        "id": i["id"], "name": i["name"], "name_local": i.get("name_local", ""), "acronym": i.get("acronym", ""), "short_name": i["short_name"],
        "wikidata": val(i.get("wikidata")), "identifier_source_ids": join(i.get("identifier_source_ids", [])), "identifier_notes": i.get("identifier_notes", ""),
        "jurisdiction_id": i["jurisdiction_id"], "status": i["status"], "announced": i.get("announced") or "",
        "established": i.get("established", ""),
        **{f"{key}_days_{end}": interval_bound(i, key, end) for key in INTERVAL_KEYS for end in ("min", "max")},
        "host": i["host"], "legal_form": i["legal_form"],
        "mandate": i["mandate"], "mandate_short": i.get("mandate_short", ""), "website": i.get("website") or "",
        "legal_basis": i.get("legal_basis", {}).get("type", ""), "legal_basis_state": i.get("legal_basis", {}).get("state", ""),
        "legal_basis_source_ids": join(i.get("legal_basis", {}).get("source_ids", [])),
        "host_type": i.get("placement", {}).get("host_type", ""), "host_type_source_ids": join(i.get("placement", {}).get("source_ids", [])),
        "head_state": i.get("head", {}).get("state", ""), "head_person_id": i.get("head", {}).get("person_id", ""), "head_source_ids": join(i.get("head", {}).get("source_ids", [])),
        "reports_to_state": i.get("reports_to", {}).get("state", ""), "reports_to": i.get("reports_to", {}).get("name", ""), "reports_to_en": i.get("reports_to", {}).get("name_en", ""),
        "reports_to_source_ids": join(i.get("reports_to", {}).get("source_ids", [])),
        "national_partners_state": i.get("national_partners", {}).get("state", ""),
        "head_notes": i.get("head", {}).get("notes", ""), "reports_to_notes": i.get("reports_to", {}).get("notes", ""), "national_partners_notes": i.get("national_partners", {}).get("notes", ""),
        "name_history": join(f"{n['name']} (from {n['from']}; {', '.join(n['source_ids'])})" for n in i.get("names", [])),
        **flag_columns(i),
        "staffing_profile": i.get("staffing_profile", ""), "last_checked": i["check"]["last_checked"], "last_checked_basis": i["check"].get("last_checked_basis", ""), "check_method": i["check"]["method"],
        "source_ids": join(i["source_ids"]), "source_urls": links(i["source_ids"]), "notes": i.get("notes", ""),
    } for i in d["institutes"]]
    fund = [{
        "id": f.get("id", ""), "institute_id": i["id"], "kind": f["kind"], "commitment": f["commitment"], "amount": val(f.get("amount")),
        "amount_qualifier": f.get("amount_qualifier", ""),
        "currency": f.get("currency") or "", "years": val(f.get("years")), "period": f.get("period", ""),
        "coverage": f.get("coverage", ""), "derived_total": "sum_of" in f, "sum_of": join(f.get("sum_of", [])),
        "annual_period_start": f.get("annual_period", {}).get("start", ""), "annual_period_end": f.get("annual_period", {}).get("end", ""),
        "decision_date": f.get("decision_date", ""), "annual_amount": val(f.get("annual_amount")), "annual_basis": f.get("annual_basis", ""), "annual_basis_code": f.get("annual_basis_code", ""),
        "eur_per_year_est": val(f.get("eur_per_year_est")), "units_per_eur": val(rate_units.get(f.get("currency")) if f.get("eur_per_year_est") is not None else None),
        **ratio_columns(f, jurisdictions[i["jurisdiction_id"]]),
        "funding_source": f.get("funding_source", ""), "description": f["description"], "confidence": f["confidence"], "figure_source": f.get("figure_source", ""),
        "source_ids": join(f["source_ids"]), "source_urls": links(f["source_ids"]), "notes": f.get("notes", ""),
    } for i in d["institutes"] for f in i.get("funding", [])]
    staff = [{
        "id": s.get("id", ""), "institute_id": i["id"], "as_of": s["as_of"], "total": val(s.get("total")), "total_qualifier": s.get("total_qualifier", ""),
        "technical": val(s.get("technical")), "technical_qualifier": s.get("technical_qualifier", ""), "target": val(s.get("target")),
        "description": s.get("description", ""), "confidence": s["confidence"], "figure_source": s.get("figure_source", ""),
        "source_ids": join(s["source_ids"]), "source_urls": links(s["source_ids"]),
    } for i in d["institutes"] for s in i.get("staff", [])]
    people = [{
        "id": p.get("id", ""), "institute_id": i["id"], "name": p["name"], "role": p["role"], "since": p.get("since", ""),
        "until": p.get("until", ""),
        "background": p.get("background", ""), "confidence": p.get("confidence", ""),
        "source_ids": join(p["source_ids"]), "source_urls": links(p["source_ids"]),
    } for i in d["institutes"] for p in i.get("people", [])]
    evidence = [{
        "id": e["kind"], "institute_id": i["id"], "kind": e["kind"], "date": e["date"], "lineage": e["lineage"],
        "source_ids": join(e["source_ids"]), "source_urls": links(e["source_ids"]),
    } for i in d["institutes"] for e in i.get("operating", [])]
    timeline = [{
        "id": e["id"], "jurisdiction_id": j["id"], "event_id": e["id"], "date": e["date"], "event": e["event"], "instrument": e["instrument"],
        "description": e["description"], "short": e.get("short", ""), "withdraws": e.get("withdraws", ""), "source_ids": join(e["source_ids"]), "source_urls": links(e["source_ids"]),
    } for j in d["jurisdictions"] for e in j.get("timeline", [])]
    agreements = [{
        "id": a.get("id", ""), "institute_id": i["id"], "counterparty": a["counterparty"], "counterparty_type": a["counterparty_type"], "state": a["state"], "participation": a.get("participation", ""), "date": a.get("date", ""), "scope": a.get("scope", ""),
        "source_ids": join(a["source_ids"]), "source_urls": links(a["source_ids"]),
    } for i in d["institutes"] for a in i.get("agreements", [])]
    partners = [{
        "id": e["id"], "institute_id": i["id"], "name": e["name"], "name_en": e.get("name_en", ""), "acronym": e.get("acronym", ""),
        "what": e["what"], "relation": e["relation"], "cyber_agency": val(e.get("cyber_agency")), "how": e["how"],
        "source_ids": join(e["source_ids"]), "source_urls": links(e["source_ids"]),
    } for i in d["institutes"] for e in i.get("national_partners", {}).get("entries", [])]
    src = [{
        "id": s["id"], "url": s["url"], "title": s["title"], "title_en": s.get("title_en", ""), "publisher": s["publisher"], "type": s["type"],
        "origin": s.get("origin") or "", "published": s.get("published", ""), "accessed": s["accessed"], "language": s.get("language", ""),
        "archive_url": s.get("archive_url") or "", "access_note": s.get("access_note", ""),
        "snapshot_retrieved": s.get("snapshot", {}).get("retrieved", ""), "snapshot_sha256": s.get("snapshot", {}).get("sha256", ""),
        "snapshot_text_sha256": s.get("snapshot", {}).get("text_sha256", ""), "snapshot_text_scheme": s.get("snapshot", {}).get("text_scheme", ""),
        "legal_text": val(s.get("legal_text")), "instrument_status_checked": s.get("instrument_status_checked", ""),
    } for s in d["sources"]]
    outputs = [{
        "id": output["lineage"], "institute_id": i["id"], "date": output["date"], "credited_from": output.get("credited_from", ""),
        "credit_institute_id": output.get("credit", {}).get("institute_id", ""), "credit_date": output.get("credit", {}).get("date", ""), "type": output["type"], "title": output["title"],
        "lineage": output["lineage"], "attribution": output["attribution"], "qualifies_as": output["qualifies_as"], "qualification_note": output["qualification_note"],
        "source_ids": join(output["source_ids"]), "source_urls": links(output["source_ids"]),
    } for i in d["institutes"] for output in i.get("outputs", [])]
    milestones = [{
        "id": m["id"], "body": m["body"], "type": m["type"], "date": m["date"], "description": m["description"], "short": m.get("short", ""),
        "source_ids": join(m["source_ids"]), "source_urls": links(m["source_ids"]),
    } for m in d["dataset"].get("network_milestones", [])]
    excerpts = [{
        "id": excerpt_identity(excerpt), "source_id": source["id"], "source_url": source["url"], "text": excerpt["text"], "gloss": excerpt.get("gloss", ""),
        "supports": json.dumps(excerpt["supports"], ensure_ascii=False), "url": excerpt.get("url", ""), "accessed": excerpt.get("accessed", ""),
    } for source in d["sources"] for excerpt in source.get("excerpts", [])]
    bodies = [{
        "id": body.get("id", ""), "jurisdiction_id": j["id"], "name": body["name"], "name_en": body.get("name_en", ""), "kind": body["kind"], "role": body["role"], "counts_as_aisi": val(body.get("counts_as_aisi")),
        "designation_state": body.get("designation_state", ""), "authority_roles": join(body.get("authority_roles", [])),
        "source_ids": join(body.get("source_ids", [])), "source_urls": links(body.get("source_ids", [])),
    } for j in d["jurisdictions"] for body in j.get("related_bodies", [])]
    closest = [{
        "id": j["id"], "jurisdiction_id": j["id"], **{key: j["closest_body"][key] for key in ("name", "stage", "gap", "note")},
        "source_ids": join(j["closest_body"]["source_ids"]), "source_urls": links(j["closest_body"]["source_ids"]),
    } for j in d["jurisdictions"] if "closest_body" in j]
    rates = [{
        "id": r["currency"], "currency": r["currency"], "units_per_eur": r["units_per_eur"], "eur_per_unit": r["eur_per_unit"],
        "basis": r["basis"], "period_start": r["period"]["start"], "period_end": r["period"]["end"],
        "source_ids": join(r["source_ids"]), "source_urls": links(r["source_ids"]),
        "source_accessed": join(accessed[i] for i in r["source_ids"]),
    } for r in d["dataset"].get("exchange_rates", [])]
    result = {"jurisdictions": jur, "institutes": inst, "funding": fund, "exchange_rates": rates, "staff": staff, "people": people, "known_activity": evidence, "national_partners": partners, "timeline": timeline, "agreements": agreements, "sources": src, "outputs": outputs, "network_milestones": milestones, "source_excerpts": excerpts, "related_bodies": bodies, "closest_bodies": closest, "events": event_rows(d)}
    check_table_identities(result)
    return result


def check_table_identities(exported: dict[str, list[dict]]) -> None:
    """Every exported row exposes a stable identity, never a positional pointer."""
    for table, rows in exported.items():
        for row in rows:
            if not isinstance(row.get("id"), str) or not row["id"].strip():
                raise ValueError(f"{table}: exported row needs a nonempty stable id")
            for key, identity in row.items():
                if (key == "id" or key.endswith("_id")) and isinstance(identity, str):
                    if identity.startswith("/") or any(
                        f"/{root}/" in identity
                        for root in ("jurisdictions", "institutes", "sources", "dataset")
                    ):
                        raise ValueError(f"{table}: {key} must not contain a JSON pointer")


# Data dictionary for the CSVs. Columns not listed here are described by the JSON Schema field of the same name.
FIELD_DOCS = {
    "institute_id": "Institute record ID; see institutes.csv or institutes[].id in aisi.json.",
    "derived_total": "True for a calculated sum of component funding records; do not add this row to its components.",
    "sum_of": "Permanent funding component IDs within this institute, separated by '; '.",
    "id": "Stable row identity, scoped by institute_id, jurisdiction_id or source_id where present. Natural-key aliases: outputs use lineage; known activity uses kind; exchange rates use currency; closest candidates use jurisdiction_id. Source excerpts use SHA-256 of exact quotation text; duplicate quotations share this quote identity and binding or URL edits retain it. Quotation edits change its content identity. Event IDs combine origin_kind and origin_id.",
    "annual_period_start": "Inclusive start of the annual comparison period; full span for a period mean or repeated identical annual allocations.",
    "annual_period_end": "Inclusive end of the annual comparison period; full span for a period mean or repeated identical annual allocations.",
    "period_start": "Inclusive start date of the cited exchange-rate table.",
    "period_end": "Inclusive end date of the cited exchange-rate table.",
    "units_per_eur": "ECB euro foreign exchange reference rate: units of the currency per euro, annual average for the period. In funding.csv, the rate applied to that row's annual_amount to give eur_per_year_est; empty when there is no EUR estimate.",
    "source_accessed": "Date the cited rate source was retrieved.",
    "snapshot_retrieved": "Recorded retrieval timestamp of the complete private source copy, not publication date.",
    "snapshot_sha256": "SHA-256 of complete private response bytes; no private file path is exported.",
    "snapshot_text_sha256": "Versioned SHA-256 of the private copy's normalised main text, for change detection; the rule is the JSON Schema field sources[].snapshot.text_sha256. Empty when the copy yields no text.",
    "snapshot_text_scheme": "Version of the snapshot_text_sha256 rule; hashes of different schemes are not compared.",
    "legal_text": "True when the source is the official text of a legal act, not an explainer; empty otherwise.",
    "title_en": "Faithful English translation of a non-English or mixed-language source title; original title is retained. Omitted when already English or only a proper name/acronym. This is not necessarily an official English title.",
    **{
        f"{key}_days_{end}": f"{'Shortest' if end == 'min' else 'Longest'} reading, in signed whole days, of the interval {words}; empty when not recorded{applies}. Equal bounds mean the interval is exact once each date is bounded (a month-only or year-only date ends no later than dataset.as_of). Rule in dataset.method (Intervals between milestones); see institutes[].intervals in the JSON schema."
        for key, words, applies in (
            ("announced_to_established", "from the first commitment or proposal event to the establishment", ""),
            ("established_to_first_qualifying_activity", "from the establishment to the first qualifying activity", " or the institute is not currently established"),
            ("announced_to_first_qualifying_activity", "from the first commitment or proposal event to the first qualifying activity", " or the institute is not currently established"),
        )
        for end in ("min", "max")
    },
    "credited_from": "Compatibility field generated from credit.date when it differs from the lineage publication date.",
    "credit_institute_id": "The output record's credit.institute_id in aisi.json; see the credit object in the JSON schema.",
    "credit_date": "The output record's credit.date in aisi.json; see the credit object in the JSON schema.",
    "short": "Plain one-line event summary, at most 12 whitespace-delimited words. Canonical for timeline and network milestones; empty for other event origins.",
    "supports": "JSON-encoded array of RFC 6901 pointers supported by this excerpt in this exact dataset version.",
    "identifier_source_ids": "Citations supporting identifiers, distinct from citations for status; see sources.csv.",
    "source_id": "ID of the source containing the excerpt; see sources.csv.",
    "source_url": "Canonical URL of the source containing the excerpt.",
    "source_ids": "Source ids, separated by '; '; see sources.csv.",
    "source_urls": "URLs of the cited sources, same order as source_ids.",
    "population_source_ids": "Source ids for the population figure; in funding.csv, for the population that eur_per_person_est divides by, empty when that ratio is empty.",
    "population_year": "Year of the population figure; in funding.csv, of the population that eur_per_person_est divides by, empty when that ratio is empty.",
    "gdp": "Gross domestic product in current national currency (World Bank WDI), recorded only for states with a compared annual budget; empty otherwise. The share-of-GDP denominator; rule budget-ratios in dataset.method.",
    "gdp_currency": "ISO 4217 code of the currency the gdp value is in.",
    "gdp_year": "Calendar year of the GDP figure; in funding.csv, of the GDP that share_of_gdp_est divides by, empty when that ratio is empty.",
    "gdp_source_ids": "Source ids for the GDP figure; in funding.csv, for the GDP that share_of_gdp_est divides by, empty when that ratio is empty; see sources.csv.",
    "eur_per_person_est": "Compared funding record only: the unrounded annual EUR amount divided by the jurisdiction's population, 6 significant figures (rounded up for an upper-bound amount_qualifier, down for a lower bound, otherwise half up). amount_qualifier applies unchanged: an upper bound stays an upper bound. Empty on other rows or without a population. Rule budget-ratios in dataset.method.",
    "share_of_gdp_est": "Compared funding record only: annual_amount as a fraction (not a percentage) of the jurisdiction's GDP, in the budget currency when it is the GDP currency, otherwise both converted to EUR at the ECB 2025 rates; 6 significant figures, rounded as eur_per_person_est; amount_qualifier applies unchanged. Empty on other rows or without GDP. Rule budget-ratios in dataset.method.",
    "status_confidence": "Confidence in the status: high, medium or low (rule in dataset.method).",
    "legal_basis": "Act giving the institute its qualifying mandate: law, decree, cabinet_decision, ministerial_act, administrative or none_found.",
    "legal_basis_state": "in_force, or foreseen for planned and proposed institutes.",
    "head_state": "State of the head row: stated, not_published or not_applicable (schema: institutes[].head).",
    "head_person_id": "ID of the institute's head in people.csv (top executive by published title, chair excluded); empty unless head_state is stated.",
    "reports_to_state": "State of the reporting-line row: stated, not_published or not_applicable.",
    "reports_to": "Ministry-level body the institute answers to, one above an agency, university or research host (equal to the host for a ministry host), in the source's language; several bodies are joined with '; '.",
    "reports_to_en": "Faithful English translation of reports_to; empty when the name is already English.",
    "head_notes": "Search log for the head row: what was searched, in which languages and when (required unless stated).",
    "reports_to_notes": "Search log for the reporting-line row (required unless stated).",
    "national_partners_notes": "Search log for the national partners row, including the cyber agency searched and the date when no cyber partner is listed.",
    "testing_capacity": "Testing modes a source states, joined with '; ' in the order in_house, via_other_bodies; or planned or not_published. Empty when not recorded. Rule in dataset.method (Testing capacity and model access); labels in dataset.vocabularies.testing_capacity.",
    "testing_capacity_source_ids": "Sources for testing_capacity: the plan's, then each mode's, then the notes'; see sources.csv. Per-mode sources are in aisi.json.",
    "testing_capacity_notes": "Notes on testing_capacity: a proposal quoted with its source and date, explicit negative statements, joint network testing reported with its date, or a newer source contradicting an older one.",
    "testing_capacity_searches": "Search log for a not_published testing_capacity, as 'date (language): query' joined with '; '; the sources read are in aisi.json.",
    "model_access": "Generated model access value: recent_access (latest access event within the twelve months up to as_of), earlier_access, by_law, access_agreed or none_found. Empty when not recorded. Rule in dataset.method (Testing capacity and model access); labels in dataset.vocabularies.model_access.",
    "model_access_latest_event": "Generated: date of the latest access event, on or before which that access took place; empty without events.",
    "model_access_events": "Access events as 'date developer model (kind)' joined with '; '; the access took place on or before the date; kind is pre_release or deeper_than_public. Full records in aisi.json.",
    "model_access_legal_power": "The legal power to require model access, as '<act>, <article>'; conditions are in aisi.json. Empty when none is recorded.",
    "model_access_agreement_ids": "IDs in agreements.csv of developer agreements providing for access to, or evaluation of, the developer's models, joined with '; '.",
    "model_access_source_ids": "Sources for model access events, the legal power, developer agreements and the notes; see sources.csv.",
    "model_access_notes": "Notes on model access: access to models outside Epoch AI's frontier set, access from other developers, unknown contractor access, or a newer source contradicting an older one.",
    "model_access_searches": "Search log when nothing is recorded, as 'date (language): query' joined with '; '; the sources read are in aisi.json.",
    "staffing_model": "How a source states the institute is staffed: own_staff, member_bodies, core_with_partners or host_unit; or planned or not_published. Empty when not recorded. Rule in dataset.method (Staffing model); labels in dataset.vocabularies.staffing_model. Dated headcounts are in staff.csv.",
    "staffing_model_source_ids": "Sources for staffing_model and its notes; see sources.csv.",
    "staffing_model_notes": "Notes on staffing_model: secondments, concurrent posts or contractors beside the main model, history from older sources, a proposal quoted with its source and date, or a newer source contradicting an older one.",
    "staffing_model_searches": "Search log for a not_published staffing_model, as 'date (language): query' joined with '; '; the sources read are in aisi.json.",
    "national_partners_state": "State of the national partners row: stated, not_published or not_applicable; entries are in national_partners.csv.",
    "what": "What the partner is, in two to six words.",
    "relation": "How the partner relates to the institute: member (a constituent of a federation institute), works_with or planned; card words in dataset.vocabularies.partner_relation.",
    "cyber_agency": "True when the partner is the national cyber security agency or the body containing it; empty otherwise.",
    "how": "One sentence, at most 25 words, written from the bound quote: how the partner works with the institute.",
    "host_type": "Where the institute sits: ministry, government_agency, public_research_body, university, supranational, multiple (network of bodies) or unknown.",
    "funding_source": "Who pays: national_government, supranational, foreign_or_multilateral, philanthropic, private, mixed or unknown.",
    "event": "Timeline event code; definitions in dataset.vocabularies.timeline_event.",
    "instrument": "The underlying act: speech, press_release, decision, strategy, budget_proposal, budget_act, draft_policy, bill, law, decree, administrative, international_statement, advisory_report (a government-appointed advisory commission's report), publication or unknown.",
    "memberships": "Codes of international bodies the jurisdiction belongs to, separated by '; '; codes are defined in dataset.vocabularies.membership_body.",
    "memberships_source_ids": "Source ids supporting the memberships (all memberships combined, deduplicated), separated by '; '; see sources.csv.",
    "check_method": "individual = sources searched and read one by one; broad_search = region-wide searches only.",
    "last_checked": "Recorded date of the last full record check; last_checked_basis states whether this is evidenced or a historical upper bound. Partial checks do not advance it.",
    "last_checked_basis": "recorded_full_check means a recorded full check; historical_upper_bound means the original full-check log is unavailable and the date does not guarantee freshness. Empty means the basis is not recorded.",
    "name_history": "Names oldest first, with start date (the date of the timeline event that gave the name) and source ids.",
    "commitment": "proposed, adopted, announced or not_applicable; see the schema.",
    "confidence": "high = stated in a primary source; medium = primary but indirect or several consistent secondary sources; low = single secondary source or estimate.",
    "figure_source": "For a low-confidence figure only: secondary_sources, press_report or our_estimate, shown as secondary sources, press report or our estimate. Empty otherwise; a medium-confidence figure is labelled 'indirect or secondary'.",
}


def table_schema(rows: list[dict], schema_doc: dict, columns=None) -> dict:
    def ftype(values):
        vs = [v for v in values if v != ""]
        if vs and all(isinstance(v, bool) for v in vs):
            return "boolean"
        if vs and all(isinstance(v, int) and not isinstance(v, bool) for v in vs):
            return "integer"
        if vs and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in vs):
            return "number"
        return "string"

    fields = []
    for col in columns if columns is not None else rows[0]:
        desc = FIELD_DOCS.get(col) or schema_doc.get(col, "")
        kind = "boolean" if col in {"scheduled", "counts_as_aisi", "cyber_agency", "legal_text"} else ftype([r[col] for r in rows])
        fields.append({"name": col, "type": kind, **({"description": desc} if desc else {})})
    return {"fields": fields, "missingValues": [""]}


TABLE_NODES = {
    "exchange_rates": ("properties", "dataset", "properties", "exchange_rates", "items"),
    "jurisdictions": ("properties", "jurisdictions", "items"),
    "institutes": ("properties", "institutes", "items"),
    "funding": ("$defs", "funding"),
    "staff": ("$defs", "staff"),
    "people": ("properties", "institutes", "items", "properties", "people", "items"),
    "known_activity": ("$defs", "operating", "items"),
    "timeline": ("properties", "jurisdictions", "items", "properties", "timeline", "items"),
    "agreements": ("properties", "institutes", "items", "properties", "agreements", "items"),
    "national_partners": ("$defs", "partner_entry"),
    "sources": ("properties", "sources", "items"),
    "outputs": ("properties", "institutes", "items", "properties", "outputs", "items"),
    "network_milestones": ("properties", "dataset", "properties", "network_milestones", "items"),
    "source_excerpts": ("properties", "sources", "items", "properties", "excerpts", "items"),
    "related_bodies": ("properties", "jurisdictions", "items", "properties", "related_bodies", "items"),
    "closest_bodies": ("properties", "jurisdictions", "items", "properties", "closest_body"),
    "events": ("$defs", "event_export"),
}

# New optional catalogues retain a downloadable header and dictionary before research fills them.
EMPTY_COLUMNS = {
    "funding": ["id", "institute_id", "kind", "commitment", "amount", "amount_qualifier", "currency", "years", "period", "coverage", "derived_total", "sum_of", "annual_period_start", "annual_period_end", "decision_date", "annual_amount", "annual_basis", "annual_basis_code", "eur_per_year_est", "units_per_eur", "eur_per_person_est", "population_year", "population_source_ids", "share_of_gdp_est", "gdp_year", "gdp_source_ids", "funding_source", "description", "confidence", "figure_source", "source_ids", "source_urls", "notes"],
    "staff": ["id", "institute_id", "as_of", "total", "total_qualifier", "technical", "technical_qualifier", "target", "description", "confidence", "figure_source", "source_ids", "source_urls"],
    "people": ["id", "institute_id", "name", "role", "since", "until", "background", "confidence", "source_ids", "source_urls"],
    "exchange_rates": ["id", "currency", "units_per_eur", "eur_per_unit", "basis", "period_start", "period_end", "source_ids", "source_urls", "source_accessed"],
    "timeline": ["id", "jurisdiction_id", "event_id", "date", "event", "instrument", "description", "short", "withdraws", "source_ids", "source_urls"],
    "national_partners": ["id", "institute_id", "name", "name_en", "acronym", "what", "relation", "cyber_agency", "how", "source_ids", "source_urls"],
    "agreements": ["id", "institute_id", "counterparty", "counterparty_type", "state", "participation", "date", "scope", "source_ids", "source_urls"],
    "events": ["id", "jurisdiction_id", "jurisdiction", "institute_id", "institute", "type", "date", "description", "short", "origin_kind", "origin_id", "source_pointer", "withdraws", "scheduled", "source_ids", "source_urls"],
    "outputs": ["id", "institute_id", "date", "credited_from", "credit_institute_id", "credit_date", "type", "title", "lineage", "attribution", "qualifies_as", "qualification_note", "source_ids", "source_urls"],
    "known_activity": ["id", "institute_id", "kind", "date", "lineage", "source_ids", "source_urls"],
    "network_milestones": ["id", "body", "type", "date", "description", "short", "source_ids", "source_urls"],
    "source_excerpts": ["id", "source_id", "source_url", "text", "gloss", "supports", "url", "accessed"],
    "related_bodies": ["id", "jurisdiction_id", "name", "name_en", "kind", "role", "counts_as_aisi", "designation_state", "authority_roles", "source_ids", "source_urls"],
    "closest_bodies": ["id", "jurisdiction_id", "name", "stage", "gap", "note", "source_ids", "source_urls"],
}


def table_descriptions(schema: dict, table: str) -> dict:
    """Field descriptions for one table, taken from that table's own schema node."""
    node = schema
    for key in TABLE_NODES[table]:
        node = node[key]
    return {k: v["description"] for k, v in node.get("properties", {}).items() if isinstance(v, dict) and "description" in v}


def prerender(site: Path) -> None:
    """Replace the built page with the page as rendered by headless Chrome; Chrome is a build requirement."""
    env = {k: v for k, v in os.environ.items() if k != "VIRTUAL_ENV"}  # the script has its own locked environment
    script = Path(__file__).resolve().parent / "prerender.py"
    result = subprocess.run(["uv", "run", "--locked", "-q", str(script), "--dist", str(site)], env=env, capture_output=True, text=True)
    if result.returncode:
        fail([f"pre-rendering failed (scripts/prerender.py exit {result.returncode}): {result.stderr.strip()[-2000:]}"])


PAGE_DATA_DROPPED = {"check": ("last_checked", "method"), "sources": ("snapshot", "access_note")}


def page_data(published: dict) -> str:
    """data/page.json, what the page script loads: aisi.json without the fields it never reads (a state's research
    log in check, except last_checked and method; a source's private-copy metadata, snapshot and access_note),
    minified. aisi.json stays the public download; the page fetches this file by a URL versioned per content."""
    page = dict(published)
    keep = PAGE_DATA_DROPPED["check"]
    page["jurisdictions"] = [j | {"check": {k: j["check"][k] for k in keep if k in j["check"]}} if "check" in j else j for j in published["jurisdictions"]]
    page["sources"] = [{k: v for k, v in s.items() if k not in PAGE_DATA_DROPPED["sources"]} for s in published["sources"]]
    return json.dumps(page, ensure_ascii=False, separators=(",", ":"))


def versioned(path: str, text: str, version: str) -> str:
    """A data URL that changes with the file: the dataset version and a short content hash, so a cached copy
    from an earlier build is never paired with this page."""
    return f"{path}?v={version}-{hashlib.sha256(text.encode('utf-8')).hexdigest()[:10]}"


def generated_page(page: str, published: dict, resources: list[dict], data_urls: tuple[str, str] = ("data/page.json", "data/datapackage.json")) -> str:
    """Build-time insertions into site/index.html: each <!-- build:NAME --> marker appears once."""
    found = re.search(r"<title>([^<]+)</title>", page)
    title = html.unescape(found[1]) if found else published["dataset"]["title"]
    data_meta = f'<meta name="aisi-page-data" content="{html.escape(data_urls[0])}" data-package="{html.escape(data_urls[1])}">'
    inserts = {"head": head_links(published, title) + "\n" + json_ld_script(published, resources) + "\n" + data_meta, "using-data": orientation_html(published)}
    for name, text in inserts.items():
        marker = f"<!-- build:{name} -->"
        if page.count(marker) != 1:
            raise ValueError(f"site/index.html needs exactly one {marker}")
        page = page.replace(marker, text)
    return page


RELEASE_WARNINGS = "release-warnings.md"


def write_release_warnings(warnings: list[str], directory: Path) -> Path:
    """The check report's Release warnings section: listed for the owner, never a failure."""
    directory.mkdir(parents=True, exist_ok=True)
    lines = ["## Release warnings", ""]
    lines += [f"- {warning}" for warning in warnings] if warnings else ["None."]
    path = directory / RELEASE_WARNINGS
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--data", type=Path, default=DATA, help="dataset to validate (default: data/aisi.json)")
    ap.add_argument("--report-dir", type=Path, help="write the informational source-origin report to this directory (default: no report file)")
    ap.add_argument("--out", type=Path, default=DIST, help="output directory for the built site (default: dist/)")
    ap.add_argument("--check", action="store_true", help="validate only; write nothing")
    ap.add_argument("--update-readme", action="store_true", help="regenerate README citation, current-count, agents and definition blocks, CITATION.cff and .zenodo.json, then validate as --check does; the only mode that writes a tracked file")
    ap.add_argument("--require-complete", action="store_true", help="require timeline and status confidence for jurisdictions, placement and short mandate for institutes, and legal basis for established institutes")
    ap.add_argument("--require-roster", action="store_true", help="require exactly the jurisdictions in data/reference/roster.csv")
    ap.add_argument("--release", action="store_true", help="also reject draft wording in release prose")
    args = ap.parse_args()
    canonical = args.data.resolve() == DATA.resolve()
    if not canonical and (args.update_readme or not args.check):
        fail(["the site and the README are only generated from data/aisi.json; use --check for other files"])
    d = json.loads(args.data.read_text(encoding="utf-8"))
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    validator = Draft202012Validator(authored_schema(schema, strict=fact_pipeline.AUTHORING_STRICT), format_checker=Draft202012Validator.FORMAT_CHECKER)
    errors = [f"{'/'.join(map(str, e.absolute_path)) or '(root)'}: {e.message}" for e in validator.iter_errors(d)]
    errors += check_consistency(d, args.require_roster, args.require_complete or args.require_roster, strict=fact_pipeline.AUTHORING_STRICT) if not errors else []
    if not errors:
        try:
            published = export_dataset(d, strict=fact_pipeline.AUTHORING_STRICT)
            errors += [f"published {'/'.join(map(str, e.absolute_path))}: {e.message}" for e in Draft202012Validator(schema, format_checker=Draft202012Validator.FORMAT_CHECKER).iter_errors(published)]
        except (ValueError, KeyError, IndexError, TypeError, ArithmeticError) as exc:
            errors.append(f"materialisation: {exc}")
    if errors:
        fail(errors)
    readme = ROOT / "README.md"
    citation_bytes = None
    if canonical:
        try:
            current_readme = readme.read_bytes()
            citation_bytes = generated_citation(current_readme, d["dataset"]["citation"])
            stale = ["citation"] if citation_bytes != current_readme else []
            counted_bytes = generated_counts(citation_bytes, published)
            if counted_bytes != citation_bytes:
                stale.append("counts")
            agents_bytes = generated_block(counted_bytes, orientation_markdown(published), "agents")
            if agents_bytes != counted_bytes:
                stale.append("agents")
            defined_bytes = generated_block(agents_bytes, definition_markdown(published["dataset"]), "definition")
            if defined_bytes != agents_bytes:
                stale.append("definition")
            citation_bytes = defined_bytes
            if stale and not args.update_readme:
                errors.append(f"README.md {' and '.join(stale)} block is stale; run scripts/build.py --update-readme")
        except (OSError, ValueError) as exc:
            errors.append(str(exc))
    # Tracked metadata files generated whole: path, current text, generated text.
    tracked = []
    if canonical:
        for name, generate in (("CITATION.cff", citation_cff), (".zenodo.json", zenodo_json)):
            path = ROOT / name
            current, text = (path.read_text(encoding="utf-8") if path.is_file() else None), generate(published)
            tracked.append((path, current, text))
            if current != text and not args.update_readme:
                errors.append(f"{name} is missing or stale; run scripts/build.py --update-readme")
    if canonical and citation_bytes is not None:
        errors.extend(check_readme_licence(citation_bytes.decode("utf-8")))
        errors.extend(check_readme_doi(citation_bytes.decode("utf-8"), d["dataset"].get("doi")))
    errors.extend(check_markdown(ROOT, citation_bytes))
    if args.release:
        errors.extend(public_url_errors())
        errors.extend(check_launch_wording(published, schema, ROOT, citation_bytes.decode("utf-8") if citation_bytes is not None else None))
    late = dates_after_as_of(d)
    if args.release:
        errors.extend(late)
    if errors:
        fail(errors)
    # Every check above ran against the regenerated citation, so a failing run writes nothing.
    if args.update_readme and citation_bytes != current_readme:
        write_citation(readme, citation_bytes)
        print(f"updated {readme.relative_to(ROOT)} citation, counts, agents and definition blocks", file=sys.stderr)
    for path, current, text in tracked:
        if args.update_readme and current != text:
            write_citation(path, text.encode("utf-8"))
            print(f"updated {path.relative_to(ROOT)}", file=sys.stderr)
    warnings = instrument_status_warnings(d, release=args.release) + late
    for warning in warnings:
        print(f"warning: {warning}", file=sys.stderr)
    if args.report_dir:
        try:
            write_release_warnings(warnings, args.report_dir)
        except OSError as exc:
            print(f"warning: could not write release warnings: {exc}", file=sys.stderr)
        try:
            origin_report = write_source_origin_report(published, args.report_dir)
        except OSError as exc:
            print(f"warning: could not write informational source-origin report: {exc}", file=sys.stderr)
        else:
            print(f"source origins: {len(origin_report['claim_gaps'])} claim records need review; see {args.report_dir / 'source-origins.md'}", file=sys.stderr)
    else:
        gaps = len(source_origin_report(published)["claim_gaps"])
        print(f"source origins: {gaps} claim records need review; pass --report-dir DIR for the report", file=sys.stderr)
    if args.check or args.update_readme:
        print(f"ok: {args.data} is valid ({len(d['jurisdictions'])} jurisdictions, {len(d['institutes'])} institutes, {len(d['sources'])} sources)")
        return

    d = published
    m = d["dataset"]
    t = tables(d)
    target = args.out.resolve()
    if ROOT.resolve().is_relative_to(target):
        fail([f"--out {args.out} would replace the repository"])
    if target.exists() and any(target.iterdir()) and not (target / "data" / "datapackage.json").is_file():
        fail([f"--out {args.out} is neither empty nor an earlier build; refusing to replace it"])
    target.parent.mkdir(parents=True, exist_ok=True)
    # Stage beside the target so the final move is a rename on the same file system.
    with tempfile.TemporaryDirectory(dir=target.parent, prefix=f".{target.name}-") as tmp:
        build = Path(tmp) / "dist"
        out = build / "data"
        out.mkdir(parents=True)
        (out / "aisi.json").write_text(json.dumps(d, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        shutil.copy(SCHEMA, out / "aisi.schema.json")
        resources = [
            {"name": "aisi", "path": "aisi.json", "format": "json", "mediatype": "application/json"},
            {"name": "aisi-schema", "path": "aisi.schema.json", "format": "json", "mediatype": "application/schema+json"},
        ]
        for name, rows in t.items():
            columns = list(rows[0]) if rows else EMPTY_COLUMNS.get(name)
            if columns is None:
                continue
            with (out / f"{name}.csv").open("w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=columns)
                w.writeheader()
                w.writerows(rows)
            resources.append({"name": name, "path": f"{name}.csv", "format": "csv", "mediatype": "text/csv", "encoding": "utf-8", "schema": table_schema(rows, table_descriptions(schema, name), columns)})
        coverage = shared_claim_coverage(d)
        (out / "shared-claim-coverage.json").write_text(json.dumps(coverage, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        resources.append({"name": "shared-claim-coverage", "path": "shared-claim-coverage.json", "format": "json", "mediatype": "application/json"})
        undocumented = sorted({r["name"] for r in resources} - set(FILES))
        if undocumented:
            fail([f"published data files lack a FILES title and description: {', '.join(undocumented)}"])
        order = list(FILES)
        resources.sort(key=lambda r: order.index(r["name"]))
        for r in resources:
            r["title"], r["description"] = FILES[r["name"]]
        package = {
            # Frictionless "id": a globally unique identifier, a DOI link where one exists.
            **({"id": doi_url(m["doi"])} if m.get("doi") else {}),
            "name": m["name"], "title": m["title"], "description": m["description"], "version": m["version"],
            "licenses": [LICENSES[m["license"]]],
            "license_note": m["license_note"],
            "contributors": [{"title": p["name"], "role": "maintainer"} for p in m["maintainers"]] + [{"title": m["publisher"], "role": "publisher"}],
            "keywords": ["AI safety", "AI security", "AI Safety Institutes", "public institutions"],
            "vocabularies": m["vocabularies"],
            "resources": resources,
        }
        package_text = json.dumps(package, indent=2, ensure_ascii=False) + "\n"
        (out / "datapackage.json").write_text(package_text, encoding="utf-8")
        page_text = page_data(d)
        (out / "page.json").write_text(page_text, encoding="utf-8")
        data_urls = (versioned("data/page.json", page_text, m["version"]), versioned("data/datapackage.json", package_text, m["version"]))
        for path, value in api_files(d).items():
            (build / path).parent.mkdir(parents=True, exist_ok=True)
            (build / path).write_text(json.dumps(value, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
        (build / api_root(d) / "index.html").write_text(api_landing_html(d), encoding="utf-8")
        for name in ("social-preview.png", "favicon.svg", "404.html"):
            shutil.copy(SITE.parent / name, build / name)
        # The first-screen map's vendored libraries and world topology, with their licences (site/assets/LICENSES.txt).
        shutil.copytree(SITE.parent / "assets", build / "assets")
        # Both licences travel with the build: MIT for the page code, CC BY 4.0 for data and text.
        for name in ("LICENSE", "LICENSE-DATA"):
            shutil.copy(ROOT / name, build / name)
        (build / "index.html").write_text(generated_page(SITE.read_text(encoding="utf-8"), d, resources, data_urls), encoding="utf-8")
        (build / "method.html").write_text(full_method_page(m, SITE.read_text(encoding="utf-8")), encoding="utf-8")
        (build / "llms.txt").write_text(llms_txt(d), encoding="utf-8")
        prerender(build)
        # Listed last, so the sitemap covers every file written above; the page is its directory URL.
        built = ["" if p.name == "index.html" and p.parent == build else p.relative_to(build).as_posix() for p in build.rglob("*") if p.is_file()]
        (build / "sitemap.xml").write_text(sitemap_xml(d, built), encoding="utf-8")
        (build / "robots.txt").write_text(robots_txt(), encoding="utf-8")
        if target.exists():
            shutil.rmtree(target)
        shutil.move(str(build), target)
    europe = [j for j in d["jurisdictions"] if j["europe_scope"]]
    print(f"ok: v{m['version']}, {len(europe)} European states, {len(d['institutes'])} institutes, {len(d['sources'])} sources -> {target}")


if __name__ == "__main__":
    main()
