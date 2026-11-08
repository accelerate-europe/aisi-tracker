#!/usr/bin/env python3
"""Offline transcript capture pilot (standard library only).

extract reads JSONL files, or recursively finds *.jsonl in directories. It never
executes transcript code or follows referenced files/URLs. Output is private:
queries, tool arguments and result snippets may contain personal information.

The versioned event envelope describes captured executions, not research scope
or screening decisions. Unknown values are null; annotations belong elsewhere.
`complete` means the supported transcript representation was captured, not that
the search was exhaustive or fetched content was usable. Results are occurrences
in recorded search result lists, never unique URLs or inferred mentions in prose;
only search operations have results. A fetch has fetch_outcomes instead.

Subagents are part of the session: a Claude <session>.jsonl input brings in every
transcript under <session>/ (nested subagents included), and extraction refuses
an input set in which a launched subagent's transcript is missing.

Codex Extension actions can collapse a mixed web batch; those events are partial.
Shell curl/wget mentions are shell_http candidates, not proof an HTTP request ran.
No JavaScript parsing: wrapper source is not execution evidence. Native hosted
items are authoritative. Diagnostics expose unsupported wrapper-only retrieval.
Tool name/native_call_id are kept separate for a future OTel execute_tool export;
parent_execution_id/via are reserved for evidenced provenance, not chronology.
"""

from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sys
from typing import Any, Iterable

Json = dict[str, Any]
FORMAT = "research-log-pilot/3"
HTTP_SHELL = re.compile(r"(?<![\w.-])(?:curl|wget)(?![\w.-])")
RESEARCH_MCP_SERVERS = {"exa"}
WEB_NAMES = {"web__run", "web.run", "web.run_search", "web_search"}
# A literal credential in a shell command (an environment reference like $KEY stays).
CREDENTIAL = re.compile(
    r"(?i)((?:authorization:\s*(?:bearer|basic|token)\s+|x-[\w-]*api-key:\s*"
    r"|[?&](?:api[_-]?key|key|token|access_token)=|password[=:]\s*)[\"']?)"
    r"([^\s\"'&<$][^\s\"'&<]*)"
)
# What an operation acts on; every other argument is a tool option (mode, allowed_domains,
# numResults, objective, maxCharacters, offset, findInPage pattern, ...).
TARGET_KEYS = {"query", "queries", "q", "url", "urls", "ref_id", "prompt", "type"}
# Set when a result is observed; compared only when both copies observed one.
RESULT_FIELDS = (
    "result_observed",
    "result_signature",
    "results",
    "results_returned",
    "fetch_outcomes",
)
# execution_status and ended_at are not result fields: hosted items record them without
# results, and a Claude copy without a result has them as None, which never conflicts.


def digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


@dataclass
class Row:
    data: Json
    ref: Json


def read_rows(path: Path) -> list[Row]:
    """Read JSONL rows; undecodable, truncated or non-object lines are skipped with a diagnostic."""
    return read_file(path)[0]


def read_file(path: Path) -> tuple[list[Row], Json]:
    """Rows and the size and SHA-256 of exactly the bytes they were parsed from."""
    blob = path.read_bytes()
    sha = hashlib.sha256(blob).hexdigest()
    rows = []
    # split on newline only: splitlines() also breaks at U+2028 and U+0085, legal inside JSON strings
    for number, raw_line in enumerate(blob.split(b"\n"), 1):
        if not raw_line.strip():
            continue
        try:
            obj = json.loads(raw_line.decode("utf-8"))
        except ValueError as exc:
            # A session killed mid-write leaves a truncated line; skip it, say so.
            print(
                json.dumps(
                    {
                        "code": "skipped_invalid_line",
                        "path": str(path),
                        "line": number,
                        "error": str(exc),
                    }
                ),
                file=sys.stderr,
            )
            continue
        if not isinstance(obj, dict):
            print(
                json.dumps(
                    {
                        "code": "skipped_invalid_line",
                        "path": str(path),
                        "line": number,
                        "error": "not a JSON object",
                    }
                ),
                file=sys.stderr,
            )
            continue
        rows.append(Row(obj, {"path": str(path), "sha256": sha, "line": number}))
    return rows, {"bytes": len(blob), "sha256": sha}


def blocks(value: Any) -> list[Json]:
    return (
        [item for item in value if isinstance(item, dict)]
        if isinstance(value, list)
        else []
    )


def text_content(value: Any) -> str:
    if isinstance(value, str):
        return value
    return "\n".join(str(item.get("text", "")) for item in blocks(value))


