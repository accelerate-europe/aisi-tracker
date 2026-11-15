"""The verifier's anchor checks (docs/design/claims-1.1.md, section 5), on fragments of real public texts: the
Commission decision establishing the AI Office (Articles 1 and 3) and Spain's IA360 plan (measure 4)."""

import json

import pytest

from scripts import check
from scripts import verify_excerpts as verify
from tests.test_verify_excerpts import cached, no_network  # noqa: F401 - the autouse fixture applies here too

EU = (
    "<html><body><h2>Article 1</h2><h3>Establishment</h3>"
    "<p>The European Artificial Intelligence Office (the ‘Office’) is established. The Office shall be part of the "
    "administrative structure of the Directorate-General for Communication Networks, Content and Technology.</p>"
    "<h2>Article 3</h2><h3>Implementation of the forthcoming Regulation</h3>"
    "<p>1. The Office shall perform the following tasks stemming from the forthcoming Regulation:</p>"
    "<p>(b) monitoring the implementation and application of rules on general-purpose AI models and systems;</p>"
    "<p>(c) monitoring the emergence of unforeseen risks stemming from general-purpose AI models, including by "
    "responding to alerts from the scientific panel;</p></body></html>"
).encode()
ITEM = "monitoring the emergence of unforeseen risks stemming from general-purpose AI models"
LEAD_IN = {"text": "The Office shall perform the following tasks stemming from the forthcoming Regulation", "kind": "heading"}
DEFINITION = {"text": "The European Artificial Intelligence Office (the ‘Office’) is established.", "kind": "definition"}
PROVISION = {"text": "Article 3 Implementation of the forthcoming Regulation", "kind": "provision"}
ES = (
    "<html><body><p>4. Instituto de Seguridad de la Inteligencia Artificial en la AESIA. España se dotará de un "
    "Instituto de Seguridad de la Inteligencia Artificial, una unidad técnico-científica para reforzar y concentrar "
    "las capacidades de análisis, evaluación y asesoramiento. Serán funciones del Instituto la evaluación de los "
    "modelos más avanzados antes y después de su despliegue; el desarrollo de metodología y herramientas de evaluación "
    "y de estándares para entornos de prueba seguros; el asesoramiento técnico a la Administración General del "
    "Estado.</p></body></html>"
).encode()


def run(tmp_path, body, excerpt, other=None):
    fetcher = verify.Fetcher(tmp_path, offline=True)
    source = {"id": "eu-decision", "url": "https://example.org/decision"}
    cached(fetcher, source["url"], body)
    if other is not None:
        source["archive_url"] = "https://web.archive.org/web/2026/https://example.org/decision"
        cached(fetcher, source["archive_url"], other, method="archive")
    return verify.verify_source(source, [(0, {"supports": ["/institutes/0/claims/0/text"], **excerpt})], fetcher)["excerpts"][0]


def test_both_links_of_the_eu_chain_are_found(tmp_path):
    row = run(tmp_path, EU, {"text": ITEM, "anchors": [LEAD_IN, DEFINITION]})
    assert row["status"] == "matched"
    assert [(a["kind"], a["status"]) for a in row["anchors"]] == [("heading", "matched"), ("definition", "matched")]
    lead_in = row["anchors"][0]
    assert 0 <= lead_in["distance"] < 200  # item (b) sits between the lead-in and item (c)


def test_a_provision_title_across_elements_precedes(tmp_path):
    """The article number and its title are separate elements; the span still matches across them."""
    row = run(tmp_path, EU, {"text": ITEM, "anchors": [PROVISION]})
    assert row["status"] == "matched" and row["anchors"][0]["distance"] > 0


def test_spanish_plan_lead_in_and_introducing_clause(tmp_path):
    item = "el desarrollo de metodología y herramientas de evaluación y de estándares para entornos de prueba seguros"
    anchors = [{"text": "Serán funciones del Instituto", "kind": "heading"},
               {"text": "España se dotará de un Instituto de Seguridad de la Inteligencia Artificial", "kind": "definition"}]
    assert run(tmp_path, ES, {"text": item, "anchors": anchors})["status"] == "matched"


