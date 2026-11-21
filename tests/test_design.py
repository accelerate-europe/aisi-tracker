"""Design contracts that remain usable when browser launch is blocked."""

import json
import re
import subprocess
import shutil
from pathlib import Path

import pytest



PAGE = Path(__file__).resolve().parents[1] / "site/index.html"


def test_page_javascript_parses_without_a_browser():
    assert shutil.which("node"), "Node is required for JavaScript parse checks (Chrome is not used)."
    script = PAGE.read_text().split("<script>", 1)[1].split("</script>", 1)[0]
    # Parse only: no DOM, network, browser or layout engine is invoked. The source goes on stdin:
    # Linux caps a single argument at 128 KiB, which the page script exceeds.
    result = subprocess.run(
        ["node", "-"],
        input="new Function("
        + json.dumps(script)
        + ");",
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


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_finding_text_strips_the_dated_and_the_legacy_opener():
    """findingText drops the status-in-words opener, with or without the generated scope (claims-1.1, pattern 1)."""
    source = re.search(r"  function findingText\(j\) .*", PAGE.read_text()).group()
    cases = [
        ("No qualifying institute identified as of October 2026. Closest body: Centre.", "Closest body: Centre."),
        ("No qualifying institute identified as of October 2026.", "No qualifying institute identified as of October 2026."),
        # Legacy openers, unchanged.
        ("No qualifying institute identified. NASK describes research.", "NASK describes research."),
        ("No qualifying national government designation or commitment confirmed; the bill is pending.", "The bill is pending."),
        ("No qualifying institute identified.", "No qualifying institute identified."),
        # Near misses keep the whole text.
        ("No qualifying designation confirmed as of October 2026. Details follow.", "No qualifying designation confirmed as of October 2026. Details follow."),
        ("No qualifying institute identified as of Spring 2026. Details follow.", "No qualifying institute identified as of Spring 2026. Details follow."),
        ("No qualifying institute identified as of October 2026; details follow.", "No qualifying institute identified as of October 2026; details follow."),
    ]
    script = source + "; console.log(JSON.stringify(" + json.dumps([text for text, _ in cases]) + ".map(function (s) { return findingText({status: 'none_identified', summary: s}); }).concat([findingText({status: 'planned', summary: 'No qualifying institute identified as of October 2026. X.'})])));"
    result = subprocess.run(["node", "-e", script], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == [expected for _, expected in cases] + ["No qualifying institute identified as of October 2026. X."]
