"""Research pass folders: pass file, findings and the log's link to the pass."""

import json
import re

import pytest

from scripts import research_pass

DATA = {
    "jurisdictions": [{"id": "IT", "name": "Italy", "head": None}],
    "institutes": [{"id": "it-aisi", "name": "Institute", "partners": [{"relation": "partner", "name": "Bank"}]}],
}
SESSION = "36f8e1de-4d06-447d-96b7-f53080152eb0"
EVENT = {"format_version": research_pass.LOG_FORMAT, "session_id": SESSION, "model": "claude-opus-5-5",
         "harness": {"name": "claude_code", "version": "2.1.318"},
         "tool": {"name": "mcp__exa__web_search_exa"}, "operations": [{"kind": "search"}]}


def pass_record(**changes):
    record = {
        "pass_id": "2026-11-07-news-recheck",
        "who": "Jason Hoelscher-Obermaier",
        "harness": {"name": "claude_code", "version": "2.1.318"},
        "models": ["claude-opus-5-5"],
        "sessions": [SESSION],
        "started": "2026-11-07",
        "ended": "2026-11-07T18:00:00+01:00",
        "type": "news-sweep",
        "scope": {"states": ["all"], "institutes": ["it-aisi"], "fields": ["partners"]},
        "search_tools": [{"tool": "mcp__exa__web_search_exa", "engine": "exa"},
                         {"tool": "WebSearch", "engine": None, "options": "standard"}],
        "stopping_reason": "every news and publications page read back to the last check",
        "status": "closed",
    }
    record.update(changes)
    return record


def make(tmp_path, record=None, log=(EVENT,), findings=()):
    folder = tmp_path / "2026-11-07-news-recheck"
    folder.mkdir(parents=True)
    (folder / "pass.json").write_text(json.dumps(pass_record() if record is None else record))
    if log is not None:
        (folder / "log.jsonl").write_text("".join(json.dumps(e) + "\n" for e in log))
    if findings is not None:
        (folder / "findings.jsonl").write_text("".join(json.dumps(f) + "\n" for f in findings))
    return folder


def test_valid_closed_pass(tmp_path):
    finding = {"url": "https://example.gov/news", "scope": "it-aisi", "verdict": "rejected", "reason": "secondary report"}
    assert research_pass.validate_pass(make(tmp_path, findings=[finding]), DATA) == []


def test_open_pass_needs_no_log_yet(tmp_path):
    record = pass_record(status="open", ended=None, stopping_reason=None, sessions=[])
    assert research_pass.validate_pass(make(tmp_path, record, log=None, findings=None), DATA) == []


@pytest.mark.parametrize("changes, expected", [
    ({"pass_id": "2026-11-07-other"}, "differs from its folder"),
    ({"started": "2026-11-08", "ended": "2026-11-08"}, "must start with the start date"),
    ({"ended": "2026-11-06"}, "ended is before started"),
    ({"scope": {"states": ["XX"], "institutes": [], "fields": []}}, "unknown state 'XX'"),
    ({"scope": {"states": [], "institutes": ["all", "it-aisi"], "fields": []}}, "'all' stands alone"),
    ({"scope": {"states": [], "institutes": [], "fields": ["budget"]}}, "unknown field 'budget'"),
    ({"scope": {"states": [], "institutes": [], "fields": []}}, "at least one state"),
    ({"search_tools": [{"tool": "WebSearch", "engine": None}]}, "at least one tool with a known engine"),
    ({"search_tools": [{"tool": "mcp__exa__web_search_exa", "engine": "exa"}, {"tool": "WebSearch", "engine": "bing"}]},
     "never inferred"),
    ({"stopping_reason": None}, "closed pass needs stopping_reason"),
    ({"type": "browse"}, "is not one of"),
])
def test_pass_file_problems(tmp_path, changes, expected):
    problems = research_pass.validate_pass(make(tmp_path, pass_record(**changes)), DATA)
    assert any(expected in p for p in problems), problems


def test_dotted_field_is_checked_whole(tmp_path):
    record = pass_record(scope={"states": [], "institutes": [], "fields": ["partners.relation"]})
    assert research_pass.validate_pass(make(tmp_path, record), DATA) == []
    record = pass_record(scope={"states": [], "institutes": [], "fields": ["partners.nonexistent"]})
    assert any("unknown field 'partners.nonexistent'" in p for p in research_pass.validate_pass(make(tmp_path / "b", record), DATA))


