"""Invented minimal fixtures modelled on transcript envelopes, never copied data."""

import json
from pathlib import Path

import pytest

from scripts.research_log import extract, legacy, main, summarise


def save(path: Path, rows: list[dict]) -> Path:
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    return path


def cc_call(name="WebSearch", args=None, native="call-a", agent="agent-a"):
    return {
        "type": "assistant",
        "sessionId": "session-a",
        "agentId": agent,
        "version": "2.test",
        "timestamp": "2026-01-01T00:00:00Z",
        "message": {
            "content": [
                {
                    "type": "tool_use",
                    "id": native,
                    "name": name,
                    "input": args or {"query": "invented institute"},
                }
            ]
        },
    }


def cc_result(raw, native="call-a", error=False, agent="agent-a"):
    return {
        "type": "user",
        "sessionId": "session-a",
        "agentId": agent,
        "timestamp": "2026-01-01T00:00:01Z",
        "toolUseResult": raw,
        "message": {
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": native,
                    "is_error": error,
                    "content": "opaque output",
                }
            ]
        },
    }


def meta():
    return {
        "type": "session_meta",
        "payload": {"id": "codex-session", "cli_version": "0.test"},
    }


def hosted(action=None, results=None, native="native-a"):
    return {
        "type": "event_msg",
        "timestamp": "2026-01-01T00:00:02Z",
        "payload": {
            "type": "item_completed",
            "started_at_ms": 1000,
            "completed_at_ms": 2000,
            "item": {
                "type": "Extension",
                "kind": "web.search",
                "id": native,
                "action": action or {"type": "search", "queries": ["alpha", "beta"]},
                "results": results
                if results is not None
                else [{"url": "https://example.org", "ref_id": "turn0search0"}],
            },
        },
    }


def direct(name, args, native="direct-a"):
    return {
        "type": "response_item",
        "timestamp": "2026-01-01T00:00:00Z",
        "payload": {
            "type": "function_call",
            "name": name,
            "call_id": native,
            "arguments": json.dumps(args),
        },
    }


def test_claude_occurrences_subagents_and_observation_times(tmp_path):
    result = {"title": "Synthetic result", "url": "https://example.org/a"}
    raw = {
        "results": [
            {"tool_use_id": "server-a", "content": [result, result]},
            "Summary https://example.org/not-a-result",
        ]
    }
    events, _ = extract([save(tmp_path / "a.jsonl", [cc_result(raw), cc_call()])])
    assert len(events) == 1
    (ev,) = events
    assert ev["results_returned"] == 2
    assert [r["occurrence"] for r in ev["results"]] == [1, 2]
    assert ev["agent_id"] == "agent-a"
    assert ev["engine"] is None and ev["run_id"] is None
    assert ev["timestamp_basis"] == "message_observation"
    assert ev["ended_at"] == "2026-01-01T00:00:01Z"
    assert ev["capture_completeness"] == "complete"


def test_copies_do_not_multiply_events_or_results(tmp_path):
    rows = [meta(), hosted()]
    a, b = save(tmp_path / "a.jsonl", rows), save(tmp_path / "b.jsonl", rows)
    events, _ = extract([b, a, a])
    (ev,) = events
    assert ev["results_returned"] == 1
    assert len(ev["raw_ref"]) == 2
    original, _ = extract([a])
    assert original[0]["event_id"] == ev["event_id"]
    assert extract([a, b])[0] == events


def test_conflicting_native_identity_keeps_first_and_is_noted(tmp_path):
    a = save(tmp_path / "a.jsonl", [meta(), hosted()])
    b = save(tmp_path / "b.jsonl", [meta(), hosted(results=[])])
    (ev,), _ = extract([a, b])
    assert "conflicting_duplicate_execution" in ev["capture_notes"]
    assert ev["results"]
    assert len(ev["raw_ref"]) == 2


def test_distinct_native_executions_same_query_are_not_deduplicated(tmp_path):
    events, _ = extract(
        [save(tmp_path / "a.jsonl", [cc_call(native="one"), cc_call(native="two")])]
    )
    assert len(events) == 2
    assert all(ev["execution_status"] is None for ev in events)
    assert all(ev["results_returned"] is None for ev in events)


