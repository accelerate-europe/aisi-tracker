"""Hand-worked contracts for method identity, reader-copy drift and full-rule rendering."""

import copy
import hashlib
import json
import subprocess
import sys
from pathlib import Path

from bs4 import BeautifulSoup
import pytest

from scripts.method_copy import (
    check_method_copy,
    fingerprint,
    full_method_page,
    refresh,
    rule_label,
)
from scripts.release_checks import check_launch_wording

ROOT = Path(__file__).resolve().parents[1]


def fixture():
    return {
        "license": "CC BY 4.0",
        "license_note": (
            "Quotations from sources remain with their rights holders and are reproduced "
            "as quotations; the licence covers our own compilation, text and data."
        ),
        "method": [
            {"id": "first", "text": "A rule."},
            {"id": "internal", "text": "Internal rule."},
        ],
        "method_summaries": [
            {
                "id": "brief",
                "title": "Brief",
                "summary": "A summary.",
                "rules": {"first": hashlib.sha256(b"A rule.").hexdigest()},
            }
        ],
        "method_no_reader_summary": ["internal"],
        "method_summary_status": "Owner copy edit pending.",
    }


def test_normalisation_is_documented_and_case_sensitive():
    assert fingerprint("  Ａ\n rule.  ") == hashlib.sha256(b"A rule.").hexdigest()
    assert fingerprint("a rule.") != fingerprint("A rule.")


def test_mutation_names_rule_and_summary_and_refresh_is_explicit():
    data = fixture()
    assert check_method_copy(data) == []
    data["method"][0]["text"] = "Changed rule."
    assert check_method_copy(data) == [
        "method rule first: summary brief fingerprint is stale; reread the summary, then run scripts/method_copy.py refresh brief"
    ]
    refresh(data, "brief")
    assert check_method_copy(data) == []
    assert data["method_summary_status"] == "Owner copy edit pending."


def test_refresh_command_changes_only_selected_fingerprints(tmp_path):
    data = fixture()
    other = copy.deepcopy(data["method_summaries"][0])
    other["id"] = "second"
    data["method_summaries"].append(other)
    data["method"][0]["text"] = "Changed rule."
    path = tmp_path / "data.json"
    path.write_text(json.dumps({"dataset": data}))
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/method_copy.py"),
            "refresh",
            "brief",
            "--data",
            str(path),
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    got = json.loads(path.read_text())["dataset"]
    assert got["method_summaries"][1] == other
    assert len(check_method_copy(got)) == 1
    assert "summary second" in check_method_copy(got)[0]


def test_unknown_duplicate_and_uncovered_rules_fail():
    data = fixture()
    data["method_no_reader_summary"] = ["unknown", "first"]
    data["method"].append(copy.deepcopy(data["method"][0]))
    data["method_summaries"].append(copy.deepcopy(data["method_summaries"][0]))
    errors = check_method_copy(data)
    assert "dataset.method: duplicate rule id" in errors
    assert "method summary brief: duplicate summary id" in errors
    assert "method rule unknown: unknown no-reader-summary id" in errors
    assert (
        "method rule first: both summarised and listed without reader summary" in errors
    )
    assert (
        "method rule internal: needs a reader summary or explicit no-reader-summary entry"
        in errors
    )
    data["method_summaries"][0]["rules"] = {"missing": "0" * 64}
    assert "method summary brief: unknown rule missing" in check_method_copy(data)


def test_full_method_preserves_every_rule_and_escapes_text():
    data = fixture()
    data["method"][0]["text"] = '<script>bad</script> & "words"'
    soup = BeautifulSoup(
        full_method_page(data, "<style>body { color: red; }</style>"), "html.parser"
    )
    assert soup.script is None
    for rule in data["method"]:
        assert soup.select_one(f"#{rule['id']} p").get_text() == rule["text"]
    assert len(soup.select("main section")) == 2
    assert soup.style is not None


