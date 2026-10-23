"""Small, synthetic records; separate regression tests also read the real dataset."""

import csv
import json
import shutil
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from scripts import agent_files, build, release_checks

ROOT = Path(__file__).resolve().parents[1]
FLAG_SEARCH = {"date": "2026-10-17", "query": "Synthetic institute testing and access", "language": "en", "sources_read": ["https://example.org/gb-source"]}
SOURCE_URL = "https://data.worldbank.org/indicator/SP.POP.TOTL"
STATUSES = ("established", "planned", "proposed", "exploring", "none_identified")
EVENTS = (
    "exploration_started", "proposed", "commitment", "designated", "launched",
    "first_output", "withdrawn", "closed", "mandate_changed",
)


def event(kind, date="2024", eid="act", **extra):
    return {
        "id": eid, "date": date, "event": kind, "instrument": "decision",
        "description": "Synthetic government act.", "short": "Government acts.", "source_ids": ["gb-source"], **extra,
    }


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False) + "\n", encoding="utf-8")
    readme = path.parent.parent / "README.md"
    if path.name == "aisi.json" and readme.exists() and "dataset" in value:
        # Fixture mutations keep unrelated generated coverage prose current.
        # Stale-block tests write their malformed README explicitly afterwards.
        if all("status" in j for j in value.get("jurisdictions", [])):
            readme.write_bytes(release_checks.generated_counts(readme.read_bytes(), value))
        if b"<!-- agents:start -->" in readme.read_bytes():
            (readme.parent / "CITATION.cff").write_text(agent_files.citation_cff(value), encoding="utf-8")
            (readme.parent / ".zenodo.json").write_text(agent_files.zenodo_json(value), encoding="utf-8")
            readme.write_bytes(release_checks.generated_block(readme.read_bytes(), agent_files.orientation_markdown(value), "agents"))
        if b"<!-- definition:start -->" in readme.read_bytes() and "definition" in value["dataset"]:
            readme.write_bytes(release_checks.generated_block(readme.read_bytes(), agent_files.definition_markdown(value["dataset"]), "definition"))
    return path


def write_roster(path, ids=("GB",)):
    rows = [{
        "id": jid, "kind": "state", "m49_region_id": "150", "m49_region": "Europe",
        "m49_subregion_id": "154", "m49_subregion": "Northern Europe",
        "population": "100", "population_year": "2025",
    } for jid in ids]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    return {row["id"]: row for row in rows}


@pytest.fixture(autouse=True)
def no_prerender(monkeypatch):
    """Builds in tests skip headless Chrome; the browser gate checks the pre-rendered page."""
    monkeypatch.setattr(build, "prerender", lambda site: None)


@pytest.fixture
def build_env(tmp_path, monkeypatch):
    """Redirect every build path, including README and roster, into this test."""
    for name, path in {
        "ROOT": tmp_path, "DATA": tmp_path / "data/aisi.json",
        "SCHEMA": ROOT / "schema/aisi.schema.json", "SITE": tmp_path / "site/index.html",
        "DIST": tmp_path / "dist", "ROSTER": tmp_path / "data/reference/roster.csv",
    }.items():
        monkeypatch.setattr(build, name, path)
    monkeypatch.setattr(build, "EUROPE", {"GB"})
    monkeypatch.setattr(build.fact_pipeline, "AUTHORING_STRICT", False)  # Explicit legacy migration fixtures.
    (tmp_path / "README.md").write_text("<!-- citation:start -->\n\nSynthetic tracker, version 1.0.0, 17 October 2026\n\n<!-- citation:end -->\n\n<!-- counts:start -->\n\n<!-- counts:end -->\n\n<!-- agents:start -->\n\n<!-- agents:end -->\n\n<!-- definition:start -->\n\n<!-- definition:end -->\n")
    with (tmp_path / "README.md").open("a") as stream:
        stream.write(
            "\n## Licences\n\nQuotations from sources remain with their rights holders "
            "and are reproduced as quotations; the licence covers our own compilation, "
            "text and data.\n"
        )
    monkeypatch.setattr(release_checks, "tracked_markdown", lambda root: list(root.glob("*.md")))
    build.SITE.parent.mkdir()
    for name in ("social-preview.png", "favicon.svg", "404.html"):
        (build.SITE.parent / name).write_bytes((ROOT / "site" / name).read_bytes())
    shutil.copytree(ROOT / "site" / "assets", build.SITE.parent / "assets")
    for name in ("LICENSE", "LICENSE-DATA"):
        (tmp_path / name).write_bytes((ROOT / name).read_bytes())
    build.SITE.write_text("<!doctype html><title>Synthetic page</title>\n<!-- build:head -->\n<!-- build:using-data -->\n")
    write_roster(build.ROSTER)
    return tmp_path


