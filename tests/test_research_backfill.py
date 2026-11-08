"""Invented minimal fixtures; never copied transcript data."""

import json
from pathlib import Path

import pytest

from scripts.research_backfill import build, write_basis


def line(obj):
    return json.dumps(obj) + "\n"


def claude_session(folder: Path, name: str, when: str, extra: str = "") -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    row = {
        "type": "assistant",
        "sessionId": name,
        "timestamp": when,
        "message": {
            "content": [
                {
                    "type": "tool_use",
                    "id": "call-" + name,
                    "name": "WebSearch",
                    "input": {"query": "q " + extra},
                }
            ]
        },
    }
    path = folder / f"{name}.jsonl"
    path.write_text(line(row))
    return path


def test_build_inventories_filters_by_cutoff_and_lists_residual(tmp_path):
    projects = tmp_path / ".claude" / "projects"
    early = claude_session(
        projects / "-Users-x-Code-ae-aisi-tracker", "early", "2026-11-01T10:00:00Z"
    )
    claude_session(
        projects / "-Users-x-Code-ae-aisi-tracker", "late", "2026-11-08T10:00:00Z"
    )
    claude_session(
        projects / "-private-tmp-x-aisi-smoke", "smoke", "2026-11-01T10:00:00Z"
    )
    other = claude_session(
        projects / "-Users-x-Projects-elsewhere",
        "other",
        "2026-11-01T10:00:00Z",
        "data/aisi.json",
    )
    claude_session(
        projects / "-Users-x-Projects-unrelated", "unrelated", "2026-11-01T10:00:00Z"
    )
    (tmp_path / ".codex" / "sessions").mkdir(parents=True)
    part = build(tmp_path, "2026-11-07T00:00:00+01:00")
    assert [item["path"] for item in part["inventory"]] == sorted(
        str(p)
        for p in [early, projects / "-Users-x-Code-ae-aisi-tracker" / "late.jsonl"]
    )
    assert all(
        len(item["sha256"]) == 64 and item["bytes"] > 0 for item in part["inventory"]
    )
    assert [ev["session_id"] for ev in part["events"]] == ["early"]
    assert part["counts"]["executions_after_cutoff"] == 1
    assert [r["path"] for r in part["residual"]] == [str(other)]


def test_basis_holds_no_transcripts_and_is_never_overwritten(tmp_path):
    projects = tmp_path / "home" / ".claude" / "projects"
    claude_session(projects / "-Users-x-aisi", "s", "2026-11-01T10:00:00Z")
    (tmp_path / "home" / ".codex" / "sessions").mkdir(parents=True)
    part = build(tmp_path / "home", "2026-11-07T00:00:00+01:00")
    out = tmp_path / "basis"
    write_basis([part], out)
    assert sorted(p.name for p in out.iterdir()) == [
        "inventory.json",
        "log.diagnostics.json",
        "log.jsonl",
        "summary.json",
    ]
    assert "events" not in json.loads((out / "inventory.json").read_text())["hosts"][0]
    with pytest.raises(ValueError, match="never overwritten"):
        write_basis([part], out)


def test_basis_refuses_literal_credentials(tmp_path):
    part = {
        "shell_commands_with_credentials": ["evt_x"],
        "events": [],
        "diagnostics": [],
    }
    with pytest.raises(ValueError, match="credentials"):
        write_basis([part], tmp_path / "basis")


def test_codex_scratch_is_excluded_and_inventory_matches_extracted_bytes(tmp_path):
    sessions = tmp_path / ".codex" / "sessions" / "2026" / "11" / "01"
    sessions.mkdir(parents=True)
    (tmp_path / ".claude" / "projects").mkdir(parents=True)
    for name, cwd in (
        ("real", "/Users/x/Code/ae/aisi-tracker"),
        ("smoke", "/tmp/aisi-smoke"),
    ):
        (sessions / f"rollout-t-{name}.jsonl").write_text(
            line({"type": "session_meta", "payload": {"id": name, "cwd": cwd}})
        )
    part = build(tmp_path, "2026-11-07T00:00:00+01:00")
    (item,) = part["inventory"]
    assert item["path"].endswith("rollout-t-real.jsonl")
    blob = Path(item["path"]).read_bytes()
    assert item["bytes"] == len(blob)


def test_escaped_quotes_are_not_credentials():
    from scripts.research_log import has_credential, redact

    command = (
        'python -c \'h=["-H","Authorization: Bearer "+key]\'; curl "u?x=1&api_key=" + q'
    )
    # Scanning strings, not their JSON, an escaped quote is not a value; whatever
    # the redaction masks (over-masking code is harmless), nothing is left after it.
    assert not has_credential({"command": redact(command)})
    assert has_credential({"command": ["curl", "u?token=abc"]})


@pytest.mark.parametrize(
    "row",
    [
        {
            "type": "response_item",
            "payload": {"type": "function_call", "name": "web__run", "call_id": "c"},
        },
        {"type": "response_item", "payload": {"type": "web_search_call", "id": "w"}},
        {
            "type": "event_msg",
            "payload": {
                "type": "item_completed",
                "item": {"type": "McpToolCall", "server": "exa"},
            },
        },
    ],
)
def test_out_of_rule_codex_research_is_residual(tmp_path, row):
    (tmp_path / ".claude" / "projects").mkdir(parents=True)
    day = tmp_path / ".codex" / "sessions" / "2026" / "11" / "01"
    day.mkdir(parents=True)
    path = day / "rollout-t-x.jsonl"
    meta = {
        "type": "session_meta",
        "payload": {"id": "x", "cwd": "/Users/x/Projects/elsewhere"},
    }
    note = {
        "type": "response_item",
        "payload": {"type": "message", "text": "see data/aisi.json"},
    }
    path.write_text(
        line(meta) + line(note) + json.dumps(row, separators=(",", ":")) + "\n"
    )
    part = build(tmp_path, "2026-11-07T00:00:00+01:00")
    assert [r["path"] for r in part["residual"]] == [str(path)]
