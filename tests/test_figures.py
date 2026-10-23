"""Two-significant-figure rounding and the counts-row median, with hand-worked expectations."""

from decimal import Decimal

import pytest
from bs4 import BeautifulSoup

from scripts.check_page import check_figures_row, check_two_significant_figures, expected_staff_total, half_up_two, short_amount, two_figures
from scripts.display import allocation_year, funding_label, median, median_bound, money, round2, staff_figure, summary_figures


@pytest.mark.parametrize(
    ("value", "qualifier", "expected"),
    [
        (8_149_876, None, 8_100_000),
        (8_150_000, None, 8_200_000),  # half up
        (640_000, None, 640_000),
        (70_028_600, None, 70_000_000),
        (31, None, 31),
        (125, "at_least", 120),  # a lower bound never rounds up
        (125, "at_most", 130),  # an upper bound never rounds down
        (121, "more_than", 120),
        (121, "less_than", 130),
        (124, "nearly", 130),  # "nearly 124" is below 124, so it never rounds down
        (0, None, 0),
    ],
)
def test_round2(value, qualifier, expected):
    assert round2(value, qualifier) == expected


@pytest.mark.parametrize(
    ("value", "text"),
    [(70_000_000, "70m"), (8_100_000, "8.1m"), (8_000_000, "8.0m"), (6_000_000, "6.0m"), (10_000_000, "10m"), (640_000, "640k"), (6_400, "6.4k"), (6_000, "6.0k"), (1_200_000_000, "1.2bn"), (1_000_000_000, "1.0bn"), (31, "31")],
)
def test_money_text(value, text):
    assert money(value) == text


def test_staff_figure_keeps_qualifier_and_rounds_to_two_figures():
    assert [staff_figure(200, "about"), staff_figure(125, "at_least"), staff_figure(31, "exact"), staff_figure(1234)] == ["~200", "~120+", "31", "~1,200"]


@pytest.mark.parametrize(("total", "qualifier"), [(200, "about"), (125, "at_least"), (31, "exact"), (1234, None), (1250, "at_most"), (1234, "about"), (34, "exact")])
def test_gate_oracle_agrees_with_the_producer_on_staff_text(total, qualifier):
    assert expected_staff_total({"total": total, "total_qualifier": qualifier}) == staff_figure(total, qualifier)
    assert staff_figure(1250, "at_most") == "up to ~1,300"


def test_median_odd_and_even():
    assert median([5, 1, 3]) == Decimal(3)
    assert median([34, 31, 38, 200]) == Decimal(36)  # mean of 34 and 38
    assert median([7]) == Decimal(7)
    with pytest.raises(ValueError):
        median([])


def institute(name, jurisdiction, eur=None, staff=None, kind="state"):
    funding = {"x": {"currency": "EUR", "annual_amount": eur, "eur_per_year_est": eur}} if eur is not None else {}
    people = {"s": {"total": staff, "display": {"total": str(staff)}}} if staff is not None else {}
    return {"id": name, "jurisdiction_id": jurisdiction, "status": "established", "display": {
        "funding_records": funding, "comparison_funding": "x" if funding else None,
        "staff_records": people, "total_staff": "s" if people else None}}


def figures_row(budget, budget_top, staff, staff_top):
    """The page's figures row for two established institutes in states A and B (B the highest), neither with a card."""
    def fig(figure, text):
        return f'<div class="figure"><b>{figure}</b><span>{text}</span></div>'
    return BeautifulSoup('<div id="figures">' + fig(budget, f"median annual budget of 2 institutes. <em>B {budget_top}.</em>") + fig(staff, f"median staff of 2 institutes. <em>B {staff_top}.</em>") + fig("0 of 2", "institutes: budget not published.") + '<p class="small scope-note" id="figures-scope">Counts cover the 2 established state institutes.</p></div>', "html.parser")


def counts(rows):
    data = {"dataset": {"exchange_rates": [{"currency": "USD", "eur_per_unit": 1}]}, "jurisdictions": [{"id": j, "kind": "state"} for j in "ABCDE"] + [{"id": "EU", "kind": "supranational"}], "institutes": rows}
    return summary_figures(data)


