"""Release review scopes from small datasets in isolated git repositories."""

import copy
import hashlib
import json
import random
import subprocess

import pytest

from scripts import release_scope


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()


def write_data(repo, data):
    (repo / "data/aisi.json").write_text(json.dumps(data), encoding="utf-8")


def commit(repo):
    git(repo, "add", "data/aisi.json")
    git(repo, "commit", "-qm", "Synthetic data")
    return git(repo, "rev-parse", "HEAD")


def target(identity="FR"):
    return ["jurisdictions", {"id": identity}, "timeline", {"id": "act"}, "event"]


def source(identity="source", record="FR"):
    return {"id": identity, "url": "https://example.org/source", "title": "SOURCE_VALUE_SENTINEL",
            "excerpts": [{"id": "quote", "text": "QUOTATION_SENTINEL", "supports": [target(record)]}]}


@pytest.fixture
def make_repo(tmp_path):
    def make(sources=None, tag=True):
        repo = tmp_path / "repo"
        repo.mkdir()
        git(repo, "init", "-q")
        (repo / "data").mkdir()
        data = {
            "dataset": {"version": "1.0.0", "as_of": "2026-01-01", "citation": {"text": "Old citation"},
                        "method": [{"id": "rule", "text": "Old wording"}]},
            "jurisdictions": [{"id": "FR", "name": "BEFORE_VALUE_SENTINEL"}, {"id": "GB", "name": "Other"}],
            "institutes": [{"id": "institute", "name": "Institute"}],
            "sources": copy.deepcopy(sources or []),
            "changelog": [],
        }
        write_data(repo, data)
        commit(repo)
        if tag:
            git(repo, "tag", "v1.0.0")
        return repo, data
    return make


def run_scope(repo, data, out=None, *args):
    write_data(repo, data)
    out = out or repo.parent / "scope"
    assert release_scope.main(["--seed", "1", "--out", str(out), *args], root=repo) == 0
    return (json.loads((out / "scope.json").read_text()),
            json.loads((out / "detail.json").read_text()), (out / "scope.md").read_text())


def ids(scope):
    return {(row["collection"], row["id"]) for row in scope["records"]}


def test_changed_field_uses_working_tree_and_keeps_blind_reports_free_of_values(make_repo):
    repo, data = make_repo()
    base = git(repo, "rev-parse", "HEAD")
    data["institutes"][0]["name"] = "Committed change"
    write_data(repo, data)
    head = commit(repo)
    data["jurisdictions"][0]["name"] = "Staged change"
    write_data(repo, data)
    git(repo, "add", "data/aisi.json")
    data["jurisdictions"][0]["name"] = "AFTER_VALUE_SENTINEL"
    data["sources"].append(source())
    scope, detail, md = run_scope(repo, data)
    assert scope["base_ref"] == "v1.0.0"
    assert scope["base_sha"] == base and scope["head_sha"] == head
    assert scope["claims"] == [
        {"collection": "institutes", "id": "institute", "pointers": ["/institutes/0/name"]},
        {"collection": "jurisdictions", "id": "FR", "pointers": ["/jurisdictions/0/name"]},
    ]
    record = next(row for row in scope["records"] if row["id"] == "FR")
    assert record["reasons"] == ["/jurisdictions/0/name", "/sources/0"]
    change = next(row for row in detail["changes"] if row["pointer"] == "/jurisdictions/0/name")
    assert change == {"pointer": "/jurisdictions/0/name", "before": "BEFORE_VALUE_SENTINEL", "after": "AFTER_VALUE_SENTINEL"}
    blind = (repo.parent / "scope/scope.json").read_text() + md
    for sentinel in ("BEFORE_VALUE_SENTINEL", "AFTER_VALUE_SENTINEL", "QUOTATION_SENTINEL", "SOURCE_VALUE_SENTINEL"):
        assert sentinel not in blind
        assert sentinel in json.dumps(detail)
    assert "for the comparer only" in md