def tool_kind(name: str, args: Json) -> str | None:
    lower = name.lower()
    if name in {"Bash", "exec_command", "shell", "shell_command", "CommandExecution"}:
        return (
            "shell_http"
            if HTTP_SHELL.search(str(args.get("command", args.get("cmd", ""))))
            else None
        )
    if name == "WebSearch" or name in WEB_NAMES:
        return "search"
    if name == "WebFetch":
        return "fetch"
    # Only public-web research servers: personal servers (mail, messages, contacts, docs)
    # also expose search/get/list tools, and their calls must never enter the log.
    if lower.startswith("mcp__"):
        if lower.split("__")[1] not in RESEARCH_MCP_SERVERS:
            return None
        leaf = lower.rsplit("__", 1)[-1]
        if "search" in leaf:
            return "search"
        if any(word in leaf for word in ("fetch", "crawl")):
            return "fetch"
        if "browse" in leaf:
            return "browse"
        if leaf.startswith(("read_", "get_", "list_", "query_")) or "__exa__" in lower:
            return "api_query"
    return None


def redact(value: Any) -> Any:
    if isinstance(value, str):
        return CREDENTIAL.sub(r"\1<redacted>", value)
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, dict):
        return {key: redact(item) for key, item in value.items()}
    return value


def has_credential(value: Any) -> bool:
    """Whether a literal credential is left in any string of a value (not its JSON)."""
    if isinstance(value, str):
        return CREDENTIAL.search(value) is not None
    if isinstance(value, list):
        return any(has_credential(item) for item in value)
    if isinstance(value, dict):
        return any(has_credential(item) for item in value.values())
    return False


def operation(kind: str, name: str, arguments: Any) -> Json:
    options = (
        {k: v for k, v in arguments.items() if k not in TARGET_KEYS}
        if isinstance(arguments, dict) and name != "shell_http"
        else {}
    )
    return {"kind": kind, "name": name, "arguments": arguments, "options": options}


def operations(name: str, args: Json) -> list[Json]:
    if name in WEB_NAMES:
        kinds = {
            "search_query": "search",
            "image_query": "search",
            "open": "fetch",
            "click": "browse",
            "find": "read",
            "screenshot": "read",
            "finance": "api_query",
            "weather": "api_query",
            "sports": "api_query",
            "time": "api_query",
        }
        return [
            operation(kind, key, item)
            for key, kind in kinds.items()
            for item in args.get(key, [])
        ]
    kind = tool_kind(name, args)
    if kind == "shell_http":
        # Shell commands are kept with literal credentials redacted.
        return [operation("fetch", "shell_http", redact(args))]
    if kind is None:
        return []
    if isinstance(args.get("urls"), list):
        return [operation(kind, name, {**args, "urls": [url]}) for url in args["urls"]]
    return [operation(kind, name, args)]


def event(
    harness: str,
    session: Any,
    agent: Any,
    native: Any,
    name: str,
    version: Any,
    ref: Json,
    model: Any = None,
) -> Json:
    if (
        not isinstance(session, str)
        or not session
        or not isinstance(native, str)
        or not native
    ):
        raise ValueError(
            f"{ref['path']}:{ref['line']}: missing native session/call identity"
        )
    namespace = name.rsplit("__", 1)[0] if name.startswith("mcp__") else None
    if name in WEB_NAMES or name == "web.search":
        namespace = "web"
    provider = None
    if name.startswith("mcp__exa__"):
        provider = "exa"
    elif name in {"WebSearch", "WebFetch"}:
        provider = "anthropic"
    elif namespace == "web":
        provider = "openai"
    return {
        "format_version": FORMAT,
        "record_type": "tool_execution",
        "event_id": "evt_" + digest([harness, session, agent, native]),
        "run_id": None,
        "session_id": session,
        "agent_id": agent,
        "native_call_id": native,
        "started_at": None,
        "ended_at": None,
        "timestamp_basis": None,
        "harness": {"name": harness, "version": version},
        # The model that issued the call, as the transcript records it; never inferred.
        "model": model if isinstance(model, str) else None,
        "tool": {"name": name, "namespace": namespace},
        "provider": provider,
        "engine": None,
        "operations": [],
        # result_observed: a result for this call was seen in this copy. Without
        # one, results=[] means "not seen", never an observed empty result.
        "result_observed": False,
        # Digest of the raw result as recorded, so copies are compared on the
        # source, not on the lossy projection below.
        "result_signature": None,
        "results": [],
        "results_returned": None,
        "execution_status": None,
        "capture_completeness": "partial",
        "capture_notes": [],
        "fetch_outcomes": [],
        "parent_execution_id": None,
        "via": [],
        "raw_ref": [ref],
    }


def add_results(ev: Json, values: list[Json], complete: bool) -> None:
    ev["results"] = [
        {"occurrence": i, "operation_index": None, **item}
        for i, item in enumerate(values, 1)
    ]
    ev["results_returned"] = len(values) if complete else None


def observe(ev: Json, raw: Any) -> None:
    ev["result_observed"] = True
    ev["result_signature"] = digest(raw)