def test_counts_odd_n_names_the_middle_and_the_highest():
    result = counts([institute("a", "A", 70_028_600), institute("b", "B", 8_849_690), institute("c", "C", 4_252_700)])
    assert result["budget"] == {"n": 3, "median": "EUR 8.8m", "highest": "~EUR 70m", "highest_jurisdiction_id": "A"}


def test_counts_even_n_averages_the_middle_pair():
    result = counts([institute("a", "A", 70_028_600), institute("b", "B", 8_849_690), institute("c", "C", 6_334_220), institute("d", "D", 4_252_700)])
    # (8,849,690 + 6,334,220) / 2 = 7,591,955
    assert result["budget"]["median"] == "EUR 7.6m" and result["budget"]["n"] == 4


def test_staff_counts_exclude_the_eu_and_state_median_is_rounded():
    rows = [institute("a", "A", staff=200), institute("b", "B", staff=34), institute("c", "C", staff=31), institute("d", "D", staff=38), institute("eu", "EU", staff=125)]
    assert counts(rows)["headcount"] == {"n": 4, "median": "36", "highest": "200", "highest_jurisdiction_id": "A"}


def test_budget_counts_exclude_the_eu():
    rows = [institute("a", "A", 70_000_000), institute("b", "B", 8_000_000), institute("eu", "EU", 90_000_000)]
    assert counts(rows)["budget"] == {"n": 2, "median": "EUR 39m", "highest": "EUR 70m", "highest_jurisdiction_id": "A"}


def test_highest_budget_keeps_its_bound_and_large_staff_median_has_no_exponent():
    top = institute("a", "A", 8_849_690)
    top["display"]["funding_records"]["x"]["amount_qualifier"] = "at_most"
    result = counts([top, institute("b", "B", 4_000_000)])
    assert result["budget"]["highest"] == "up to ~EUR 8.9m"
    from scripts.check_page import half_up_two
    assert format(int(half_up_two(150)), ",") == "150"


def test_fewer_than_two_gives_no_tile():
    assert counts([institute("a", "A", 5_000_000)]) == {"budget": None, "headcount": None}


@pytest.mark.parametrize("text", ["EUR 70m", "~EUR 8.1m (USD 9.1m)", "EUR 640k", "36", "31", "~200", "120+", "EUR 1.2bn", "Common scale: 0 to EUR 80m a year"])
def test_gate_accepts_two_figures(text):
    assert two_figures(text)


@pytest.mark.parametrize("text", ["EUR 8.15m", "EUR 641k", "12.875", "1,234", "~EUR 70.03m", "KRW 3.975bn"])
def test_gate_rejects_three_or_more_figures(text):
    assert not two_figures(text)


def test_rounded_native_figures_are_marked_and_exact_ones_are_not():
    base = {"kind": "one_off", "currency": "KRW", "commitment": "adopted"}
    def shown(amount, **extra):
        return funding_label({**base, "amount": amount, **extra}, annual=False, context=False)[0]

    assert shown(3_975_000_000) == "~KRW 4.0bn"
    assert shown(13_500_000, currency="EUR") == "~EUR 14m"
    assert shown(8_800_000_000) == "KRW 8.8bn"
    assert shown(9_000_000, amount_qualifier="at_most") == "Up to KRW 9.0m"
    assert shown(9_150_000, amount_qualifier="at_most") == "Up to ~KRW 9.2m"
    converted = {**base, "currency": "USD", "annual_amount": 10_000_000, "eur_per_year_est": 8_849_690, "kind": "annual"}
    assert funding_label(converted, annual=True, rates={"USD": 0.884969})[0] == "~EUR 8.8m (USD 10m)"
    converted["annual_amount"] = 12_875_000
    assert funding_label(converted, annual=True, rates={"USD": 0.884969})[0] == "~EUR 11m (~USD 13m)"


def test_two_figure_gate_fails_when_a_selector_matches_nothing():
    errors = check_two_significant_figures(BeautifulSoup("<p>nothing here</p>", "html.parser"))
    assert errors and all("matches no node" in e for e in errors)


