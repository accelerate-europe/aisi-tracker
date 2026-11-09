"""Design contracts that remain usable when browser launch is blocked."""

import json
import re
import subprocess
import shutil
from pathlib import Path

import pytest

from scripts.check_page import PAINT_JS, check_meta_separators, check_style_source, check_text_contrast, report_line


PAGE = Path(__file__).resolve().parents[1] / "site/index.html"


def test_type_and_underline_tokens():
    assert check_style_source(PAGE.read_text()) == []


@pytest.mark.parametrize("role", ["title", "label", "body", "meta", "figure"])
def test_literal_leading_cannot_bypass_role_token(role):
    source = PAGE.read_text().replace(f"/var(--leading-{role})", "/2")
    assert report_line(f"{role} line height does not use its role token") in check_style_source(source)  # cosmetic: D1


@pytest.mark.parametrize(
    ("prop", "token", "value"),
    [
        ("text-decoration-thickness", "--underline-width", "3px"),
        ("text-underline-offset", "--underline-offset", "3px"),
        ("text-decoration-color", "--underline-colour", "red"),
    ],
)
def test_anchor_underline_cannot_bypass_tokens(prop, token, value):
    # Leave the token in other rules; only the inline anchor rule counts.
    source = PAGE.read_text().replace(f"{prop}: var({token})", f"{prop}: {value}", 1)
    assert report_line(f"inline links do not use {token}") in check_style_source(source)


def test_page_and_separator_gate_javascript_parse_without_a_browser():
    class Capture:
        def evaluate(self, source, *args):
            self.source = source
            return []

    assert shutil.which("node"), "Node is required for JavaScript parse checks (Chrome is not used)."
    page = Capture()
    check_meta_separators(page)
    script = PAGE.read_text().split("<script>", 1)[1].split("</script>", 1)[0]
    # Parse only: no DOM, network, browser or layout engine is invoked. The source goes on stdin:
    # Linux caps a single argument at 128 KiB, which the page script exceeds.
    result = subprocess.run(
        ["node", "-"],
        input="new Function("
        + json.dumps(script)
        + "); new Function('return (' + "
        + json.dumps(page.source)
        + " + ')');",
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr


def test_text_contrast_javascript_parses_without_a_browser():
    class Capture:
        def evaluate(self, source, *args):
            self.source = source
            return []

    page = Capture()
    assert check_text_contrast(page) == []
    result = subprocess.run(["node", "-"], input="new Function('return (' + " + json.dumps(page.source) + " + ')');", capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_method_dates_preserve_day_and_month_precision_without_breaking():
    source = re.search(r"  function nonbreakingDates\(s\) .*", PAGE.read_text()).group()
    cases = [
        (
            "Checked 19 October 2026 and Oct 2026.",
            "Checked 19\u00a0October\u00a02026 and Oct\u00a02026.",
        ),
        ("September 2026; 2 Sept 2026", "September\u00a02026; 2\u00a0Sept\u00a02026"),
        (
            "May improve; 2026; October policy; Januaryish 2026",
            "May improve; 2026; October policy; Januaryish 2026",
        ),
    ]
    result = subprocess.run(
        [
            "node",
            "-e",
            source
            + "; console.log(JSON.stringify("
            + json.dumps([text for text, _ in cases])
            + ".map(nonbreakingDates)));",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert json.loads(result.stdout) == [expected for _, expected in cases]


def paint_js(call: str):
    """Run one of PAINT_JS's pure functions in Node and return its JSON result."""
    result = subprocess.run(["node", "-"], input=PAINT_JS + f"process.stdout.write(JSON.stringify({call}));", capture_output=True, text=True, check=True)
    return json.loads(result.stdout)


def painted(colour, layers):
    """The screen colour of colour inside layers (innermost first)."""
    return paint_js(f"painted({json.dumps(colour)}, {json.dumps(layers)})")


def contrast(a, b):
    lum = lambda c: sum(w * ((v / 255) / 12.92 if v / 255 <= 0.03928 else ((v / 255 + 0.055) / 1.055) ** 2.4) for w, v in zip((0.2126, 0.7152, 0.0722), c))  # noqa: E731
    x, y = sorted((lum(a), lum(b)), reverse=True)
    return (x + 0.05) / (y + 0.05)


def test_text_contrast_composites_opacity_groups_as_the_browser_paints():
    # White text on a black box at opacity .5 over a white page: white on mid grey, below 4.5:1. Fading only the text
    # (and not the box behind it) would have measured about 5.3:1 and passed.
    box = [[[0, 0, 0, 1], 0.5], [[0, 0, 0, 0], 1]]
    ink, ground = painted([255, 255, 255, 1], box), painted([0, 0, 0, 0], box)
    assert ink == [255, 255, 255] and [round(v) for v in ground] == [128, 128, 128]
    assert 3.9 < contrast(ink, ground) < 4.0
    # A translucent layer over an opaque one: the nearer layer is painted last.
    layers = [[[255, 0, 0, 0.5], 1], [[0, 0, 255, 1], 1]]
    assert [round(v) for v in painted([0, 0, 0, 0], layers)] == [128, 0, 128]
    # Text at half alpha on white: halfway to white.
    assert [round(v) for v in painted([0, 0, 0, 0.5], [[[255, 255, 255, 1], 1]])] == [128, 128, 128]


def test_a_background_image_counts_where_it_shows_through():
    black, white, clear = [0, 0, 0, 1], [255, 255, 255, 1], [0, 0, 0, 0]
    assert paint_js(f"imageShows({json.dumps([[black, 1, False], [white, 1, True]])})") is False  # hidden behind an opaque box
    assert paint_js(f"imageShows({json.dumps([[black, 0.5, False], [white, 1, True]])})") is True  # the box is faded: the image shows
    assert paint_js(f"imageShows({json.dumps([[clear, 1, False], [white, 1, True]])})") is True
    assert paint_js(f"imageShows({json.dumps([[clear, 1, True]])})") is True


@pytest.mark.parametrize("args, hides", [
    (["inset(50%)", "auto", "static", 200, 40, "visible", "visible"], True),
    (["none", "rect(0px, 0px, 0px, 0px)", "absolute", 200, 40, "visible", "visible"], True),
    (["none", "auto", "absolute", 1, 1, "hidden", "hidden"], True),  # the usual visually-hidden box
    (["none", "auto", "static", 200, 1, "visible", "hidden"], True),  # 1px tall, clipped vertically
    (["inset(50% 0px 0px)", "auto", "static", 200, 40, "visible", "visible"], False),  # the bottom half shows
    (["none", "rect(0px, 200px, 40px, 0px)", "absolute", 200, 40, "visible", "visible"], False),  # the whole box shows
    (["none", "auto", "static", 1, 40, "visible", "visible"], False),  # overflow shows
    (["none", "auto", "static", 200, 1, "clip", "visible"], False),  # 1px tall but clipped only sideways
    (["none", "auto", "static", 1, 200, "visible", "clip"], False),  # 1px wide but clipped only vertically
])
def test_only_text_clipped_to_nothing_is_skipped(args, hides):
    assert paint_js(f"clipHides(...{json.dumps(args)})") is hides
