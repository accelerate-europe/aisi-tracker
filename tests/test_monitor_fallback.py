"""Fallback compare (headless, jina, capture): layout differences are not losses, a render that lacks a quotation's
region is not comparable for it, and a changed quotation is still lost.

Each case reproduces, in a few lines, a false alarm of the first unattended weekly run (16 November 2026): the cited copy's text
around the quotation, and the fallback's reading of the same passage.
"""

import hashlib

import pytest

from scripts import monitor_sources as monitor

URL = "https://example.gov/page"
FILLER = "This page also describes other work of the agency in general terms. " * 5
KEPT = "describes other work of the agency"  # a quotation every fixture keeps, so the copy is comparable


def fallback(tmp_path, cited: str, new: str, quotes: list[str], route="jina", cited_mime="text/plain", new_mime="text/plain"):
    path = tmp_path / "cited"
    body = cited.encode()
    path.write_bytes(body)
    source = {
        "id": "src", "url": URL, "snapshot": {"sha256": hashlib.sha256(body).hexdigest()},
        "excerpts": [{"text": q, "supports": [["institutes", {"id": "xx"}, "name"]]} for q in quotes],
    }
    manifest = {"method": "live", "requested_url": URL, "mime": cited_mime}
    return monitor.compare_copy(source, (manifest, path), new.encode(), new_mime, {"route": route, "final_url": URL, "http": 200})


def states(row):
    return [(q["in_checked_copy"], q.get("comparable", True)) for q in row["quotes"]]


# (case, cited text, fallback text, quotation): the quotation is in the fallback, laid out differently.
LAYOUT = [
    ("es-plan: space before a hyphen",
     "Plan IA360. España se dotará de un Instituto, una unidad técnico-científica para reforzar las capacidades de análisis.",
     "Plan IA360. España se dotará de un Instituto, una unidad técnico -científica para reforzar las capacidades de análisis.",
     "una unidad técnico-científica para reforzar las capacidades de análisis."),
    ("ke-draft-policy-2026: table cells read in another order",
     "Responsibilities Responsibilities\nEstablish Kenya AI Safety Institute\nInstitute established; annual budget allocated 0–24 months 2,000\n"
     "Exchequer; Development Partners\nLEAD Support\nState Department responsible for Science, Research, Technology and Innovation\n"
     "MICDE, NACOSTI, universities\nDevelop national AI testing protocols",
     "Funding Responsibilities Responsibilities\n\nSources  LEAD  Support \n\nEstablish Kenya AI Safety Institute \n\nInstitute established; annual budget allocated  0–24 months  2,000 \n\nExchequer; Development Partners \n\nState Department responsible for Science, Research, Technology and Innovation \n\nMICDE, NACOSTI, universities \n\nDevelop national AI testing protocols",
     "LEAD Support State Department responsible for Science, Research, Technology and Innovation MICDE, NACOSTI, universities"),
    ("kr-msit-2024-11-pdf: Korean spacing split differently",
     "인공지능 안전연구소는 다양한 인공지능 위험에 체계적‧전문적\n으로 대응하기 위한 인공지능 안전연구 전담조직이다. (일시) ’24.11.27.(수), 09:30~10:30 / AI안전연구소(판교 글로벌R&D센터)",
     "인공지능 안전연구소는 다양한 인공지능 위험에 체계적 ‧전문적 으로 대응하기 위한 인공지능 안전연구 전담조직이다. (일시 ) ’24.11.27.( 수), 09:30~10:30 / AI 안전연구소 (판교 글로벌 R&D 센터 )",
     "다양한 인공지능 위험에 체계적‧전문적\n으로 대응하기 위한 인공지능 안전연구 전담조직이다."),
    ("kr-msit-2024-11-pdf: spaced brackets and units",
     "인공지능 안전연구소는 전담조직이다. (일시) ’24.11.27.(수), 09:30~10:30 / AI안전연구소(판교 글로벌R&D센터)",
     "인공지능 안전연구소는 전담조직이다. (일시 ) ’24.11.27.( 수), 09:30~10:30 / AI 안전연구소 (판교 글로벌 R&D 센터 )",
     "’24.11.27.(수), 09:30~10:30 / AI안전연구소(판교 글로벌R&D센터)"),
    ("sg-imda: a space inside a bracket in the cited copy",
     "With an initial funding of S$50million, IMDA had appointed Nanyang Technological University ( NTU) to establish the DTC.",
     "With an initial funding of S$50million, IMDA had appointed Nanyang Technological University (NTU) to establish the DTC.",
     "Nanyang Technological University ( NTU) to establish the DTC."),
    ("si-ai-act-ziudhpui-2025: no space after a full stop",
     "7. člen\n(enotna kontaktna točka)\nEnotna kontaktna točka iz drugega odstavka 70. člena Uredbe 2024/1689/EU je agencija.\n8. člen",
     "7.člen\n\n(enotna kontaktna točka)\n\nEnotna kontaktna točka iz drugega odstavka 70.člena Uredbe 2024/1689/EU je agencija.\n\n8.člen",
     "Enotna kontaktna točka iz drugega odstavka 70. člena Uredbe 2024/1689/EU je agencija."),
    ("list items with bullet markers (defect 4)",
     "The institute has 3 goals:\nAnalyse and test new AI models.\nSupport regulators and agencies.\nShape safe AI development.",
     "The institute has 3 goals:\n- Analyse and test new AI models.\n- Support regulators and agencies.\n- Shape safe AI development.",
     "The institute has 3 goals: Analyse and test new AI models. Support regulators and agencies."),
]