@pytest.mark.parametrize("event, expected", [
    ({**EVENT, "session_id": "other"}, "sessions the pass file does not name: other"),
    ({**EVENT, "model": "gpt-5"}, "models the pass file does not name: gpt-5"),
    ({**EVENT, "harness": {"name": "claude_code", "version": "2.1.400"}}, "harness the pass file does not name"),
    ({**EVENT, "format_version": "research-log-pilot/1"}, "format the pass file does not name: research-log-pilot/1"),
    ({**EVENT, "tool": {"name": "mcp__other__search"}}, "search tools the pass file does not name: mcp__other__search"),
    ({**EVENT, "model": ["a"]}, "row 2: ['a'] is not of type"),
    ({**EVENT, "harness": None}, "row 2: None is not of type 'object'"),
    ([1, 2], "row 2: [1, 2] is not of type 'object'"),
    ({**EVENT, "operations": [{}]}, "row 2: 'kind' is a required property"),
    ({**EVENT, "operations": [{"kind": ["search"]}]}, "row 2: ['search'] is not of type 'string'"),
])
def test_log_must_belong_to_the_pass(tmp_path, event, expected):
    problems = research_pass.validate_pass(make(tmp_path, log=[EVENT, event]), DATA)
    assert any(expected in p for p in problems), problems


def test_closed_pass_needs_log_and_findings(tmp_path):
    problems = research_pass.validate_pass(make(tmp_path, log=None, findings=None), DATA)
    assert any("needs log.jsonl" in p for p in problems) and any("needs findings.jsonl" in p for p in problems)


def test_rejected_finding_needs_reason(tmp_path):
    finding = {"url": "https://example.gov/news", "scope": "it-aisi", "verdict": "rejected"}
    problems = research_pass.validate_pass(make(tmp_path, findings=[finding]), DATA)
    assert any("'reason' is a required property" in p for p in problems)


def test_store_location(monkeypatch, tmp_path):
    monkeypatch.delenv("AISI_PASSES", raising=False)
    monkeypatch.setenv("AISI_SNAPSHOTS", str(tmp_path / "private/sources"))
    assert research_pass.store_directory() == tmp_path / "private/passes"
    monkeypatch.setenv("AISI_PASSES", str(tmp_path / "elsewhere"))
    assert research_pass.store_directory() == tmp_path / "elsewhere"


def test_cli_validates_every_pass_in_the_store(tmp_path, capsys):
    make(tmp_path)
    data = tmp_path / "aisi.json"
    data.write_text(json.dumps(DATA))
    assert research_pass.main(["--store", str(tmp_path), "validate", "--data", str(data)]) == 0
    assert "2026-11-07-news-recheck: ok" in capsys.readouterr().out


def test_extract_copies_transcripts_and_reports_what_the_pass_must_name(tmp_path):
    from test_research_log import cc_call, cc_result, save

    source = tmp_path / "project"
    (source / "session-a/subagents").mkdir(parents=True)
    save(source / "session-a.jsonl", [cc_call("mcp__exa__web_search_exa"),
                                      cc_result([{"type": "text", "text": "Title: A\nURL: https://example.org/a"}])])
    save(source / "session-a/subagents/agent-b.jsonl", [cc_call("WebFetch", {"url": "https://example.org/a"},
                                                                native="call-b", agent="agent-b")])
    folder = tmp_path / "2026-11-07-news-recheck"
    folder.mkdir()
    found = research_pass.extract_pass(folder, [source])
    assert (folder / "transcripts/session-a/subagents/agent-b.jsonl").is_file()
    assert found["sessions"] == ["session-a"] and found["search tools"] == ["mcp__exa__web_search_exa"]
    assert found["harness"] == ["claude_code 2.test"]
    events = [json.loads(line) for line in (folder / "log.jsonl").read_text().splitlines()]
    assert {e["tool"]["name"] for e in events} == {"mcp__exa__web_search_exa", "WebFetch"}


def test_transcripts_are_found_in_both_homes(tmp_path):
    store = tmp_path / "store"
    folder = store / "2026-11-07_news.v2"
    claude = research_pass.home(store, "claude") / "projects" / re.sub(r"[^A-Za-z0-9]", "-", str(folder.resolve()))
    claude.mkdir(parents=True)
    sessions = research_pass.home(store, "codex") / "sessions/2026/11/07"
    sessions.mkdir(parents=True)
    for name, cwd in (("mine", folder), ("other", tmp_path)):
        meta = {"type": "session_meta", "payload": {"id": name, "cwd": str(cwd)}}
        (sessions / f"rollout-{name}.jsonl").write_text(json.dumps(meta) + "\n")
    (sessions / "rollout-broken.jsonl").write_text("not json\n")
    assert research_pass.pass_transcripts(folder, store) == [claude, sessions / "rollout-mine.jsonl"]
    assert claude.name.endswith("-store-2026-11-07-news-v2")