def result_records(
    value: Any, prose_summaries: bool = False
) -> tuple[list[Json], bool]:
    """Accept only explicit result containers, never arbitrary nested URL fields.

    String entries are skipped without losing the total only where prose_summaries is
    set: Claude's WebSearch appends its assistant prose summary to results. Elsewhere a
    string entry is an unparsed result, so the total stays unknown.
    """
    if isinstance(value, dict) and isinstance(value.get("results"), list):
        found = []
        complete = True
        for item in value["results"]:
            if isinstance(item, dict) and isinstance(item.get("content"), list):
                children = item["content"]
                complete &= all(
                    isinstance(child, dict) and "url" in child for child in children
                )
                found.extend(
                    child
                    for child in children
                    if isinstance(child, dict) and "url" in child
                )
            elif isinstance(item, dict):
                found.append(item)
            elif not (prose_summaries and isinstance(item, str)):
                complete = False
        return found, complete
    return [], False


def claude_result(ev: Json, row: Row, block: Json, raw: Any) -> None:
    observe(ev, [block, raw, row.data.get("timestamp")])
    ev["raw_ref"].append(row.ref)
    ev["ended_at"] = row.data.get("timestamp")
    ev["execution_status"] = "failed" if block.get("is_error") is True else "completed"
    name = ev["tool"]["name"]
    if isinstance(raw, dict) and raw.get("interrupted"):
        ev["execution_status"] = "interrupted"
    if name == "WebFetch":
        code = raw.get("code") if isinstance(raw, dict) else None
        ev["fetch_outcomes"] = [
            {
                "url": raw.get("url") if isinstance(raw, dict) else None,
                "http_status": code,
                "content_usable": None,
                "outcome": "http_response"
                if code is not None
                else "tool_error"
                if block.get("is_error")
                else None,
            }
        ]
        if code is not None:
            ev["capture_completeness"] = "complete"
        else:
            ev["capture_notes"].append("fetch_without_structured_http_response")
    elif name.startswith("mcp__"):
        mcp_result(ev, raw, block.get("is_error") is True)
    elif name == "WebSearch":
        values, complete = result_records(raw, prose_summaries=True)
        add_results(ev, values, complete)
        ev["capture_completeness"] = "complete" if complete else "partial"
        if not complete:
            ev["capture_notes"].append("unstructured_or_error_result")
    else:
        ev["capture_notes"].append("shell_http_candidate_not_request_evidence")


def mcp_result(ev: Json, raw: Any, is_error: bool) -> None:
    """Parse a research MCP call's output, the same for Claude Code and Codex."""
    name = ev["tool"]["name"]
    kinds = {op["kind"] for op in ev["operations"]}
    exa = name.startswith("mcp__exa__")
    if "search" in kinds:
        values, complete = result_records(raw)
        if not complete and exa and isinstance(raw, list):
            # Exa's observed text envelope has Title/URL headers. Record visible
            # occurrences, but do not claim a total from an unframed text format.
            values.extend(exa_headers(raw))
            ev["capture_notes"].append("exa_text_headers_only_total_unknown")
        add_results(ev, values, complete)
        ev["capture_completeness"] = "complete" if complete else "partial"
        if not complete and not ev["capture_notes"]:
            ev["capture_notes"].append("unstructured_or_error_result")
    elif exa and "fetch" in kinds:
        # Exa fetch returns text with Title/URL headers per page and no HTTP status.
        outcomes = [
            {"url": item["url"], "outcome": "content_returned"}
            for item in exa_headers(raw)
        ] or ([{"url": None, "outcome": "tool_error"}] if is_error else [])
        ev["fetch_outcomes"] = [
            {"url": o["url"], "http_status": None, "content_usable": None, **o}
            for o in outcomes
        ]
        ev["capture_notes"].append("exa_fetch_text_headers_only")
    else:
        ev["capture_notes"].append("non_search_output_not_parsed")


def exa_headers(raw: Any) -> list[Json]:
    return [
        {"title": match.group(1), "url": match.group(2)}
        for chunk in blocks(raw)
        for match in re.finditer(
            r"(?m)^(?:Title: |# )([^\n]+)\nURL: (https?://[^\s]+)",
            str(chunk.get("text", "")),
        )
    ]


