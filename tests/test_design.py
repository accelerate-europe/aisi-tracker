"""Design contracts that remain usable when browser launch is blocked."""

import json
import re
import subprocess
import shutil
from pathlib import Path

import pytest

from scripts.check_page import check_meta_separators, check_style_source


PAGE = Path(__file__).resolve().parents[1] / "site/index.html"


def test_type_and_underline_tokens():
    assert check_style_source(PAGE.read_text()) == []


@pytest.mark.parametrize("role", ["title", "label", "body", "meta", "figure"])
def test_literal_leading_cannot_bypass_role_token(role):
    source = PAGE.read_text().replace(f"/var(--leading-{role})", "/2")
    assert f"{role} line height does not use its role token" in check_style_source(
        source
    )


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
    assert f"inline links do not use {token}" in check_style_source(source)


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