def test_a_heading_after_the_quote_fails(tmp_path):
    later = {"text": "including by responding to alerts from the scientific panel", "kind": "heading"}
    row = run(tmp_path, EU, {"text": ITEM, "anchors": [later]})
    assert row["status"] == "anchor_not_before_quote" and row["anchors"][0]["distance"] < 0


@pytest.mark.parametrize("kind", ["definition", "membership"])
def test_a_definition_or_membership_may_follow_the_quote(tmp_path, kind):
    body = EU.replace(b"</body>", b"<p>In this Decision, the Office (the \xe2\x80\x98AI Office\xe2\x80\x99) means the Office.</p></body>")
    definition = {"text": "the Office (the ‘AI Office’) means the Office.", "kind": kind}
    assert run(tmp_path, body, {"text": ITEM, "anchors": [definition]})["status"] == "matched"


def test_a_missing_anchor_fails(tmp_path):
    row = run(tmp_path, EU, {"text": ITEM, "anchors": [{"text": "The AI Safety Institute will:", "kind": "heading"}]})
    assert row["status"] == "anchor_mismatch"


def test_an_anchor_from_another_copy_fails(tmp_path):
    """The quote is in the live copy; the lead-in only in the archived copy: not the same copy."""
    live = EU.replace(b"1. The Office shall perform the following tasks stemming from the forthcoming Regulation:", b"")
    row = run(tmp_path, live, {"text": ITEM, "anchors": [LEAD_IN]}, other=EU)
    assert row["evidence_origin"] == "live" and row["status"] == "anchor_other_copy"


def test_an_excerpt_without_anchors_is_unchanged(tmp_path):
    row = run(tmp_path, EU, {"text": ITEM})
    assert row["status"] == "matched" and "anchors" not in row


def test_anchors_are_not_checked_without_the_quote(tmp_path):
    row = run(tmp_path, EU, {"text": "a sentence the decision does not contain", "anchors": [LEAD_IN]})
    assert row["status"] == "mismatch" and row["anchors"][0]["status"] == "not_checked"


def test_report_lists_anchors_by_distance(tmp_path):
    row = run(tmp_path, EU, {"text": ITEM, "anchors": [LEAD_IN, DEFINITION]})
    data = tmp_path / "aisi.json"
    data.write_text("{}")
    verify.write_report(tmp_path / "report.json", [{"source_id": "eu-decision", "excerpts": [row], "attempts": []}], data)
    md = (tmp_path / "report.md").read_text()
    lines = [line for line in md.splitlines() if line.startswith("| eu-decision ")]
    assert "## Anchors" in md and len(lines) == 2
    assert "| definition |" in lines[0]  # farther from the quote, so first


def test_compare_reverifies_an_excerpt_whose_anchor_changed():
    old = {"sources": [{"id": "eu", "excerpts": [{"text": ITEM, "supports": []}]}]}
    new = {"sources": [{"id": "eu", "excerpts": [{"text": ITEM, "supports": [], "anchors": [LEAD_IN]}]}]}
    changed = check.excerpt_keys(new) - check.excerpt_keys(old)
    assert changed == {("eu", ITEM, "", check.anchor_key([LEAD_IN]))}
    rows = check.binding_changes(old, new)
    assert len(rows) == 1 and any("anchors" in g for g in rows[0]["gloss_after"])