def claude_events(rows: list[Row]) -> list[Json]:
    calls: dict[tuple[Any, ...], Json] = {}
    results: list[tuple[tuple[Any, ...], Row, Json, Any]] = []
    for row in rows:
        obj = row.data
        session, agent = obj.get("sessionId"), obj.get("agentId")
        content = blocks(obj.get("message", {}).get("content"))
        result_count = sum(block.get("type") == "tool_result" for block in content)
        for block in content:
            # Only assistant messages issue calls; api-request-blob rows repeat them
            # as sent (e.g. urls as a JSON string) and are never the only copy.
            if block.get("type") == "tool_use" and obj.get("type") == "assistant":
                name, args = block.get("name", ""), block.get("input", {})
                if not isinstance(args, dict):
                    raise ValueError(f"{row.ref}: tool input is not an object")
                if tool_kind(name, args) is None:
                    continue
                ev = event(
                    "claude_code",
                    session,
                    agent,
                    block.get("id"),
                    name,
                    obj.get("version"),
                    row.ref,
                    obj.get("message", {}).get("model"),
                )
                ev["started_at"] = obj.get("timestamp")
                ev["timestamp_basis"] = "message_observation"
                ev["operations"] = operations(name, args)
                key = (session, agent, block.get("id"))
                if key in calls:
                    merge_event(calls[key], ev)
                else:
                    calls[key] = ev
            elif block.get("type") == "tool_result":
                # A message-wide toolUseResult cannot be assigned to two calls.
                raw = (
                    obj.get("toolUseResult", block.get("content"))
                    if result_count == 1
                    else block.get("content")
                )
                results.append(
                    ((session, agent, block.get("tool_use_id")), row, block, raw)
                )
    for key, row, block, raw in results:
        if key not in calls:
            continue
        if calls[key]["result_observed"]:
            # Transcripts can repeat a result line (resume, compaction); the first
            # one stays, a differing repeat is noted, never merged into counts.
            if (
                calls[key]["result_signature"]
                != digest([block, raw, row.data.get("timestamp")])
                and "conflicting_duplicate_result" not in calls[key]["capture_notes"]
            ):
                calls[key]["capture_notes"].append("conflicting_duplicate_result")
            calls[key]["raw_ref"].append(row.ref)
            continue
        claude_result(calls[key], row, block, raw)
    for ev in calls.values():
        if not ev["result_observed"]:
            ev["capture_notes"].append("missing_tool_result")
    return list(calls.values())


def iso_ms(value: Any) -> str | None:
    return (
        datetime.fromtimestamp(value / 1000, timezone.utc).isoformat()
        if isinstance(value, (int, float))
        else None
    )


def hosted_operations(action: Json) -> list[Json]:
    kind = action.get("type")
    if kind == "search":
        queries = action.get("queries")
        if not isinstance(queries, list):
            queries = [action["query"]] if action.get("query") is not None else []
        return [operation("search", "search", {"query": query}) for query in queries]
    mapping = {
        "openPage": "fetch",
        "open_page": "fetch",
        "findInPage": "read",
        "find_in_page": "read",
    }
    return [operation(mapping[kind], kind, action)] if kind in mapping else []


def mcp_item(
    events: dict[str, Json],
    item: Json,
    payload: Json,
    row: Row,
    session: Any,
    agent: Any,
    version: Any,
    model: Any,
) -> str | None:
    """A Codex MCP call: code-mode exec runs it, the completed item is the evidence."""
    server, tool = item.get("server"), item.get("tool")
    args = item.get("arguments") if isinstance(item.get("arguments"), dict) else {}
    if not isinstance(server, str) or not isinstance(tool, str):
        return None
    name = f"mcp__{server}__{tool}"  # the same tool name as in Claude Code
    if tool_kind(name, args) is None:  # also drops every non-research server
        return None
    ev = event("codex", session, agent, item.get("id"), name, version, row.ref, model)
    ev["started_at"] = iso_ms(payload.get("started_at_ms"))
    ev["ended_at"] = iso_ms(payload.get("completed_at_ms"))
    ev["timestamp_basis"] = "execution" if ev["started_at"] or ev["ended_at"] else None
    ev["operations"] = operations(name, args)
    raw = item.get("result")
    if isinstance(raw, dict):
        error = raw.get("isError") is True
        ev["execution_status"] = "failed" if error else item.get("status")
        observe(ev, raw)
        content = raw.get("content")
        mcp_result(ev, content if isinstance(content, list) else raw, error)
    else:
        ev["execution_status"] = item.get("status")
        ev["capture_notes"].append("missing_tool_result")
    insert_event(events, ev)
    return ev["event_id"]


def hosted_results(ev: Json, values: list[Any]) -> None:
    """A hosted item's entries are search results only when the item ran a search."""
    if any(op["kind"] == "search" for op in ev["operations"]):
        kept = blocks(values)
        add_results(ev, kept, len(kept) == len(values))
    elif values:
        ev["capture_notes"].append("non_search_entries_not_results")


