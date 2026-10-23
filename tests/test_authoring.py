"""Production CLI accepts primary facts and rejects authored generated copies."""

import copy
import sys

import pytest

from conftest import write_json
from scripts import build, release_checks
from scripts.migrate_facts import migrate
from scripts.migrate_references import migrate as stable
from scripts.references import materialise_references

PRODUCTION_STRICT = build.fact_pipeline.AUTHORING_STRICT


@pytest.mark.parametrize("mutation", ["status", "usd", "credited_from", "pointer"])
def test_production_cli_enforces_primary_authored_boundary(
    dataset, monkeypatch, capsys, mutation
):
    reviewed = stable(dataset)
    reviewed["institutes"][0]["outputs"][0]["credit"] = {
        "institute_id": "gb-test",
        "date": "2024-10",
    }
    primary = migrate(reviewed)
    output = primary["institutes"][0]["outputs"][0]
    primary["sources"][0]["excerpts"][0]["supports"].extend(
        [
            [
                "institutes",
                {"id": "gb-test"},
                "outputs",
                {"lineage": output["lineage"]},
                "credit",
                field,
            ]
            for field in ("institute_id", "date")
        ]
    )
    # Legacy fixtures intentionally relax strictness; restore the actual module default.
    monkeypatch.setattr(build.fact_pipeline, "AUTHORING_STRICT", PRODUCTION_STRICT)
    write_json(build.DATA, primary)
    readme = build.ROOT / "README.md"
    readme.write_bytes(
        release_checks.generated_counts(
            readme.read_bytes(), build.export_dataset(primary)
        )
    )
    monkeypatch.setattr(sys, "argv", ["build.py", "--check"])
    build.main()
    broken = copy.deepcopy(primary)
    if mutation == "status":
        broken["institutes"][0]["status"] = "established"
    elif mutation == "usd":
        broken["institutes"][0]["funding"][0]["eur_per_year_est"] = None
    elif mutation == "credited_from":
        broken["institutes"][0]["outputs"][0]["credited_from"] = output["credit"][
            "date"
        ]
    else:
        broken = materialise_references(broken)
    write_json(build.DATA, broken)
    with pytest.raises(SystemExit) as error:
        build.main()
    assert error.value.code == 1
    message = capsys.readouterr().err
    assert (
        "False schema" if mutation != "pointer" else "is not of type 'array'"
    ) in message
