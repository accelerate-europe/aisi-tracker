"""Intervals between milestones: hand-worked expectations, never the producer's own output."""

import copy
import json
from datetime import date
from pathlib import Path

import pytest

from scripts import build
from scripts.display import interval_phrase
from scripts.published import date_bounds, earliest, interval, intervals, publish

DATA = Path(__file__).resolve().parents[1] / "data/aisi.json"
KEYS = (
    "announced_to_established",
    "established_to_first_qualifying_activity",
    "announced_to_first_qualifying_activity",
)

# Worked by hand from the timeline and credit dates at as_of 2026-10-23 (see the commit message).
# (announced to established, established to first qualifying activity, announced to first qualifying activity)
EXPECTED = {
    "gb-aisi": ((7, 7), (99, 99), (106, 106)),  # 26 Oct 2023, 2 Nov 2023, 9 Feb 2024
    "fr-inesia": (None, (146, 146), None),  # no commitment or proposal; 31 Jan 2025, 26 Jun 2025
    "de-aisi": ((84, 84), None, None),  # 8 Jun 2026, 31 Aug 2026, nothing qualifying yet
    "es-isia": (None, None, None),  # planned
    "no-ki-norge": (None, None, None),  # proposed
    "eu-ai-office": ((28, 28), (512, 512), (540, 540)),  # 24 Jan 2024, 21 Feb 2024, 17 Jul 2025
    "us-caissi": ((98, 98), (286, 286), (384, 384)),  # 1 Nov 2023, 7 Feb 2024, 19 Nov 2024
    "ca-caisi": ((210, 210), (226, 226), (436, 436)),  # proposal 16 Apr 2024, 12 Nov 2024, 26 Jun 2025
    "jp-aisi": ((55, 55), (411, 411), (466, 466)),  # 21 Dec 2023, 14 Feb 2024, 31 Mar 2025
    "kr-aisi": ((189, 189), (211, 211), (400, 400)),  # 22 May 2024, 27 Nov 2024 (not the 2026 redesignation), 26 Jun 2025
    "sg-aisi": (None, (182, 182), None),  # no commitment or proposal; 22 May 2024, 20 Nov 2024
    # 25 Nov 2025; "2026" read as 1 Jan to 23 Oct 2026 (as_of cap); 8 Oct 2026.
    "au-aisi": ((37, 332), (-15, 280), (317, 317)),
    "my-aisafe": (None, None, None),  # no commitment or proposal; nothing qualifying yet
    "in-aisi": ((-29, 335), None, None),  # 30 Jan 2025; "2025" read as the whole year
    # Planned: qualifying network credit (26 Jun 2025) is not an institute interval.
    "ke-ai-safety-institute": (None, None, None),
}

CARD = {
    "gb-aisi": ("7 days after announcement", "about 3 months after establishment"),
    "fr-inesia": (None, "about 5 months after establishment"),
    "de-aisi": ("about 3 months after announcement", None),
    "eu-ai-office": ("28 days after announcement", "about 17 months after establishment"),
    "us-caissi": ("about 3 months after announcement", "about 9 months after establishment"),
    "ca-caisi": ("about 7 months after proposal", "about 7 months after establishment"),
    "jp-aisi": ("55 days after announcement", "about 14 months after establishment"),
    "kr-aisi": ("about 6 months after announcement", "about 7 months after establishment"),
    "sg-aisi": (None, "about 6 months after establishment"),
    "au-aisi": (None, None),
    "my-aisafe": (None, None),
    "in-aisi": (None, None),
}


@pytest.fixture(scope="module")
def published():
    return publish(json.loads(DATA.read_text(encoding="utf-8")))


def as_pairs(record):
    return tuple(
        None if record["intervals"][key] is None
        else (record["intervals"][key]["days_min"], record["intervals"][key]["days_max"])
        for key in KEYS
    )


def test_every_record_matches_hand_worked_intervals(published):
    assert {i["id"]: as_pairs(i) for i in published["institutes"]} == EXPECTED


def test_card_wording_for_established_records(published):
    shown = {
        i["id"]: (
            i["display"]["intervals"]["status_since"],
            (i["display"]["intervals"]["known_activity"] or {}).get("text"),
        )
        for i in published["institutes"]
        if i["status"] == "established"
    }
    assert shown == CARD


def test_interval_bounds_are_ordered_in_every_record(published):
    for institute in published["institutes"]:
        for span in institute["intervals"].values():
            assert span is None or span["days_min"] <= span["days_max"]


@pytest.mark.parametrize(
    "day,as_of,bounds",
    [
        ("2025-03-07", "2026-10-20", (date(2025, 3, 7), date(2025, 3, 7))),
        ("2024-02", "2026-10-20", (date(2024, 2, 1), date(2024, 2, 29))),
        ("2025-12", "2026-10-20", (date(2025, 12, 1), date(2025, 12, 31))),
        ("2025", "2026-10-20", (date(2025, 1, 1), date(2025, 12, 31))),
        ("2026", "2026-10-20", (date(2026, 1, 1), date(2026, 10, 20))),  # never later than as_of
        ("2026-10", "2026-10-20", (date(2026, 10, 1), date(2026, 10, 20))),
    ],
)
def test_partial_dates_cover_their_whole_period_up_to_as_of(day, as_of, bounds):
    assert date_bounds(day, as_of) == bounds


