"""Browser-free checks for sweep capture geometry and report failures."""

import pytest

from scripts import sweep_page


@pytest.mark.parametrize("height, heights", [(1, [1]), (1600, [1600]), (1601, [1600, 1]), (3200, [1600, 1600])])
def test_slice_clips_cover_the_region_without_gaps(height, heights):
    clips = sweep_page.slice_clips({"x": 12, "y": 2000, "width": 366, "height": height})
    assert [c["height"] for c in clips] == heights
    assert all(c["x"] == 12 and c["width"] == 366 for c in clips)
    assert clips[0]["y"] == 2000
    assert all(a["y"] + a["height"] == b["y"] for a, b in zip(clips, clips[1:]))
    assert clips[-1]["y"] + clips[-1]["height"] == 2000 + height


def test_slice_clips_round_outwards_without_exceeding_the_height_limit():
    assert sweep_page.slice_clips({"x": 12.5, "y": 2000.25, "width": 365.25, "height": 1600}) == [
        {"x": 12, "y": 2000, "width": 366, "height": 1600},
        {"x": 12, "y": 3600, "width": 366, "height": 1},
    ]


@pytest.mark.parametrize("width, height", [(0, 100), (100, 0), (-1, 100), (100, -1)])
def test_empty_regions_cannot_count_as_captured(width, height):
    with pytest.raises(ValueError, match="empty capture region"):
        sweep_page.slice_clips({"x": 0, "y": 0, "width": width, "height": height})


@pytest.mark.parametrize("change", [
    {"errors": ["console error"]}, {"sw": 391}, {"collapsed": ["function-label"]},
    {"filtered": True}, {"fn_errors": ["institute: row did not open"]},
    {"fn_errors": ["institute: empty capture region"]}, {"fn_errors": ["grid: grid did not open"]},
    {"fund_errors": ["budget-institute: row did not open"]},
])
def test_extra_capture_failures_and_existing_checks_fail_the_view(change):
    info = {"errors": [], "sw": 390, "cw": 390, "collapsed": [], "filtered": False, "fn_errors": [], "fund_errors": []}
    assert not sweep_page.view_failed(info)
    assert sweep_page.view_failed({**info, **change})
