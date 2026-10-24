"""The one check command: snapshot binding, outcome rules and the base comparison helpers."""

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from scripts import check


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def test_snapshot_is_the_staged_index_not_the_working_tree(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    (repo / "a.txt").write_text("committed\n")
    git(repo, "add", "a.txt")
    git(repo, "-c", "user.name=t", "-c", "user.email=t@example.org", "commit", "-q", "-m", "base")
    (repo / "a.txt").write_text("staged\n")
    git(repo, "add", "a.txt")
    (repo / "a.txt").write_text("unstaged\n")
    (repo / "untracked.txt").write_text("untracked\n")
    first = check.make_snapshot(repo, tmp_path / "one")
    assert (tmp_path / "one/a.txt").read_text() == "staged\n"
    assert not (tmp_path / "one/untracked.txt").exists()
    # The snapshot carries its own index so tools that list tracked files still work.
    listed = subprocess.run(["git", "-C", str(tmp_path / "one"), "ls-files"], capture_output=True, text=True, check=True).stdout
    assert listed.split() == ["a.txt"]
    (repo / "a.txt").write_text("other\n")
    assert check.make_snapshot(repo, tmp_path / "two") == first
    git(repo, "add", "a.txt")
    assert check.make_snapshot(repo, tmp_path / "three") != first


@pytest.mark.parametrize(("statuses", "allow_skip", "blocked"), [
    (["PASS", "PASS"], False, 0),
    (["PASS", "NOT RUN"], False, 1),
    (["PASS", "NOT RUN"], True, 0),
    (["FAIL", "NOT RUN"], True, 1),
])
def test_not_run_fails_unless_skips_are_allowed(statuses, allow_skip, blocked):
    results = [check.Result(f"s{i}", status) for i, status in enumerate(statuses)]
    assert len(check.blocking(results, allow_skip)) == blocked


def test_gate_exit_codes(tmp_path):
    log = tmp_path / "gate.log"
    log.write_text("error: card misaligned\n")
    assert check.gate_result("gate", 0, 1, log).status == "PASS"
    assert check.gate_result("gate", check.NOT_RUN, 1, log).status == "NOT RUN"
    failed = check.gate_result("gate", 1, 1, log)
    assert failed.status == "FAIL" and "card misaligned" in failed.detail


def test_allow_skip_is_refused_in_ci(monkeypatch, capsys):
    monkeypatch.setenv("CI", "true")
    monkeypatch.setattr(sys, "argv", ["check.py", "--allow-skip"])
    with pytest.raises(SystemExit) as caught:
        check.main()
    assert caught.value.code == 2
    assert "local runs only" in capsys.readouterr().err


def test_fast_group_expands_and_unknown_steps_fail(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["check.py", "--only", "fast,bogus"])
    with pytest.raises(SystemExit):
        check.main()
    assert "unknown steps: bogus" in capsys.readouterr().err
    assert check.GROUPS["fast"] == ("validate", "lint", "tests")


@pytest.mark.parametrize("argv, message", [
    (["--only", "fast", "--gate-merge", "results"], "--gate-merge needs the gate step"),
    (["--only", "gate", "--gate-results", "shard.json"], "go together"),
    (["--only", "gate", "--gate-shard", "1/4"], "go together"),
    (["--only", "gate", "--gate-shard", "1/4", "--gate-results", "s.json", "--gate-merge", "results"], "exclude each other"),
    (["--only", "build,gate", "--gate-dist", "site"], "checks an existing build"),
    (["--only", "gate", "--gate-dist", "site", "--out", "dist"], "checks an existing build"),
    (["--only", "gate", "--gate-jobs", "0"], "--gate-jobs needs at least 1"),
    (["--only", "tests", "--test-workers", "-1"], "--test-workers needs 0 or more"),
    *((["--only", "gate", "--gate-shard", spec, "--gate-results", "s.json"], "expects K/N") for spec in ("", "0/4", "5/4", "4", "a/b", "1/0")),
])
def test_gate_and_test_options_are_checked_before_anything_runs(monkeypatch, capsys, argv, message):
    monkeypatch.setattr(sys, "argv", ["check.py", *argv])
    with pytest.raises(SystemExit) as caught:
        check.main()
    assert caught.value.code == 2
    assert message in capsys.readouterr().err


def test_gate_arguments_reach_check_page(tmp_path):
    from argparse import Namespace

    args = Namespace(gate_shard="2/4", gate_results=tmp_path / "s.json", gate_merge=None, gate_jobs=3)
    assert check.gate_arguments(args) == ["--shard=2/4", f"--results={tmp_path / 's.json'}", "--jobs=3"]
    args = Namespace(gate_shard=None, gate_results=None, gate_merge=tmp_path / "gate", gate_jobs=None)
    assert check.gate_arguments(args) == [f"--merge={tmp_path / 'gate'}"]
    assert check.gate_arguments(Namespace(gate_shard=None, gate_results=None, gate_merge=None, gate_jobs=None)) == []


def test_diff_matches_records_by_id_and_reports_new_pointers():
    old = {"jurisdictions": [{"id": "AT", "status": "exploring"}, {"id": "GB", "status": "planned"}]}
    new = {"jurisdictions": [{"id": "GB", "status": "established"}, {"id": "FR", "status": "planned"}]}
    assert check.diff(old, new) == [
        ("/jurisdictions/0/status", "planned", "established"),
        ("/jurisdictions/1", None, {"id": "FR", "status": "planned"}),
        ("/jurisdictions/0 (removed)", {"id": "AT", "status": "exploring"}, None),
    ]


def test_removed_record_is_matched_to_its_own_evidence_in_the_base():
    old = {"jurisdictions": [{"id": "AT"}, {"id": "GB"}], "sources": [{"id": "s", "excerpts": [
        {"text": "Austria", "supports": ["/jurisdictions/0/status"]},
        {"text": "UK", "supports": ["/jurisdictions/1/status"]},
    ]}]}
    [(pointer, _, after)] = check.diff(old["jurisdictions"], [{"id": "GB"}], "/jurisdictions")
    assert after is None
    assert [e["text"] for _, e in check.supporting(old, pointer)] == ["Austria"]


def test_nested_removal_keeps_its_base_pointer_after_reordering():
    old = [{"id": "AT", "name": "Austria"}, {"id": "GB", "name": "UK", "name_local": "UK"}]
    new = [{"id": "GB", "name": "UK"}]
    assert check.diff(old, new, "/institutes") == [
        ("/institutes/1/name_local (removed)", "UK", None),
        ("/institutes/0 (removed)", old[0], None),
    ]


def test_supporting_excerpts_cover_the_claim_its_parts_and_its_record():
    data = {"sources": [{"id": "s", "excerpts": [
        {"text": "exact", "supports": ["/jurisdictions/0/status"]},
        {"text": "inside", "supports": ["/jurisdictions/0/timeline/1/date"]},
        {"text": "unrelated", "supports": ["/jurisdictions/10/status"]},
    ]}]}
    assert [e["text"] for _, e in check.supporting(data, "/jurisdictions/0/status")] == ["exact"]
    assert [e["text"] for _, e in check.supporting(data, "/jurisdictions/0")] == ["exact", "inside"]
    assert [e["text"] for _, e in check.supporting(data, "/jurisdictions/0/timeline/1/date/x")] == ["inside"]


@pytest.mark.parametrize(("report", "status"), [
    ({"sources": [{"excerpts": [{"source_id": "s", "text": "a", "status": "matched"}, {"source_id": "s", "text": "b", "status": "not_cached"}]}]}, "NOT RUN"),
    ({"sources": [{"excerpts": [{"source_id": "s", "text": "a", "status": "mismatch"}, {"source_id": "s", "text": "b", "status": "matched"}]}]}, "FAIL"),
    ({"sources": [{"excerpts": [{"source_id": "s", "text": "a", "status": "matched"}]}]}, "FAIL"),
    ({"partial": True, "sources": [{"excerpts": [{"source_id": "s", "text": "a", "status": "matched"}, {"source_id": "s", "text": "b", "status": "matched"}]}]}, "FAIL"),
])
def test_quote_verification_needs_a_complete_report(tmp_path, monkeypatch, report, status):
    (tmp_path / "cache").mkdir()
    ctx = check.Context(tmp_path, tmp_path, tmp_path, tmp_path, "HEAD", False, tmp_path / "dist", tmp_path / "cache")

    def fake_run(ctx, step, cmd, cwd=None):
        (tmp_path / "excerpt-verification.json").write_text(json.dumps(report))
        log = tmp_path / "log"
        log.write_text("verifier output\n")
        return 1, 0.0, log

    monkeypatch.setattr(check, "run", fake_run)
    assert check.verify_quotes(ctx, {}, [("s", "a"), ("s", "b")])[0] == status


def test_child_environment_drops_git_and_uv_routing(monkeypatch):
    routing = {"GIT_INDEX_FILE", "VIRTUAL_ENV", "UV_WORKING_DIR", "UV_PROJECT", "UV_PROJECT_ENVIRONMENT", "UV_NO_PROJECT", "UV_NO_SYNC", "UV_FROZEN"}
    for name in routing | {"UV_CACHE_DIR"}:
        monkeypatch.setenv(name, "/checkout")
    monkeypatch.setenv("UV_EXCLUDE_NEWER", "7 days")  # CI sets the window uv.lock records
    env = check.clean_env()
    assert not routing & set(env)
    assert env["UV_CACHE_DIR"] == "/checkout"
    assert env["UV_EXCLUDE_NEWER"] == "7 days"
    assert env["PYTHONDONTWRITEBYTECODE"] == "1"


def test_extremes_track_longest_strings_and_lists_per_field():
    data = {"jurisdictions": [{"name": "Chad", "timeline": [1, 2]}, {"name": "Liechtenstein", "timeline": []}]}
    assert check.extremes(data) == {"/jurisdictions[]": 2, "/jurisdictions/*/name": 13, "/jurisdictions/*/timeline[]": 2}


@pytest.mark.parametrize(("path", "content", "impact"), [
    ("site/index.html", "<p>changed</p>", "site/index.html changed"),
    ("scripts/published.py", "derived facts", "scripts/published.py changed"),
    ("scripts/display.py", "field citations", "scripts/display.py changed"),
    ("scripts/row_checks.py", "row citations", "scripts/row_checks.py changed"),
    ("data/aisi.json", '{"jurisdictions": [{"name": "Liechtensteinerland"}]}', "longest content grew"),
    ("data/aisi.json", '{"jurisdictions": [{"name": "Chad"}]}', None),
    ("README.md", "words", None),
])
def test_rendering_impact_from_page_renderer_or_longer_content(tmp_path, path, content, impact):
    repo = tmp_path / "repo"
    (repo / "site").mkdir(parents=True)
    (repo / "data").mkdir()
    (repo / "scripts").mkdir()
    (repo / "site/index.html").write_text("<p>page</p>")
    (repo / "data/aisi.json").write_text('{"jurisdictions": [{"name": "Liechtenstein"}]}')
    (repo / "README.md").write_text("readme")
    git(repo, "init", "-q")
    git(repo, "add", "-A")
    git(repo, "-c", "user.name=t", "-c", "user.email=t@example.org", "commit", "-q", "-m", "base")
    (repo / path).write_text(content)
    git(repo, "add", path)
    work = tmp_path / "work"
    check.make_snapshot(repo, work / "snapshot")
    ctx = check.Context(repo, work / "snapshot", work, tmp_path, "HEAD", False, work / "dist", tmp_path)
    reasons = check.rendering_impact(ctx)
    assert (impact is None and reasons == []) or any(impact in r for r in reasons)


def test_crop_links_name_a_missing_side(tmp_path):
    (tmp_path / "after").mkdir()
    (tmp_path / "after/390-light-hero.png").write_bytes(b"png")
    links = check.crop_links(tmp_path, "390-light-hero")
    assert links.startswith("before: absent, [after](")


def test_where_to_look_then_visual_pass_come_before_the_claims_in_the_report():
    # compare runs before sheet, so insertion order would put the claims first.
    sections = {"compare": "## Changes against base", "sheet": "## Visual pass", "where": "## Where to look"}
    assert check.ordered_sections(sections) == ["## Where to look", "## Visual pass", "## Changes against base"]


def test_where_to_look_links_each_change_into_the_served_build(tmp_path):
    (tmp_path / "dist").mkdir()
    (tmp_path / "dist/index.html").write_text("page")
    report = tmp_path / "report"
    report.mkdir()
    ctx = check.Context(tmp_path, tmp_path, tmp_path, report, "HEAD", False, tmp_path / "dist", tmp_path / "cache", serve_url="http://127.0.0.1:9000/")
    change = {"number": 1, "label": "Sweden card, Budget field", "anchor": "c-se", "what": "changed", "states": "390 px dark"}
    text = check.where_to_look(ctx, [change])
    assert "1. Sweden card, Budget field: changed; at 390 px dark. [Open](http://127.0.0.1:9000/#c-se)" in text
    assert "http.server 9000" in text and (report / "site/index.html").read_text() == "page"


def test_binding_only_edits_appear_in_report_with_full_references(tmp_path, monkeypatch):
    old_target = ["institutes", {"id": "first"}, "mandate_short"]
    new_target = ["institutes", {"id": "second"}, "mandate_short"]
    def dataset(target):
        return {"sources": [{"id": "shared", "url": "https://example.org/source", "excerpts": [
            {"text": "Unchanged retained quotation", "supports": [target]},
            {"text": "Unchanged retained quotation", "supports": [["institutes", {"id": "neighbour"}, "name"]]},
        ]}]}
    old, new = dataset(old_target), dataset(new_target)
    (tmp_path / "data").mkdir()
    (tmp_path / "data/aisi.json").write_text(json.dumps(new))
    monkeypatch.setattr(check, "git", lambda *args, **kwargs: json.dumps(old))
    ctx = check.Context(tmp_path, tmp_path, tmp_path, tmp_path, "HEAD", False, tmp_path / "dist", tmp_path / "absent-cache")
    result = check.step_compare(ctx)
    assert result.status == "PASS"
    assert "0 changed claims, 1 changed evidence entries, 0 new or edited excerpts" in result.detail
    assert "Changed evidence bindings and glosses (1)" in ctx.sections["compare"]
    assert "Unchanged retained quotation" in ctx.sections["compare"]
    assert "not unchanged quotations whose bindings changed" in ctx.sections["compare"]
    rows = json.loads((tmp_path / "evidence-bindings.json").read_text())
    assert rows == [{
        "source_id": "shared", "excerpt_id": "sha256:" + hashlib.sha256(b"Unchanged retained quotation").hexdigest(), "text": "Unchanged retained quotation", "url": "https://example.org/source",
        "added": [new_target], "removed": [old_target],
    }]
    new["sources"][0]["excerpts"].reverse()
    assert check.binding_changes(old, new) == rows
    assert check.binding_changes(new, new) == []


def test_gloss_only_edit_is_visible_without_claiming_literal_verification(tmp_path, monkeypatch):
    old = {"sources": [{"id": "s", "url": "https://example.org/source", "excerpts": [
        {"text": "Seguridad", "gloss": "Safety", "supports": ["/institutes/0/name"]}
    ]}]}
    new = json.loads(json.dumps(old))
    new["sources"][0]["excerpts"][0]["gloss"] = "Security"
    (tmp_path / "data").mkdir()
    (tmp_path / "data/aisi.json").write_text(json.dumps(new))
    monkeypatch.setattr(check, "git", lambda *args, **kwargs: json.dumps(old))
    ctx = check.Context(tmp_path, tmp_path, tmp_path, tmp_path, "HEAD", False, tmp_path / "dist", tmp_path / "absent-cache")
    result = check.step_compare(ctx)
    assert result.status == "PASS"
    assert "1 changed evidence entries, 0 new or edited excerpts" in result.detail
    assert "Safety" in ctx.sections["compare"] and "Security" in ctx.sections["compare"]
    assert json.loads((tmp_path / "evidence-bindings.json").read_text()) == [{
        "source_id": "s", "excerpt_id": "sha256:" + hashlib.sha256(b"Seguridad").hexdigest(), "text": "Seguridad", "url": "https://example.org/source",
        "added": [], "removed": [], "gloss_before": ["Safety"], "gloss_after": ["Security"],
    }]


@pytest.mark.parametrize("value", [None, "", "~/private-sources"])
def test_snapshot_defaults_follow_environment(value):
    env = dict(os.environ)
    env.pop("AISI_SNAPSHOTS", None)
    if value is not None:
        env["AISI_SNAPSHOTS"] = value
    result = subprocess.run(
        [sys.executable, "-c", "from scripts import check, snapshot_sources as s; "
         "print(check.SNAPSHOTS); print(s.PRIVATE_SNAPSHOTS)"],
        env=env, capture_output=True, text=True, check=True,
    )
    expected = str(Path(value).expanduser()) if value else "None"
    assert result.stdout.splitlines() == [expected, expected]


def test_snapshot_cli_overrides_environment_default(tmp_path, monkeypatch):
    monkeypatch.setattr(check, "SNAPSHOTS", tmp_path / "environment-store")
    selected = []
    monkeypatch.setattr(check, "preflight", lambda cache, store: selected.append(store) or 0)
    monkeypatch.setattr(sys, "argv", ["check.py", "--preflight", "--snapshots", str(tmp_path / "cli-store")])
    assert check.main() == 0
    assert selected == [tmp_path / "cli-store"]


def test_preflight_reports_unconfigured_store(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(check.shutil, "which", lambda tool: "/bin/" + tool)
    monkeypatch.setattr(check.subprocess, "run", lambda *a, **kw: subprocess.CompletedProcess(a, 0, stdout="/browser"))
    assert check.preflight(tmp_path, None) == 1
    assert "not configured (set AISI_SNAPSHOTS" in capsys.readouterr().out


def test_quote_verification_runs_from_snapshots_without_a_cache(tmp_path, monkeypatch):
    (tmp_path / "snapshots").mkdir()
    ctx = check.Context(tmp_path, tmp_path, tmp_path, tmp_path, "HEAD", False, tmp_path / "dist", tmp_path / "no-cache",
                        snapshots=tmp_path / "snapshots")
    seen = []

    def fake_run(ctx, step, cmd, cwd=None):
        seen.append(cmd)
        (tmp_path / "excerpt-verification.json").write_text(json.dumps({"sources": [{"excerpts": [{"source_id": "s", "text": "a", "status": "matched"}]}]}))
        log = tmp_path / "log"
        log.write_text("verifier output\n")
        return 0, 0.0, log

    monkeypatch.setattr(check, "run", fake_run)
    assert check.verify_quotes(ctx, {}, [("s", "a")])[0] == "PASS"
    assert "--seed-snapshots" in seen[0] and "--seed-cache" not in seen[0]
    for store in (None, tmp_path / "absent"):
        ctx.snapshots = store
        status, lines = check.verify_quotes(ctx, {}, [("s", "a")])
        assert status == "NOT RUN"
        detail = " ".join(lines)
        assert "at None" not in detail
        assert ("not configured (set AISI_SNAPSHOTS or pass --snapshots DIR)" if store is None else f"at {store}") in detail


def copies_ctx(tmp_path, store):
    snapshot = tmp_path / "snapshot"
    (snapshot / "data").mkdir(parents=True)
    body = b"<html>cited copy</html>"
    digest = hashlib.sha256(body).hexdigest()
    data = {"sources": [
        {"id": "held", "snapshot": {"retrieved": "2026-10-17T14:00:00Z", "sha256": digest}},
        {"id": "none"},
    ]}
    (snapshot / "data/aisi.json").write_text(json.dumps(data))
    if store is not None:
        (store / "held").mkdir(parents=True)
        (store / "held/copy.html").write_bytes(body)
        (store / "held/manifest.json").write_text(json.dumps([{"path": "held/copy.html", "sha256": digest}]))
    return check.Context(tmp_path, snapshot, tmp_path, tmp_path, "HEAD", False, tmp_path / "dist",
                         tmp_path / "cache", snapshots=store)


def test_copies_step_requires_every_recorded_snapshot_in_the_private_store(tmp_path):
    store = tmp_path / "store"
    ctx = copies_ctx(tmp_path, store)
    assert check.step_copies(ctx).status == "PASS"
    (store / "held/copy.html").write_bytes(b"<html>other bytes</html>")
    result = check.step_copies(ctx)
    assert result.status == "FAIL" and result.detail.endswith(": held")
    (store / "held/copy.html").unlink()
    assert check.step_copies(ctx).status == "FAIL"


def test_copies_step_is_not_run_without_the_private_store(tmp_path):
    ctx = copies_ctx(tmp_path, None)
    for store in (None, tmp_path / "absent"):
        ctx.snapshots = store
        result = check.step_copies(ctx)
        assert result.status == "NOT RUN" and "private store" in result.detail
        assert "AISI_SNAPSHOTS" in result.detail
        assert "at None" not in result.detail
        assert ("not configured (set AISI_SNAPSHOTS or pass --snapshots DIR)" if store is None else f"at {store}") in result.detail
        assert check.blocking([result], allow_skip=False) == [result]
        assert check.blocking([result], allow_skip=True) == []


@pytest.mark.parametrize("unverified", ["not_cached", "unreachable", "rejected_content", "unextractable"])
@pytest.mark.parametrize("other,expected", [("matched", "NOT RUN"), ("mismatch", "FAIL")])
def test_unverified_changed_quotes_block_and_list_identity(tmp_path, monkeypatch, unverified, other, expected):
    (tmp_path / "cache").mkdir()
    ctx = check.Context(tmp_path, tmp_path, tmp_path, tmp_path, "HEAD", False, tmp_path / "dist", tmp_path / "cache")
    rows = [
        {"source_id": "s", "excerpt_id": "unavailable-evidence", "text": "a", "status": unverified},
        {"source_id": "s", "excerpt_id": "other-evidence", "text": "b", "status": other},
    ]

    def fake_run(ctx, step, cmd, cwd=None):
        (tmp_path / "excerpt-verification.json").write_text(json.dumps({"sources": [{"excerpts": rows}]}))
        log = tmp_path / "log"
        log.write_text("verifier output\n")
        return 1, 0.0, log

    monkeypatch.setattr(check, "run", fake_run)
    status, lines = check.verify_quotes(ctx, {}, [("s", "a"), ("s", "b")])
    assert status == expected
    assert any("unavailable-evidence" in line and unverified in line for line in lines)
    assert "1 unverified" in "\n".join(lines)


def test_no_changed_quotes_need_no_cache(tmp_path):
    ctx = check.Context(tmp_path, tmp_path, tmp_path, tmp_path, "HEAD", False, tmp_path / "dist", tmp_path / "absent-cache")
    assert check.verify_quotes(ctx, {}, [])[0] == "PASS"


def test_release_warnings_follow_where_to_look():
    sections = {"compare": "## Changes against base", "warnings": "## Release warnings", "where": "## Where to look"}
    assert check.ordered_sections(sections) == ["## Where to look", "## Release warnings", "## Changes against base"]


@pytest.mark.parametrize("warnings, detail", [(["- source eu-act: status never verified"], "1 release warning (see Release warnings)"), ([], "")])
def test_validate_carries_release_warnings_into_the_report_without_failing(tmp_path, monkeypatch, warnings, detail):
    ctx = check.Context(tmp_path, tmp_path, tmp_path, tmp_path, "HEAD", False, tmp_path / "dist", tmp_path / "cache")
    section = "\n".join(["## Release warnings", "", *(warnings or ["None."])]) + "\n"

    def validated(ctx, step, cmd):
        report_dir = Path(cmd[cmd.index("--report-dir") + 1])
        report_dir.mkdir(parents=True)
        (report_dir / "release-warnings.md").write_text(section)
        return check.Result(step, "PASS")

    monkeypatch.setattr(check, "command_step", validated)
    result = check.step_validate(ctx)
    assert (result.status, result.detail) == ("PASS", detail)
    assert ctx.sections["warnings"] == section