def codex_events(rows: list[Row], diagnostics: list[Json]) -> list[Json]:
    meta: Json = {}
    for row in rows:
        if row.data.get("type") == "session_meta":
            meta = row.data.get("payload", {})
            break
    session = meta.get("id", meta.get("session_id"))
    agent = meta.get("agent_id")
    version = meta.get("cli_version")
    events: dict[str, Json] = {}
    outputs: dict[str, tuple[Row, Json]] = {}
    conflicting_outputs: set[str] = set()
    wrapper_refs = []
    native_hosted = 0
    item_events: set[str | None] = set()  # completed items carry their own result
    model = None  # from the latest turn_context, which names the model for its turn
    for row in rows:
        obj, payload = row.data, row.data.get("payload", {})
        typ = payload.get("type")
        if obj.get("type") == "turn_context":
            model = payload.get("model")
        elif obj.get("type") == "event_msg" and typ == "item_completed":
            item = payload.get("item", {})
            name = (
                item.get("kind", "")
                if item.get("type") == "Extension"
                else item.get("type", "")
            )
            if item.get("type") == "McpToolCall":
                item_events.add(
                    mcp_item(events, item, payload, row, session, agent, version, model)
                )
                continue
            shell = name == "CommandExecution" and HTTP_SHELL.search(
                str(item.get("command", ""))
            )
            if name != "web.search" and not shell:
                continue
            ev = event(
                "codex", session, agent, item.get("id"), name, version, row.ref, model
            )
            ev["started_at"] = iso_ms(payload.get("started_at_ms"))
            ev["ended_at"] = iso_ms(payload.get("completed_at_ms"))
            ev["timestamp_basis"] = (
                "execution" if ev["started_at"] or ev["ended_at"] else None
            )
            if shell:
                ev["operations"] = operations(name, {"command": item.get("command")})
                ev["execution_status"] = item.get("status")
                ev["capture_notes"] = ["shell_http_candidate_not_request_evidence"]
            else:
                native_hosted += 1
                action = item.get("action", {})
                ev["operations"] = hosted_operations(action)
                ev["native_action"] = action
                ev["execution_status"] = "completed"
                ev["capture_notes"] = ["hosted_action_may_omit_batch_operations"]
                if not ev["operations"]:
                    ev["capture_notes"].append("unsupported_hosted_action")
                values = item.get("results")
                # Only an explicit list, empty included, is an observed result; an
                # absent one is not seen, so a copy that has it can still supply it.
                if isinstance(values, list):
                    observe(ev, [action, values])
                    hosted_results(ev, values)
            insert_event(events, ev)
        elif obj.get("type") == "response_item":
            if typ in {"function_call_output", "custom_tool_call_output"}:
                native = payload.get("call_id")
                # Keep the first output; a differing repeat is noted on the event.
                if native in outputs:
                    if outputs[native][1] != payload:
                        conflicting_outputs.add(native)
                else:
                    outputs[native] = (row, payload)
            elif typ == "web_search_call":
                ev = event(
                    "codex",
                    session,
                    agent,
                    payload.get("id"),
                    "web.search",
                    version,
                    row.ref,
                    model,
                )
                ev["native_action"] = payload.get("action", {})
                ev["operations"] = hosted_operations(ev["native_action"])
                ev["execution_status"] = payload.get("status")
                ev["ended_at"] = obj.get("timestamp")
                ev["timestamp_basis"] = "message_observation"
                ev["capture_notes"] = ["hosted_action_may_omit_batch_operations"]
                values = payload.get("results")
                if isinstance(values, list):
                    # Same basis as the Extension item, so the two shapes agree.
                    observe(ev, [ev["native_action"], values])
                    hosted_results(ev, values)
                insert_event(events, ev)
            elif typ in {"function_call", "custom_tool_call"}:
                name = payload.get("name", "")
                raw = payload.get("arguments", payload.get("input", {}))
                if name in {"exec", "functions.exec"}:
                    if re.search(
                        r"tools\.(?:web__run|mcp__\w*(?:search|fetch)\w*)\s*\(",
                        str(raw),
                    ):
                        wrapper_refs.append(row.ref)
                    continue
                try:
                    args = json.loads(raw) if isinstance(raw, str) else raw
                except ValueError as exc:
                    if tool_kind(name, {}) is not None:
                        raise ValueError(f"{row.ref}: invalid tool arguments") from exc
                    continue
                if not isinstance(args, dict) or tool_kind(name, args) is None:
                    continue
                ev = event(
                    "codex",
                    session,
                    agent,
                    payload.get("call_id"),
                    name,
                    version,
                    row.ref,
                    model,
                )
                ev["operations"] = operations(name, args)
                ev["started_at"] = obj.get("timestamp")
                ev["timestamp_basis"] = "message_observation"
                insert_event(events, ev)
    for ev in events.values():
        if (
            ev["tool"]["name"] in {"web.search", "CommandExecution"}
            or ev["event_id"] in item_events
        ):
            continue
        joined = outputs.get(ev["native_call_id"])
        if joined is None:
            ev["capture_notes"].append("missing_tool_result")
            continue
        row, payload = joined
        observe(ev, payload)
        if ev["native_call_id"] in conflicting_outputs:
            ev["capture_notes"].append("conflicting_duplicate_result")
        ev["raw_ref"].append(row.ref)
        ev["ended_at"] = row.data.get("timestamp")
        raw = payload.get("output")
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except ValueError:
                pass
        # Receipt of an opaque output is not proof of success.
        ev["execution_status"] = (
            "failed"
            if isinstance(raw, dict) and raw.get("isError") is True
            else payload.get("status")
        )
        if any(op["kind"] == "search" for op in ev["operations"]):
            values, complete = result_records(raw)
            add_results(ev, values, complete)
            ev["capture_completeness"] = "complete" if complete else "partial"
            if not complete:
                ev["capture_notes"].append("unstructured_or_error_result")
        else:
            ev["capture_notes"].append("non_search_output_not_parsed")
        if any(op["name"] == "shell_http" for op in ev["operations"]):
            ev["capture_completeness"] = "partial"
            ev["capture_notes"].append("shell_http_candidate_not_request_evidence")
    if wrapper_refs:
        diagnostics.append(
            {
                "code": "wrapper_source_not_parsed",
                "raw_ref": wrapper_refs,
                "native_hosted_items_in_file": native_hosted,
                "note": "No source-level calls counted; no wrapper/child join inferred.",
            }
        )
    return list(events.values())


