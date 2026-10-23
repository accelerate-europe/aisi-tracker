import copy
import csv
import json
import os
import shutil
import subprocess
import sys

import pytest

from conftest import ROOT, event, write_json, write_roster
from scripts import build


@pytest.mark.parametrize("failure", ["schema", "consistency", "materialisation", "arithmetic"])
def test_invalid_release_reports_validation_error_before_release_checks(dataset, monkeypatch, capsys, failure):
    if failure == "schema":
        dataset["institutes"][0]["legal_form"] = "not-a-legal-form"
        expected = "not-a-legal-form"
    elif failure == "consistency":
        dataset["institutes"][0]["source_ids"].append("missing-source")
        expected = "unknown source id"
    else:
        error_type = ArithmeticError if failure == "arithmetic" else ValueError

        def failed_export(*args, **kwargs):
            raise error_type("cannot produce published facts")

        monkeypatch.setattr(build, "export_dataset", failed_export)
        expected = "materialisation: cannot produce published facts"
    write_json(build.DATA, dataset)

    def release_must_not_run(*args, **kwargs):
        pytest.fail("release wording check ran without validated published facts")

    monkeypatch.setattr(build, "check_launch_wording", release_must_not_run)
    monkeypatch.setattr(sys, "argv", ["build.py", "--check", "--release"])
    with pytest.raises(SystemExit) as error:
        build.main()
    assert error.value.code == 1
    assert expected in capsys.readouterr().err


def test_funding_datapackage_preserves_numeric_eur_and_method_description(dataset, monkeypatch):
    funding = dataset["institutes"][0]["funding"][0]
    funding.update(annual_amount=1, annual_basis="Stated annual amount.", annual_basis_code="other", eur_per_year_est=1.25)
    dataset["sources"][0]["excerpts"][0]["supports"].append("/institutes/0/funding/0/annual_amount")
    write_json(build.DATA, dataset)
    monkeypatch.setattr(sys, "argv", ["build.py"])
    build.main()
    out = build.DIST / "data"
    package = json.loads((out / "datapackage.json").read_text())
    resource = next(r for r in package["resources"] if r["name"] == "funding")
    field = next(f for f in resource["schema"]["fields"] if f["name"] == "eur_per_year_est")
    assert field["type"] == "number"
    assert "rounded to 6 significant figures: up for an upper-bound amount_qualifier, down for a lower bound, otherwise half up" in field["description"]
    assert "dataset.exchange_rates" in field["description"]
    with (out / "funding.csv").open() as stream:
        assert next(csv.DictReader(stream))["eur_per_year_est"] == "1.25"


@pytest.mark.parametrize(("events", "expected"), [
    ([], ("none_identified", None)),
    ([event("related_body_planned", "2026-08-11")], ("none_identified", None)),
    ([event("launched"), event("first_output", eid="output")], ("none_identified", None)),
    ([event("exploration_started")], ("exploring", "2024")),
    ([event("proposed")], ("proposed", "2024")),
    ([event("commitment", "2024-02")], ("planned", "2024-02")),
    ([event("designated", "2024-02-29")], ("established", "2024-02-29")),
    ([event("commitment", "2023", "a"), event("designated")], ("established", "2024")),
    ([event("designated", "2023", "a"), event("commitment")], ("established", "2023")),
    ([event("commitment", "2023", "a"), event("commitment")], ("planned", "2023")),
    ([event("designated"), event("withdrawn", "2025", "w", withdraws="act")], ("none_identified", None)),
    ([event("commitment", "2023", "a"), event("designated"),
      event("withdrawn", "2025", "w", withdraws="act")], ("planned", "2023")),
    ([event("commitment", "2023", "a"), event("commitment"),
      event("withdrawn", "2025", "w", withdraws="a")], ("planned", "2024")),
    ([event("designated"), event("closed", "2025", "c")], ("none_identified", None)),
    ([event("designated"), event("closed", "2025", "c"),
      event("commitment", "2026", "b")], ("planned", "2026")),
    ([event("designated"), event("closed", "2025", "c"),
      event("designated", "2026", "b")], ("established", "2026")),
    ([event("designated"), event("closed", "2025", "c"),
      event("withdrawn", "2026", "w", withdraws="c")], ("established", "2024")),
    # Equal and overlapping dates keep list order, including closure boundaries.
    ([event("closed", "2024", "c"), event("designated", "2024-01")], ("established", "2024-01")),
    ([event("designated", "2024-01"), event("closed", "2024", "c")], ("none_identified", None)),
    ([event("closed", "2024", "c"), event("commitment", "2024")], ("planned", "2024")),
    ([event("commitment", "2024"), event("closed", "2024", "c")], ("none_identified", None)),
])
def test_timeline_status(events, expected):
    assert build.timeline_status(events) == expected


@pytest.mark.parametrize(("value", "valid"), [
    ("2024", True), ("2024-02", True), ("2024-02-29", True),
    ("2023-02-29", False), ("2024-13", False), ("2024-04-31", False),
    ("", False), ("202", False), ("2024-2", False), ("not a date", False),
])
def test_partial_dates(value, valid):
    assert build.valid_partial_date(value) is valid