def test_full_method_footer_has_licence_notice_and_back_link():
    data = fixture()
    soup = BeautifulSoup(full_method_page(data, ""), "html.parser")
    footer = soup.select_one("footer #foot")
    assert "Code: MIT; data, page text and documentation: CC BY 4.0" in footer.get_text()
    assert [p.get_text() for p in footer.select("p")][1] == data["license_note"]
    assert footer.a["href"] == "index.html#method-sec"
    assert footer.a.get_text() == "Back to the tracker"
    data["license_note"] = '<script>bad</script> & "rights"'
    soup = BeautifulSoup(full_method_page(data, ""), "html.parser")
    assert soup.script is None
    assert soup.select("footer p")[1].get_text() == data["license_note"]


@pytest.mark.parametrize(
    ("old", "new", "message"),
    [
        (
            "Code: MIT; data, page text and documentation: CC BY 4.0",
            "Code and data: MIT",
            "method.html: footer: missing code/content licence split",
        ),
        (
            "Quotations from sources remain with their rights holders and are reproduced "
            "as quotations; the licence covers our own compilation, text and data.",
            "",
            "method.html: footer: missing quotations licence notice",
        ),
        (
            'href="index.html#method-sec"',
            'href="index.html#missing"',
            "method.html: missing footer back link",
        ),
    ],
)
def test_method_footer_launch_asset_mutations(tmp_path, old, new, message):
    from scripts.check_page import check_launch_assets

    for name in ("index.html", "404.html", "favicon.svg"):
        (tmp_path / name).write_bytes((ROOT / "site" / name).read_bytes())
    for name in ("LICENSE", "LICENSE-DATA"):
        (tmp_path / name).write_bytes((ROOT / name).read_bytes())
    path = tmp_path / "method.html"
    original = full_method_page(fixture(), "")
    path.write_text(original, encoding="utf-8")
    assert check_launch_assets(tmp_path) == []
    assert old in original
    path.write_text(original.replace(old, new), encoding="utf-8")
    assert check_launch_assets(tmp_path) == [message]
    path.write_text(original, encoding="utf-8")
    assert check_launch_assets(tmp_path) == []


def test_real_rules_covered_and_row_summaries_are_reader_copy():
    data = json.loads((ROOT / "data/aisi.json").read_text())["dataset"]
    assert check_method_copy(data) == []
    rules = {r["id"]: r["text"] for r in data["method"]}
    summaries = {r["id"]: r["summary"] for r in data["method_summaries"]}
    for rid in ("leadership", "reporting-line", "national-partners"):
        assert summaries[rid] != rules[rid]
        assert "Owner copy edit pending" not in summaries[rid] + rules[rid]


def test_pending_summaries_block_release_even_without_pending_rules(dataset, build_env):
    dataset["dataset"]["method_summary_status"] = "Owner copy edit pending."
    schema = json.loads((ROOT / "schema/aisi.schema.json").read_text())
    errors = check_launch_wording(dataset, schema, build_env, "Release ready")
    assert (
        "dataset.method_summary_status: remove draft wording before release" in errors
    )


def test_rule_heading_uses_short_lead_in_or_falls_back_to_id():
    assert rule_label({"id": "freshness", "text": "Freshness: the check date."}) == "Freshness"
    assert rule_label({"id": "qualifying-body", "text": "Qualifying body. A government act."}) == "Qualifying body"
    assert rule_label({"id": "status-confidence", "text": "Status confidence measures evidence, not disagreement: high."}) == "Status confidence"