@pytest.mark.parametrize("case,cited,new,quote", LAYOUT, ids=[c[0].split(":")[0] for c in LAYOUT])
def test_layout_difference_is_not_a_loss(tmp_path, case, cited, new, quote):
    row = fallback(tmp_path, cited + "\n" + FILLER, new + "\n" + FILLER, [quote])
    assert row["quotes"][0]["in_cited_copy"]
    assert states(row) == [(True, True)], case
    assert (row["class"], row["reason"]) == ("trivial", "quotes_kept")


def test_layout_difference_is_still_strict_on_the_direct_route(tmp_path):
    case, cited, new, quote = LAYOUT[0]
    row = fallback(tmp_path, cited + "\n" + FILLER, new + "\n" + FILLER, [quote], route="direct")
    assert states(row) == [(False, True)]
    assert (row["class"], row["reason"]) == ("substantive", "quote_lost")


# (case, cited text, fallback text, quotations): the fallback lacks the region of every missing quotation.
PARTIAL = [
    ("fr-inesia guidance: a body-only render drops the page header",
     "What should evaluators prioritise\n08 juillet 2026\nINESIA - the French AISI - has published a blog post on evaluators' priorities.\n"
     "Evaluations are costly. Teams must choose which risks to measure first and how deeply.",
     "Title: What should evaluators prioritise\n\nEvaluations are costly. Teams must choose which risks to measure first and how deeply.",
     ["08 juillet 2026", "INESIA - the French AISI - has published a blog post on evaluators' priorities.", "Teams must choose which risks to measure first"]),
    ("sd-suna-allafrica-2025: a paywall cuts the article after its lead",
     "Khartoum - The council approved three new bodies on Wednesday.\nThe minister said the reform follows a year of work.\n"
     "Les nouveaux organismes sont l'Autorité de la transformation numérique et l'Autorité soudanaise de la cybersécurité.\nThe decree takes effect at once.",
     "Khartoum - The council approved three new bodies on Wednesday.\nSubscribe to read the full article. AllAfrica publishes around 400 reports a day.",
     ["The council approved three new bodies on Wednesday.", "Les nouveaux organismes sont l'Autorité de la transformation numérique et l'Autorité soudanaise de la cybersécurité."]),
]


@pytest.mark.parametrize("case,cited,new,quotes", PARTIAL, ids=[c[0].split(":")[0] for c in PARTIAL])
def test_render_without_the_region_is_not_comparable(tmp_path, case, cited, new, quotes):
    row = fallback(tmp_path, cited + "\n" + FILLER, new + "\n" + FILLER, quotes)
    kept = [q for q in row["quotes"] if q["in_checked_copy"]]
    assert kept and all(q.get("comparable", True) for q in kept), case
    assert all(q.get("comparable") is False for q in row["quotes"] if not q["in_checked_copy"]), case
    assert (row["class"], row["reason"]) == ("failed", "not_comparable")
    assert monitor.lost_quotes(row) == set()


def test_changed_quotation_inside_a_rendered_region_is_lost(tmp_path):
    """au-industry: the second goal's wording changed between items that did not; the loss stands."""
    cited = "The institute has 3 goals:\nAnalyse and test new AI models and applications.\nSupport regulators and agencies in responding to emerging AI risks and harms.\nShape safe AI development."
    new = "The institute has 3 goals:\n- Analyse and test new AI models and applications.\n- Support regulators and agencies in responding to emerging AI capabilities, risks and harms.\n- Shape safe AI development."
    quote = "Support regulators and agencies in responding to emerging AI risks and harms."
    row = fallback(tmp_path, cited + "\n" + FILLER, new + "\n" + FILLER, [quote, "Analyse and test new AI models and applications."])
    assert states(row) == [(False, True), (True, True)]
    assert (row["class"], row["reason"]) == ("failed", "fallback_quote_unconfirmed")