def test_minimal_dataset_passes(dataset):
    assert build.check_consistency(dataset, require_roster=True, complete=True) == []


def test_related_body_plan_uses_the_declared_vocabulary(dataset):
    from jsonschema import Draft202012Validator

    code = "related_body_planned"
    dataset["dataset"]["vocabularies"]["timeline_event"][code] = "Non-status related-body plan."
    dataset["jurisdictions"][0]["timeline"].append(event(code, "2026-08-11", "body-plan"))
    assert list(Draft202012Validator(json.loads(build.SCHEMA.read_text())).iter_errors(dataset)) == []
    assert build.check_consistency(dataset) == []
    del dataset["dataset"]["vocabularies"]["timeline_event"][code]
    assert any("related_body_planned' is not in dataset.vocabularies.timeline_event" in e
               for e in build.check_consistency(dataset))


def at(d, path):
    for key in path:
        d = d[key]
    return d


@pytest.mark.parametrize("path", [
    ("jurisdictions", 0), ("jurisdictions", 0, "population"),
    ("jurisdictions", 0, "region"), ("jurisdictions", 0, "timeline", 0),
    ("institutes", 0), ("institutes", 0, "funding", 0), ("institutes", 0, "staff", 0),
    ("institutes", 0, "people", 0),
    ("institutes", 0, "legal_basis"),
    ("institutes", 0, "placement"), ("institutes", 0, "agreements", 0),
    ("institutes", 0, "outputs", 0), ("dataset", "network_milestones", 0),
    ("jurisdictions", 0, "related_bodies", 0),
])
def test_unknown_source_ids_at_each_location(dataset, path):
    at(dataset, path)["source_ids"] = ["missing"]
    assert any("unknown source id 'missing'" in e for e in build.check_consistency(dataset))


@pytest.mark.parametrize(("section", "key", "value"), [
    ("dataset", "other_overviews", [{"note": "Test", "source_ids": ["missing"]}]),
    ("jurisdictions", "memberships", [{"body": "test-network", "source_ids": ["missing"]}]),
    ("institutes", "names", [{"name": "Earlier name", "event": "plan", "from": "2023", "source_ids": ["missing"]}]),
])
def test_unknown_source_ids_in_optional_lists(dataset, section, key, value):
    target = dataset[section] if section == "dataset" else dataset[section][0]
    target[key] = value
    assert any("unknown source id 'missing'" in e for e in build.check_consistency(dataset))


@pytest.mark.parametrize(("table", "singular"), [
    ("sources", "source"), ("jurisdictions", "jurisdiction"), ("institutes", "institute"),
])
def test_duplicate_ids(dataset, table, singular):
    dataset[table].append(copy.deepcopy(dataset[table][0]))
    assert any(f"duplicate {singular} ids" in e for e in build.check_consistency(dataset))


@pytest.mark.parametrize(("path", "value", "message"), [
    (("dataset", "as_of"), "2026-02-30", "not a real date"),
    (("dataset", "glossary"), {"AISI": {"ZZ": "Unknown state"}}, "unknown jurisdiction ids ['ZZ']"),
    (("jurisdictions", 0, "memberships"), [{"body": "absent", "source_ids": ["gb-source"]}], "membership body 'absent'"),
    (("jurisdictions", 0, "institute_id"), "missing", "unknown institute 'missing'"),
    (("jurisdictions", 0, "institute_id"), None, "needs an institute record"),
    (("institutes", 0, "jurisdiction_id"), "ZZ", "belongs to ZZ"),
    (("institutes", 0, "status"), "planned", "status: stored"),
    (("jurisdictions", 0, "check", "method"), "broad_search", "needs an individual check"),
    (("sources", 0, "type"), "secondary", "needs at least one primary source"),
    (("institutes", 0, "jurisdiction_id"), "ZZ", "unknown jurisdiction 'ZZ'"),
    (("jurisdictions", 0, "institute_id"), None, "does not link back"),
    (("institutes", 0, "operating"), [], "operating: stored"),
    (("jurisdictions", 0, "europe_scope"), False, "europe_scope set differs"),
    (("jurisdictions", 0, "kind"), "territory", "europe_scope states need"),
    (("jurisdictions", 0, "timeline", 1, "event"), "missing", "not in dataset.vocabularies.timeline_event"),
    (("jurisdictions", 0, "timeline", 1, "event"), "first_output", "disagrees with derived 'planned'"),
    (("jurisdictions", 0, "status_since"), "2024-01-01", "disagrees with derived '2024'"),
    (("institutes", 0, "announced"), "2022", "announced: stored '2022' disagrees with derived '2023'"),
    (("institutes", 0, "established"), "2025", "established: stored"),
    (("institutes", 0, "names", 1, "from"), "2025", "from: stored '2025' disagrees with derived '2024'"),
    (("institutes", 0, "names", 1, "event"), "missing-act", "names/test-name: event 'missing-act' is not in the GB timeline"),
    (("dataset", "citation"), "Wrong version", "dataset.citation does not name"),
    (("changelog", 0, "version"), "0.9.0", "latest changelog version"),
])
def test_consistency_error_classes(dataset, path, value, message):
    at(dataset, path[:-1])[path[-1]] = value
    assert any(message in e for e in build.check_consistency(dataset))


