"""Conservative joining of one source's consecutive role claims; expected prose is written out in full."""

import copy

import pytest

from scripts import summaries


def claims(*texts, sources=("source",)):
    return [{"id": f"claim-{n}", "kind": "function", "body_id": "body", "source_ids": list(sources), "text": text}
            for n, text in enumerate(texts)]


def test_pa_rule_b_uses_longest_qualifying_cut():
    roles = claims(
        "SENACYT's strategy plans an AI Risk Observatory to monitor global trends in AI security.",
        "SENACYT's strategy plans an AI Risk Observatory to track local incidents of algorithmic harm.")
    before = copy.deepcopy(roles)
    decisions = []
    assert summaries.join_role_claims(roles, decisions=decisions) == (
        "SENACYT's strategy plans an AI Risk Observatory to monitor global trends in AI security "
        "and to track local incidents of algorithmic harm.")
    assert decisions[0]["rule"] == "b"
    assert roles == before


def test_qa_long_run_uses_also_a():
    roles = claims(
        "The decision tasks the centre's strategies, compliance and quality department with proposing legislation to regulate, encourage and develop AI use in the state, with the competent administrative unit.",
        "The decision tasks the centre's partnerships and empowerment department with forming strategic partnerships with government bodies and the private sector to support AI adoption.")
    assert summaries.join_role_claims(roles) == (
        "The decision tasks the centre's strategies, compliance and quality department with proposing legislation to regulate, encourage and develop AI use in the state, with the competent administrative unit. "
        "The decision also tasks the centre's partnerships and empowerment department with forming strategic partnerships with government bodies and the private sector to support AI adoption.")


def test_kz_long_run_uses_also_a_and_keeps_quotations():
    roles = claims(
        'It says it creates "tools for safe model control and evaluation" to make advanced AI systems more transparent, reliable and aligned with human intentions.',
        "It says its policy research explores how governance frameworks, regulatory approaches and international cooperation can ensure the safe and beneficial deployment of advanced AI systems.",
        'It says it investigates how neural representations emerge and interact, applying techniques such as "activation steering, adversarial robustness analysis, and mechanistic interpretability".')
    assert summaries.join_role_claims(roles) == (
        'It says it creates "tools for safe model control and evaluation" to make advanced AI systems more transparent, reliable and aligned with human intentions. '
        'It also says its policy research explores how governance frameworks, regulatory approaches and international cooperation can ensure the safe and beneficial deployment of advanced AI systems. '
        'It also says it investigates how neural representations emerge and interact, applying techniques such as "activation steering, adversarial robustness analysis, and mechanistic interpretability".')


def test_latvia_shared_pronoun_prevents_join_but_allows_also_a():
    roles = claims(
        "The Artificial Intelligence Centre Law tasks it with organising, with the competent institutions, a special regulatory environment in which rules different from those in force may apply, so that AI systems can be developed, tested and improved.",
        "The Artificial Intelligence Centre Law tasks it with promoting the application of AI technologies in the public and private sectors.")
    assert summaries.join_role_claims(roles) == (
        "The Artificial Intelligence Centre Law tasks it with organising, with the competent institutions, a special regulatory environment in which rules different from those in force may apply, so that AI systems can be developed, tested and improved. "
        "The Artificial Intelligence Centre Law also tasks it with promoting the application of AI technologies in the public and private sectors.")


def test_cy_long_run_uses_also_a():
    roles = claims(
        "The deputy ministry says it has been designated market-surveillance authority for high-risk AI systems in points 1, 6, 7 and 8 of Annex III of the AI Act.",
        "The deputy ministry says the Commissioner for Personal Data Protection is responsible for enforcing the Article 5 prohibitions on AI practices insofar as they fall within her domain of competence.")
    assert summaries.join_role_claims(roles) == (
        "The deputy ministry says it has been designated market-surveillance authority for high-risk AI systems in points 1, 6, 7 and 8 of Annex III of the AI Act. "
        "The deputy ministry also says the Commissioner for Personal Data Protection is responsible for enforcing the Article 5 prohibitions on AI practices insofar as they fall within her domain of competence.")


def test_greece_uses_also_c_before_the_different_verb():
    roles = claims(
        "Law 5321/2026 designates it, with EETT, as competent authority for operating the AI regulatory sandbox.",
        "Law 5321/2026 makes it the market-surveillance authority for AI systems in the AI Act's prohibited practices, high-risk systems in Annex III and systems with transparency obligations under Article 50.")
    decisions = []
    assert summaries.join_role_claims(roles, decisions=decisions) == (
        "Law 5321/2026 designates it, with EETT, as competent authority for operating the AI regulatory sandbox. "
        "Law 5321/2026 also makes it the market-surveillance authority for AI systems in the AI Act's prohibited practices, high-risk systems in Annex III and systems with transparency obligations under Article 50.")
    assert [d["step"] for d in decisions] == ["unchanged", "also (c)"]