def test_compare_keys_an_anchored_excerpt_by_its_copy():
    """One quotation in two copies: an anchor added to the second is checked in that copy, even when the first
    already has the same anchor."""
    archived = "https://web.archive.org/web/2026/https://example.org/decision"
    first = {"text": ITEM, "supports": [], "anchors": [LEAD_IN]}
    old = {"sources": [{"id": "eu", "excerpts": [first, {"text": ITEM, "supports": [], "url": archived}]}]}
    new = {"sources": [{"id": "eu", "excerpts": [first, {"text": ITEM, "supports": [], "url": archived, "anchors": [LEAD_IN]}]}]}
    changed = check.excerpt_keys(new) - check.excerpt_keys(old)
    assert changed == {("eu", ITEM, archived, check.anchor_key([LEAD_IN]))}
    # The verifier's row for that excerpt (declared URL, anchors with status and distance) has the same key.
    row = {"source_id": "eu", "text": ITEM, "declared_excerpt_url": archived,
           "anchors": [{"kind": "heading", "text": LEAD_IN["text"], "status": "matched", "distance": 3}]}
    assert (row["source_id"], row["text"], check.anchored_copy(row["declared_excerpt_url"], row["anchors"]),
            check.anchor_key(row["anchors"])) in changed
    # Without anchors a URL edit alone still does not re-verify.
    moved = {"sources": [{"id": "eu", "excerpts": [{"text": ITEM, "supports": [], "url": archived}]}]}
    assert check.excerpt_keys(moved) == check.excerpt_keys({"sources": [{"id": "eu", "excerpts": [{"text": ITEM, "supports": []}]}]})


def test_placement_takes_the_view_where_the_anchor_precedes():
    """A PDF wrap inside a CJK provision number: only the joined view finds the article title before the quote."""
    text = "第\n3条\nQuote text here\n第3条"
    assert verify.anchor_placement("第3条", "Quote text here", text) >= 0


@pytest.mark.parametrize("status, verdict", [("matched", "PASS"), ("anchor_mismatch", "FAIL"), ("anchor_not_before_quote", "FAIL"), ("not_cached", "NOT RUN")])
def test_compare_fails_on_an_anchor_failure(tmp_path, monkeypatch, status, verdict):
    report = tmp_path / "report"
    report.mkdir()
    rows = [{"source_id": "eu", "text": ITEM, "status": status, "anchors": [{"kind": "heading", "text": LEAD_IN["text"], "status": status}]}]
    (report / "excerpt-verification.json").write_text(json.dumps({"sources": [{"excerpts": rows}]}))

    class Ctx:
        cache = tmp_path
        snapshots = None
        work = tmp_path
    Ctx.report = report
    monkeypatch.setattr(check, "run", lambda ctx, step, cmd: (0, None, tmp_path / "log"))
    result, _ = check.verify_quotes(Ctx, {}, [("eu", ITEM, "", check.anchor_key([LEAD_IN]))])
    assert result == verdict


def cross_copy_row(tmp_path, named_body, *, body=EU, anchor=None, quote=ITEM):
    fetcher = verify.Fetcher(tmp_path / "cache", offline=True)
    source = {"id": "eu-decision", "url": "https://example.org/decision"}
    named = {"id": "unit-source", "url": "https://example.org/unit"}
    cached(fetcher, source["url"], body)
    if named_body is not None:
        cached(fetcher, named["url"], named_body)
    excerpt = {"text": quote, "supports": ["/institutes/0/claims/0/text"],
               "anchors": [anchor if anchor is not None else {**DEFINITION, "source_id": named["id"]}]}
    return verify.verify_source(source, [(0, excerpt)], fetcher, sources={source["id"]: source, named["id"]: named})["excerpts"][0]


@pytest.mark.parametrize("kind", ["definition", "membership"])
def test_other_copy_anchor_matches_without_placement_or_distance(tmp_path, kind):
    anchor = {**DEFINITION, "kind": kind, "source_id": "unit-source"}
    row = cross_copy_row(tmp_path, EU, body=EU.replace(DEFINITION["text"].encode(), b""), anchor=anchor)
    assert row["status"] == "matched"
    result, = row["anchors"]
    assert result["status"] == "matched" and result["source_id"] == "unit-source"
    assert "distance" not in result
    assert check.anchor_key(row["anchors"]) == check.anchor_key([anchor])