def test_duplicate_timeline_event_ids(dataset):
    dataset["jurisdictions"][0]["timeline"][1]["id"] = "plan"
    dataset["institutes"][0].pop("names")  # A name would no longer find its event "act".
    assert any("duplicate timeline event ids" in e for e in build.check_consistency(dataset))


def test_uncited_sources(dataset):
    dataset["sources"].append({**dataset["sources"][0], "id": "unused", "url": "https://example.org/unused", "excerpts": []})
    assert build.check_consistency(dataset) == ["sources never cited: ['unused']"]


def test_duplicate_source_urls(dataset):
    first = dataset["sources"][0]
    dataset["sources"].append({**first, "id": "dupe"})
    errors = build.check_consistency(dataset)
    assert any(f"sources {first['id']} and dupe share the URL" in e for e in errors)


def test_glossary_known_jurisdictions_and_shared_expansion(dataset):
    dataset["dataset"]["glossary"] = {"AISI": {"GB": "Institute"}, "AI": "Artificial intelligence"}
    assert build.check_consistency(dataset) == []


@pytest.mark.parametrize(("dates", "ordered"), [
    (["2024", "2024-01", "2024-01-01"], True),
    (["2024-01-01", "2024", "2024-01"], True),
    (["2024-01", "2024-01"], True),
    (["2023-12-31", "2024"], True),
    (["2024", "2023-12"], False),
    (["2024-02", "2024-01"], False),
    (["2024-01-02", "2024-01-01"], False),
    # The broad date in the middle must not hide a definite reversal.
    (["2024-02", "2024", "2024-01"], False),
])
def test_timeline_ordering(dataset, dates, ordered):
    dataset["jurisdictions"][0]["timeline"] = [event("designated", date, f"e-{n}") for n, date in enumerate(dates)]
    errors = build.check_consistency(dataset)
    assert any("timeline not in date order" in e for e in errors) is not ordered


@pytest.mark.parametrize("target", ["missing", "w", "future", "earlier-withdrawal"])
def test_withdrawal_requires_earlier_non_withdrawal(dataset, target):
    dataset["jurisdictions"][0]["timeline"] = [
        event("commitment", "2021", "plan"),
        event("withdrawn", "2022", "earlier-withdrawal", withdraws="plan"),
        event("withdrawn", "2023", "w", withdraws=target),
        event("designated", "2024", "future"),
    ]
    assert any("must name an earlier event" in e for e in build.check_consistency(dataset))


def test_status_since_reaffirmation_and_new_run(dataset):
    j, i = dataset["jurisdictions"][0], dataset["institutes"][0]
    j["timeline"].append(event("designated", "2025", "again"))
    assert build.check_consistency(dataset) == []
    j["status_since"] = "2025"
    assert any("disagrees with derived '2024'" in e for e in build.check_consistency(dataset))
    j["timeline"].insert(3, event("closed", "2024-12", "closed"))
    assert build.check_consistency(dataset) == []
    # established is the first designation, while status_since starts the new run.
    assert i["established"] == "2024"


def test_announced_uses_first_proposal_not_later_commitment(dataset):
    dataset["jurisdictions"][0]["timeline"].insert(0, event("proposed", "2022-06", "proposal"))
    excerpt = dataset["sources"][0]["excerpts"][0]
    excerpt["supports"] = [p.replace("/timeline/2/", "/timeline/3/") for p in excerpt["supports"]]
    errors = build.check_consistency(dataset)
    assert any("announced: stored '2023' disagrees with derived '2022-06'" in e for e in errors)
    dataset["institutes"][0].pop("announced")
    assert build.check_consistency(dataset) == []


def test_closed_status_has_no_since_and_needs_negative_confidence_evidence(dataset):
    j = dataset["jurisdictions"][0]
    j["timeline"].append(event("closed", "2025", "closed"))
    j["status"] = dataset["institutes"][0]["status"] = "none_identified"
    errors = build.check_consistency(dataset)
    assert any("negative confidence exceeds low evidence limit" in e for e in errors)
    assert any("disagrees with derived None" in e for e in errors)
    j.pop("status_since")
    dataset["sources"][0]["excerpts"][0]["supports"].remove("/jurisdictions/0/status_since")
    j["status_confidence"] = "low"
    assert build.check_consistency(dataset) == []


@pytest.mark.parametrize(("change", "message"), [
    ("missing", "roster jurisdictions missing"),
    ("extra", "jurisdictions not in the roster"),
    ("region", "region differs from the roster"),
])
def test_roster_coverage(dataset, change, message):
    if change == "missing":
        write_roster(build.ROSTER, ("GB", "FR"))
    elif change == "extra":
        write_roster(build.ROSTER, ("FR",))
    else:
        dataset["jurisdictions"][0]["region"]["subregion_id"] = "155"
    assert build.check_consistency(dataset) == []
    assert any(message in e for e in build.check_consistency(dataset, require_roster=True))


