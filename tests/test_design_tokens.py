"""K: literal drift must block the gate, even inside an expression or nested rule."""

from pathlib import Path

import pytest

from scripts.check_page import WIDTH_GEOMETRY, check_css_tokens, browser_seeds


PAGE = Path(__file__).resolve().parents[1] / "site/index.html"


def mutate_css(rule):
    return PAGE.read_text().replace("</style>", rule + "</style>", 1)


def test_page_uses_width_colour_and_breakpoint_tokens():
    assert check_css_tokens(PAGE.read_text()) == []


@pytest.mark.parametrize("prop,value", [
    ("width", "32rem"),
    ("max-width", "640px"),
    ("min-width", "40ch"),
    ("width", "min(100%, 31rem)"),
    ("max-width", "calc(var(--text-width) - 1px)"),
    ("max-width", "calc(-1px + var(--text-width))"),
    ("width", "var(--text-width, 49rem)"),
    ("width", "640PX !important"),
    ("width", "1e2em"),
    ("max-width", "calc(var(--text-width) - 2vh)"),
    ("min-width", "var(--text-width, 75%)"),
    ("width", "min(100cqi, var(--text-width))"),
])
def test_raw_width_mutations_block(prop, value):
    errors = check_css_tokens(mutate_css(f"@media (width <= 640px) {{ .mutant {{ {prop}: {value}; }} }}"))
    assert any("raw width" in error for error in errors)


@pytest.mark.parametrize("unit", [
    "px", "cm", "mm", "Q", "in", "pc", "pt",
    "em", "ex", "cap", "ch", "ic", "lh", "rem", "rex", "rcap", "rch", "ric", "rlh",
    "vw", "vh", "vi", "vb", "vmin", "vmax",
    "svw", "svh", "svi", "svb", "svmin", "svmax",
    "lvw", "lvh", "lvi", "lvb", "lvmin", "lvmax",
    "dvw", "dvh", "dvi", "dvb", "dvmin", "dvmax",
    "cqw", "cqh", "cqi", "cqb", "cqmin", "cqmax", "%",
])
@pytest.mark.parametrize("prop", ["width", "min-width", "max-width"])
def test_all_length_units_and_percentages_block_outside_geometry(prop, unit):
    errors = check_css_tokens(mutate_css(f".mutant {{ {prop}: 20{unit}; }}"))
    assert any("raw width" in error for error in errors)


def test_geometry_exceptions_have_reasons_and_cannot_spread():
    for (selector, prop, value), reason in WIDTH_GEOMETRY.items():
        assert reason
        assert check_css_tokens(mutate_css(f"{selector} {{ {prop}: {value}; }}")) == []
        assert any("raw width" in e for e in check_css_tokens(mutate_css(f".mutant {{ {prop}: {value}; }}")))
        for other_prop in {"width", "min-width", "max-width"}:
            if (selector, other_prop, value) not in WIDTH_GEOMETRY:
                assert any("raw width" in e for e in check_css_tokens(mutate_css(f"{selector} {{ {other_prop}: {value}; }}")))
        assert any("raw width" in e for e in check_css_tokens(mutate_css(f"{selector} {{ {prop}: calc({value} + 1px); }}")))


@pytest.mark.parametrize("query", [
    "@media (width <= 639px)",
    "@media(width <= 639px)",
    "@media (min-width: 641px)",
    "@media (max-width: 640px)",
    "@media (height <= 640px)",
    "@media (640px < width < 900px)",
    "@container funding (width < 47rem)",
    "@container funding (width < 60rem)",
    "@container other (width < 48rem)",
    "@container funding (width < calc(47rem))",
    "@container funding (width < calc(48rem))",
    "@media (width <= calc(640px + 1px))",
    "@media (width <= min(640px, 50vw))",
    "@media (width <= var(--bp))",
    "@media (calc(640px + 1px) >= width)",
    "@media (640px >= width)",
    "@media (WIDTH <= 639px)",
    "@media (Width <= 639px)",
    "@container funding (WIDTH < 47rem)",
    "@container funding (inline-size < 48rem)",
    "@container funding (block-size < 48rem)",
])
def test_unlisted_or_inconsistent_breakpoint_mutations_block(query):
    assert any("unlisted breakpoint" in e for e in check_css_tokens(mutate_css(query + " { .mutant { display: block; } }")))


def test_funding_container_boundary_accepts_only_its_recorded_rem_width():
    assert check_css_tokens(mutate_css("@container funding (width < 40rem) { .mutant { display: block; } }")) == []
    assert any("unlisted breakpoint" in e for e in check_css_tokens(mutate_css("@container funding (width < 36.5rem) { .mutant { display: block; } }")))


def test_declarations_before_a_nested_rule_are_not_skipped():
    source = mutate_css(".mutant { width: 31rem; @media (width <= 640px) { color: var(--ink); } }")
    assert any("nested rule after declarations" in e for e in check_css_tokens(source))