def test_homes_are_written_from_scripts(tmp_path):
    written = research_pass.setup_homes(tmp_path)
    assert {p.relative_to(tmp_path).as_posix() for p in written} == {
        ".homes/claude/settings.json", ".homes/claude/mcp.json", ".homes/codex/config.toml"}
    settings = json.loads((tmp_path / ".homes/claude/settings.json").read_text())
    assert settings["permissions"]["defaultMode"] == "dontAsk" and "Bash" in settings["permissions"]["deny"]
    config = (tmp_path / ".homes/codex/config.toml").read_text()
    assert "shell_tool = false" in config and 'mcp_oauth_credentials_store = "file"' in config

def test_extract_refuses_to_overwrite_and_reuses_the_archive(tmp_path):
    from test_research_log import cc_call, save

    folder = tmp_path / "2026-11-07-news-recheck"
    folder.mkdir()
    for name in ("a", "b"):
        (tmp_path / name).mkdir()
        save(tmp_path / name / "s.jsonl", [cc_call(native=f"call-{name}")])
    with pytest.raises(ValueError, match="would both be copied"):
        research_pass.extract_pass(folder, [tmp_path / "a", tmp_path / "b"])
    research_pass.extract_pass(folder, [tmp_path / "a"])
    save(tmp_path / "a/s.jsonl", [cc_call(native="call-a"), cc_call(native="call-c")])  # the session went on
    research_pass.extract_pass(folder, [tmp_path / "a"])
    with pytest.raises(ValueError, match="does not extend it"):
        research_pass.extract_pass(folder, [tmp_path / "b"])
    assert research_pass.extract_pass(folder, [folder / "transcripts"])["sessions"] == ["session-a"]


def test_changed_records_ignores_changes_outside_records():
    old = {"dataset": {"method": "a"}, "jurisdictions": [{"id": "IT", "x": 1}], "institutes": [], "sources": [{"id": "s1"}]}
    new = {"dataset": {"method": "b"}, "jurisdictions": [{"id": "IT", "x": 2}], "institutes": [{"id": "it-aisi"}], "sources": []}
    assert research_pass.changed_records(json.dumps(old), json.dumps({**old, "dataset": {"method": "b"}})) == []
    assert research_pass.changed_records(json.dumps(old), json.dumps(new)) == ["IT", "it-aisi", "s1"]
    assert research_pass.changed_records(None, json.dumps(new)) == ["IT", "it-aisi"]


@pytest.mark.parametrize("message, changed, expected", [
    ("Data\n", ["IT"], "names no pass"),
    ("Data\n\nResearch-pass: none (typo)\n", ["IT"], None),
    ("Data\n\nResearch-pass: none\n", ["IT"], "neither a pass ID"),
    ("Data\n\nResearch-pass: 2026-11-07-news-recheck\n", ["IT"], None),
    ("Data\n\nResearch-pass: 2026-11-08-missing\n", ["IT"], "no pass file"),
    ("Code\n", [], None),
])
def test_message_trailers(tmp_path, message, changed, expected):
    make(tmp_path)
    problems, _ = research_pass.message_problems(message, changed, tmp_path, DATA)
    assert (expected is None and problems == []) or any(expected in p for p in problems), problems


def test_open_pass_does_not_count(tmp_path):
    make(tmp_path, pass_record(status="open"))
    problems, _ = research_pass.message_problems("D\n\nResearch-pass: 2026-11-07-news-recheck\n", ["IT"], tmp_path, DATA)
    assert problems == ["pass 2026-11-07-news-recheck: the pass is still open"]


def test_named_pass_without_store_is_unchecked(tmp_path):
    problems, unchecked = research_pass.message_problems("D\n\nResearch-pass: 2026-11-07-news-recheck\n", ["IT"], None, DATA)
    assert problems == [] and unchecked == ["2026-11-07-news-recheck"]


def test_commit_report(tmp_path):
    import os
    import subprocess

    repo = tmp_path / "repo"
    (repo / "data").mkdir(parents=True)

    def git(*args, date="2026-11-07T12:00:00+01:00"):
        env = {**os.environ, "GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date}
        subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *args],
                       check=True, capture_output=True, env=env)

    def commit(message, x, **kw):
        (repo / "data/aisi.json").write_text(json.dumps({"jurisdictions": [{"id": "IT", "x": x}]}))
        git("add", "-A", **kw)
        git("commit", "-q", "-m", message, **kw)

    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    commit("base", 0)
    git("tag", "base")
    commit("old data", 1, date="2026-11-06T12:00:00+01:00")
    commit("typo\n\nResearch-pass: none (typo fix)", 2)
    git("checkout", "-q", "-b", "side")
    commit("news\n\nResearch-pass: 2026-11-07-news-recheck", 3)
    git("checkout", "-q", "main")
    git("merge", "-q", "--no-ff", "-m", "Merge side", "side")  # a merge inherits its parents' trailers
    store = tmp_path / "store"
    make(store)
    lines, code = research_pass.commit_report(repo, "base", store, DATA)
    assert code == 0, lines
    assert sorted(line.split()[0] for line in lines[:2]) == ["none", "ok"] and len(lines) == 3, lines
    assert any("typo fix" in line for line in lines) and any("2026-11-07-news-recheck" in line for line in lines)
    assert lines[2].endswith("1 commit(s) changing records")
    lines, code = research_pass.commit_report(repo, "base", None, DATA)
    assert code == research_pass.NOT_RUN and "not checked, no pass store: 2026-11-07-news-recheck" in lines[-1]
    commit("unnamed", 4)
    lines, code = research_pass.commit_report(repo, "base", None, DATA)
    assert code == 1 and any("names no pass" in line for line in lines)