def test_agent_identity_prevents_wrong_result_join(tmp_path):
    rows = [
        cc_call(),
        cc_call(agent="agent-b"),
        cc_result({"results": []}, agent="agent-b"),
    ]
    events, _ = extract([save(tmp_path / "a.jsonl", rows)])
    by_agent = {ev["agent_id"]: ev for ev in events}
    assert by_agent["agent-a"]["results_returned"] is None
    assert by_agent["agent-b"]["results_returned"] == 0


def test_http_response_is_not_content_usability_or_tool_success(tmp_path):
    rows = [
        cc_call("WebFetch", {"url": "https://example.org/a"}),
        cc_result(
            {
                "code": 200,
                "url": "https://example.org/a",
                "result": "Cannot extract this PDF",
            }
        ),
    ]
    events, _ = extract([save(tmp_path / "a.jsonl", rows)])
    (ev,) = events
    assert ev["execution_status"] == "completed"
    assert ev["fetch_outcomes"] == [
        {
            "url": "https://example.org/a",
            "http_status": 200,
            "content_usable": None,
            "outcome": "http_response",
        }
    ]
    assert summarise(events)["fetch_outcomes"] == {"200": 1}


def test_fetch_error_and_missing_output_are_distinct(tmp_path):
    rows = [
        cc_call("WebFetch", {"url": "https://example.org/a"}),
        cc_result("Timeout", error=True),
        cc_call("WebFetch", {"url": "https://example.org/b"}, native="missing"),
    ]
    events, _ = extract([save(tmp_path / "a.jsonl", rows)])
    assert summarise(events)["by_status"] == {"failed": 1, "unknown": 1}
    assert summarise(events)["fetch_outcomes"] == {"tool_error": 1}


def test_hosted_batch_preserves_native_evidence_and_unknown_parent(tmp_path):
    rows = [
        meta(),
        hosted(),
        hosted(
            action={"type": "other"},
            results=[{"title": "Internal Error", "ref_id": "view0"}],
            native="other",
        ),
    ]
    events, _ = extract([save(tmp_path / "a.jsonl", rows)])
    assert summarise(events)["by_kind_operations"] == {"search": 2}
    # Entries of a non-search item are not search results.
    assert summarise(events)["observed_result_occurrences"] == 1
    for ev in events:
        assert ev["capture_completeness"] == "partial"
        assert ev["parent_execution_id"] is None and ev["via"] == []
        assert ev["started_at"] == "1970-01-01T00:00:01+00:00"
    (other,) = [ev for ev in events if ev["native_call_id"] == "other"]
    assert other["operations"] == []
    assert other["results"] == []
    assert "non_search_entries_not_results" in other["capture_notes"]


def test_wrapper_source_is_not_execution_evidence(tmp_path):
    wrapper = {
        "type": "response_item",
        "payload": {
            "type": "custom_tool_call",
            "name": "exec",
            "call_id": "wrapper-a",
            "input": 'if (false) { tools.web__run({search_query:[{q:"never"}]}); }',
        },
    }
    events, diagnostics = extract([save(tmp_path / "a.jsonl", [meta(), wrapper])])
    assert events == []
    assert diagnostics[0]["code"] == "wrapper_source_not_parsed"
    events, _ = extract([save(tmp_path / "b.jsonl", [meta(), wrapper, hosted()])])
    assert len(events) == 1


def test_codex_direct_web_batch_and_error(tmp_path):
    rows = [
        meta(),
        direct(
            "web__run",
            {"search_query": [{"q": "a"}, {"q": "b"}], "open": [{"ref_id": "view0"}]},
        ),
        {
            "type": "response_item",
            "payload": {
                "type": "function_call_output",
                "call_id": "direct-a",
                "output": json.dumps({"isError": True, "message": "Denied"}),
            },
        },
    ]
    events, _ = extract([save(tmp_path / "a.jsonl", rows)])
    (ev,) = events
    assert ev["execution_status"] == "failed"
    assert [op["kind"] for op in ev["operations"]] == ["search", "search", "fetch"]
    assert ev["results_returned"] is None