@pytest.mark.parametrize("currency,amount,qualifier,expected", [
    ("EUR", 9_000_000, None, "EUR 9.0m"),
    ("EUR", 9_150_000, None, "~EUR 9.2m"),
    ("USD", 9_000_000, None, "~EUR 9.0m"),
    ("EUR", 9_000_000, "about", "About EUR 9.0m"),
    ("EUR", 9_150_000, "about", "About EUR 9.2m"),
    ("USD", 9_150_000, "about", "About EUR 9.2m"),
    ("EUR", 9_000_000, "at_most", "Up to EUR 9.0m"),
])
def test_annual_markers_and_highest_summary(currency, amount, qualifier, expected):
    record = {
        "currency": currency,
        "annual_amount": amount,
        "eur_per_year_est": amount,
        "amount_qualifier": qualifier,
    }
    assert funding_label(record, annual=True, rates={"USD": 1})[0].split(" (")[0] == expected
    top = institute("a", "A", amount)
    top["display"]["funding_records"]["x"].update(record)
    summary = counts([top, institute("b", "B", 1_000_000)])["budget"]
    expected_highest = expected[0].lower() + expected[1:] if qualifier else expected
    assert summary["highest"] == expected_highest


@pytest.mark.parametrize("staff,expected", [([1, 2], "1.5"), ([1, 1], "1"), ([0, 1], "0.5"), ([1200, 1400], "1,300")])
def test_staff_median_gate_and_truncation_mutation(staff, expected):
    from scripts.check_page import check_figures_row

    rows = [institute("a", "A", 1_000_000, staff[0]), institute("b", "B", 1_000_000, staff[1])]
    for row in rows:
        row["display"]["funding_records"]["x"]["display"] = {"value": "EUR 1.0m"}
    data = {"institutes": rows, "jurisdictions": [{"id": key, "name": key, "kind": "state"} for key in "AB"]}
    soup = figures_row("EUR 1.0m", "EUR 1.0m", expected, staff[1])
    assert counts(rows)["headcount"]["median"] == expected
    assert check_figures_row(soup, data) == []
    tile = soup.select("#figures > .figure b")[1]  # Exactly three figures in this fixture; the second is staff.
    tile.string = "1" if expected == "1.5" else "99"
    assert any("staff median figure" in error and "differs from the data median" in error for error in check_figures_row(soup, data))
    tile.string = expected
    assert check_figures_row(soup, data) == []
    tile.string = "~" + expected
    assert any("staff median figure" in error and "carries ~" in error for error in check_figures_row(soup, data))
    tile.string = expected


@pytest.mark.parametrize(
    "currency,amount,qualifier,figure",
    [
        ("EUR", 9_000_000, None, "EUR 9m"),
        ("EUR", 9_150_000, None, "~EUR 9.2m"),
        ("USD", 9_000_000, None, "~EUR 9m"),
        ("EUR", 9_150_000, "about", "About EUR 9.2m"),
        ("USD", 9_000_000, "about", "About EUR 9m"),
        ("EUR", 1_249_999, None, "~EUR 1.2m"),
        ("EUR", 1_299_999, "at_least", "At least ~EUR 1.2m"),
        ("EUR", 1_200_001, "at_most", "Up to ~EUR 1.3m"),
    ],
)
def test_funding_gate_approximation_marker_mutation(currency, amount, qualifier, figure):
    from scripts.check_page import check_funding_bars

    class Node:
        def __init__(self, soup):
            self.soup = soup

        def locator(self, selector):
            return Node(self.soup.select_one(selector))

        def count(self):
            return int(self.soup is not None)

        def get_attribute(self, name):
            return self.soup.get(name)

        def inner_text(self):
            return self.soup.get_text(" ", strip=True)

    soup = BeautifulSoup(f'<div class="fund-item" data-j="A" data-funding-id="x"><div class="fund-budget"><div class="fund-value"><strong>{figure}</strong></div></div><div class="bar-value"></div></div>', "html.parser")
    record = {
        "id": "x",
        "currency": currency,
        "annual_amount": amount,
        "eur_per_year_est": amount,
        "amount_qualifier": qualifier,
        "commitment": "adopted",
    }
    from scripts.published import annual_eur_estimate

    record["eur_per_year_est"] = annual_eur_estimate(record, {"USD": 1})
    data = {
        "dataset": {"exchange_rates": [{"currency": "USD", "eur_per_unit": 1}]},
        "institutes": [{"jurisdiction_id": "A", "funding": [record]}],
    }
    if currency != "EUR":
        from scripts.check_page import short_amount, half_up_two

        stated = short_amount(half_up_two(amount))
        mark = "~" if half_up_two(amount) != amount else ""
        figure += f" ({mark}USD {stated})"
        soup.select_one("strong").string = figure
    assert check_funding_bars(Node(soup), data, {"A": "x"}) == []
    strong = soup.select_one("strong")
    strong.string = figure.replace("~EUR", "EUR") if "~EUR" in figure else figure.replace("EUR", "~EUR")
    assert check_funding_bars(Node(soup), data, {"A": "x"}) == ["A: comparison approximation marker differs from selected record"]
    strong.string = figure
    assert check_funding_bars(Node(soup), data, {"A": "x"}) == []


