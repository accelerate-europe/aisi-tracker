"""Running lines abbreviate a source (owner, 18 Nov 2026): the publisher without its bracketed parts or a "hosted by",
"published by" or "reproducing" tail, and a title over 80 characters cut at a word boundary with an ellipsis. A record's
source list and the hover keep the full form. One function each, in site/index.html."""

import json
import subprocess
from pathlib import Path

import pytest

PAGE = Path(__file__).resolve().parents[1] / "site" / "index.html"


def call(expression: str):
    lines = PAGE.read_text(encoding="utf-8").splitlines()
    source = "\n".join(next(line for line in lines if line.startswith(f"  function {name}(")) for name in ("shortPublisher", "shortTitle"))
    result = subprocess.run(["node", "-"], input=source + "\nconsole.log(JSON.stringify(" + expression + "));", text=True, capture_output=True, check=True)
    return json.loads(result.stdout)


@pytest.mark.parametrize("full, short", [
    ("Industrial Technology Research Institute (ITRI)", "Industrial Technology Research Institute"),
    ("AI Malaysia Berhad (National AI Office), Ministry of Digital", "AI Malaysia Berhad, Ministry of Digital"),
    ("National Technical Committee 260 on Cybersecurity (TC260), published by the Cyberspace Administration of China", "National Technical Committee 260 on Cybersecurity"),
    ("Ministry of Information, Communications and the Digital Economy, Kenya (copy hosted by Data4SDGs)", "Ministry of Information, Communications and the Digital Economy, Kenya"),
    ("Hungarian AI Council (file hosted by the Hungarian Intellectual Property Office)", "Hungarian AI Council"),
    ("Government of the Federation of Bosnia and Herzegovina, hosted by the Government of West Herzegovina Canton", "Government of the Federation of Bosnia and Herzegovina"),
    ("Some outlet reproducing a Reuters report", "Some outlet"),
    ("Department for Science, Innovation and Technology", "Department for Science, Innovation and Technology"),
    ("", ""),
])
def test_short_publisher(full, short):
    assert call(f"shortPublisher({json.dumps(full)})") == short


def test_short_title_cuts_at_a_word_boundary_with_an_ellipsis():
    short = "Budget proposition 1 S (2026-2027), Ministry of Digitalisation and Public Governance"
    assert call(f"shortTitle({json.dumps(short[:80])})") == short[:80]
    title = "Major countries step up AI risk verification; Korea has only 6 safety evaluation staff and more"
    cut = call(f"shortTitle({json.dumps(title)})")
    assert cut.endswith("…") and len(cut) <= 81 and title.startswith(cut[:-1]) and title[len(cut) - 1] == " "
    assert not cut[:-1].endswith((",", ";", ":", " "))
    assert call('shortTitle("' + "x" * 81 + '")').endswith("…")


def test_method_links_keep_the_full_form_as_their_accessible_name():
    """Method's inline links are a running line: abbreviated text, the full publisher and title as the accessible name."""
    page = PAGE.read_text(encoding="utf-8")
    lines = page.splitlines()
    helpers = "\n".join(next(line for line in lines if line.startswith(f"  function {name}(")) for name in ("shortPublisher", "shortTitle"))
    body = page[page.index("  function sourceLinks("):page.index("\n  }\n", page.index("  function sourceLinks(")) + 4]
    stub = """
function el(tag, cls, text) { return {tag, className: cls || '', attrs: {}, children: text == null ? [] : [{text}], title: '',
  setAttribute(k, v) { this.attrs[k] = v; }, appendChild(n) { this.children.push(n); return n; }}; }
const document = {createTextNode: text => ({text})};
function rereadTail() { return false; } function archiveCopy() {}
"""
    long_title = "A very long report title about the national approach to the evaluation of advanced artificial intelligence systems"
    S = {"a": {"publisher": "Ministry of Digital Affairs (MoDA)", "title": long_title, "url": "https://example.org/a"},
         "b": {"publisher": "Ministry of Digital Affairs (MoDA)", "title": "Short", "url": "https://example.org/b"}}
    script = stub + helpers + "\n" + body + f"\nconsole.log(JSON.stringify(sourceLinks(['a', 'b'], {json.dumps(S)})));"
    span = json.loads(subprocess.run(["node", "-"], input=script, text=True, capture_output=True, check=True).stdout)
    first, second = [c for c in span["children"] if c.get("tag") == "a"]
    assert first["children"][0]["text"] == "Ministry of Digital Affairs: " + long_title[:80].rsplit(" ", 1)[0] + "…"
    assert first["attrs"]["aria-label"] == "Ministry of Digital Affairs (MoDA): " + long_title
    assert second["attrs"]["aria-label"] == "Ministry of Digital Affairs (MoDA): Short"  # the publisher is abbreviated too