def test_added_sources_support_records_and_get_verification_flags(make_repo):
    repo, data = make_repo()
    data["sources"] = [source("z"), {"id": "a", "title": "No quotations yet"}]
    scope, detail, md = run_scope(repo, data)
    assert ids(scope) == {("jurisdictions", "FR")}
    assert scope["claims"] == []
    assert scope["sources"] == [
        {"id": "a", "change": "added", "keys": ["id", "title"]},
        {"id": "z", "change": "added", "keys": ["excerpts", "id", "title", "url"]},
    ]
    assert scope["verify_sources"] == ["a", "z"]
    assert scope["excerpts"] == [{"source_id": "z", "excerpt_id": "quote"}]
    assert detail["excerpts"][0]["after"] == data["sources"][0]["excerpts"]
    assert "uv run --locked scripts/verify_excerpts.py --source a --source z" in md


def test_edited_quotation_is_in_scope_and_detail(make_repo):
    repo, data = make_repo([source()])
    before = copy.deepcopy(data["sources"][0]["excerpts"])
    data["sources"][0]["excerpts"][0]["text"] = "EDITED_QUOTATION_SENTINEL"
    scope, detail, md = run_scope(repo, data)
    assert ids(scope) == {("jurisdictions", "FR")}
    assert scope["claims"] == []
    assert scope["excerpts"] == [{"source_id": "source", "excerpt_id": "quote"}]
    assert scope["verify_sources"] == ["source"]
    assert scope["sources"] == [{"id": "source", "change": "changed", "keys": ["excerpts"]}]
    assert detail["excerpts"] == [{"source_id": "source", "excerpt_id": "quote", "before": before,
                                   "after": data["sources"][0]["excerpts"]}]
    assert "EDITED_QUOTATION_SENTINEL" not in json.dumps(scope) + md


@pytest.mark.parametrize("change", ["binding", "gloss", "removal"])
def test_binding_gloss_and_removal_keep_old_support_without_reverification(make_repo, change):
    original = source()
    del original["excerpts"][0]["id"]
    repo, data = make_repo([original])
    excerpt = data["sources"][0]["excerpts"][0]
    if change == "binding":
        excerpt["supports"] = [target("GB")]
    elif change == "gloss":
        excerpt["gloss"] = "GLOSS_SENTINEL"
    else:
        data["sources"][0]["excerpts"] = []
    scope, detail, md = run_scope(repo, data)
    expected = {("jurisdictions", "FR"), ("jurisdictions", "GB")} if change == "binding" else {("jurisdictions", "FR")}
    assert ids(scope) == expected
    assert scope["claims"] == scope["verify_sources"] == []
    assert scope["excerpts"] == [{"source_id": "source", "excerpt_id":
                                 "sha256:" + hashlib.sha256(b"QUOTATION_SENTINEL").hexdigest()}]
    assert detail["bindings"]
    assert "GLOSS_SENTINEL" not in json.dumps(scope) + md


@pytest.mark.parametrize("change", ["text", "copy", "gloss"])
def test_anchored_excerpts_reuse_verification_semantics(make_repo, change):
    original = source()
    original["excerpts"][0].update(url="https://example.org/copy", anchors=[{"kind": "same_copy", "text": "Anchor"}])
    repo, data = make_repo([original])
    excerpt = data["sources"][0]["excerpts"][0]
    if change == "text":
        excerpt["anchors"][0]["text"] = "Changed anchor"
    elif change == "copy":
        excerpt["url"] = "https://example.org/other-copy"
    else:
        excerpt["anchors"][0]["gloss"] = "Changed anchor gloss"
    scope, _, _ = run_scope(repo, data)
    assert scope["excerpts"] == [{"source_id": "source", "excerpt_id": "quote"}]
    assert scope["verify_sources"] == ([] if change == "gloss" else ["source"])


def test_changed_source_metadata_reaches_supports_and_nested_source_ids(make_repo):
    repo, data = make_repo([source()])
    data["institutes"][0]["outputs"] = [{"lineage": "report", "source_ids": ["source"]}]
    write_data(repo, data)
    commit(repo)
    git(repo, "tag", "v1.0.1")
    data["sources"][0].update(url="https://example.org/new", checked="2026-02-01")
    scope, _, _ = run_scope(repo, data)
    assert ids(scope) == {("jurisdictions", "FR"), ("institutes", "institute")}
    assert scope["claims"] == scope["verify_sources"] == []
    assert scope["excerpts"] == [{"source_id": "source", "excerpt_id": "quote"}]
    assert scope["sources"] == [{"id": "source", "change": "changed", "keys": ["checked", "url"]}]