def test_codex_hosted_response_shape(tmp_path):
    rows = [
        meta(),
        {
            "type": "response_item",
            "payload": {
                "type": "web_search_call",
                "id": "hosted-a",
                "status": "completed",
                "action": {"type": "open_page", "url": "https://example.org"},
            },
        },
    ]
    events, _ = extract([save(tmp_path / "a.jsonl", rows)])
    (ev,) = events
    assert ev["operations"][0]["kind"] == "fetch"
    assert ev["results_returned"] is None


def test_exa_keeps_unknown_count_and_other_mcp_is_skipped(tmp_path):
    rows = [
        cc_call("mcp__exa__web_search_exa"),
        cc_result(
            [
                {
                    "type": "text",
                    "text": "Title: Example\nURL: https://example.org/a\nText mentions https://example.org/b",
                }
            ]
        ),
        cc_call("mcp__service__search_records", native="other"),
        cc_result("3 results found", native="other"),
    ]
    events, _ = extract([save(tmp_path / "a.jsonl", rows)])
    assert summarise(events)["observed_result_occurrences"] == 1
    assert summarise(events)["search_executions_with_unknown_result_total"] == 1
    (exa,) = events
    assert exa["provider"] == "exa"
    assert exa["engine"] is None


def test_shell_http_is_candidate_only_and_command_status_is_separate(tmp_path):
    item = {
        "type": "CommandExecution",
        "id": "shell-a",
        "command": ["sh", "-c", "curl https://example.org; false"],
        "status": "failed",
        "exit_code": 1,
        "aggregated_output": "HTTP/1.1 200 OK",
    }
    rows = [
        meta(),
        {"type": "event_msg", "payload": {"type": "item_completed", "item": item}},
    ]
    events, _ = extract([save(tmp_path / "a.jsonl", rows)])
    (ev,) = events
    assert ev["operations"][0]["name"] == "shell_http"
    assert ev["capture_completeness"] == "partial"
    assert ev["execution_status"] == "failed"
    assert ev["fetch_outcomes"] == [] and ev["results_returned"] is None


def test_tool_discovery_and_ordinary_shell_are_not_research(tmp_path):
    events, _ = extract(
        [
            save(
                tmp_path / "a.jsonl",
                [
                    cc_call("ToolSearch"),
                    cc_call("Bash", {"command": "ls"}, native="shell"),
                ],
            )
        ]
    )
    assert events == []


def test_multi_result_message_does_not_duplicate_shared_metadata(tmp_path):
    a, b = cc_call(native="a"), cc_call(native="b")
    result = cc_result({"results": [{"url": "https://example.org"}]}, native="a")
    result["message"]["content"].append(
        {"type": "tool_result", "tool_use_id": "b", "content": "opaque"}
    )
    events, _ = extract([save(tmp_path / "a.jsonl", [a, b, result])])
    assert summarise(events)["observed_result_occurrences"] == 0


@pytest.mark.parametrize("bad", ["not json", "[]", '{"type":"unknown"}'])
def test_malformed_or_unknown_input_is_skipped_visibly(tmp_path, capsys, bad):
    path = tmp_path / "bad.jsonl"
    path.write_text(bad + "\n")
    events, diagnostics = extract([path])
    assert events == []
    codes = {d["code"] for d in diagnostics} | (
        {"skipped_invalid_line"}
        if "skipped_invalid_line" in capsys.readouterr().err
        else set()
    )
    assert codes & {"skipped_invalid_line", "skipped_unrecognised_file"}


def test_missing_native_identity_is_not_replaced_by_path(tmp_path):
    row = cc_call()
    del row["message"]["content"][0]["id"]
    with pytest.raises(ValueError, match="missing native"):
        extract([save(tmp_path / "a.jsonl", [row])])


def test_legacy_assertions_are_never_returned_result_counts(tmp_path):
    assertion = {
        "engine": "Custom label",
        "results_considered": [
            {"url": "https://example.org", "verdict": "not relevant"}
        ],
    }
    path = tmp_path / "legacy.json"
    path.write_text(
        json.dumps({"run": "historical", "searches": [assertion, assertion]})
    )
    records = legacy(path)
    assert (
        len(records) == 2 and records[0]["assertion_id"] != records[1]["assertion_id"]
    )
    assert records[0]["assertion"] == assertion
    assert summarise(records)["known_returned_result_total"] == 0
    assert summarise(records)["legacy_assertions"] == 2