def test_message_hook(tmp_path, monkeypatch):
    import subprocess

    repo = tmp_path / "repo"
    (repo / "data").mkdir(parents=True)
    git = lambda *a: subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *a],  # noqa: E731
                                    check=True, capture_output=True)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / "data/aisi.json").write_text(json.dumps(DATA))
    git("add", "-A")
    git("commit", "-q", "-m", "base")
    changed = {**DATA, "jurisdictions": [{"id": "IT", "name": "Italia"}]}
    (repo / "data/aisi.json").write_text(json.dumps(changed))
    git("add", "-A")
    message = repo / "MSG"
    message.write_text("Data\n")
    monkeypatch.chdir(repo)
    monkeypatch.setenv("GIT_AUTHOR_DATE", "2026-11-08T10:00:00+01:00")
    hook = ["message", str(message), "--data", "data/aisi.json"]
    assert research_pass.main(hook) == 1
    message.write_text("Data\n\nResearch-pass: none (spelling)\n")
    assert research_pass.main(hook) == 0
    message.write_text("Data\n")
    monkeypatch.setenv("GIT_AUTHOR_DATE", "2026-11-06T10:00:00+01:00")
    assert research_pass.main(hook) == 0  # authored before passes began
    monkeypatch.setenv("GIT_AUTHOR_DATE", "@1793955600 +0100")  # git's own form: 2026-11-06 10:00 +01:00
    assert research_pass.main(hook) == 0
    monkeypatch.setenv("GIT_AUTHOR_DATE", "@1794128400 +0100")  # 2026-11-08 10:00 +01:00
    assert research_pass.main(hook) == 1
    monkeypatch.setenv("GIT_AUTHOR_DATE", "next Tuesday")
    monkeypatch.setattr(research_pass, "PASSES_BEGIN", research_pass.datetime.fromisoformat("2026-01-01T00:00:00+00:00"))
    assert research_pass.main(hook) == 1  # not ISO: counts as now, after the (moved) start
    (repo / ".git/MERGE_HEAD").write_text("0" * 40)
    assert research_pass.main(hook) == 0  # a merge inherits
    (repo / ".git/MERGE_HEAD").unlink()
    git("rm", "-q", "-f", "data/aisi.json")
    message.write_text("Remove\n\nResearch-pass: none (dataset moved)\n")
    assert research_pass.main(hook) == 0


def test_extract_brings_a_claude_sessions_subagents(tmp_path):
    from test_research_log import cc_call, save

    (tmp_path / "project/session-a/subagents").mkdir(parents=True)
    main = save(tmp_path / "project/session-a.jsonl", [cc_call(native="call-a")])
    save(tmp_path / "project/session-a/subagents/agent-b.jsonl", [cc_call(native="call-b", agent="agent-b")])
    folder = tmp_path / "2026-11-07-news-recheck"
    folder.mkdir()
    research_pass.extract_pass(folder, [main])
    assert (folder / "transcripts/session-a/subagents/agent-b.jsonl").is_file()


def test_closed_pass_must_have_logged_a_known_engine_search(tmp_path):
    hosted = {**EVENT, "tool": {"name": "WebSearch"}}
    record = pass_record(search_tools=[{"tool": "mcp__exa__web_search_exa", "engine": "exa"},
                                       {"tool": "WebSearch", "engine": None}])
    problems = research_pass.validate_pass(make(tmp_path, record, log=[hosted]), DATA)
    assert any("no search with a known-engine tool" in p for p in problems), problems


def test_homes_prints_quoted_login_commands(tmp_path, capsys):
    store = tmp_path / "Research passes"
    assert research_pass.main(["--store", str(store), "homes"]) == 0
    assert f"CLAUDE_CONFIG_DIR='{store}/.homes/claude' claude auth login" in capsys.readouterr().out