@pytest.fixture
def dataset(build_env):
    """One jurisdiction, institute and cited source, with a row in every export."""
    d = {
        "dataset": {
            "name": "test-aisi", "title": "Synthetic tracker", "description": "Test data.", "definition": "Synthetic definition.",
            "version": "1.0.0", "as_of": "2026-10-17", "license": "CC BY 4.0",
            "license_note": "Quotations from sources remain with their rights holders and are reproduced as quotations; the licence covers our own compilation, text and data.",
            "publisher": "Test publisher", "maintainers": [{"name": "Tester", "affiliation": "Test"}],
            "contact": "Test contact", "citation": "Synthetic tracker, version 1.0.0, 17 October 2026",
            "scope": "One synthetic jurisdiction.", "method": [{"id": "fixtures", "text": "Synthetic fixtures only."}, {"id": "missing-values", "text": "Missing values: an omitted field or null means not known."}],
            "method_summaries": [], "method_no_reader_summary": ["fixtures", "missing-values"], "method_summary_status": "Owner copy approved.",
            "status_definitions": dict.fromkeys(STATUSES, "Synthetic definition."),
            "exchange_rates": [
                {"currency": currency, "units_per_eur": units, "eur_per_unit": rate,
                 "basis": "Synthetic annual average.",
                 "period": {"start": "2025-01-01", "end": "2025-12-31"},
                 "source_ids": ["gb-source"]}
                for currency, units, rate in [("GBP", 0.8, 1.25), ("USD", 1.25, 0.8)]
            ],
            "vocabularies": {
                "testing_capacity": {code: {"label": code, "definition": "Synthetic definition."} for code in ("in_house", "via_other_bodies", "planned", "not_published")},
                "model_access": {code: {"label": code, "definition": "Synthetic definition."} for code in ("recent_access", "earlier_access", "by_law", "access_agreed", "none_found")},
                "staffing_model": {code: {"label": code, "definition": "Synthetic definition."} for code in ("own_staff", "member_bodies", "core_with_partners", "host_unit", "planned", "not_published")},
                "membership_body": {"test-network": "Synthetic network."},
                "timeline_event": dict.fromkeys(EVENTS, "Synthetic event definition."),
            },
            "network_milestones": [{"id": "test-founded", "body": "test-network", "type": "founding", "date": "2024-11", "description": "Synthetic network founding.", "short": "Network founded.", "source_ids": ["gb-source"]}],
        },
        "jurisdictions": [{
            "id": "GB", "name": "Synthetic state", "kind": "state", "europe_scope": True,
            "eu_member": False, "population": {"value": 100, "year": 2025, "source_ids": ["gb-source"]},
            # A compared state budget needs both ratio denominators (build.check_budget_ratio_inputs).
            "gdp": {"value": 1000, "currency": "EUR", "year": 2025, "source_ids": ["gb-source"]},
            "region": {"id": "150", "name": "Europe", "subregion_id": "154",
                       "subregion": "Northern Europe", "source_ids": ["gb-source"]},
            "status": "established", "status_since": "2024", "status_confidence": "high",
            "institute_id": "gb-test", "summary": "Synthetic institute established.",
            "check": {"last_checked": "2026-10-17", "method": "individual"},
            "source_ids": ["gb-source"],
            "memberships": [{"body": "test-network", "since": "2024-11", "source_ids": ["gb-source"]}],
            "related_bodies": [{"id": "test-regulator", "name": "Synthetic regulator", "kind": "other", "role": "Consumer protection", "counts_as_aisi": False, "source_ids": ["gb-source"]}],
            "timeline": [event("commitment", "2023", "plan"), event("designated"),
                         event("first_output", "2024-10", "first-report", instrument="publication", output_lineage="evaluation-report")],
        }],
        "institutes": [{
            "id": "gb-test", "name": "Synthetic institute", "short_name": "UK Synthetic", "jurisdiction_id": "GB",
            "status": "established", "announced": "2023", "established": "2024",
            "host": "Test ministry", "legal_form": "unit_in_existing_body", "mandate": "Evaluate models.",
            "mandate_short": "Evaluate models.",
            "legal_basis": {"type": "administrative", "state": "in_force", "source_ids": ["gb-source"]},
            "placement": {"host_type": "ministry", "source_ids": ["gb-source"]},
            "check": {"last_checked": "2026-10-17", "method": "individual"}, "source_ids": ["gb-source"],
            "funding": [{"id": "test-budget", "kind": "annual", "amount": 0, "currency": "GBP", "commitment": "adopted",
                         "coverage": "whole_institute", "annual_period": {"start": "2026-01-01", "end": "2026-12-31"},
                         "description": "Synthetic budget.", "confidence": "high", "source_ids": ["gb-source"]}],
            "staff": [{"id": "test-headcount", "as_of": "2026-10-17", "total": 0, "technical": None,
                       "confidence": "high", "source_ids": ["gb-source"]}],
            "people": [{"id": "test-lead", "name": "Synthetic lead", "role": "Director", "title_status": "published", "since": "2024-02", "source_ids": ["gb-source"]}],
            "names": [{"id": "test-old-name", "name": "Synthetic predecessor", "event": "plan", "from": "2023", "source_ids": ["gb-source"]}, {"id": "test-name", "name": "Synthetic institute", "event": "act", "from": "2024", "source_ids": ["gb-source"]}],
            "outputs": [{"lineage": "evaluation-report", "attribution": "institutional_author", "qualifies_as": "published_work", "qualification_note": "Synthetic evaluation result credited to the institute.", "date": "2024-10", "type": "report", "title": "Synthetic evaluation report", "source_ids": ["gb-source"]}],
            "agreements": [{"id": "test-agreement", "participation": "institute_party", "domestic_interagency": False, "counterparty": "Synthetic developer", "counterparty_type": "developer", "date": "2024-09", "state": "signed", "source_ids": ["gb-source"]}],
            "testing_capacity": {"state": "not_published", "searches": [FLAG_SEARCH]},
            "model_access": {"searches": [FLAG_SEARCH], "value": "none_found"},
            "staffing_model": {"code": "not_published", "searches": [FLAG_SEARCH]},
        }],
        "sources": [{"id": "gb-source", "output_role": "document", "output_lineage": "evaluation-report", "origin": "GB", "url": SOURCE_URL, "title": "Synthetic citation",
                     "publisher": "Test publisher", "type": "primary", "language": "en", "accessed": "2026-10-17", "excerpts": [{"text": "Synthetic institute", "supports": [
                         "/institutes/0/name", "/jurisdictions/0/status", "/jurisdictions/0/status_since",
                         "/jurisdictions/0/timeline/2/date", "/jurisdictions/0/timeline/2/description",
                         "/institutes/0/status", "/institutes/0/legal_basis/type", "/institutes/0/legal_basis/state",
                         "/institutes/0/funding/0/amount", "/institutes/0/staff/0/total",
                         "/institutes/0/agreements/0/counterparty", "/institutes/0/agreements/0/state",
                         "/institutes/0/agreements/0/date", "/institutes/0/agreements/0/participation",
                     ]}]}],
        "changelog": [{"date": "2026-10-17", "version": "1.0.0", "summary": "Synthetic test release.", "changes": ["Synthetic fixture."]}],
    }
    schema = json.loads(build.SCHEMA.read_text())
    d["sources"][0]["excerpts"][0]["supports"].extend(
        f"/dataset/exchange_rates/{n}/{field}"
        for n in range(2)
        for field in ("units_per_eur", "period/start", "period/end")
    )
    Draft202012Validator(schema, format_checker=Draft202012Validator.FORMAT_CHECKER).validate(d)
    assert build.check_consistency(d, require_roster=True, complete=True) == []
    return d