def test_cli_and_machine_summary(tmp_path, capsys):
    source = save(tmp_path / "a.jsonl", [cc_call(), cc_result({"results": []})])
    output = tmp_path / "events.jsonl"
    assert main(["extract", str(source), "--out", str(output)]) == 0
    assert capsys.readouterr().out == ""
    assert main(["summary", str(output), "--json"]) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["executions"] == 1 and summary["known_returned_result_total"] == 0
    bad = tmp_path / "bad.jsonl"
    bad.write_text('{"format_version":"future"}\n')
    assert main(["summary", str(bad)]) == 1
    assert "unsupported event format" in capsys.readouterr().err


def test_personal_mcp_servers_are_never_research_calls():
    from scripts.research_log import tool_kind

    for name in (
        "mcp__google-workspace__search_contacts",
        "mcp__claude_ai_Gmail__search_threads",
        "mcp__beeper__search_messages",
        "mcp__claude_ai_Notion__notion-search",
    ):
        assert tool_kind(name, {}) is None
    assert tool_kind("mcp__exa__web_search_exa", {}) == "search"


def test_unicode_line_separators_inside_strings_do_not_split_lines(tmp_path):
    from scripts.research_log import read_rows

    path = tmp_path / "a.jsonl"
    text = "a" + chr(0x2028) + "b" + chr(0x85) + "c"
    path.write_text('{"text": "' + text + '"}\n', encoding="utf-8")
    (row,) = read_rows(path)
    assert row.data["text"] == text


def test_duplicate_fills_missing_result_and_drops_missing_note(tmp_path):
    a = save(tmp_path / "a.jsonl", [cc_call()])
    b = save(
        tmp_path / "b.jsonl", [cc_call(), cc_result({"query": "q", "results": []})]
    )
    (ev,), _ = extract([a, b])
    assert ev["execution_status"] is not None
    assert "missing_tool_result" not in ev["capture_notes"]


def test_conflicting_copy_never_supplies_counts(tmp_path):
    a = save(tmp_path / "a.jsonl", [meta(), hosted(results=["not a result object"])])
    b = save(tmp_path / "b.jsonl", [meta(), hosted()])
    (ev,), _ = extract([a, b])
    assert ev["results"] == [] and ev["results_returned"] is None
    assert "conflicting_duplicate_execution" in ev["capture_notes"]


def test_later_copy_notes_survive_cross_file_merge(tmp_path):
    first = {"query": "q", "results": [{"title": "A", "url": "https://example.org/a"}]}
    second = {"query": "q", "results": [{"title": "B", "url": "https://example.org/b"}]}
    a = save(tmp_path / "a.jsonl", [cc_call(), cc_result(first)])
    b = save(tmp_path / "b.jsonl", [cc_call(), cc_result(first), cc_result(second)])
    (ev,), _ = extract([a, b])
    assert "conflicting_duplicate_result" in ev["capture_notes"]


def codex_call(name, call_id):
    payload = {
        "type": "function_call",
        "name": name,
        "call_id": call_id,
        "arguments": '{"query": "q"}',
    }
    return {
        "type": "response_item",
        "timestamp": "2026-01-01T00:00:00Z",
        "payload": payload,
    }


def codex_output(call_id, output):
    payload = {"type": "function_call_output", "call_id": call_id, "output": output}
    return {
        "type": "response_item",
        "timestamp": "2026-01-01T00:00:01Z",
        "payload": payload,
    }


def test_conflicting_codex_outputs_are_noted_not_fatal(tmp_path):
    rows = [
        meta(),
        codex_call("mcp__exa__web_search_exa", "c1"),
        codex_output("c1", "first"),
        codex_output("c1", "second"),
        codex_call("mcp__beeper__search_messages", "c2"),
        codex_output("c2", "first"),
        codex_output("c2", "second"),
    ]
    events, _ = extract([save(tmp_path / "a.jsonl", rows)])
    (ev,) = events
    assert ev["provider"] == "exa"
    assert "conflicting_duplicate_result" in ev["capture_notes"]