def test_other_copy_anchor_is_not_rescued_by_the_quotes_own_copy(tmp_path):
    row = cross_copy_row(tmp_path, EU.replace(DEFINITION["text"].encode(), b""))
    assert row["status"] == "anchor_mismatch"
    assert row["anchors"][0]["source_id"] == "unit-source"


def test_other_copy_anchor_unavailable_is_unverified_in_compare(tmp_path, monkeypatch):
    row = cross_copy_row(tmp_path, None)
    anchor, = row["anchors"]
    assert row["status"] == "not_cached"
    assert anchor["status"] == "not_checked" and anchor["source_id"] == "unit-source"
    assert "distance" not in anchor
    data = tmp_path / "aisi.json"
    data.write_text("{}")
    verify.write_report(tmp_path / "excerpt-verification.json", [{"source_id": "eu-decision", "excerpts": [row], "attempts": []}], data)

    class Ctx:
        cache = work = report = tmp_path
        snapshots = None

    monkeypatch.setattr(check, "run", lambda ctx, step, cmd: (0, None, tmp_path / "log"))
    result, _ = check.verify_quotes(Ctx, {}, [("eu-decision", ITEM, "", check.anchor_key(row["anchors"]))])
    assert result == "NOT RUN"


def test_unmatched_quote_keeps_other_copy_anchor_source_id(tmp_path):
    row = cross_copy_row(tmp_path, EU, quote="This quote is not in the decision.")
    assert row["status"] == "mismatch"
    assert row["anchors"] == [{**DEFINITION, "source_id": "unit-source", "status": "not_checked"}]


def test_same_copy_anchor_cannot_match_another_sources_copy(tmp_path):
    row = cross_copy_row(tmp_path, EU, body=EU.replace(DEFINITION["text"].encode(), b""), anchor=DEFINITION)
    assert row["status"] == "anchor_mismatch"


def test_other_copy_anchor_report_names_its_source(tmp_path):
    row = cross_copy_row(tmp_path, EU)
    data = tmp_path / "aisi.json"
    data.write_text("{}")
    verify.write_report(tmp_path / "report.json", [{"source_id": "eu-decision", "excerpts": [row], "attempts": []}], data)
    assert "| Anchor source |" in (tmp_path / "report.md").read_text()
    assert "| unit-source | definition | matched |  |" in (tmp_path / "report.md").read_text()
    saved = json.loads((tmp_path / "report.json").read_text())
    assert saved["sources"][0]["excerpts"][0]["anchors"][0]["source_id"] == "unit-source"


def test_source_id_changes_the_anchor_key_but_gloss_stays_a_binding_change():
    own = [DEFINITION]
    first = [{**DEFINITION, "source_id": "first"}]
    second = [{**DEFINITION, "source_id": "second"}]
    assert len({check.anchor_key(a) for a in (own, first, second)}) == 3
    old = {"sources": [{"id": "eu", "excerpts": [{"text": ITEM, "supports": [], "anchors": first}]}]}
    new = {"sources": [{"id": "eu", "excerpts": [{"text": ITEM, "supports": [], "anchors": second}]}]}
    assert check.excerpt_keys(new) - check.excerpt_keys(old)
    new["sources"][0]["excerpts"][0]["anchors"] = [{**first[0], "gloss": "An edited gloss."}]
    assert check.excerpt_keys(new) == check.excerpt_keys(old)
    assert check.binding_changes(old, new)


def test_other_copy_anchor_with_an_unknown_source_is_a_mismatch(tmp_path):
    row = cross_copy_row(tmp_path, EU, anchor={**DEFINITION, "source_id": "no-such-source"})
    assert row["status"] == "anchor_mismatch" and row["anchors"][0]["source_id"] == "no-such-source"