def test_removed_record_and_source_indices_use_the_base(make_repo):
    repo, data = make_repo([source("first"), source("second", "GB")])
    data["jurisdictions"].pop(0)
    data["jurisdictions"][0]["name"] = "Changed GB"
    data["institutes"].append({"id": "added", "name": "New institute"})
    data["sources"].pop(0)
    del data["sources"][0]["title"]
    scope, detail, _ = run_scope(repo, data)
    assert scope["claims"] == [
        {"collection": "institutes", "id": "added", "pointers": ["/institutes/1"]},
        {"collection": "jurisdictions", "id": "FR", "pointers": ["/jurisdictions/0 (removed)"]},
        {"collection": "jurisdictions", "id": "GB", "pointers": ["/jurisdictions/0/name"]},
    ]
    assert scope["sources"] == [
        {"id": "first", "change": "removed", "keys": ["excerpts", "id", "title", "url"]},
        {"id": "second", "change": "changed", "keys": ["title"]},
    ]
    gb = next(row for row in scope["records"] if row["id"] == "GB")
    assert gb["reasons"] == ["/jurisdictions/0/name", "/sources/1/title (removed)"]
    assert scope["verify_sources"] == []
    removed = next(row for row in detail["changes"] if row["pointer"] == "/jurisdictions/0 (removed)")
    assert removed["before"]["id"] == "FR" and removed["after"] is None


def test_dataset_wording_is_separate_and_release_metadata_is_ignored(make_repo):
    repo, data = make_repo()
    data["dataset"].update(version="1.0.1", as_of="2026-02-01", citation={"text": "NEW_CITATION"})
    data["changelog"] = [{"version": "1.0.1", "summary": "CHANGELOG_SENTINEL"}]
    data["dataset"]["method"][0]["text"] = "WORDING_SENTINEL"
    scope, detail, md = run_scope(repo, data)
    assert scope["records"] == scope["claims"] == scope["blind_draw"] == []
    assert scope["dataset_changes"] == ["/dataset/method/0/text"]
    assert detail["changes"] == [{"pointer": "/dataset/method/0/text", "before": "Old wording", "after": "WORDING_SENTINEL"}]
    assert "WORDING_SENTINEL" not in json.dumps(scope) + md
    assert "Owner wording approval" in md and "nothing changed" not in md


@pytest.mark.parametrize("metadata_only", [False, True])
def test_empty_scope_never_falls_back_to_all_records_or_sources(make_repo, capsys, metadata_only):
    repo, data = make_repo([source()])
    if metadata_only:
        data["dataset"].update(version="1.0.1-dev", as_of="2026-02-01", citation={})
        data["changelog"] = [{"version": "1.0.1"}]
        data["jurisdictions"].reverse()
    scope, detail, md = run_scope(repo, data)
    for name in ("records", "claims", "excerpts", "sources", "verify_sources", "dataset_changes", "blind_draw"):
        assert scope[name] == []
    assert detail["changes"] == detail["excerpts"] == detail["bindings"] == []
    assert "nothing changed since v1.0.0" in md
    assert "nothing changed since v1.0.0" in capsys.readouterr().out
    assert "scripts/verify_excerpts.py" not in md