def test_truncated_multibyte_line_keeps_earlier_executions(tmp_path):
    path = tmp_path / "a.jsonl"
    good = "".join(
        json.dumps(row) + "\n"
        for row in [cc_call(), cc_result({"query": "q", "results": []})]
    )
    path.write_bytes(good.encode() + b'{"text": "\xe2\x82')
    events, _ = extract([path])
    assert len(events) == 1


def test_web_search_call_with_dropped_entries_has_unknown_total(tmp_path):
    payload = {
        "type": "web_search_call",
        "id": "ws-1",
        "status": "completed",
        "action": {"type": "search", "query": "q"},
        "results": [{"url": "https://example.org"}, "not a result object"],
    }
    rows = [
        meta(),
        {
            "type": "response_item",
            "timestamp": "2026-01-01T00:00:00Z",
            "payload": payload,
        },
    ]
    (ev,), _ = extract([save(tmp_path / "a.jsonl", rows)])
    assert ev["results_returned"] is None


SEARCH_RESULTS = {
    "query": "q",
    "results": [
        {
            "tool_use_id": "s",
            "content": [{"title": "A", "url": "https://example.org/a"}],
        },
        "Summary prose",
    ],
}


@pytest.mark.parametrize(
    "name,args,raw,returned",
    [
        ("WebSearch", None, SEARCH_RESULTS, 1),
        (
            "WebFetch",
            {"url": "https://example.org/a"},
            {"code": 200, "url": "https://example.org/a"},
            None,
        ),
    ],
)
def test_copy_without_result_takes_non_empty_result(
    tmp_path, name, args, raw, returned
):
    # Issue 1: a copy with no output is not an observed empty result.
    for order in ("ab", "ba"):
        a = save(tmp_path / "a.jsonl", [cc_call(name, args)])
        b = save(tmp_path / "b.jsonl", [cc_call(name, args), cc_result(raw)])
        files = [a, b] if order == "ab" else [b, a]
        (ev,), _ = extract(files)
        assert ev["result_observed"] is True
        assert ev["results_returned"] == returned
        # A fetch has an outcome, not results.
        assert bool(ev["results"]) == (name == "WebSearch")
        assert bool(ev["fetch_outcomes"]) == (name == "WebFetch")
        assert ev["capture_completeness"] == "complete"
        assert not {"conflicting_duplicate_execution", "missing_tool_result"} & set(
            ev["capture_notes"]
        )


def test_cross_file_conflict_compares_raw_signature(tmp_path):
    # Issue 2: equal projections from different raw outputs are still a conflict.
    lossy = json.dumps({"results": [{"url": "https://example.org"}, 42]})
    clean = json.dumps({"results": [{"url": "https://example.org"}]})
    call = codex_call("mcp__exa__web_search_exa", "c1")
    a = save(tmp_path / "a.jsonl", [meta(), call, codex_output("c1", lossy)])
    b = save(tmp_path / "b.jsonl", [meta(), call, codex_output("c1", clean)])
    (ev,), _ = extract([a, b])
    assert ev["results_returned"] is None
    assert "conflicting_duplicate_execution" in ev["capture_notes"]


def test_string_result_entries_are_not_a_known_zero_outside_websearch(tmp_path):
    # Issue 3: only Claude WebSearch carries prose entries; elsewhere they are unparsed.
    output = json.dumps({"results": ["https://example.org/a"]})
    rows = [
        meta(),
        codex_call("mcp__exa__web_search_exa", "c1"),
        codex_output("c1", output),
    ]
    (ev,), _ = extract([save(tmp_path / "a.jsonl", rows)])
    assert ev["results_returned"] is None and ev["capture_completeness"] == "partial"
    rows = [
        cc_call("mcp__exa__web_search_exa"),
        cc_result({"results": ["https://example.org/a"]}),
    ]
    (ev,), _ = extract([save(tmp_path / "b.jsonl", rows)])
    assert ev["results_returned"] is None