def test_earliest_of_mixed_precision_is_an_interval_not_a_string_order():
    # Lexically "2025" sorts first, but qualifying work is certain by 1 March 2025.
    assert earliest([(0, "2025"), (1, "2025-03-01")], "2026-10-20") == (
        (date(2025, 1, 1), date(2025, 3, 1)),
        [0, 1],
    )
    # A later item whose period starts after that bound cannot be the earliest.
    assert earliest([(0, "2025-03-01"), (1, "2025-04")], "2026-10-20") == (
        (date(2025, 3, 1), date(2025, 3, 1)),
        [0],
    )
    # Ties keep every candidate; order of input does not change the bounds.
    assert earliest([(4, "2025-06-26"), (2, "2025-06-26")], "2026-10-20") == (
        (date(2025, 6, 26), date(2025, 6, 26)),
        [4, 2],
    )
    assert earliest([], "2026-10-20") == (None, [])


def test_signed_ranges():
    day = date(2025, 1, 1)
    assert interval((day, day), (day, day)) == {"days_min": 0, "days_max": 0}
    assert interval((date(2025, 2, 1),) * 2, (day, day)) == {"days_min": -31, "days_max": -31}
    # Order unknown: the range crosses zero rather than being clamped.
    assert interval((date(2025, 1, 30),) * 2, (date(2025, 1, 1), date(2025, 12, 31))) == {"days_min": -29, "days_max": 335}
    assert interval(None, (day, day)) is None


@pytest.mark.parametrize(
    "days,text",
    [
        (0, "same day as announcement"),
        (1, "1 day after announcement"),
        (59, "59 days after announcement"),
        (60, "about 2 months after announcement"),
        (99, "about 3 months after announcement"),
        (411, "about 14 months after announcement"),  # 13.503 rounds half up to 14
        (-1, "1 day before announcement"),
        (-59, "59 days before announcement"),
        (-100, "about 3 months before announcement"),
    ],
)
def test_card_wording_of_exact_intervals(days, text):
    assert interval_phrase({"days_min": days, "days_max": days}, "announcement") == text


@pytest.mark.parametrize("span", [{"days_min": 37, "days_max": 329}, {"days_min": -12, "days_max": 280}, None])
def test_ranges_and_gaps_have_no_card_wording(span):
    assert interval_phrase(span, "establishment") is None


def institute(status, outputs=()):
    return {
        "status": status,
        "outputs": [
            {"lineage": lineage, "qualifies_as": kind, "date": day, "credit": {"date": day}}
            for lineage, kind, day in outputs
        ],
    }


def test_planned_body_with_qualifying_work_gets_no_activity_interval():
    # Kenya-shaped: a commitment and named network credit, but no designation.
    events = [{"id": "c", "event": "commitment", "date": "2025-03-27"}]
    result = intervals(institute("planned", [("net", "joint_testing", "2025-06-26")]), events, "2026-10-20")
    assert result == dict.fromkeys(KEYS)


def test_closed_institute_keeps_its_history_but_no_activity_interval():
    events = [
        {"id": "c", "event": "commitment", "date": "2024-01-01"},
        {"id": "d", "event": "designated", "date": "2024-01-31"},
        {"id": "x", "event": "closed", "date": "2025-06-01"},
    ]
    body = institute("none_identified", [("eval", "published_work", "2024-05-01")])
    assert intervals(body, events, "2026-10-20") == {
        "announced_to_established": {"days_min": 30, "days_max": 30},
        "established_to_first_qualifying_activity": None,
        "announced_to_first_qualifying_activity": None,
    }


def test_designation_alone_is_not_an_announcement_and_withdrawn_acts_drop_out():
    events = [
        {"id": "d1", "event": "designated", "date": "2024-01-31"},
        {"id": "w", "event": "withdrawn", "date": "2024-02-10", "withdraws": "d1"},
        {"id": "d2", "event": "designated", "date": "2024-03-01"},
    ]
    body = institute("established", [("guide", "none", "2024-03-02"), ("eval", "published_work", "2024-03-11")])
    assert intervals(body, events, "2026-10-20") == {
        "announced_to_established": None,
        "established_to_first_qualifying_activity": {"days_min": 10, "days_max": 10},
        "announced_to_first_qualifying_activity": None,
    }


def test_predecessor_credit_before_establishment_is_negative_not_clamped():
    events = [{"id": "d", "event": "designated", "date": "2023-11-02"}]
    body = institute("established", [("taskforce-eval", "published_work", "2023-09-07")])
    assert intervals(body, events, "2026-10-20")["established_to_first_qualifying_activity"] == {"days_min": -56, "days_max": -56}


def test_authored_intervals_are_rejected_and_stale_values_disagree():
    authored = json.loads(DATA.read_text(encoding="utf-8"))
    strict = copy.deepcopy(authored)
    strict["institutes"][0]["intervals"] = dict.fromkeys(KEYS)
    with pytest.raises(ValueError, match="intervals: derived field must not be authored"):
        publish(strict, strict=True)
    stale = copy.deepcopy(authored)
    stale["institutes"][0]["intervals"] = dict.fromkeys(KEYS)
    with pytest.raises(ValueError, match="intervals: stored .* disagrees with derived"):
        publish(stale, strict=False)


def test_institutes_csv_exports_the_same_bounds(published):
    rows = {row["id"]: row for row in build.tables(published)["institutes"]}
    for record in published["institutes"]:
        for key in KEYS:
            span = record["intervals"][key]
            for end in ("min", "max"):
                assert rows[record["id"]][f"{key}_days_{end}"] == ("" if span is None else span[f"days_{end}"])
    assert build.FIELD_DOCS["established_to_first_qualifying_activity_days_min"].endswith("see institutes[].intervals in the JSON schema.")