def test_sort_oracle_reads_exact_rounded_and_about_euros():
    import json
    import subprocess
    from scripts.check_page import SORT_ROWS_JS

    # Minimal DOM interface: execute the actual browser oracle, not a copied regex.
    script = r'''
    const labels = ['EUR 9m a year', '~EUR 9.2m a year', 'About EUR 9m a year'];
    const cell = textContent => ({textContent, querySelector: () => null,
      querySelectorAll: () => [], cloneNode() { return this; }});
    global.document = {querySelectorAll: () => labels.map((label, i) => ({dataset: {j: String(i)}, children:
      [Object.assign(cell(''), {querySelector: () => cell('Institute ' + i)}),
       ...Array.from({length: 5}, () => cell('')), cell(label)]}))};
    const oracle = ORACLE;
    console.log(JSON.stringify(oracle('eur').map(row => [row.tier, row.v])));
    '''.replace("ORACLE", SORT_ROWS_JS)
    result = subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True)
    assert json.loads(result.stdout) == [[0, 9_000_000], [0, 9_200_000], [0, 9_000_000]]


@pytest.mark.parametrize(
    "amount,qualifier,expected",
    [
        (1_249_999, None, "~EUR 1.2m"),
        (1_299_999, None, "~EUR 1.3m"),
        (1_299_999, "at_least", "At least ~EUR 1.2m"),
        (1_200_001, "at_most", "Up to ~EUR 1.3m"),
        (1_300_000, "at_least", "At least EUR 1.3m"),
        (1_200_000, "at_most", "Up to EUR 1.2m"),
    ],
)
def test_annual_display_rounds_primary_amount_once(amount, qualifier, expected):
    from scripts.published import annual_eur_estimate

    record = {
        "kind": "annual",
        "currency": "EUR",
        "amount": amount,
        "annual_amount": amount,
        "amount_qualifier": qualifier,
    }
    record["eur_per_year_est"] = annual_eur_estimate(record, {})
    assert funding_label(record, annual=False)[0] == expected
    assert funding_label(record, annual=True)[0] == expected
    from scripts.display import derive_display
    from test_display import document, funding

    data = document()
    data["institutes"][0]["funding"] = [funding("budget", **record)]
    shown = derive_display(data)["institutes"][0]["funding"][0]["display"]
    assert shown["value"] == shown["native_value"] == expected


def figure_sort_rows(labels, key):
    import json
    import subprocess
    from scripts.check_page import SORT_ROWS_JS

    script = (
        r"""
    const labels = LABELS;
    const cell = textContent => ({textContent, querySelector: () => null,
      querySelectorAll: () => [], cloneNode() { return this; }});
    global.document = {querySelectorAll: () => labels.map(([jid, name, label]) => ({dataset: {j: jid}, children:
      [Object.assign(cell(''), {querySelector: () => cell(name)}),
       ...Array.from({length: 5}, () => cell('')), cell(label), cell(label)]}))};
    const oracle = ORACLE;
    console.log(JSON.stringify(oracle(KEY)));
    """.replace("LABELS", json.dumps(labels))
        .replace("ORACLE", SORT_ROWS_JS)
        .replace("KEY", json.dumps(key))
    )
    result = subprocess.run(
        ["node", "-e", script], capture_output=True, text=True, check=True
    )
    return json.loads(result.stdout)


def test_sort_gate_accepts_combined_bound_and_marker():
    from scripts.check_page import sort_errors_for

    rows = figure_sort_rows(
        [("Z", "Zed", "up to ~1,300"), ("A", "Alpha", "1,500")], "staff"
    )
    assert (
        sort_errors_for(
            rows,
            "staff",
            1,
            sort_data("staff", [("Z", 1250, "at_most"), ("A", 1500, None)]),
        )
        == []
    )