def test_join_exactly_at_the_limit():
    roles = claims(
        "The centre plans to test UK models against realistic threats with independent teams and publish the results.",
        "The centre plans to study risks across public services and share its findings with national policymakers.")
    expected = (
        "The centre plans to test UK models against realistic threats with independent teams and publish the results "
        "and to study risks across public services and share its findings with national policymakers.")
    assert summaries.JOIN_MAX_CHARS == len(expected) == 200
    decisions = []
    assert summaries.join_role_claims(roles, decisions=decisions) == expected
    assert decisions[0]["step"] == "joined" and decisions[0]["length"] == 200


def test_join_one_character_over_the_limit_uses_also_a():
    roles = claims(
        "The centre plans to test new models against realistic threats with independent teams and publish the results.",
        "The centre plans to study risks across public services and share its findings with national policymakers.")
    candidate = (
        "The centre plans to test new models against realistic threats with independent teams and publish the results "
        "and to study risks across public services and share its findings with national policymakers.")
    assert len(candidate) == 201
    decisions = []
    assert summaries.join_role_claims(roles, decisions=decisions) == (
        "The centre plans to test new models against realistic threats with independent teams and publish the results. "
        "The centre also plans to study risks across public services and share its findings with national policymakers.")
    assert decisions[1]["step"] == "also (a)"
    assert decisions[1]["reason"] == "joined length 201 exceeds 200"


def test_long_third_claim_keeps_the_whole_run_as_separate_sentences():
    roles = claims("The centre says it tests models.", "The centre says its staff publish findings.",
                   "The centre says the testing programme brings together independent researchers and public authorities to assess realistic risks, document the findings and publish detailed guidance for organisations using advanced AI systems.")
    assert summaries.join_role_claims(roles) == (
        "The centre says it tests models. The centre also says its staff publish findings. "
        "The centre also says the testing programme brings together independent researchers and public authorities to assess realistic risks, document the findings and publish detailed guidance for organisations using advanced AI systems.")


def test_an_intervening_source_breaks_the_run():
    roles = claims("It says it tests models.", "It says its staff study risks.", "It says its researchers publish findings.")
    roles[1]["source_ids"] = ["another-source"]
    assert summaries.join_role_claims(roles) == (
        "It says it tests models. It says its staff study risks. It says its researchers publish findings.")


def test_also_preserves_original_spacing_and_quotes():
    roles = claims('  The centre  says it tests "models". ', 'The centre\tsays it studies "risks".')
    before = copy.deepcopy(roles)
    assert summaries.join_role_claims(roles) == '  The centre  says it tests "models".  The centre\talso says it studies "risks".'
    assert roles == before


@pytest.mark.parametrize("later, expected", [
    ("The law defines its remit.", "The law establishes a centre. The law also defines its remit."),
    ("The law also defines its remit.", "The law establishes a centre. The law also defines its remit."),
    ("The law defines the remit.", "The law establishes a centre. The law defines the remit."),
])
def test_also_c_needs_exactly_one_word_then_it_or_its(later, expected):
    assert summaries.join_role_claims(claims("The law establishes a centre.", later)) == expected


@pytest.mark.parametrize("first, second, expected", [
    ("The centre says it tests models.", "The centre says it studies risks.",
     "The centre says it tests models. The centre also says it studies risks."),
    ("The centre lists its findings.", "The centre lists its costs.",
     "The centre lists its findings. The centre also lists its costs."),
    ("The centre says they test models.", "The centre says they study risks.",
     "The centre says they test models. The centre also says they study risks."),
    ("The centre lists their findings.", "The centre lists their costs.",
     "The centre lists their findings. The centre also lists their costs."),
])
def test_same_pronoun_in_every_remainder_blocks_the_cut(first, second, expected):
    assert summaries.join_role_claims(claims(first, second)) == expected


def test_run_of_three_recomputes_the_cut_for_every_remainder():
    roles = claims("The announcement says it plans to test models.", "The announcement says it plans to study risks.",
                   "The announcement says its researchers publish findings.")
    assert summaries.join_role_claims(roles) == (
        "The announcement says it plans to test models, it plans to study risks, and its researchers publish findings.")


def test_different_ministries_and_sources_stay_apart():
    roles = claims("The Ministry of Digital Affairs says it tests models.", "The Ministry of Finance says it funds research.")
    roles[1]["source_ids"] = ["other-source"]
    assert summaries.join_role_claims(roles) == (
        "The Ministry of Digital Affairs says it tests models. The Ministry of Finance says it funds research.")