def test_model_is_recorded_from_transcript(tmp_path):
    # Issue 4: Claude from the assistant message, Codex from the turn context.
    call = cc_call()
    call["message"]["model"] = "model-claude"
    (ev,), _ = extract([save(tmp_path / "a.jsonl", [call])])
    assert ev["model"] == "model-claude"
    rows = [
        meta(),
        {"type": "turn_context", "payload": {"model": "model-codex"}},
        hosted(),
    ]
    (ev,), _ = extract([save(tmp_path / "b.jsonl", rows)])
    assert ev["model"] == "model-codex"
    (ev,), _ = extract([save(tmp_path / "c.jsonl", [meta(), hosted()])])
    assert ev["model"] is None


def test_tool_options_are_lifted(tmp_path):
    # Issue 5: options are separate from what the operation acts on.
    rows = [
        cc_call(
            "WebSearch",
            {"query": "q", "mode": "extended", "allowed_domains": ["example.org"]},
        ),
        cc_call(
            "mcp__exa__web_search_exa",
            {"query": "q", "numResults": 5, "objective": "find x"},
            native="exa",
        ),
    ]
    events, _ = extract([save(tmp_path / "a.jsonl", rows)])
    options = {ev["tool"]["name"]: ev["operations"][0]["options"] for ev in events}
    assert options["WebSearch"] == {
        "mode": "extended",
        "allowed_domains": ["example.org"],
    }
    assert options["mcp__exa__web_search_exa"] == {
        "numResults": 5,
        "objective": "find x",
    }
    summary = summarise(events)["operation_options"]
    assert summary["WebSearch:mode=extended"] == 1
    assert summary["WebSearch:allowed_domains=set"] == 1
    action = {"type": "findInPage", "url": "https://example.org", "pattern": "x"}
    (ev,), _ = extract(
        [save(tmp_path / "b.jsonl", [meta(), hosted(action=action, results=[])])]
    )
    assert ev["operations"][0]["kind"] == "read"
    assert ev["operations"][0]["options"] == {"pattern": "x"}


def hosted_response(results=None, native="native-a"):
    payload = {
        "type": "web_search_call",
        "id": native,
        "status": "completed",
        "action": {"type": "search", "queries": ["alpha", "beta"]},
    }
    if results is not None:
        payload["results"] = results
    return {
        "type": "response_item",
        "timestamp": "2026-01-01T00:00:02Z",
        "payload": payload,
    }


def hosted_without_results():
    row = hosted()
    del row["payload"]["item"]["results"]
    return row


@pytest.mark.parametrize("shape", ["extension", "response"])
@pytest.mark.parametrize("first_has_results", [True, False])
def test_hosted_copy_without_results_takes_results(tmp_path, shape, first_has_results):
    # An absent hosted results list is not seen; an explicit [] is an observed zero.
    found = [{"url": "https://example.org"}]
    if shape == "extension":
        bare, full = hosted_without_results(), hosted(results=found)
    else:
        bare, full = hosted_response(), hosted_response(results=found)
    rows = [full, bare] if first_has_results else [bare, full]
    a = save(tmp_path / "a.jsonl", [meta(), rows[0]])
    b = save(tmp_path / "b.jsonl", [meta(), rows[1]])
    (ev,), _ = extract([a, b])
    assert ev["results_returned"] == 1 and ev["result_observed"] is True
    assert "conflicting_duplicate_execution" not in ev["capture_notes"]
    (empty,), _ = extract([save(tmp_path / "c.jsonl", [meta(), hosted(results=[])])])
    assert empty["result_observed"] is True and empty["results_returned"] == 0


@pytest.mark.parametrize("bare_first", [True, False])
def test_hosted_status_and_time_survive_merge_with_results_copy(tmp_path, bare_first):
    bare = hosted_response()
    full = hosted_response(results=[{"url": "https://example.org"}])
    del full["payload"]["status"], full["timestamp"]
    rows = [bare, full] if bare_first else [full, bare]
    a = save(tmp_path / "a.jsonl", [meta(), rows[0]])
    b = save(tmp_path / "b.jsonl", [meta(), rows[1]])
    (ev,), _ = extract([a, b])
    assert ev["execution_status"] == "completed"
    assert ev["ended_at"] == "2026-01-01T00:00:02Z"
    assert ev["results_returned"] == 1
    assert "conflicting_duplicate_execution" not in ev["capture_notes"]