def test_sort_gate_uses_underlying_values_for_rounded_collisions():
    from scripts.check_page import sort_errors_for

    # Z=121, A=124: alphabetical order is wrong despite identical labels.
    rows = figure_sort_rows([("Z", "Zed", "~120"), ("A", "Alpha", "~120")], "staff")
    assert (
        sort_errors_for(
            rows, "staff", 1, sort_data("staff", [("Z", 121, None), ("A", 124, None)])
        )
        == []
    )


def sort_data(key, entries):
    collection = "funding" if key == "eur" else "staff"
    selection = "comparison_funding" if key == "eur" else "staff"
    return {
        "jurisdictions": [
            {"id": jid, "status": "established"} for jid, _, _ in entries
        ],
        "institutes": [
            {
                "jurisdiction_id": jid,
                "display": {selection: "record", **({"budget": "record"} if key == "eur" else {})},
                collection: [
                    {
                        "id": "record",
                        **(
                            {
                                "eur_per_year_est": value,
                                "annual_amount": value,
                                "currency": "EUR",
                                "amount_qualifier": qualifier,
                            }
                            if key == "eur"
                            else {"total": value, "total_qualifier": qualifier}
                        ),
                    }
                ],
            }
            for jid, value, qualifier in entries
        ],
    }


@pytest.mark.parametrize(
    "key,entries,labels",
    [
        ("staff", [("Z", 121, None), ("A", 124, None)], ["~120", "~120"]),
        (
            "eur",
            [("Z", 1_210_000, None), ("A", 1_240_000, None)],
            ["~EUR 1.2m a year", "~EUR 1.2m a year"],
        ),
        (
            "staff",
            [("Z", 1250, "at_most"), ("A", 1500, None)],
            ["up to ~1,300", "1,500"],
        ),
        ("staff", [("Z", 1399, "at_least"), ("A", 1500, None)], ["~1,300+", "1,500"]),
        (
            "eur",
            [("Z", 1_200_001, "at_most"), ("A", 1_500_000, None)],
            ["Up to ~EUR 1.3m a year", "EUR 1.5m a year"],
        ),
    ],
)
def test_numeric_sort_directions_and_reversed_order_mutation(key, entries, labels):
    from scripts.check_page import sort_errors_for

    data = sort_data(key, entries)
    rows = figure_sort_rows(
        [(jid, jid, label) for (jid, _, _), label in zip(entries, labels)], key
    )
    for direction, ordered in ((1, rows), (-1, list(reversed(rows)))):
        assert sort_errors_for(ordered, key, direction, data) == []
        errors = sort_errors_for(list(reversed(ordered)), key, direction, data)
        assert any("out of order" in e for e in errors)
        assert sort_errors_for(ordered, key, direction, data) == []


@pytest.mark.parametrize(
    "key,label,value", [("staff", "~120", 121), ("eur", "~EUR 1.2m a year", 1_210_000)]
)
def test_numeric_real_ties_stay_alphabetical_in_both_directions(key, label, value):
    from scripts.check_page import sort_errors_for

    data = sort_data(key, [("A", value, None), ("Z", value, None)])
    rows = figure_sort_rows([("A", "A", label), ("Z", "Z", label)], key)
    for direction in (1, -1):
        assert sort_errors_for(rows, key, direction, data) == []
        assert any(
            "not alphabetical" in e
            for e in sort_errors_for(rows[::-1], key, direction, data)
        )


def test_six_figure_export_ties_remain_page_sort_ties():
    from scripts.check_page import sort_errors_for

    data = sort_data("eur", [("A", 1_250_000, None), ("Z", 1_250_000, None)])
    data["institutes"][0]["funding"][0]["annual_amount"] = 1_249_999
    rows = figure_sort_rows(
        [("A", "A", "~EUR 1.2m a year"), ("Z", "Z", "~EUR 1.3m a year")], "eur"
    )
    for direction in (1, -1):
        assert sort_errors_for(rows, "eur", direction, data) == []
        assert any(
            "not alphabetical" in e
            for e in sort_errors_for(rows[::-1], "eur", direction, data)
        )