def test_different_sources_block_even_a_qualifying_cut():
    roles = claims("The ministry says it tests models.", "The ministry says it funds research.")
    roles[1]["source_ids"] = ["other-source"]
    assert summaries.join_role_claims(roles) == "The ministry says it tests models. The ministry says it funds research."


def test_one_source_without_a_shared_opening_stays_apart():
    roles = claims("Its researchers test models.", "The centre publishes findings.")
    assert summaries.join_role_claims(roles) == "Its researchers test models. The centre publishes findings."


def test_one_shared_word_stays_apart():
    roles = claims("The centre tests models.", "The ministry funds research.")
    assert summaries.join_role_claims(roles) == "The centre tests models. The ministry funds research."


def test_one_word_cut_cannot_join_even_before_to():
    roles = claims("Plans to test models.", "Plans to fund research.")
    assert summaries.join_role_claims(roles) == "Plans to test models. Plans to fund research."


def test_shared_opening_without_a_qualifying_cut_stays_apart():
    roles = claims("The centre tests models.", "The centre funds research.")
    assert summaries.join_role_claims(roles) == "The centre tests models. The centre funds research."


def test_nepal_capitalised_remainders_use_also_a_twice():
    roles = claims(
        "The policy tasks AI Excellence Centres with research.",
        "The policy tasks AI Excellence Centres with training.",
        "The policy tasks AI Excellence Centres with advice.")
    before = copy.deepcopy(roles)
    decisions = []
    assert summaries.join_role_claims(roles, decisions=decisions) == (
        "The policy tasks AI Excellence Centres with research. "
        "The policy also tasks AI Excellence Centres with training. "
        "The policy also tasks AI Excellence Centres with advice.")
    assert [d["step"] for d in decisions] == ["unchanged", "also (a)", "also (a)"]
    assert roles == before


def test_nepal_national_ai_centre_uses_also_a_for_only_the_qualifying_pair():
    roles = claims(
        "The policy tasks the National AI Centre with promoting and encouraging AI study, research, development and application.",
        "The policy tasks the National AI Centre with acting as the focal point for international coordination and cooperation on AI.",
        "The policy provides for skill-based training for policymakers, employees and professionals in the public and private sectors through the National AI Centre, AI Excellence Centres, training centres and relevant organisations working in this field.")
    before = copy.deepcopy(roles)
    decisions = []
    assert summaries.join_role_claims(roles, decisions=decisions) == (
        "The policy tasks the National AI Centre with promoting and encouraging AI study, research, development and application. "
        "The policy also tasks the National AI Centre with acting as the focal point for international coordination and cooperation on AI. "
        "The policy provides for skill-based training for policymakers, employees and professionals in the public and private sectors through the National AI Centre, AI Excellence Centres, training centres and relevant organisations working in this field.")
    assert [d["step"] for d in decisions] == ["unchanged", "also (a)", "unchanged"]
    assert roles == before


def test_also_c_uses_the_pairwise_prefix_when_the_run_has_no_common_prefix():
    roles = claims("The law establishes a centre.", "The law defines its remit.", "Other guidance covers training.")
    decisions = []
    assert summaries.join_role_claims(roles, decisions=decisions) == (
        "The law establishes a centre. The law also defines its remit. Other guidance covers training.")
    assert [d["step"] for d in decisions] == ["unchanged", "also (c)", "unchanged"]


def test_capitalised_remainder_blocks_a_short_join_but_allows_also_a():
    roles = claims("The decision tasks Digital Affairs with testing models.", "The decision tasks Finance with funding research.")
    candidate = "The decision tasks Digital Affairs with testing models and Finance with funding research."
    assert len(candidate) < summaries.JOIN_MAX_CHARS
    decisions = []
    assert summaries.join_role_claims(roles, decisions=decisions) == (
        "The decision tasks Digital Affairs with testing models. The decision also tasks Finance with funding research.")
    assert [d["step"] for d in decisions] == ["unchanged", "also (a)"]


def test_reporting_verb_with_trailing_punctuation():
    roles = claims("The centre says: it tests models.", "The centre says: its staff publish findings.")
    assert summaries.join_role_claims(roles) == "The centre says: it tests models and its staff publish findings."


def test_cut_inside_a_straight_quotation_stays_apart():
    roles = claims('Its "research plans to test models" are published.', 'Its "research plans to study risks" are published.')
    assert summaries.join_role_claims(roles) == (
        'Its "research plans to test models" are published. Its "research plans to study risks" are published.')


def test_rule_b_allows_a_complete_straight_quotation_in_the_prefix():
    roles = claims('The centre plans "safe testing" to assess models.', 'The centre plans "safe testing" to study risks.')
    assert summaries.join_role_claims(roles) == 'The centre plans "safe testing" to assess models and to study risks.'


