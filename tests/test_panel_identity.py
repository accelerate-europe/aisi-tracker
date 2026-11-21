"""State-first record heads and the browser gate's independent failure cases."""


import pytest

from scripts import check_page


@pytest.mark.parametrize('mini', [False, True])
def test_card_content_requires_state_heading_and_original_label_binding(mini):
    from bs4 import BeautifulSoup

    shell, head = ('details', 'summary') if mini else ('article', 'div class="top"')
    end = head.split()[0]
    tip = '' if mini else ' title="France"'
    status = '' if mini else '<span class="vh" id="status">, Established</span>'
    labelled = 'name' if mini else 'name status'
    html = f'<div class="statusgroup"><{shell} class="card {"mini" if mini else ""}" data-j="FR" aria-labelledby="{labelled}"><{head}><h4 class="name" id="name"{tip}>France</h4>{status}</{end}></{shell}></div>'
    data = dict(jurisdictions=[dict(id='FR', name='France', status='established')])
    assert check_page.check_card_names(html, data) == []
    for field in ('text', 'label', 'description') + (() if mini else ('tooltip', 'status')):
        soup = BeautifulSoup(html, 'html.parser')
        if field == 'text':
            soup.select_one('.name').string = 'France, National Institute for AI Evaluation and Security'
        elif field == 'label':
            soup.select_one('.card')['aria-labelledby'] = 'missing'
        elif field == 'description':
            soup.select_one('.card')['aria-describedby'] = 'status'
        elif field == 'tooltip':
            soup.select_one('.name')['title'] = 'INESIA'
        else:
            soup.select_one('.vh').decompose()
        assert check_page.check_card_names(str(soup), data), field


def test_phone_panel_identity_uses_base_names_for_same_named_institutes():
    from test_check_page import _names_data

    data = _names_data()
    data['dataset'] = {'vocabularies': {'function': {}}}
    for state in data['jurisdictions']:
        state['status'] = 'established' if state['id'] == 'GB' else 'planned'
    rows = {row['id']: row for row in check_page.function_phone_model(data)['rows']}
    assert rows['gb-aisi']['head_name'] == 'United Kingdom, AI Security Institute'
    assert rows['es-isia']['head_name'] == 'Spain, AI Security Institute'
    # Standalone Institute fields still disambiguate the same base name with the state suffix.
    assert check_page.expected_display_names(data)['gb-aisi'] == 'AI Security Institute (United Kingdom)'
    assert check_page.expected_display_names(data)['es-isia'] == 'AI Security Institute (Spain)'


@pytest.mark.parametrize('replacement', ['AI Security Institute (Spain)', 'AI Security Institute (Spain), Spain', 'Spain, Spain AISI'])
def test_funding_content_requires_state_before_full_recorded_name(replacement):
    from test_check_page import _names_data, _names_html

    html = _names_html().replace('>Spain, AI Security Institute</span>', '>' + replacement + '</span>')
    assert any('in full' in e for e in check_page.check_institute_names(html, _names_data()))


@pytest.mark.parametrize('spoken,valid', [
    ('France, National Institute for AI Evaluation and Security', True),
    ('France , National Institute for AI Evaluation and Security', True),
    ('France,National Institute for AI Evaluation and Security', True),
    ('France National Institute for AI Evaluation and Security', False),
    ('France, National Institute', False),
    ('National Institute for AI Evaluation and Security, France', False),
])
def test_wrapped_accessible_identity_allows_only_separator_whitespace(spoken, valid):
    pattern = check_page.identity_name_pattern('France, National Institute for AI Evaluation and Security')
    assert bool(pattern.fullmatch(spoken)) is valid


def test_funding_content_keeps_the_visually_hidden_separator():
    from test_check_page import _names_data, _names_html

    html = _names_html().replace('>Spain, AI Security Institute</span>',
                                '><span class="head-state">Spain</span><span class="head-separator vh">, </span>'
                                '<span class="head-institute">AI Security Institute</span></span>')
    assert check_page.check_institute_names(html, _names_data()) == []