@pytest.mark.parametrize(
    "label", ["~~120", "up to up to ~120", "Not published", "~130"]
)
def test_numeric_sort_rejects_bad_or_mismatched_labels(label):
    from scripts.check_page import sort_errors_for

    data = sort_data("staff", [("A", 121, None)])
    rows = figure_sort_rows([("A", "A", label)], "staff")
    assert any("numeric label" in e for e in sort_errors_for(rows, "staff", 1, data))


def test_numeric_sort_rejects_missing_data_and_jurisdiction():
    from scripts.check_page import sort_errors_for

    data = sort_data("staff", [("A", None, None)])
    rows = figure_sort_rows([("A", "A", "120")], "staff")
    assert any("presence differs" in e for e in sort_errors_for(rows, "staff", 1, data))
    rows[0].pop("jid")
    assert any("jurisdiction" in e for e in sort_errors_for(rows, "staff", 1, data))


@pytest.mark.parametrize(
    "amount,qualifier,raw,exported,expected",
    [
        (2_499_998, None, "1249999.0", 1_250_000, "~EUR 1.2m (~USD 2.5m)"),
        (
            2_599_998,
            "at_least",
            "1299999.0",
            1_299_990,  # a lower bound exports rounded down, never up to 1.3m
            "At least ~EUR 1.2m (~USD 2.5m)",
        ),
        (2_400_002, "at_most", "1200001.0", 1_200_010, "Up to ~EUR 1.3m (~USD 2.5m)"),
    ],
)
def test_converted_boundary_uses_raw_product(
    amount, qualifier, raw, exported, expected
):
    from scripts.published import annual_eur_amount, annual_eur_estimate
    from scripts.display import derive_display
    from test_display import document, funding

    data = document()
    data["dataset"]["exchange_rates"] = [{"currency": "USD", "eur_per_unit": 0.5}]
    record = funding(
        "budget",
        amount=amount,
        annual_amount=amount,
        currency="USD",
        amount_qualifier=qualifier,
    )
    assert annual_eur_amount(record, {"USD": 0.5}) == Decimal(raw)
    record["eur_per_year_est"] = annual_eur_estimate(record, {"USD": 0.5})
    assert record["eur_per_year_est"] == exported
    data["institutes"][0]["funding"] = [record]
    shown = derive_display(data)["institutes"][0]["funding"][0]["display"]
    assert shown["value"] == expected
    assert (
        shown["native_value"]
        == {
            None: "~USD 2.5m",
            "at_least": "At least ~USD 2.5m",
            "at_most": "Up to ~USD 2.5m",
        }[qualifier]
    )


def test_annual_display_requires_primary_inputs():
    with pytest.raises(ValueError, match="annual_amount"):
        funding_label({"eur_per_year_est": 1_250_000}, annual=True)
    with pytest.raises(ValueError, match="exchange rate"):
        funding_label({"annual_amount": 2_499_998, "currency": "USD"}, annual=True)


def test_summary_median_avoids_export_double_rounding():
    rows = [institute("a", "A", 1_250_000, 31), institute("b", "B", 1_250_000, 31)]
    for row in rows:
        row["display"]["funding_records"]["x"]["annual_amount"] = 1_249_999
    assert counts(rows)["budget"]["median"] == "EUR 1.2m"

    from scripts.check_page import check_figures_row

    for row in rows:
        row["display"]["funding_records"]["x"]["display"] = {"value": "~EUR 1.2m"}
    data = {
        "institutes": rows,
        "jurisdictions": [{"id": key, "name": key, "kind": "state"} for key in "AB"],
    }
    html = str(figures_row("EUR 1.2m", "~EUR 1.2m", "31", "31"))
    assert check_figures_row(BeautifulSoup(html, "html.parser"), data) == []
    broken = html.replace("<b>EUR 1.2m</b>", "<b>EUR 1.3m</b>")
    assert any(
        "budget median figure" in e
        for e in check_figures_row(BeautifulSoup(broken, "html.parser"), data)
    )
    assert check_figures_row(BeautifulSoup(html, "html.parser"), data) == []


def test_native_eur_one_off_is_not_a_comparable_sort_value():
    from scripts.check_page import sort_errors_for

    data = sort_data("eur", [("A", 1_500_000, None), ("Z", None, None)])
    institute = data["institutes"][1]
    institute["display"].update(budget="record", comparison_funding=None)
    institute["funding"][0].update(amount=1_000_000, kind="one_off")
    rows = figure_sort_rows(
        [
            ("A", "A", "EUR 1.5m a year"),
            ("Z", "Z", "EUR 1.0m one-off"),
        ],
        "eur",
    )
    for direction in (1, -1):
        assert sort_errors_for(rows, "eur", direction, data) == []
        assert any("gaps must stay last" in e for e in sort_errors_for(rows[::-1], "eur", direction, data))