@pytest.mark.parametrize("field", ["status_confidence", "timeline", "mandate_short", "legal_basis", "placement"])
@pytest.mark.parametrize("flag", ["--require-roster", "--require-complete"])
def test_cli_release_completeness(dataset, monkeypatch, capsys, field, flag):
    table = "jurisdictions" if field in ("status_confidence", "timeline") else "institutes"
    dataset[table][0].pop(field)
    # Remove citations to the omitted field; a fragment without a timeline has no output catalogue.
    prefix = f"/{table}/0/{field}"
    excerpt = dataset["sources"][0]["excerpts"][0]
    excerpt["supports"] = [p for p in excerpt["supports"] if not p.startswith(prefix + "/")]
    if field == "timeline":
        dataset["institutes"][0]["outputs"] = []
    write_json(build.DATA, dataset)
    monkeypatch.setattr(sys, "argv", ["build.py", "--check"])
    if field == "timeline":
        with pytest.raises(SystemExit):
            build.main()
        assert "disagrees with derived" in capsys.readouterr().err
        return  # A positive result without its primary timeline cannot publish.
    build.main()
    monkeypatch.setattr(sys, "argv", ["build.py", "--check", flag])
    with pytest.raises(SystemExit) as caught:
        build.main()
    assert caught.value.code == 1
    assert f"{field} missing" in capsys.readouterr().err
    assert not build.DIST.exists()


@pytest.mark.parametrize(("status", "kind"), [
    ("planned", "commitment"), ("proposed", "proposed"), ("exploring", "exploration_started"),
])
def test_nonestablished_release_allows_unknown_legal_basis(dataset, monkeypatch, status, kind):
    jurisdiction = dataset["jurisdictions"][0]
    jurisdiction.update(status=status, status_since="2023", timeline=[event(kind, "2023", "plan")])
    institute = dataset["institutes"][0]
    institute.update(status=status, outputs=[], operating=[])
    institute.pop("established")
    institute.pop("announced")  # Generated: exploration is not an announcement.
    institute["names"].pop()  # Its naming act, the designation, is not in this timeline.
    institute.pop("legal_basis")
    excerpt = dataset["sources"][0]["excerpts"][0]
    excerpt["supports"] = [p for p in excerpt["supports"] if not p.startswith((
        "/institutes/0/legal_basis/", "/jurisdictions/0/timeline/",
    ))]
    write_json(build.DATA, dataset)
    monkeypatch.setattr(sys, "argv", ["build.py", "--check", "--require-complete"])
    build.main()
    for field in ("mandate_short", "placement"):
        value = institute.pop(field)
        assert f"institute gb-test: {field} missing" in build.check_consistency(dataset, complete=True)
        institute[field] = value


@pytest.mark.parametrize(("path", "value"), [
    (("jurisdictions", 0, "status"), "invented"),
    (("jurisdictions", 0, "timeline", 0, "source_ids"), []),
    (("institutes", 0, "legal_basis", "source_ids"), []),
    (("institutes", 0, "placement", "source_ids"), []),
    (("institutes", 0, "agreements", 0, "source_ids"), []),
    (("jurisdictions", 0, "timeline", 0, "instrument"), "invented"),
])
def test_schema_errors_block_build(dataset, monkeypatch, path, value):
    at(dataset, path[:-1])[path[-1]] = value
    write_json(build.DATA, dataset)
    build.DIST.mkdir()
    sentinel = build.DIST / "keep.txt"
    sentinel.write_text("Previous successful build")
    monkeypatch.setattr(sys, "argv", ["build.py"])
    with pytest.raises(SystemExit) as caught:
        build.main()
    assert caught.value.code == 1
    assert sentinel.read_text() == "Previous successful build"


def test_check_mode_leaves_data_and_existing_dist_unchanged(dataset, monkeypatch):
    write_json(build.DATA, dataset)
    original = build.DATA.read_bytes()
    build.DIST.mkdir()
    sentinel = build.DIST / "index.html"
    sentinel.write_text("Previous successful page")
    monkeypatch.setattr(sys, "argv", ["build.py", "--check", "--require-roster"])
    build.main()
    assert build.DATA.read_bytes() == original
    assert list(build.DIST.iterdir()) == [sentinel]
    assert sentinel.read_text() == "Previous successful page"


def test_out_builds_elsewhere_and_leaves_the_checkout_alone(dataset, monkeypatch, tmp_path_factory):
    write_json(build.DATA, dataset)
    before = {p: p.read_bytes() for p in build.ROOT.rglob("*") if p.is_file()}
    out = tmp_path_factory.mktemp("elsewhere") / "site"
    monkeypatch.setattr(sys, "argv", ["build.py", "--out", str(out)])
    build.main()
    assert (out / "index.html").is_file() and (out / "data/datapackage.json").is_file()
    assert {p: p.read_bytes() for p in build.ROOT.rglob("*") if p.is_file()} == before
    assert [p.name for p in out.parent.iterdir()] == ["site"]