@pytest.mark.parametrize("quote,new", [
    ("founded in 2024", "The institute was founded in 20245 with a budget of 10 million euro."),
    ("10 million euro", "The institute was founded in 2024 with a budget of 110 million euro."),
])
def test_folded_match_never_cuts_a_number(tmp_path, quote, new):
    cited = "The institute was founded in 2024 with a budget of 10 million euro."
    row = fallback(tmp_path, cited + "\n" + FILLER, new + "\n" + FILLER, [quote, KEPT])
    assert states(row) == [(False, True), (True, True)]
    assert row["reason"] == "fallback_quote_unconfirmed"


def test_a_change_next_to_the_quotation_is_not_comparable_on_a_fallback(tmp_path):
    """The region is the cited text on both sides of the quotation; when that changed too, a fallback cannot tell a
    rewrite from a render that lacks the passage, so the quotation is not compared (the direct route still is)."""
    cited = "The institute was founded in 2024 with a budget of 10 million euro."
    new = "The institute was founded in 2025 with a budget of 12 million euro."
    row = fallback(tmp_path, cited + "\n" + FILLER, new + "\n" + FILLER, ["founded in 2024", KEPT])
    assert states(row) == [(False, False), (True, True)]
    assert row["reason"] == "not_comparable"


def test_pieces_far_apart_are_not_a_match(tmp_path):
    """Cells of a table row found only pages apart do not make the quotation present."""
    cited = "LEAD Support\nState Department responsible for Innovation\nMICDE, universities"
    new = "LEAD Support\n" + FILLER * 3 + "\nState Department responsible for Innovation\nMICDE, universities"
    row = fallback(tmp_path, cited + "\n" + FILLER, new, ["LEAD Support State Department responsible for Innovation MICDE, universities"])
    assert not row["quotes"][0]["in_checked_copy"]


def test_folded_lines_drop_list_markers_and_table_rules():
    assert monitor.folded_lines("- one item\n* two | cells\n3) three\n\n•  four") == ["oneitem", "twocells", "three", "four"]


@pytest.mark.parametrize("cited,new,quote", [
    ("The institute is\nindependent of the ministry.", "The institute is\nnot independent of the ministry.",
     "The institute is independent of the ministry."),
    ("Support regulators in responding to emerging AI\nrisks and harms.", "Support regulators in responding to emerging AI capabilities, risks and harms.",
     "Support regulators in responding to emerging AI risks and harms."),
])
def test_a_wording_change_at_a_cited_line_break_is_lost(tmp_path, cited, new, quote):
    """Only whole lines the cited copy also has may sit between two pieces, never an inserted word."""
    row = fallback(tmp_path, cited + "\n" + FILLER, new + "\n" + FILLER, [quote, KEPT])
    assert states(row) == [(False, True), (True, True)]
    assert row["reason"] == "fallback_quote_unconfirmed"


def test_piece_search_is_bounded():
    """Repeated pieces, a missing last piece and gaps rejected over and over all end quickly."""
    import time

    started = time.monotonic()
    assert not monitor.pieces_in_order(["a"] * 9 + ["z"], "a" * 20000, list(range(0, 20001)), 230, "")
    hay, bounds = monitor.folded_view("\n".join(["a", "x", "b"] * 5000))
    assert not monitor.pieces_in_order(["a", "z"], hay, bounds, 600, "a z")
    assert not monitor.pieces_in_order(["a", "b"], hay, bounds, 600, "a b")  # x is not in the window
    assert time.monotonic() - started < 5


def test_a_piece_inside_a_longer_line_does_not_hide_it_on_its_own_line():
    """Every reachable place is kept: A inside AX would leave a gap starting mid-line, the whole-line A does not."""
    hay, bounds = monitor.folded_view("H\nAX\nA\nB")
    assert monitor.pieces_in_order(["H", "A", "B"], hay, bounds, 100, "H AX A B")


def test_piece_search_with_permitted_gaps_is_linear():
    """Every line is a permitted gap and the last piece never comes: still one sweep per piece."""
    import time

    hay, bounds = monitor.folded_view("\n".join(["a"] * 20000))
    started = time.monotonic()
    assert not monitor.pieces_in_order(["a"] * 9 + ["z"], hay, bounds, 230, "aaaaaaaaaz")
    assert time.monotonic() - started < 2


def test_a_valid_match_after_many_failed_starts_is_found():
    """No cap on the first piece's places: the valid start after 200 that fail is still found."""
    hay, bounds = monitor.folded_view("H\n" * 200 + "Q\nH\nX\nA\nB")
    assert monitor.pieces_in_order(["H", "A", "B"], hay, bounds, 209, "HABX")
    assert not monitor.pieces_in_order(["H", "A", "B"], hay, bounds, 209, "HAB")  # X is not in the window