@pytest.mark.parametrize("value", [8_000_000, 8_020_000, 5_990_000, 10_000_000, 95_000, 1_000_000_000, 640_000, 9_950_000])
def test_gate_oracle_agrees_with_the_producer_on_amount_text(value):
    assert short_amount(half_up_two(value)) == money(round2(value))


def test_budget_median_over_a_bound_keeps_it_and_rounds_towards_it():
    # Hand-worked: 5,993,970 / 8,020,000 / 8,849,690 (up to) / 70,028,600; median of four = (8,020,000 + 8,849,690) / 2 = 8,434,845.
    rows = [institute("au", "A", 5_993_970), institute("kr", "B", 8_020_000), institute("us", "C", 8_849_690), institute("gb", "D", 70_028_600)]
    assert counts(rows)["budget"]["median"] == "EUR 8.4m"  # no bound: half up
    rows[2]["display"]["funding_records"]["x"]["amount_qualifier"] = "at_most"
    assert counts(rows)["budget"]["median"] == "up to EUR 8.5m"  # an upper bound rounds up
    rows[2]["display"]["funding_records"]["x"]["amount_qualifier"] = "about"
    assert counts(rows)["budget"]["median"] == "EUR 8.4m"  # about is not a bound
    # The gate expects the same text and rejects the unbounded one.
    rows[2]["display"]["funding_records"]["x"]["amount_qualifier"] = "at_most"
    for row in rows:
        row["display"]["funding_records"]["x"]["display"] = {"value": "EUR 1.0m"}
    data = {"institutes": rows, "jurisdictions": [{"id": key, "name": key, "kind": "state"} for key in "ABCD"]}
    figures = lambda budget: BeautifulSoup(  # noqa: E731
        '<div id="figures"><div class="figure"><b>' + budget + '</b><span>median annual budget of 4 institutes. D EUR 1.0m.</span></div>'
        '<div class="figure"><b>0</b><span>x</span></div><div class="figure"><b>0 of 0</b><span>institutes: budget not published.</span></div></div>',
        "html.parser",
    )
    budget_errors = lambda soup: [e for e in check_figures_row(soup, data) if e.startswith("budget median")]  # noqa: E731
    assert budget_errors(figures("up to EUR 8.5m")) == []
    assert budget_errors(figures("EUR 8.4m")) == ["budget median figure 'EUR 8.4m' differs from the data median 'up to EUR 8.5m'"]
    # A lower bound rounds down.
    rows[2]["display"]["funding_records"]["x"].update(amount_qualifier="at_least", annual_amount=8_920_000, eur_per_year_est=8_920_000)
    # (8,020,000 + 8,920,000) / 2 = 8,470,000: half up 8.5m, floor 8.4m.
    assert counts(rows)["budget"]["median"] == "at least EUR 8.4m"
    assert budget_errors(figures("at least EUR 8.4m")) == []
    assert budget_errors(figures("EUR 8.5m")) == ["budget median figure 'EUR 8.5m' differs from the data median 'at least EUR 8.4m'"]


def test_median_bound_needs_one_direction():
    assert [median_bound(set()), median_bound({None, "about"}), median_bound({None, "at_most"}), median_bound({"nearly"}), median_bound({"at_least"}), median_bound({None, "more_than"})] == [None, None, "at_most", "at_most", "at_least", "at_least"]
    with pytest.raises(ValueError, match="mixes upper and lower bounds"):
        median_bound({"at_most", "at_least"})


