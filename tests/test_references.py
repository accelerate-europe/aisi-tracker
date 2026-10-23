"""Authored identities must survive insertions beside records citing the same source."""

import copy
import json

import pytest
from jsonschema import Draft202012Validator

from scripts import build, check
from scripts.migrate_references import migrate
from scripts.references import (
    authored_schema,
    materialise_references,
    pointer_ancestors,
)
from scripts.shared_claims import resolve


def test_migration_preserves_every_claim_and_is_idempotent(dataset):
    migrated = migrate(dataset)
    assert migrate(migrated) == migrated
    assert not list(
        Draft202012Validator(
            authored_schema(json.loads(build.SCHEMA.read_text()))
        ).iter_errors(migrated)
    )
    published = materialise_references(migrated)
    for old, new in zip(
        dataset["sources"][0]["excerpts"], published["sources"][0]["excerpts"]
    ):
        assert old["supports"] == new["supports"]
        for pointer in old["supports"]:
            assert (
                pointer_ancestors(dataset, pointer)[-1]
                == pointer_ancestors(published, pointer)[-1]
            )
    assert build.check_consistency(migrated, require_roster=True, complete=True) == []


def test_equal_source_neighbours_inserted_and_reordered(dataset):
    authored = migrate(dataset)
    output = authored["institutes"][0]["outputs"][0]
    authored["sources"][0]["excerpts"][0]["supports"].append(
        [
            "institutes",
            {"id": "gb-test"},
            "outputs",
            {"lineage": output["lineage"]},
            "title",
        ]
    )
    neighbour = {**output, "lineage": "neighbour", "title": "Different output"}
    authored["institutes"][0]["outputs"].insert(0, neighbour)
    authored["institutes"].insert(
        0, {"id": "other", "outputs": [copy.deepcopy(output)]}
    )
    published = materialise_references(authored)
    assert (
        published["sources"][0]["excerpts"][0]["supports"][-1]
        == "/institutes/1/outputs/1/title"
    )
    assert (
        pointer_ancestors(published, "/institutes/1/outputs/1/title")[-1]
        == "Synthetic evaluation report"
    )
    authored["institutes"].reverse()
    authored["institutes"][0]["outputs"].reverse()
    assert (
        materialise_references(authored)["sources"][0]["excerpts"][0]["supports"][-1]
        == "/institutes/0/outputs/0/title"
    )
    assert check.supporting(
        materialise_references(authored), "/institutes/0/outputs/0/title"
    )


def test_sum_components_follow_identity_and_do_not_follow_neighbours():
    data = {
        "institutes": [
            {
                "id": "i",
                "funding": [
                    {"id": "a", "amount": 10, "source_ids": ["same"]},
                    {"id": "b", "amount": 20, "source_ids": ["same"]},
                    {"id": "total", "sum_of": ["a", "b"]},
                ],
            }
        ]
    }
    data["institutes"][0]["funding"].reverse()
    data["institutes"][0]["funding"].insert(
        1, {"id": "new", "amount": 99, "source_ids": ["same"]}
    )
    published = materialise_references(data)
    assert published["institutes"][0]["funding"][0]["sum_of"] == ["a", "b"]
    assert data["institutes"][0]["funding"][0]["sum_of"] == ["a", "b"]


@pytest.mark.parametrize("identity", ["missing", "same"])
def test_unknown_and_ambiguous_selectors_fail(identity):
    data = {
        "institutes": [{"id": "same"}, {"id": "same"}],
        "sources": [{"excerpts": [{"supports": [["institutes", {"id": identity}]]}]}],
    }
    with pytest.raises(ValueError, match="matched [02] records"):
        materialise_references(data)


def test_mutable_selector_is_not_an_authored_reference():
    data = {
        "sources": [
            {"excerpts": [{"supports": [["institutes", {"name": "renameable"}]]}]}
        ]
    }
    with pytest.raises(ValueError, match="stable identity"):
        materialise_references(data)


def test_pointer_escaping_is_rfc6901():
    data = {"a/b": [{"id": "x", "~key": 12}]}
    assert resolve(data, ["a/b", {"id": "x"}, "~key"])[2] == "/a~1b/0/~0key"
    assert pointer_ancestors(data, "/a~1b/0/~0key")[-1] == 12