def merge_event(old: Json, new: Json) -> None:
    """Merge two copies of one native call (repeated lines, forked or resumed sessions).

    If the copies disagree on any recorded value, the first copy stays as it is and the
    conflict is noted: a conflicting copy never supplies counts or results. Result
    fields are compared only when both copies observed a result, and then on the raw
    result signature, not the projection. A copy without a result takes the result of a
    copy that has one. Otherwise values missing from the first copy are filled from the
    second. Notes are combined.
    """

    def disagree(a: Any, b: Any) -> bool:
        if a is None or b is None:
            return False
        if isinstance(a, dict) and isinstance(b, dict):
            return any(disagree(a.get(k), b.get(k)) for k in set(a) | set(b))
        return a != b

    both = old["result_observed"] and new["result_observed"]
    # capture_completeness describes a copy, not the call: it is settled after the merge.
    skip = {"raw_ref", "capture_notes", "capture_completeness"}
    if not both:
        skip.update(RESULT_FIELDS)
    fields = [k for k in new if k not in skip]
    conflict = any(disagree(old.get(k), new[k]) for k in fields) or (
        both and old["result_signature"] != new["result_signature"]
    )
    if not conflict:
        for key in fields:
            mine, value = old.get(key), new[key]
            if mine is None:
                old[key] = value
            elif isinstance(mine, dict) and isinstance(value, dict):
                for sub, subvalue in value.items():
                    if mine.get(sub) is None:
                        mine[sub] = subvalue
        if new["result_observed"] and not old["result_observed"]:
            for key in RESULT_FIELDS:
                old[key] = new[key]
            old["capture_completeness"] = new["capture_completeness"]
    notes = old["capture_notes"]
    for note in new["capture_notes"]:
        if note not in notes:
            notes.append(note)
    if old["result_observed"] and "missing_tool_result" in notes:
        notes.remove("missing_tool_result")
    if conflict and "conflicting_duplicate_execution" not in notes:
        notes.append("conflicting_duplicate_execution")
    for ref in new["raw_ref"]:
        if ref not in old["raw_ref"]:
            old["raw_ref"].append(ref)


def insert_event(events: dict[str, Json], ev: Json) -> None:
    if ev["event_id"] in events:
        merge_event(events[ev["event_id"]], ev)
    else:
        events[ev["event_id"]] = ev


def subagent_links(rows: list[Row], harness: str) -> tuple[set[str], dict[str, Json]]:
    """Agents whose transcript this file is, and agents it launched (id -> where).

    Claude: a subagent's rows carry its agentId; the launching tool result carries the
    same agentId. Codex: a child rollout's session_meta id is the agent_thread_id of
    the parent's SubAgentActivity "started" item.
    """
    present: set[str] = set()
    launched: dict[str, Json] = {}
    for row in rows:
        obj = row.data
        if harness == "claude_code":
            if isinstance(obj.get("agentId"), str):
                present.add(obj["agentId"])
            result = obj.get("toolUseResult")
            if isinstance(result, dict) and isinstance(result.get("agentId"), str):
                launched.setdefault(result["agentId"], row.ref)
        else:
            payload = obj.get("payload", {})
            if not isinstance(payload, dict):
                continue
            if obj.get("type") == "session_meta" and isinstance(payload.get("id"), str):
                present.add(payload["id"])
            item = payload.get("item", {})
            if (
                payload.get("type") == "item_completed"
                and isinstance(item, dict)
                and item.get("type") == "SubAgentActivity"
                and item.get("kind") == "started"
                and isinstance(item.get("agent_thread_id"), str)
            ):
                launched.setdefault(item["agent_thread_id"], row.ref)
    return present, launched


def codex_children(path: Path) -> set[str]:
    """Thread IDs of the subagents a Codex rollout started (cheap byte scan first)."""
    found = set()
    with path.open("rb") as handle:
        for line in handle:
            if b"SubAgentActivity" not in line:
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                continue  # read_rows reports invalid lines
            payload = obj.get("payload") if isinstance(obj, dict) else None
            item = payload.get("item") if isinstance(payload, dict) else None
            if (
                isinstance(item, dict)
                and item.get("type") == "SubAgentActivity"
                and item.get("kind") == "started"
                and isinstance(item.get("agent_thread_id"), str)
            ):
                found.add(item["agent_thread_id"])
    return found