def test_open_quote_cut_falls_back_to_an_earlier_qualifying_cut():
    roles = claims('It says the "research plans to test models" are published.',
                   'It says the "research plans to study risks" are published.')
    assert summaries.join_role_claims(roles) == (
        'It says the "research plans to test models" are published and the "research plans to study risks" are published.')


@pytest.mark.parametrize("different_source, expected", [
    (False, "The centre plans to test models. The centre also plans to study risks. The centre also reports its findings. The centre reports the costs."),
    (True, "The centre plans to test models and to study risks. The centre reports its findings and the costs."),
])
def test_source_change_ends_the_run(different_source, expected):
    roles = claims("The centre plans to test models.", "The centre plans to study risks.",
                   "The centre reports its findings.", "The centre reports the costs.")
    if different_source:
        for claim in roles[2:]:
            claim["source_ids"] = ["other-source"]
    assert summaries.join_role_claims(roles) == expected


def test_a_nonqualifying_pair_does_not_block_other_sentences_in_the_run():
    roles = claims("The centre plans to test models.", "The centre plans to study risks.", "Other staff publish findings.",
                   "The centre plans to fund research.")
    assert summaries.join_role_claims(roles) == (
        "The centre plans to test models. The centre also plans to study risks. Other staff publish findings. The centre also plans to fund research.")


def record_with_roles(roles):
    return {"id": "XX", "status": "none_identified", "check": {"last_checked": "2026-11-16"}, "claims": roles,
            "related_bodies": [{"id": "body", "name": "Centre", "kind": "research"}],
            "closest_body": {"body_id": "body", "name": "Centre", "stage": "exists"}}


def test_generate_joins_roles_and_note_but_keeps_the_lead_and_expiry():
    roles = claims("Its founding act establishes the centre.", "It says it tests models.", "It says it studies risks.",
                   "It says its staff publish findings.")
    roles[0]["lead"] = True
    roles[0]["source_ids"] = ["founding-act"]
    roles[2]["expired"] = True
    record = record_with_roles(roles)
    before = copy.deepcopy(record)
    texts = summaries.generate(record, None, None, as_of="2026-11-16")
    assert texts["note"] == "It says it tests models and its staff publish findings."
    assert texts["roles"]["body"] == "Its founding act establishes the centre. It says it tests models and its staff publish findings."
    assert texts["summary"] == (
        "No qualifying institute identified as of November 2026. Closest body: Centre. Its founding act establishes the centre.")
    assert record == before


def test_kept_lead_stays_marked_between_joined_live_runs():
    roles = claims("It says it tests models.", "It says its staff study risks.", "It says it leads research.",
                   "It says its staff publish findings.", "It says it trains researchers.")
    roles[2].update(lead=True, expired=True, reread={"state": "source_gone", "since": "2026-11-02", "last_found": "2026-10-14"})
    texts = summaries.generate(record_with_roles(roles), None, None, as_of="2026-11-16")
    assert texts["roles"]["body"] == (
        "It says it tests models and its staff study risks. It says it leads research. "
        "Source offline since 2 November 2026; last found 14 October 2026. It says its staff publish findings and it trains researchers.")
    assert texts["note"] == "It says it tests models, its staff study risks, its staff publish findings, and it trains researchers."


def test_empty_and_single_claim():
    assert summaries.join_role_claims([]) == ""
    assert summaries.join_role_claims(claims("It says it tests models.")) == "It says it tests models."


@pytest.mark.parametrize("stage_first", [False, True])
def test_page_gate_requires_separate_claim_disclosures(stage_first):
    from html import escape

    from scripts.check_page import check_nonidentified_folds

    record = record_with_roles(claims("It says it tests models.", "It says its staff study risks."))
    record["closest_body"]["stage"] = "planned"
    if stage_first:
        record["closest_body"]["active_in"] = "2026"

    def page(texts):
        details = [*texts]
        details.insert(0 if stage_first else len(details), "Planned.")
        return ('<details class="card mini" id="c-xx"><div class="body-group"><p class="gl">Closest body</p>'
                '<ul><li><span class="body-name">Centre</span>'
                + "".join(f'<span class="det">{escape(text)}</span>' for text in details) + '</li></ul></div></details>')

    assert check_nonidentified_folds(page(["It says it tests models.", "It says its staff study risks."]), {"jurisdictions": [record]}) == []
    assert summaries.join_role_claims(record["claims"]) == "It says it tests models and its staff study risks."
    assert check_nonidentified_folds(page(["It says it tests models and its staff study risks."]), {"jurisdictions": [record]})
    assert check_nonidentified_folds(page(["It says it tests models.", "It says its staff study costs."]), {"jurisdictions": [record]})