def test_fetches_have_outcomes_not_results(tmp_path):
    exa_page = [
        {"type": "text", "text": "Title: Page\nURL: https://example.org/p\n\nBody"}
    ]
    rows = [
        cc_call("WebFetch", {"url": "https://example.org/a"}),
        cc_result({"code": 403, "url": "https://example.org/a"}),
        cc_call(
            "mcp__exa__web_fetch_exa",
            {"urls": ["https://example.org/p"], "maxCharacters": 100},
            native="exa",
        ),
        cc_result(exa_page, native="exa"),
    ]
    events, _ = extract([save(tmp_path / "a.jsonl", rows)])
    assert all(ev["results"] == [] for ev in events)
    summary = summarise(events)
    assert summary["observed_result_occurrences"] == 0
    assert summary["fetch_outcomes"] == {"403": 1, "content_returned": 1}
    assert summary["search_executions_with_unknown_result_total"] == 0


def mcp_item(tool, arguments, result, native="mcp-a", server="exa"):
    item = {
        "type": "McpToolCall",
        "id": native,
        "server": server,
        "tool": tool,
        "arguments": arguments,
        "status": "completed",
    }
    if result is not None:
        item["result"] = result
    return {
        "type": "event_msg",
        "payload": {
            "type": "item_completed",
            "started_at_ms": 1000,
            "completed_at_ms": 2000,
            "item": item,
        },
    }


def test_codex_mcp_items_match_claude_exa_events(tmp_path):
    text = {"type": "text", "text": "Title: A\nURL: https://example.org/a\n"}
    rows = [
        meta(),
        {"type": "turn_context", "payload": {"model": "model-codex"}},
        mcp_item(
            "web_search_exa", {"query": "q", "numResults": 3}, {"content": [text]}
        ),
        mcp_item(
            "web_fetch_exa",
            {"urls": ["https://example.org/a"]},
            {"content": [text]},
            native="mcp-b",
        ),
        mcp_item("search_messages", {"query": "q"}, {"content": []}, "mcp-c", "beeper"),
        mcp_item(
            "web_search_exa", {"query": "q"}, {"content": [], "isError": True}, "mcp-d"
        ),
    ]
    events, _ = extract([save(tmp_path / "a.jsonl", rows)])
    by_id = {ev["native_call_id"]: ev for ev in events}
    assert set(by_id) == {"mcp-a", "mcp-b", "mcp-d"}  # personal servers never logged
    search = by_id["mcp-a"]
    assert search["tool"]["name"] == "mcp__exa__web_search_exa"
    assert search["provider"] == "exa" and search["model"] == "model-codex"
    assert search["operations"][0]["options"] == {"numResults": 3}
    assert [r["url"] for r in search["results"]] == ["https://example.org/a"]
    assert search["results_returned"] is None
    assert "missing_tool_result" not in search["capture_notes"]
    assert by_id["mcp-b"]["fetch_outcomes"][0]["url"] == "https://example.org/a"
    assert by_id["mcp-d"]["execution_status"] == "failed"


def claude_agent_launch(agent_id, session="session-a", parent=None):
    row = cc_result(
        {"status": "completed", "agentId": agent_id}, native="launch-" + agent_id
    )
    row["sessionId"] = session
    if parent:
        row["agentId"] = parent
    else:
        del row["agentId"]
    return row


def test_subagent_transcripts_are_included_and_required(tmp_path):
    main_file = save(tmp_path / "s.jsonl", [claude_agent_launch("child")])
    sub = tmp_path / "s" / "subagents"
    sub.mkdir(parents=True)
    # A nested subagent: the child launches a grandchild.
    save(
        sub / "agent-child.jsonl",
        [cc_call(agent="child"), claude_agent_launch("grand", parent="child")],
    )
    with pytest.raises(ValueError, match="1 launched subagent transcript"):
        extract([main_file])
    save(sub / "agent-grand.jsonl", [cc_call(native="g", agent="grand")])
    events, _ = extract([main_file])  # the main file brings in s/ recursively
    assert {ev["agent_id"] for ev in events} == {"child", "grand"}
    # A copy of the main file alone, without its folder, is refused.
    alone = tmp_path / "copy"
    alone.mkdir()
    (alone / "s.jsonl").write_bytes(main_file.read_bytes())
    with pytest.raises(ValueError, match="missing from the inputs"):
        extract([alone / "s.jsonl"])
    events, diagnostics = extract([alone / "s.jsonl"], allow_missing_subagents=True)
    assert [
        d["agent_id"] for d in diagnostics if d["code"] == "missing_subagent_transcript"
    ] == ["child"]