@pytest.mark.parametrize("collection", ["institutes", "jurisdictions"])
@pytest.mark.parametrize("field_change", [False, True])
def test_check_only_records_stay_in_scope_but_are_not_drawn(make_repo, capsys, collection, field_change):
    repo, data = make_repo()
    record = data[collection][0]
    record["check"] = {"partial": []}
    write_data(repo, data)
    commit(repo)
    git(repo, "tag", "v1.0.1")
    record["check"]["partial"].append({"checked": "2026-02-01"})
    reasons = [f"/{collection}/0/check/partial/0"]
    if field_change:
        record["name"] = "Changed name"
        reasons.append(f"/{collection}/0/name")
    scope, _, md = run_scope(repo, data)
    identity = {"collection": collection, "id": record["id"]}
    assert scope["records"] == [{**identity, "reasons": reasons}]
    assert scope["draw_population"] == int(field_change)
    assert scope["draw_excluded_check_only"] == ([] if field_change else [record["id"]])
    assert scope["blind_draw"] == ([identity] if field_change else [])
    assert f"Records excluded from the draw as check-only: {int(not field_change)}." in md
    message = f"Only {int(field_change)} in the draw population; taking them all."
    assert message in md and message in capsys.readouterr().out


def test_seeded_draw_is_reproducible_sorted_and_capped(make_repo):
    repo, data = make_repo()
    data["jurisdictions"] += [{"id": f"J{n}", "name": "Added"} for n in range(8)]
    first = repo.parent / "first"
    scope, _, _ = run_scope(repo, data, first, "--seed", "17", "--draw", "3")
    second = repo.parent / "second"
    second.mkdir()
    run_scope(repo, data, second, "--seed", "17", "--draw", "3")
    for name in ("scope.json", "scope.md", "detail.json"):
        assert (first / name).read_bytes() == (second / name).read_bytes()
    assert scope["seed"] == 17
    expected = sorted(random.Random(17).sample([f"J{n}" for n in range(8)], 3))
    assert [row["id"] for row in scope["blind_draw"]] == expected
    capped, _, md = run_scope(repo, data, repo.parent / "capped", "--draw", "20")
    assert len(capped["blind_draw"]) == 8
    assert "Only 8 in the draw population; taking them all." in md


def test_no_release_tag_is_refused_but_explicit_base_is_allowed(make_repo, capsys):
    repo, data = make_repo(tag=False)
    out = repo.parent / "scope"
    assert release_scope.main(["--seed", "1", "--out", str(out)], root=repo) == 2
    assert "no reachable v* release tag" in capsys.readouterr().err
    assert not out.exists()
    scope, _, _ = run_scope(repo, data, out, "--base", "HEAD")
    assert scope["base_ref"] == "HEAD"


def test_default_base_uses_annotated_tag_even_with_same_named_branch(make_repo):
    repo, data = make_repo()
    data["dataset"]["version"] = "1.0.1"
    write_data(repo, data)
    tagged = commit(repo)
    git(repo, "tag", "-a", "v1.0.1", "-m", "Release")
    git(repo, "branch", "v1.0.1", "v1.0.0")
    scope, _, _ = run_scope(repo, data)
    assert scope["base_ref"] == "v1.0.1" and scope["base_sha"] == tagged


@pytest.mark.parametrize("file", [False, True])
def test_output_must_be_new_or_empty(make_repo, capsys, file):
    repo, _ = make_repo()
    out = repo.parent / "occupied"
    if file:
        out.write_text("Keep")
    else:
        out.mkdir()
        (out / "keep").write_text("Keep")
    assert release_scope.main(["--seed", "1", "--out", str(out)], root=repo) == 2
    assert "must be new or empty" in capsys.readouterr().err
    assert (out if file else out / "keep").read_text() == "Keep"


@pytest.mark.parametrize("args", [[], ["--seed", "1", "--draw", "-1"]])
def test_invalid_draw_arguments_are_refused(make_repo, args):
    repo, _ = make_repo()
    with pytest.raises(SystemExit) as exc:
        release_scope.main(["--out", str(repo.parent / "scope"), *args], root=repo)
    assert exc.value.code == 2


def test_draw_zero_needs_no_seed_and_invalid_base_is_refused(make_repo, capsys):
    repo, _ = make_repo()
    out = repo.parent / "scope"
    assert release_scope.main(["--draw", "0", "--base", "missing", "--out", str(out)], root=repo) == 2
    assert "not a commit" in capsys.readouterr().err
    assert not out.exists()
    assert release_scope.main(["--draw", "0", "--out", str(out)], root=repo) == 0
    scope = json.loads((out / "scope.json").read_text())
    assert scope["seed"] is None and scope["blind_draw"] == []