def test_gate_checks_rendered_summaries_links_and_full_rules(tmp_path):
    from scripts.check_page import check_method_pages

    data = {"dataset": fixture()}
    page = (
        '<details id="method"><ul id="rules"><li id="method-summary-brief"><span class="method-copy">A summary.</span> '
        '<a href="method.html#first">Full rule</a></li></ul><a href="method.html">Full method</a></details>'
        '<footer><a href="method.html">Full method</a></footer>'
    )
    (tmp_path / "method.html").write_text(full_method_page(data["dataset"], ""), encoding="utf-8")
    assert check_method_pages(page, data, tmp_path) == []
    assert check_method_pages(page.replace("A summary.", "Other words."), data, tmp_path) == ["Method summary brief: reader copy differs"]
    assert check_method_pages(page.replace(">Full rule<", ">Internal rule<"), data, tmp_path) == ["Method summary brief: full-rule links differ"]
    assert check_method_pages(page.replace("<footer><a", "<footer><span"), data, tmp_path) == ["Full method: missing link in footer"]
    (tmp_path / "method.html").write_text(full_method_page(data["dataset"], "").replace("Internal rule.", "Internal."), encoding="utf-8")
    assert check_method_pages(page, data, tmp_path) == ["Full method rule internal: text differs from the full rule"]
    (tmp_path / "method.html").unlink()
    assert check_method_pages(page, data, tmp_path) == ["Full method: method.html is missing"]


def test_gate_names_each_link_when_a_summary_covers_several_rules(tmp_path):
    from scripts.check_page import check_method_pages

    data = {"dataset": fixture()}
    data["dataset"]["method"][0]["text"] = "First rule: a rule."
    data["dataset"]["method_summaries"][0]["rules"]["internal"] = fingerprint("Internal rule.")
    page = (
        '<details id="method"><ul id="rules"><li id="method-summary-brief"><span class="method-copy">A summary.</span> Full rules on '
        '<a href="method.html#first">First rule</a> and <a href="method.html#internal">Internal</a></li></ul><a href="method.html">Full method</a></details>'
        '<footer><a href="method.html">Full method</a></footer>'
    )
    (tmp_path / "method.html").write_text(full_method_page(data["dataset"], ""), encoding="utf-8")
    assert check_method_pages(page, data, tmp_path) == []
    assert check_method_pages(page.replace(">First rule<", ">Full rule<"), data, tmp_path) == ["Method summary brief: full-rule links differ"]
    assert check_method_pages(page.replace("Full rules on ", ""), data, tmp_path) == ["Method summary brief: several rules without 'Full rules on'"]


def test_gate_requires_the_median_exception_wherever_tilde_is_explained(tmp_path):
    from scripts.check_page import MEDIAN_EXCEPTION, READER_MEDIAN_EXCEPTION, check_method_pages

    data = {"dataset": fixture()}
    data["dataset"]["method"][1]["text"] = f"Rounding: ~ marks a rounded figure, {MEDIAN_EXCEPTION}."
    page = (
        '<details id="method"><ul id="rules"><li id="method-summary-brief"><span class="method-copy">A summary.</span> '
        '<a href="method.html#first">Full rule</a></li></ul><a href="method.html">Full method</a></details>'
        '<footer><a href="method.html">Full method</a></footer>'
    )
    (tmp_path / "method.html").write_text(full_method_page(data["dataset"], ""), encoding="utf-8")
    assert check_method_pages(page, data, tmp_path) == []
    data["dataset"]["method"][1]["text"] = "Rounding: ~ marks a rounded figure."
    (tmp_path / "method.html").write_text(full_method_page(data["dataset"], ""), encoding="utf-8")
    assert check_method_pages(page, data, tmp_path) == [f"Full method rule internal: says what ~ marks without '{MEDIAN_EXCEPTION}'"]
    data["dataset"]["method_summaries"][0]["summary"] = "~ marks rounded figures."
    assert f"Method summary brief: says what ~ marks without '{READER_MEDIAN_EXCEPTION}'" in check_method_pages(page.replace("A summary.", "~ marks rounded figures."), data, tmp_path)
    # Reader summaries name the medians' place on the page, never the internal "counts row".
    data["dataset"]["method_summaries"][0]["summary"] = f"~ marks rounded figures, {MEDIAN_EXCEPTION}."
    assert "Method summary brief: names the internal 'counts row'" in check_method_pages(page, data, tmp_path)
    data["dataset"]["method_summaries"][0]["summary"] = f"~ marks rounded figures, {READER_MEDIAN_EXCEPTION}."
    assert not [e for e in check_method_pages(page, data, tmp_path) if e.startswith("Method summary brief: says what ~ marks")]