def test_browser_skip_requires_explicit_local_skip(monkeypatch, capsys):
    monkeypatch.setattr(check, "git", lambda *args, **kwargs: "0" * 40)
    monkeypatch.setattr(check, "make_snapshot", lambda *args, **kwargs: "snapshot")
    monkeypatch.setattr(
        check,
        "RUNNERS",
        {step: lambda ctx: check.Result("stub", "PASS") for step in check.STEPS},
    )
    monkeypatch.setattr("sys.argv", ["check.py", "--skip-browser"])
    with pytest.raises(SystemExit):
        check.main()
    assert "--skip-browser requires --allow-skip" in capsys.readouterr().err


def test_real_migration_preserves_all_reference_targets():
    from pathlib import Path
    from scripts.references import support_pointer

    original = json.loads((Path(__file__).parents[1] / "data/aisi.json").read_text())
    migrated = migrate(original)
    assert not list(
        Draft202012Validator(
            authored_schema(json.loads(build.SCHEMA.read_text()))
        ).iter_errors(migrated)
    )
    assert build.check_consistency(migrated, require_roster=True, complete=True) == []
    assert sum(len(s.get("excerpts", [])) for s in original["sources"]) == sum(
        len(s.get("excerpts", [])) for s in migrated["sources"]
    )
    assert len(original["dataset"]["shared_claims"]) == len(
        migrated["dataset"]["shared_claims"]
    )
    assert migrate(migrated) == migrated
    for source, changed in zip(original["sources"], migrated["sources"]):
        for excerpt, revised in zip(
            source.get("excerpts", []), changed.get("excerpts", [])
        ):
            assert [support_pointer(original, p) for p in excerpt["supports"]] == [
                resolve(migrated, p)[2] for p in revised["supports"]
            ]
    for claim, revised in zip(
        original["dataset"]["shared_claims"], migrated["dataset"]["shared_claims"]
    ):
        assert [resolve(original, p)[2] for p in claim["targets"]] == [
            resolve(migrated, p)[2] for p in revised["targets"]
        ]
    assert [
        r.get("sum_of")
        for i in materialise_references(original)["institutes"]
        for r in i.get("funding", [])
    ] == [
        r.get("sum_of")
        for i in materialise_references(migrated)["institutes"]
        for r in i.get("funding", [])
    ]


def test_generated_download_is_validated(dataset, monkeypatch, capsys):
    from conftest import write_json

    write_json(build.DATA, dataset)
    broken = copy.deepcopy(dataset)
    broken["institutes"][0]["operating"] = "invalid generated value"
    monkeypatch.setattr(build, "export_dataset", lambda data, **kwargs: broken)
    monkeypatch.setattr("sys.argv", ["build.py", "--check"])
    with pytest.raises(SystemExit):
        build.main()
    assert "published institutes/0/operating" in capsys.readouterr().err


@pytest.mark.parametrize(
    "collection, key", [("funding", "currency"), ("staff", "body")]
)
def test_identity_keys_are_scoped_to_their_collection(collection, key):
    data = {collection: [{"id": "stable", key: "x"}], "sources": [{"excerpts": [{"supports": [[collection, {key: "x"}]]}]}]}
    with pytest.raises(ValueError, match="stable identity"):
        materialise_references(data)


def test_unreferenced_duplicate_record_identity_fails():
    with pytest.raises(ValueError, match="duplicate stable record identity"):
        materialise_references(
            {"institutes": [{"funding": [{"id": "a"}, {"id": "a"}]}]}
        )


def test_stable_mode_rejects_positional_components(dataset):
    data = migrate(dataset)
    data["institutes"][0]["funding"][0]["sum_of"] = [0]
    with pytest.raises(ValueError, match="component IDs"):
        materialise_references(data)


def test_mixed_components_rejected_even_without_excerpt_selectors():
    with pytest.raises(ValueError, match="component IDs"):
        materialise_references(
            {"institutes": [{"funding": [{"id": "a", "sum_of": [0, "a"]}]}]}
        )


