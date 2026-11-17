"""A and D: mutation checks for measured panel and card geometry, without a browser."""

import copy
from pathlib import Path

import pytest

from scripts import check_page


# A. Long and short text have different ink extents, but the same available measure.
@pytest.fixture
def panel():
    return {"measure": 784, "left": 24, "right": 1000, "blocks": [
        {"name": "claim", "max_width": 784, "left": 24, "right": 808, "text_right": 799, "aligned": True},
        {"name": "quote-anchor", "max_width": 784, "left": 24, "right": 808, "text_right": 720, "aligned": True},
    ]}


def test_panel_measure_accepts_desktop_and_narrow_phone(panel):
    assert check_page.panel_measure_errors(panel) == []
    panel["right"] = 366
    for block in panel["blocks"]:
        block.update(right=366, text_right=360)
    assert check_page.panel_measure_errors(panel) == []


@pytest.mark.parametrize(("field", "value", "finding"), [
    ("max_width", None, "max-width"),
    ("max_width", 640, "max-width"),
    ("max_width", 900, "max-width"),
    ("right", 810, "past"),
    ("text_right", 810, "past"),
    ("left", 20, "outside"),
    ("left", 28, "left edge"),
])
def test_panel_measure_rejects_mismatched_or_overflowing_blocks(panel, field, value, finding):
    panel["blocks"][1][field] = value
    assert any(finding in e for e in check_page.panel_measure_errors(panel))


def test_panel_measure_cannot_pass_with_one_block(panel):
    panel["blocks"].pop()
    assert "fewer than two text blocks measured" in check_page.panel_measure_errors(panel)


# D. The head occupies its own column directly below two site bars. Reserved space is measured to the fields.
@pytest.fixture
def card_head():
    return {"position": "sticky", "top": 96, "bars": 96, "left": 24, "right": 366,
            "card_left": 24, "card_right": 366, "transparent": False, "rule": True,
            "z": 3, "bars_z": 4, "height": 43, "reserved": 43, "phone": True}


def test_card_head_accepts_compact_phone_head(card_head):
    assert check_page.card_head_errors(card_head) == []


@pytest.mark.parametrize(("field", "value", "finding"), [
    ("position", "static", "stick"),
    ("top", 98, "below"),
    ("left", 20, "column"),
    ("right", 370, "column"),
    ("transparent", True, "ground"),
    ("rule", False, "rule"),
    ("z", 5, "stacking"),
    ("reserved", 0, "reserved"),
    ("reserved", None, "reserved"),
    ("reserved", float("nan"), "finite"),
    ("reserved", float("inf"), "finite"),
    ("reserved", float("-inf"), "finite"),
    ("height", float("nan"), "finite"),
    ("height", float("inf"), "finite"),
    ("height", 49, "48px"),
])
def test_card_head_rejects_bad_sticking_or_clearance(card_head, field, value, finding):
    card_head[field] = value
    assert any(finding in e for e in check_page.card_head_errors(card_head))


def test_card_head_allows_extra_space_from_a_taller_peer(card_head):
    card_head["reserved"] = 90
    assert check_page.card_head_errors(card_head) == []


def test_wrapped_saved_head_needs_its_full_height_before_any_field():
    assert check_page.card_head_space_errors({"height": 80, "reserved": 96}) == []
    assert check_page.card_head_space_errors({"height": 80, "reserved": 43}) == ["reserved head track overlaps a field"]


@pytest.fixture
def saved_cards():
    # A fractional title track shared by unequal heads, with the first head already under the sticky bars.
    return [{"id": "c-a", "height": 43.125, "head_top": 42, "reserved": 49.25},
            {"id": "c-b", "height": 68.25, "head_top": 42, "reserved": 49.25}]


def test_card_hydration_preserves_fractional_tracks_and_stuck_heads(saved_cards):
    assert check_page.card_hydration_errors(saved_cards, copy.deepcopy(saved_cards)) == []


@pytest.mark.parametrize(("field", "delta"), [("height", 2 / 64), ("reserved", 2 / 64), ("head_top", 42)])
def test_card_hydration_rejects_rounding_and_enabling_sticky_late(saved_cards, field, delta):
    live = copy.deepcopy(saved_cards)
    live[0][field] += delta
    assert any(field + " changes" in e for e in check_page.card_hydration_errors(saved_cards, live))


@pytest.mark.parametrize("field", ["height", "head_top", "reserved"])
def test_card_hydration_allows_one_layout_unit(saved_cards, field):
    live = copy.deepcopy(saved_cards)
    live[0][field] += 1 / 64
    assert check_page.card_hydration_errors(saved_cards, live) == []


@pytest.mark.parametrize("field", ["height", "head_top", "reserved"])
@pytest.mark.parametrize("value", [None, float("nan"), float("inf"), float("-inf")])
@pytest.mark.parametrize("side", ["saved", "live"])
def test_card_hydration_rejects_nonfinite_geometry(saved_cards, field, value, side):
    live = copy.deepcopy(saved_cards)
    (saved_cards if side == "saved" else live)[0][field] = value
    assert any("not finite" in e for e in check_page.card_hydration_errors(saved_cards, live))


def test_card_hydration_requires_both_snapshots(saved_cards):
    assert check_page.card_hydration_errors([], []) == ["no visible saved cards measured"]
    assert check_page.card_hydration_errors(saved_cards, saved_cards[1:]) == ["c-a: card missing after hydration"]


def test_panel_and_card_jobs_are_registered():
    names = [check_page.job_id(*job) for job in check_page.browser_jobs(Path("dist"), {}, None)]
    for name in ("check_panel_measure", "check_card_heads", "check_saved_card_heads"):
        assert names.count("interactions/" + name) == 1