@pytest.mark.parametrize("out", [".", "data", ".."])
def test_out_never_replaces_the_repository_or_its_inputs(dataset, monkeypatch, capsys, out):
    write_json(build.DATA, dataset)
    before = {p: p.read_bytes() for p in build.ROOT.rglob("*") if p.is_file()}
    monkeypatch.setattr(sys, "argv", ["build.py", "--out", str(build.ROOT / out)])
    with pytest.raises(SystemExit):
        build.main()
    err = capsys.readouterr().err
    assert "would replace the repository" in err or "refusing to replace it" in err
    assert {p: p.read_bytes() for p in build.ROOT.rglob("*") if p.is_file()} == before


def test_validation_writes_no_bytecode_on_a_cold_start(tmp_path):
    scripts = tmp_path / "scripts"
    shutil.copytree(ROOT / "scripts", scripts, ignore=shutil.ignore_patterns("__pycache__"))
    env = {k: v for k, v in os.environ.items() if k != "PYTHONDONTWRITEBYTECODE"}
    result = subprocess.run([sys.executable, str(scripts / "build.py"), "--help"], env=env, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    assert not (scripts / "__pycache__").exists()


def test_csv_datapackage_columns_and_descriptions(dataset, monkeypatch):
    write_json(build.DATA, dataset)
    monkeypatch.setattr(sys, "argv", ["build.py"])
    build.main()
    out = build.DIST / "data"
    package = json.loads((out / "datapackage.json").read_text())
    schema = json.loads(build.SCHEMA.read_text())
    tables = build.tables(build.export_dataset(dataset))
    assert set(tables) == {
        "jurisdictions", "institutes", "funding", "staff", "people", "known_activity",
        "timeline", "agreements", "national_partners", "sources", "outputs", "network_milestones", "source_excerpts", "related_bodies", "closest_bodies", "events", "exchange_rates",
    }
    resources = {r["name"]: r for r in package["resources"] if r["format"] == "csv"}
    assert resources.keys() == tables.keys()
    for name, resource in resources.items():
        with (out / resource["path"]).open(newline="", encoding="utf-8") as stream:
            reader = csv.DictReader(stream)
            rows = list(reader)
            fields = resource["schema"]["fields"]
            columns = list(tables[name][0]) if tables[name] else build.EMPTY_COLUMNS[name]
            assert reader.fieldnames == [f["name"] for f in fields] == columns
        assert len(rows) == len(tables[name])
        node = schema
        for key in build.TABLE_NODES[name]:
            node = node[key]
        for field in fields:
            expected = build.FIELD_DOCS.get(field["name"]) or node["properties"].get(field["name"], {}).get("description")
            assert field.get("description") == expected
        assert resource["schema"]["missingValues"] == [""]
    assert {"status_confidence", "status_since"} <= set(tables["jurisdictions"][0])
    assert {"mandate_short", "legal_basis", "legal_basis_state", "host_type"} <= set(tables["institutes"][0])
    assert "funding_source" in tables["funding"][0]
    assert {"jurisdiction_id", "event_id", "date", "event", "instrument", "withdraws"} <= set(tables["timeline"][0])
    assert {"institute_id", "counterparty", "counterparty_type", "state", "date", "scope"} <= set(tables["agreements"][0])
    assert tables["staff"][0]["total"] == 0
    assert tables["staff"][0]["technical"] == ""
    assert tables["funding"][0]["amount"] == 0
    published = json.loads((out / "aisi.json").read_text())
    assert published == build.export_dataset(dataset)
    activity = published["institutes"][0]["operating"][0]
    assert (activity["kind"], activity["date"], activity["lineage"]) == ("published_work", "2024-10", "evaluation-report")
    assert activity["citations"]["display"] == []  # Legacy association is not credit evidence.


def test_table_schema_types_and_description_precedence():
    fields = build.table_schema([
        {"flag": False, "count": 0, "amount": 1.5, "missing": "", "event": "designated"},
        {"flag": True, "count": 2, "amount": 1, "missing": "", "event": "closed"},
    ], {"event": "Overridden", "count": "A count"})["fields"]
    assert {f["name"]: f["type"] for f in fields} == {
        "flag": "boolean", "count": "integer", "amount": "number", "missing": "string", "event": "string",
    }
    assert fields[1]["description"] == "A count"
    assert fields[-1]["description"] == build.FIELD_DOCS["event"]


def test_real_dataset_smoke():
    result = subprocess.run(
        ["uv", "run", "scripts/build.py", "--check", "--require-roster"],
        cwd=ROOT, text=True, capture_output=True, timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "is valid" in result.stdout


@pytest.mark.parametrize("pointer", [
    "", "institutes/0/name", "#/institutes/0/name", "/institutes/00/name",
    "/institutes/-1/name", "/institutes/-/name", "/institutes/2/name",
    "/institutes/0/missing", "/institutes/0/name/child", "/institutes/0/name~2",
])
def test_bad_excerpt_pointers(dataset, pointer):
    dataset["sources"][0]["excerpts"][0]["supports"] = [pointer]
    assert any("does not resolve" in e for e in build.check_excerpts(dataset))


def test_pointer_escaping_and_array_values():
    doc = {"a/b": {"~key": [0, False, None, ""]}}
    for index, value in enumerate([0, False, None, ""]):
        assert build.pointer_ancestors(doc, f"/a~1b/~0key/{index}")[-1] == value
    assert build.pointer_ancestors({"~1": 42}, "/~01")[-1] == 42


def test_excerpt_cannot_borrow_unrelated_parent_citation(dataset):
    dataset["institutes"][0]["agreements"][0]["source_ids"] = ["different-source"]
    dataset["sources"][0]["excerpts"][0]["supports"] = ["/institutes/0/agreements/0/counterparty"]
    assert any("nearest cited record" in e for e in build.check_excerpts(dataset))


def test_identifier_excerpt_uses_identifier_sources(dataset):
    institute = dataset["institutes"][0]
    institute.update(wikidata="Q123", identifier_source_ids=["different-source"])
    dataset["sources"][0]["excerpts"][0]["supports"] = ["/institutes/0/wikidata"]
    assert any("nearest cited record" in e for e in build.check_excerpts(dataset))
    institute["identifier_source_ids"] = ["gb-source"]
    assert build.check_excerpts(dataset) == []


def test_excerpt_gloss(dataset):
    source = dataset["sources"][0]
    source["language"] = "fr"
    assert any("English gloss" in e for e in build.check_excerpts(dataset))
    source["excerpts"][0]["gloss"] = "Synthetic institute"
    assert build.check_excerpts(dataset) == []


def test_excerpt_length_and_missing_nested_citation(dataset):
    from jsonschema import Draft202012Validator

    validator = Draft202012Validator(json.loads(build.SCHEMA.read_text()))
    dataset["sources"][0]["excerpts"][0]["text"] = "x" * 301
    assert any("too long" in e.message for e in validator.iter_errors(dataset))
    dataset["sources"][0]["excerpts"][0]["text"] = "Synthetic"
    dataset["institutes"][0]["agreements"][0].pop("source_ids")
    assert any("'source_ids' is a required property" in e.message for e in validator.iter_errors(dataset))


def test_old_agreement_schema_rejected(dataset):
    from jsonschema import Draft202012Validator

    institute = dataset["institutes"][0]
    institute["access_agreements"] = institute.pop("agreements")
    validator = Draft202012Validator(json.loads(build.SCHEMA.read_text()))
    assert any("access_agreements" in e.message for e in validator.iter_errors(dataset))


def test_new_milestone_does_not_change_status(dataset):
    from jsonschema import Draft202012Validator

    timeline = dataset["jurisdictions"][0]["timeline"]
    timeline.append(event("mandate_changed", "2025", "mandate"))
    Draft202012Validator(json.loads(build.SCHEMA.read_text())).validate(dataset)
    assert build.timeline_status(timeline) == ("established", "2024")
    assert build.check_consistency(dataset) == []


def test_events_cover_canonical_records_without_undated_inference(dataset):
    institute = dataset["institutes"][0]
    institute["funding"][0]["period"] = "2025-2026"
    rows = build.event_rows(dataset)
    assert {r["type"] for r in rows} == {"commitment", "designated", "first_output", "appointment", "membership", "agreement", "network_founding"}
    assert len(rows) == len({r["source_pointer"] for r in rows})
    assert not any(r["origin_kind"] == "names" for r in rows)  # Each naming act appears once, as its timeline row.
    assert not any(r["type"] == "funding_decision" for r in rows)
    institute["funding"][0]["decision_date"] = "2023-02-01"
    fund = next(r for r in build.event_rows(dataset) if r["type"] == "funding_decision")
    assert fund["date"] == "2023-02-01"
    assert fund["source_pointer"] == "/institutes/0/funding/0"
    assert fund["jurisdiction"] == "Synthetic state"
    assert fund["institute"] == "Synthetic institute"
    for row in rows:
        assert build.pointer_ancestors(dataset, row["source_pointer"])[-1]["source_ids"] == ["gb-source"]
        assert row["source_urls"] == dataset["sources"][0]["url"]
    institute["people"][0].pop("since")
    institute["agreements"][0].pop("date")
    dataset["jurisdictions"][0]["memberships"][0].pop("since")
    assert not {"appointment", "agreement", "membership"} & {r["type"] for r in build.event_rows(dataset)}


def test_event_order_precision_and_future_flag(dataset):
    institute = dataset["institutes"][0]
    base = institute["agreements"][0]
    institute["agreements"] = [
        base | {"id": "year-agreement", "counterparty": "Year", "date": "2024"},
        base | {"id": "month-agreement", "counterparty": "Month", "date": "2024-01"},
        base | {"id": "day-agreement", "counterparty": "Day", "date": "2024-01-01"},
    ]
    institute["people"][0]["since"] = "2027"
    rows = build.event_rows(dataset)
    assert rows == build.event_rows(copy.deepcopy(dataset))
    agreements = [r for r in rows if r["origin_kind"] == "agreements"]
    assert [r["description"] for r in agreements] == ["Signed: Year", "Signed: Month", "Signed: Day"]
    assert next(r for r in rows if r["type"] == "appointment")["scheduled"] is True
    assert all(not r["scheduled"] for r in agreements)
    network = next(r for r in rows if r["origin_kind"] == "network_milestones")
    assert network["jurisdiction_id"] == network["institute_id"] == ""


def test_missing_language_export_is_not_assumed_english(dataset):
    dataset["sources"][0].pop("language")
    assert build.tables(dataset)["sources"][0]["language"] == ""


def test_source_language_does_not_accept_three_letter_code(dataset):
    from jsonschema import Draft202012Validator

    dataset["sources"][0]["language"] = "cnr"
    validator = Draft202012Validator(json.loads(build.SCHEMA.read_text()))
    assert any(list(e.path) == ["sources", 0, "language"] for e in validator.iter_errors(dataset))


def test_identifier_and_network_consistency(dataset):
    dataset["jurisdictions"][0]["iso3"] = "GBR"
    assert any("non-null identifiers" in e for e in build.check_consistency(dataset))
    dataset["jurisdictions"][0]["identifier_source_ids"] = ["gb-source"]
    assert build.check_consistency(dataset) == []
    dataset["dataset"]["network_milestones"][0]["body"] = "absent"
    assert any("unknown membership body" in e for e in build.check_consistency(dataset))


def test_excerpt_requires_known_language(dataset):
    dataset["sources"][0].pop("language")
    assert any("source language is required" in e for e in build.check_excerpts(dataset))


def test_related_body_excerpt_needs_own_citation(dataset):
    dataset["sources"][0]["excerpts"][0]["supports"] = ["/jurisdictions/0/related_bodies/0/role"]
    assert build.check_excerpts(dataset) == []
    dataset["jurisdictions"][0]["related_bodies"][0].pop("source_ids")
    assert any("related body needs its own" in e for e in build.check_excerpts(dataset))


@pytest.mark.parametrize("fields", [
    {"designation_state": "designated"},
    {"designation_state": "proposed"},
    {"authority_roles": ["market_surveillance", "single_point_of_contact"]},
])
def test_authority_designation_requires_explicit_exclusion_and_citations(dataset, fields):
    from jsonschema import Draft202012Validator

    validator = Draft202012Validator(json.loads(build.SCHEMA.read_text()))
    body = dataset["jurisdictions"][0]["related_bodies"][0]
    body.update(kind="ai_act_authority", designation_state="designated", designation_basis="law")
    body.update(fields)
    assert list(validator.iter_errors(dataset)) == []
    body["counts_as_aisi"] = True
    assert list(validator.iter_errors(dataset))
    body.pop("counts_as_aisi")
    assert list(validator.iter_errors(dataset))
    body["counts_as_aisi"] = False
    body.pop("source_ids")
    assert list(validator.iter_errors(dataset))


def test_authority_codes_export_without_losing_sector_limits(dataset):
    body = dataset["jurisdictions"][0]["related_bodies"][0]
    body.update(kind="ai_act_authority", designation_state="proposed", designation_basis="none_yet", authority_roles=["notifying"])
    body["role"] = "Proposed notifying authority for the medical sector."
    row = build.tables(dataset)["related_bodies"][0]
    assert row["kind"] == "ai_act_authority"
    assert (row["designation_state"], row["authority_roles"], row["role"]) == (
        "proposed", "notifying", "Proposed notifying authority for the medical sector.",
    )


def test_name_history_reversal(dataset):
    dataset["institutes"][0]["names"].reverse()
    assert any("names not in date order" in e for e in build.check_consistency(dataset))


def test_empty_new_catalogues_have_headers_and_dictionaries(dataset, monkeypatch):
    dataset["dataset"]["exchange_rates"] = []
    dataset["dataset"]["network_milestones"] = []
    dataset["institutes"][0]["outputs"] = []
    dataset["sources"][0]["excerpts"] = []
    dataset["jurisdictions"][0]["related_bodies"] = []
    dataset["jurisdictions"][0]["timeline"] = []
    dataset["jurisdictions"][0]["status"] = "none_identified"
    dataset["jurisdictions"][0]["status_confidence"] = "low"
    dataset["jurisdictions"][0].pop("status_since", None)
    dataset["jurisdictions"][0]["institute_id"] = None
    dataset["jurisdictions"][0]["memberships"] = []
    dataset["institutes"] = []
    write_json(build.DATA, dataset)
    monkeypatch.setattr(sys, "argv", ["build.py"])
    build.main()
    out = build.DIST / "data"
    package = json.loads((out / "datapackage.json").read_text())
    resources = {r["name"]: r for r in package["resources"]}
    for name, columns in build.EMPTY_COLUMNS.items():
        with (out / f"{name}.csv").open(newline="") as stream:
            reader = csv.DictReader(stream)
            assert reader.fieldnames == columns
            assert list(reader) == []
        assert [f["name"] for f in resources[name]["schema"]["fields"]] == columns
    for table, column in (("events", "scheduled"), ("related_bodies", "counts_as_aisi")):
        field = next(f for f in resources[table]["schema"]["fields"] if f["name"] == column)
        assert field["type"] == "boolean"


def test_empty_headers_match_populated_tables(dataset):
    tables = build.tables(build.export_dataset(dataset))
    for name, columns in build.EMPTY_COLUMNS.items():
        if name in {"closest_bodies", "national_partners"}:
            assert tables[name] == []  # Covered with a populated fixture separately (tests/test_rows.py).
            continue
        assert list(tables[name][0]) == columns


def test_event_export_preserves_withdrawal_target(dataset):
    dataset["jurisdictions"][0]["timeline"] = [
        event("designated", "2024", "act"),
        event("withdrawn", "2025", "reversal", withdraws="act"),
    ]
    rows = [r for r in build.event_rows(dataset) if r["origin_kind"] == "timeline"]
    assert rows[1]["withdraws"] == rows[0]["origin_id"] == "GB:act"
    assert rows[0]["withdraws"] == ""


@pytest.mark.parametrize("collections", [
    ("jurisdictions", "institutes"),
    ("jurisdictions", "jurisdictions"),
    ("institutes", "institutes"),
])
def test_wikidata_unique_across_all_entities(dataset, collections):
    left, right = collections
    if left == right:
        dataset[right].append(copy.deepcopy(dataset[right][0]))
    dataset[left][0].update(wikidata="Q123", identifier_source_ids=["gb-source"])
    dataset[right][-1].update(wikidata="Q123", identifier_source_ids=["gb-source"])
    assert any("duplicate wikidata identifiers" in e for e in build.check_consistency(dataset))
    dataset[right][-1]["wikidata"] = "Q456"
    assert not any("duplicate wikidata identifiers" in e for e in build.check_consistency(dataset))


@pytest.mark.parametrize("body_sources", [[], ["gb-source"], ["different-source"]])
def test_related_bodies_list_pointer_cannot_borrow_jurisdiction_citation(dataset, body_sources):
    dataset["jurisdictions"][0]["related_bodies"][0]["source_ids"] = body_sources
    dataset["sources"][0]["excerpts"][0]["supports"] = ["/jurisdictions/0/related_bodies"]
    assert any("must identify an individual related body" in e for e in build.check_excerpts(dataset))


@pytest.mark.parametrize("table", ["jurisdictions", "institutes"])
@pytest.mark.parametrize("basis", ["recorded_full_check", "historical_upper_bound", None])
def test_csv_check_date_retains_its_basis(dataset, table, basis):
    if basis is not None:
        dataset[table][0]["check"]["last_checked_basis"] = basis
    row = build.tables(dataset)[table][0]
    assert row["last_checked"] == "2026-10-17"
    assert row["last_checked_basis"] == (basis or "")
    assert "historical upper bound" in build.FIELD_DOCS["last_checked"]
    assert "historical_upper_bound" in build.FIELD_DOCS["last_checked_basis"]


@pytest.mark.parametrize("missing", ["designation_basis", "source_ids", "counts_as_aisi"])
def test_authority_kind_requires_designation_contract(dataset, missing):
    from jsonschema import Draft202012Validator

    body = dataset["jurisdictions"][0]["related_bodies"][0]
    body.update(kind="ai_act_authority", designation_state="designated", designation_basis="law")
    validator = Draft202012Validator(json.loads(build.SCHEMA.read_text()))
    assert not list(validator.iter_errors(dataset))
    body.pop(missing)
    assert any(e.validator == "required" and missing in e.message for e in validator.iter_errors(dataset))


def test_changelog_requires_reader_summary(dataset):
    from jsonschema import Draft202012Validator

    validator = Draft202012Validator(json.loads(build.SCHEMA.read_text()))
    dataset["changelog"][0].pop("summary")
    assert any("summary" in e.message for e in validator.iter_errors(dataset))


def test_csv_preserves_later_institute_credit_date(dataset):
    dataset["institutes"][0]["outputs"][0]["credited_from"] = "2025-01"
    row = build.tables(dataset)["outputs"][0]
    assert row["date"] == "2024-10"
    assert row["credited_from"] == "2025-01"
    assert ("/institutes/0/outputs/0/credited_from", "2025-01") in list(build.walk_dates(dataset))


@pytest.mark.parametrize("strict", [None, False])
def test_validation_fails_a_compared_state_budget_without_gdp(dataset, strict):
    """Through check_consistency, in the default and the legacy (non-strict) path."""
    funding = dataset["institutes"][0]["funding"][0]
    funding.update(annual_amount=1, annual_basis="Stated annual amount.", annual_basis_code="other")
    funding.pop("eur_per_year_est", None)
    dataset["sources"][0]["excerpts"][0]["supports"].append("/institutes/0/funding/0/annual_amount")
    assert build.check_consistency(dataset, strict=strict) == []
    dataset["jurisdictions"][0].pop("gdp")
    assert any("institute gb-test" in e and "needs a positive gdp" in e for e in build.check_consistency(dataset, strict=strict))


@pytest.mark.parametrize(("doi", "valid"), [
    ("10.5281/zenodo.1234567", True),
    ("https://doi.org/10.5281/zenodo.1234567", False),  # the bare DOI, not its link
    ("10.5281/zenodo 1234567", False),
])
def test_dataset_doi_schema(dataset, doi, valid):
    from jsonschema import Draft202012Validator

    dataset["dataset"]["doi"] = doi
    errors = list(Draft202012Validator(json.loads(build.SCHEMA.read_text())).iter_errors(dataset))
    assert (not any(list(e.path) == ["dataset", "doi"] for e in errors)) == valid