def test_mixed_excerpt_representation_rejected(dataset):
    data = migrate(dataset)
    data["sources"][0]["excerpts"][0]["supports"].append("/institutes/0/name")
    with pytest.raises(ValueError, match="cannot mix"):
        materialise_references(data)


def test_shared_mutable_target_rejected_only_in_stable_mode(dataset):
    legacy = copy.deepcopy(dataset)
    legacy["dataset"]["shared_claims"] = [
        {"targets": [["institutes", {"name": "Synthetic institute"}, "name"]]}
    ]
    assert materialise_references(legacy)
    data = migrate(dataset)
    data["dataset"]["shared_claims"] = legacy["dataset"]["shared_claims"]
    with pytest.raises(ValueError, match="stable identity"):
        materialise_references(data)


def test_stable_mode_requires_identity_for_new_rows(dataset):
    data = migrate(dataset)
    data["institutes"][0]["staff"].append({"as_of": "2026"})
    with pytest.raises(ValueError, match="record needs permanent id"):
        materialise_references(data)


def test_stable_input_builds_checked_download(dataset, monkeypatch, tmp_path):
    from conftest import write_json

    write_json(build.DATA, migrate(dataset))
    monkeypatch.setattr("sys.argv", ["build.py", "--check"])
    build.main()
    out = tmp_path / "published"
    monkeypatch.setattr("sys.argv", ["build.py", "--out", str(out)])
    build.main()
    published = json.loads((out / "data/aisi.json").read_text())
    for source in published["sources"]:
        for excerpt in source["excerpts"]:
            for pointer in excerpt["supports"]:
                assert isinstance(pointer, str)
                pointer_ancestors(published, pointer)
    for institute in published["institutes"]:
        for record in institute["funding"]:
            assert all(isinstance(identity, str) for identity in record.get("sum_of", []))


@pytest.mark.parametrize("stable_base", [False, True])
def test_report_resolves_changed_and_removed_claims_in_snapshot(tmp_path, monkeypatch, stable_base):
    import shutil
    from pathlib import Path

    old = {"institutes": [{"id": "a", "name": "Before", "host": "Removed"}], "sources": [{"id": "s", "excerpts": [{"text": "Name evidence", "supports": ["/institutes/0/name"]}, {"text": "Host evidence", "supports": ["/institutes/0/host"]}]}]}
    new = copy.deepcopy(old)
    new["institutes"][0] = {"id": "a", "name": "After"}
    new["sources"][0]["excerpts"] = [{"text": "Name evidence", "supports": [["institutes", {"id": "a"}, "name"]]}]
    if stable_base:
        for excerpt in old["sources"][0]["excerpts"]:
            excerpt["supports"] = [["institutes", {"id": "a"}, pointer.rsplit("/", 1)[1]] for pointer in excerpt["supports"]]
    snapshot = tmp_path / "snapshot"
    (snapshot / "data").mkdir(parents=True)
    (snapshot / "scripts").mkdir()
    for filename in ("references.py", "shared_claims.py"):
        shutil.copy(Path(__file__).parents[1] / "scripts" / filename, snapshot / "scripts" / filename)
    (snapshot / "data/aisi.json").write_text(json.dumps(new))
    ctx = check.Context(tmp_path, snapshot, tmp_path, tmp_path, "HEAD", False, tmp_path / "dist", tmp_path / "cache")
    monkeypatch.setattr(check, "git", lambda *args, **kwargs: json.dumps(old))
    result = check.step_compare(ctx)
    assert result.status == "PASS"
    report = ctx.sections["compare"]
    assert '| `/institutes/0/name` | Before | After | s: "Name evidence" |' in report
    assert '| `/institutes/0/host (removed)` | Removed |  | s: "Host evidence" |' in report
    # A conflicting live module cannot replace the resolver in this snapshot.
    (snapshot / "scripts/references.py").write_text('raise ValueError("snapshot resolver sentinel")')
    with pytest.raises(ValueError, match="snapshot resolver sentinel"):
        check.resolved_report_data(ctx, new)
