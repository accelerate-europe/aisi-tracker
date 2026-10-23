"""Run the page's pure timeline binding without a DOM or browser."""

import json
from pathlib import Path
import shutil
import subprocess

import pytest


PAGE = Path(__file__).resolve().parents[1] / "site/index.html"
pytestmark = pytest.mark.skipif(
    shutil.which("node") is None,
    reason="Node is required for the page's JavaScript binding tests",
)


def binding_call(expression):
    source = PAGE.read_text()
    start = source.index("  function firstSentence(")
    end = source.index("  function render(d", start)
    result = subprocess.run(
        [
            "node",
            "-e",
            source[start:end] + "\nconsole.log(JSON.stringify(" + expression + "));",
        ],
        text=True,
        capture_output=True,
        check=True,
    )
    return json.loads(result.stdout)


def event(identity, date, kind="designated", **fields):
    return {
        "id": identity,
        "date": date,
        "event": kind,
        "description": "First sentence. Second sentence.",
        "source_ids": [identity + "-source"],
        **fields,
    }


def entries(events, identity="XX", name="Fixture jurisdiction"):
    return {"id": identity, "name": name, "timeline": events}


def developments(jurisdictions):
    from scripts.display import recent_developments

    for jurisdiction in jurisdictions:
        for item in jurisdiction.get("timeline", []):
            item["citations"] = {"description": item["source_ids"]}
    return recent_developments(jurisdictions, lambda *groups: list(dict.fromkeys(s for group in groups for s in group)))


def test_short_fallback_and_same_day_merge_keep_all_sources():
    rows = developments(
        [
            entries(
                [
                    event(
                        "first",
                        "2026-05-01",
                        short="Short account",
                        source_ids=["one", "shared"],
                    ),
                    event(
                        "second", "2026-05-01", "launched", source_ids=["shared", "two"]
                    ),
                    event("other", "2026-05-01", "appointment"),
                ]
            )
        ]
    )
    assert len(rows) == 1
    assert rows[0]["texts"] == ["Short account", "First sentence."]
    assert rows[0]["source_ids"] == ["one", "shared", "two"]
    assert rows[0]["events"] == ["first", "second"]


def test_partial_dates_are_preserved_and_never_merged():
    rows = developments(
        [
            entries(
                [
                    event("year", "2026"),
                    event("month", "2026-05"),
                    event("month2", "2026-05"),
                    event("day", "2026-05-01"),
                ]
            )
        ]
    )
    assert [row["date"] for row in rows] == ["2026-05-01", "2026-05", "2026-05", "2026"]
    assert [row["events"] for row in rows] == [["day"], ["month"], ["month2"], ["year"]]


def test_same_date_different_jurisdictions_stays_separate_with_stable_order():
    rows = developments(
        [entries([event("b", "2026-05-01")], "YY"), entries([event("a", "2026-05-01")])]
    )
    assert [row["jurisdiction"] for row in rows] == ["XX", "YY"]


def test_ten_row_limit_applies_after_grouping_and_accepts_all_requested_types():
    kinds = [
        "commitment",
        "proposed",
        "designated",
        "launched",
        "first_output",
        "exploration_started",
        "withdrawn",
        "closed",
    ]
    events = [
        event(str(index), f"2026-05-{index + 1:02}", kinds[index % len(kinds)])
        for index in range(12)
    ]
    events.append(event("same-day", "2026-05-12", "launched"))
    rows = developments([entries(events)])
    assert len(rows[:3]) == 3 and len(rows[3:]) == 7
    assert rows[0]["events"] == ["11", "same-day"]
    assert rows[-1]["date"] == "2026-05-03"
    assert {e for row in rows for e in row["events"]} >= {str(i) for i in range(4, 12)}


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            "Dr. Smith reported for the U.K. institute. Later work followed.",
            "Dr. Smith reported for the U.K. institute.",
        ),
        (
            "Tests include e.g. cyber risks. More text.",
            "Tests include e.g. cyber risks.",
        ),
        ("Version 1.2 was published! More text.", "Version 1.2 was published!"),
        ('The report says "done." Another sentence.', 'The report says "done."'),
        (
            "An undated statement with no terminal punctuation",
            "An undated statement with no terminal punctuation",
        ),
    ],
)
def test_first_sentence_preserves_abbreviations_and_punctuation(text, expected):
    assert binding_call("firstSentence(" + json.dumps(text) + ")") == expected


def test_empty_short_and_missing_timeline():
    rows = developments(
        [entries([event("one", "2026", short="  ")]), {"id": "YY", "name": "Empty"}]
    )
    assert rows[0]["texts"] == ["First sentence."]
    assert developments([]) == []