def add_codex_children(files: set[Path], roots: list[Path]) -> None:
    """Bring in the child rollouts of Codex inputs, nested ones included.

    A child is a separate rollout-<time>-<thread id>.jsonl, possibly in another date
    folder: it is looked up under the parent's sessions/ tree and the input folders.
    """
    seen = {path.resolve() for path in files}
    queue = sorted(files)
    while queue:
        path = queue.pop()
        sessions = next((p for p in path.parents if p.name == "sessions"), None)
        trees = [*roots, *([sessions] if sessions else [])]
        for child in sorted(codex_children(path)):
            for tree in trees:
                hits = sorted(tree.rglob(f"rollout-*-{child}.jsonl"))
                for hit in hits:
                    if hit.resolve() not in seen:
                        seen.add(hit.resolve())
                        files.add(hit)
                        queue.append(hit)
                if hits:
                    break


def extract(
    paths: Iterable[Path], allow_missing_subagents: bool = False
) -> tuple[list[Json], list[Json]]:
    files: set[Path] = set()
    for path in paths:
        if not path.exists():
            raise ValueError(f"input does not exist: {path}")
        if path.is_dir():
            files.update(path.rglob("*.jsonl"))
        else:
            files.add(path)
            # Claude keeps a session's subagent transcripts, nested ones included,
            # under <session>/ beside <session>.jsonl.
            if path.with_suffix("").is_dir():
                files.update(path.with_suffix("").rglob("*.jsonl"))
    if not files:
        raise ValueError("no transcript files found")
    add_codex_children(files, [path for path in paths if path.is_dir()])
    events: dict[str, Json] = {}
    diagnostics: list[Json] = []
    present: set[tuple[str, str]] = set()
    launched: dict[tuple[str, str], Json] = {}
    for path in sorted(files):
        rows, read = read_file(path)
        if any(row.data.get("type") == "session_meta" for row in rows):
            captured = codex_events(rows, diagnostics)
            harness = "codex"
        elif any("sessionId" in row.data and "message" in row.data for row in rows):
            captured = claude_events(rows)
            harness = "claude_code"
        else:
            # Not a session transcript (e.g. a workflow journal): skipped, listed.
            diagnostics.append(
                {
                    "code": "skipped_unrecognised_file",
                    "path": str(path),
                    "lines": len(rows),
                    **read,
                }
            )
            continue
        here, started = subagent_links(rows, harness)
        present.update((harness, agent) for agent in here)
        for agent, ref in started.items():
            launched.setdefault((harness, agent), ref)
        diagnostics.append(
            {
                "code": "file_inventory",
                "path": str(path),
                "harness": harness,
                "lines": len(rows),
                **read,
                "events_before_deduplication": len(captured),
            }
        )
        for ev in captured:
            insert_event(events, ev)
    missing = sorted(key for key in launched if key not in present)
    for harness, agent in missing:
        diagnostics.append(
            {
                "code": "missing_subagent_transcript",
                "harness": harness,
                "agent_id": agent,
                "launched_at": launched[(harness, agent)],
            }
        )
    if missing and not allow_missing_subagents:
        shown = ", ".join(
            f"{agent} (launched at {launched[(h, agent)]['path']}:{launched[(h, agent)]['line']})"
            for h, agent in missing[:5]
        )
        raise ValueError(
            f"{len(missing)} launched subagent transcript(s) missing from the inputs: "
            f"{shown}; pass the session directory, or --allow-missing-subagents "
            "for a knowingly partial corpus"
        )
    # One native call ID under two sessions is a re-keyed copy (e.g. a fork), counted twice.
    sessions: dict[tuple[str, str], list[Json]] = {}
    for ev in events.values():
        sessions.setdefault((ev["harness"]["name"], ev["native_call_id"]), []).append(
            ev
        )
    for (harness, native), copies in sorted(sessions.items()):
        if len(copies) > 1:
            diagnostics.append(
                {
                    "code": "call_id_in_several_sessions",
                    "harness": harness,
                    "native_call_id": native,
                    "event_ids": sorted(ev["event_id"] for ev in copies),
                }
            )
    # Stable native identity order, independent of input order and file location.
    return sorted(events.values(), key=lambda ev: ev["event_id"]), diagnostics