@pytest.mark.parametrize(
    ("period", "start", "end", "expected"),
    [
        ("2025-26 to 2028-29", "2026-07-01", "2027-06-30", "2026-27"),
        ("2026/27 to 2029/30", "2026-04-01", "2027-03-31", "2026/27"),
        ("2025 to 2028", "2026-01-01", "2026-12-31", "2026"),
    ],
)
def test_current_year_allocation_names_its_own_year(period, start, end, expected):
    assert allocation_year({"start": start, "end": end}, period) == expected
    record = {"currency": "EUR", "kind": "multi_year", "amount": 40_000_000, "annual_amount": 10_500_000, "eur_per_year_est": 10_500_000,
              "period": period, "commitment": "announced", "annual_basis_code": "allocation_current_year", "annual_period": {"start": start, "end": end}}
    assert funding_label(record, annual=True, context=False)[1] == ["a year", f"{expected} allocation", "announced"]
    assert funding_label(record, annual=False, context=False)[1] == ["multi-year", period, "announced"]  # the whole amount keeps its period
    record["period"] = period + " (announced)"  # a commitment stated only in the hidden period label is still shown
    assert funding_label(record, annual=True, context=False)[1] == ["a year", f"{expected} allocation", "announced"]
    assert funding_label(record, annual=False, context=False)[1] == ["multi-year", period + " (announced)"]


def test_figures_note_counts_established_state_institutes():
    from scripts.check_page import check_figures_row

    rows = [institute("a", "A", 1_000_000, 31), institute("b", "B", 1_000_000, 31)]
    for row in rows:
        row["display"]["funding_records"]["x"]["display"] = {"value": "EUR 1.0m"}
    data = {"institutes": rows, "jurisdictions": [{"id": key, "name": key, "kind": "state"} for key in "AB"]}
    html = str(figures_row("EUR 1.0m", "EUR 1.0m", "31", "31"))
    assert check_figures_row(BeautifulSoup(html, "html.parser"), data) == []
    wrong = html.replace("the 2 established", "the 11 established")
    assert any("note is not one muted line" in e for e in check_figures_row(BeautifulSoup(wrong, "html.parser"), data))
    missing = html.replace('<p class="small scope-note" id="figures-scope">Counts cover the 2 established state institutes.</p>', "")
    assert any("note is not one muted line" in e for e in check_figures_row(BeautifulSoup(missing, "html.parser"), data))
    rows[1]["status"] = "planned"  # a planned institute's budget in the medians would make the note false
    errors = check_figures_row(BeautifulSoup(html.replace("0 of 2", "0 of 1").replace("the 2 established", "the 1 established"), "html.parser"), data)
    assert any("not established (b)" in e for e in errors)


def test_figures_note_layout_mutations():
    from scripts.check_page import figures_note_layout_errors

    good = {"visible": True, "span": "1 / -1", "left": 16, "right": 374, "top": 300, "boxLeft": 16, "boxRight": 374, "figuresBottom": 280}
    assert figures_note_layout_errors(good) == []
    assert figures_note_layout_errors(None) == ["figures row: counts note missing"]
    assert any("not visible" in e for e in figures_note_layout_errors({**good, "visible": False}))
    assert figures_note_layout_errors({**good, "right": 300}) == []  # its measure may stop it short of the edge
    assert any("does not span" in e for e in figures_note_layout_errors({**good, "span": "auto / auto", "right": 130}))  # grid-column auto: one track only
    assert any("does not span" in e for e in figures_note_layout_errors({**good, "left": 140}))
    assert any("not below" in e for e in figures_note_layout_errors({**good, "top": 200}))


def test_first_output_line_guards():
    from scripts.check_page import FIRST_OUTPUT_LINE, first_output_data_errors, first_output_part_errors

    first = {"id": "a-first-output", "event": "first_output", "short": FIRST_OUTPUT_LINE}
    other = {"id": "a-launch", "event": "launched", "short": "Launched."}
    data = {"jurisdictions": [{"id": "A", "timeline": [other, first]}]}
    assert first_output_data_errors(data) == []
    assert first_output_data_errors({"jurisdictions": [{"id": "A", "timeline": [other, {**first, "short": "Earliest identified institutionally credited output."}]}]})
    assert first_output_part_errors([FIRST_OUTPUT_LINE], [first]) == []
    assert first_output_part_errors(["Launched.", FIRST_OUTPUT_LINE], [other, first]) == []
    assert first_output_part_errors(["First published work credited to the institute"], [first])  # altered rendered text
    assert first_output_part_errors(["Launched.", "Earliest identified output."], [other, first])  # first output later in a merged row
    assert first_output_part_errors(["Launched."], [other, first])  # first-output part dropped
    assert first_output_part_errors(["Something else."], [other]) == []  # not a first-output row: the summary check covers it