def test_codex_subagent_rollout_is_required(tmp_path):
    started = {
        "type": "event_msg",
        "payload": {
            "type": "item_completed",
            "item": {
                "type": "SubAgentActivity",
                "kind": "started",
                "agent_thread_id": "child-thread",
            },
        },
    }
    parent = save(tmp_path / "parent.jsonl", [meta(), started])
    with pytest.raises(ValueError, match="child-thread"):
        extract([parent])
    child = save(
        tmp_path / "child.jsonl",
        [{"type": "session_meta", "payload": {"id": "child-thread"}}],
    )
    extract([parent, child])


def test_call_id_under_two_sessions_is_diagnosed(tmp_path):
    other = cc_call()
    other["sessionId"] = "session-b"
    _, diagnostics = extract(
        [save(tmp_path / "a.jsonl", [cc_call()]), save(tmp_path / "b.jsonl", [other])]
    )
    (d,) = [d for d in diagnostics if d["code"] == "call_id_in_several_sessions"]
    assert d["native_call_id"] == "call-a" and len(d["event_ids"]) == 2


def subagent_started(thread):
    return {
        "type": "event_msg",
        "payload": {
            "type": "item_completed",
            "item": {
                "type": "SubAgentActivity",
                "kind": "started",
                "agent_thread_id": thread,
            },
        },
    }


def test_codex_child_rollouts_are_brought_in(tmp_path):
    # A child (and its own child) may sit in another date folder of sessions/.
    day1 = tmp_path / "sessions" / "2026" / "01" / "01"
    day2 = tmp_path / "sessions" / "2026" / "01" / "02"
    day1.mkdir(parents=True)
    day2.mkdir(parents=True)
    parent = save(day1 / "rollout-t1-parent.jsonl", [meta(), subagent_started("kid")])

    def rollout(thread, rows):
        head = {"type": "session_meta", "payload": {"id": thread}}
        return save(day2 / f"rollout-t2-{thread}.jsonl", [head, *rows])

    rollout("kid", [subagent_started("grandkid"), hosted(native="k")])
    with pytest.raises(ValueError, match="grandkid"):
        extract([parent])
    rollout("grandkid", [hosted(native="g")])
    events, _ = extract([parent])
    assert {ev["session_id"] for ev in events} == {"kid", "grandkid"}


def test_api_request_blob_copies_are_not_calls(tmp_path):
    blob = cc_call("mcp__exa__web_fetch_exa", {"urls": '["https://example.org"]'})
    blob["type"] = "api-request-blob"
    del blob["timestamp"], blob["version"]
    rows = [
        cc_call("mcp__exa__web_fetch_exa", {"urls": ["https://example.org"]}),
        blob,
    ]
    (ev,), _ = extract([save(tmp_path / "a.jsonl", rows)])
    assert "conflicting_duplicate_execution" not in ev["capture_notes"]
    (only,), _ = extract([save(tmp_path / "b.jsonl", [rows[0]])])
    assert only["event_id"] == ev["event_id"]


def redact_text(value):
    from scripts.research_log import redact

    return redact(value)


def test_shell_credentials_are_redacted_and_references_kept(tmp_path):
    command = (
        'curl -H "X-Algolia-API-Key: abcdef1234567890" "https://x.example/q?apikey=SECRET123&f=1"'
        ' -H "Authorization: Bearer $KEY"'
    )
    (ev,), _ = extract(
        [save(tmp_path / "a.jsonl", [cc_call("Bash", {"command": command})])]
    )
    kept = ev["operations"][0]["arguments"]["command"]
    assert "abcdef1234567890" not in kept and "SECRET123" not in kept
    assert kept.count("<redacted>") == 2 and "Bearer $KEY" in kept
    short = redact_text("curl 'https://x.example/?token=12345' -d password=abc")
    assert "12345" not in short and "abc" not in short