def summarise(events: list[Json]) -> Json:
    executions = [ev for ev in events if ev.get("record_type") == "tool_execution"]

    def count(values: Iterable[Any]) -> Json:
        return dict(
            sorted(Counter("unknown" if v is None else str(v) for v in values).items())
        )

    return {
        "executions": len(executions),
        "legacy_assertions": sum(
            ev.get("record_type") == "legacy_assertion" for ev in events
        ),
        "by_harness": count(ev["harness"]["name"] for ev in executions),
        "by_tool": count(ev["tool"]["name"] for ev in executions),
        "by_model": count(ev["model"] for ev in executions),
        "operation_options": count(
            f"{ev['tool']['name']}:{key}="
            # Values only for numbers and short tokens; free text (objective) is "set".
            + (
                str(value)
                if isinstance(value, (int, float, bool))
                or (isinstance(value, str) and len(value) <= 20 and " " not in value)
                else "set"
            )
            for ev in executions
            for op in ev["operations"]
            for key, value in op["options"].items()
        ),
        "by_kind_operations": count(
            op["kind"] for ev in executions for op in ev["operations"]
        ),
        "by_status": count(ev["execution_status"] for ev in executions),
        "by_capture_completeness": count(
            ev["capture_completeness"] for ev in executions
        ),
        "observed_result_occurrences": sum(len(ev["results"]) for ev in executions),
        "known_returned_result_total": sum(
            ev["results_returned"]
            for ev in executions
            if ev["results_returned"] is not None
        ),
        # Only search operations have results, so the unknown count is over searches.
        "search_executions_with_unknown_result_total": sum(
            ev["results_returned"] is None
            for ev in executions
            if any(op["kind"] == "search" for op in ev["operations"])
        ),
        "observed_result_occurrences_by_tool": count(
            ev["tool"]["name"] for ev in executions for _ in ev["results"]
        ),
        "fetch_outcomes": count(
            outcome.get("http_status")
            if outcome.get("http_status") is not None
            else outcome.get("outcome")
            for ev in executions
            for outcome in ev["fetch_outcomes"]
        ),
        "fetch_operations_without_outcomes": sum(
            max(
                0,
                sum(op["kind"] == "fetch" for op in ev["operations"])
                - len(ev["fetch_outcomes"]),
            )
            for ev in executions
        ),
        "capture_notes": count(
            note for ev in executions for note in ev["capture_notes"]
        ),
    }


def legacy(path: Path) -> list[Json]:
    blob = path.read_bytes()
    obj = json.loads(blob)
    if not isinstance(obj, dict) or not isinstance(obj.get("searches"), list):
        raise ValueError("legacy input must contain a searches array")
    sha = hashlib.sha256(blob).hexdigest()
    metadata = {key: value for key, value in obj.items() if key != "searches"}
    return [
        {
            "format_version": FORMAT,
            "record_type": "legacy_assertion",
            "assertion_id": "legacy_" + digest([sha, index]),
            "metadata": metadata,
            "assertion": assertion,
            "raw_ref": {
                "path": str(path),
                "sha256": sha,
                "json_pointer": f"/searches/{index}",
            },
        }
        for index, assertion in enumerate(obj["searches"])
    ]


def write_jsonl(path: Path, records: list[Json]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(
            json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n"
            for item in records
        ),
        encoding="utf-8",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    extract_parser = commands.add_parser(
        "extract", help="Capture supported native retrieval executions"
    )
    extract_parser.add_argument("paths", nargs="+", type=Path)
    extract_parser.add_argument("--out", required=True, type=Path)
    extract_parser.add_argument(
        "--allow-missing-subagents",
        action="store_true",
        help="Extract a knowingly partial corpus; missing subagents stay in diagnostics",
    )
    extract_parser.add_argument(
        "--diagnostics",
        type=Path,
        help="Optional JSON diagnostics file (also printed to stderr)",
    )
    summary_parser = commands.add_parser(
        "summary", help="Count executions, operations and observed results separately"
    )
    summary_parser.add_argument("path", type=Path)
    summary_parser.add_argument("--json", action="store_true")
    legacy_parser = commands.add_parser(
        "legacy", help="Import hand-kept assertions, never execution evidence"
    )
    legacy_parser.add_argument("path", type=Path)
    legacy_parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "extract":
            events, diagnostics = extract(args.paths, args.allow_missing_subagents)
            write_jsonl(args.out, events)
            if args.diagnostics:
                args.diagnostics.write_text(
                    json.dumps(diagnostics, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
            for item in diagnostics:
                print(json.dumps(item, ensure_ascii=False), file=sys.stderr)
            print(f"Wrote {len(events)} executions to {args.out}", file=sys.stderr)
        elif args.command == "legacy":
            records = legacy(args.path)
            write_jsonl(args.out, records)
            print(
                f"Wrote {len(records)} legacy assertions to {args.out}", file=sys.stderr
            )
        else:
            records = [row.data for row in read_rows(args.path)]
            if any(item.get("format_version") != FORMAT for item in records):
                raise ValueError("unsupported event format version")
            summary = summarise(records)
            if args.json:
                print(json.dumps(summary, ensure_ascii=False, indent=2))
            else:
                for key, value in summary.items():
                    print(
                        f"{key.replace('_', ' ')}: {json.dumps(value, ensure_ascii=False) if isinstance(value, dict) else value}"
                    )
    except (OSError, ValueError, TypeError) as exc:
        print(f"research_log: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