@pytest.mark.parametrize("old,new", [
    ('matchMedia("(width <= 430px)")', 'matchMedia("(width <= 431px)")'),
    ('matchMedia("(width <= 430px)")', 'matchMedia ( "(width <= 431px)" )'),
    ('data-narrow-media="(width <= 640px)"', 'data-narrow-media="(width <= 641px)"'),
])
def test_javascript_and_early_map_breakpoints_are_guarded(old, new):
    source = PAGE.read_text()
    assert old in source
    assert any("unlisted breakpoint" in e for e in check_css_tokens(source.replace(old, new, 1)))


def test_saved_map_query_accepts_html_escaping_but_still_checks_the_boundary():
    source = PAGE.read_text().replace('data-narrow-media="(width <= 640px)"', 'data-narrow-media="(width &lt;= 640px)"')
    assert check_css_tokens(source) == []
    assert any("unlisted breakpoint" in e for e in check_css_tokens(source.replace('width &lt;= 640px', 'width &lt;= 641px')))


@pytest.mark.parametrize("prop,value", [
    ("color", "#6E6760"),
    ("color", "red"),
    ("color", "rgb(110 103 96)"),
    ("color", "var(--muted, #6E6760)"),
    ("color", "color-mix(in srgb, var(--ink), white)"),
    ("text-decoration-color", "#fff"),
    ("-webkit-text-fill-color", "black"),
    ("-webkit-text-stroke-color", "red"),
    ("text-emphasis-color", "red"),
    ("-webkit-text-emphasis-color", "red"),
    ("text-decoration", "underline dotted red"),
    ("text-decoration", "#fff underline"),
    ("text-decoration", "underline rgb(110 103 96)"),
    ("text-decoration", "underline var(--muted, red)"),
    ("text-decoration", "underline color-mix(in srgb, var(--ink), white)"),
    ("text-decoration", "underline dotted RED !important"),
    ("text-emphasis", "filled dot red"),
    ("text-emphasis", "'x' #fff"),
    ("-webkit-text-emphasis", "open circle red"),
    ("-webkit-text-stroke", "1px red"),
    ("-webkit-text-stroke", "0 #fff"),
])
def test_raw_text_colour_mutations_block(prop, value):
    assert any("raw text colour" in e for e in check_css_tokens(mutate_css(f".mutant {{ {prop}: {value}; }}")))


def test_existing_glossary_decoration_cannot_introduce_a_raw_colour():
    source = PAGE.read_text()
    declaration = "text-decoration: underline dotted var(--muted)"
    assert declaration in source
    errors = check_css_tokens(source.replace(declaration, "text-decoration: underline dotted red", 1))
    assert any("raw text colour in abbr.g: text-decoration:" in e for e in errors)


@pytest.mark.parametrize("prop,value", [
    ("text-decoration", "underline dotted var(--muted)"),
    ("text-decoration", "underline var(--rule-strong) var(--focus)"),
    ("text-decoration", "underline dotted currentColor"),
    ("text-decoration", "underline 1px CanvasText !important"),
    ("text-decoration", "none"),
    ("text-decoration", "inherit"),
    ("text-emphasis", "filled dot var(--ink)"),
    ("text-emphasis", "'x' var(--ink)"),
    ("-webkit-text-emphasis", '"x" currentColor'),
    ("-webkit-text-stroke", "var(--rule) var(--ink)"),
    ("-webkit-text-stroke", "0"),
    ("-webkit-text-stroke", "thin currentColor"),
])
def test_text_shorthands_allow_tokens_and_non_colour_components(prop, value):
    assert check_css_tokens(mutate_css(f".mutant {{ {prop}: {value}; }}")) == []


def test_token_definitions_comments_and_system_colours_are_allowed():
    source = mutate_css("""
        :root { --test-width: 25ch; --test-colour: #abcdef; }
        /* .old { width: 100px; color: red; } @media (width <= 123px) {} */
        .mutant { width: var(--test-width); color: var(--test-colour); }
        @media (forced-colors: active) { .mutant { color: CanvasText; } }
    """)
    assert check_css_tokens(source) == []


def test_raw_declaration_in_root_is_not_a_token_definition():
    assert any("raw width" in e for e in check_css_tokens(mutate_css(":root { width: 20rem; }")))
    assert any("raw text colour" in e for e in check_css_tokens(mutate_css(":root { color: red; }")))


def test_token_guard_is_blocking_in_the_gate_seed(tmp_path):
    (tmp_path / "index.html").write_text(mutate_css(".mutant { width: 30rem; color: red; }"))
    errors = browser_seeds(tmp_path)["interactions"]()
    assert any("raw width" in e for e in errors)
    assert any("raw text colour" in e for e in errors)


def test_inline_styles_cannot_bypass_tokens():
    source = PAGE.read_text().replace("</body>", '<p style="width: 30rem; color: red">Mutation</p></body>')
    errors = check_css_tokens(source)
    assert any("raw width" in e for e in errors)
    assert any("raw text colour" in e for e in errors)


def test_script_set_bar_proportions_are_not_design_widths():
    source = PAGE.read_text().replace("</body>", '<span class="fund-fill" style="width: 42.5%"></span></body>')
    assert not [e for e in check_css_tokens(source) if "raw width" in e]
    source = PAGE.read_text().replace("</body>", '<span style="max-width: 42.5%"></span><span style="width: 42.5em"></span></body>')
    assert len([e for e in check_css_tokens(source) if "raw width" in e]) == 2
