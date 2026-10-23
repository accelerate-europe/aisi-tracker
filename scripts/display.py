"""Derive display selections and citations from published facts and evidence.

Input excerpt supports must already be resolved RFC 6901 pointers. Source lists
are never evidence fallbacks. This layer copies its input and performs no I/O.
"""

import copy
import re
from decimal import Decimal, ROUND_HALF_UP

if __package__:  # one home for the rule heading: method.html headings and the page's "Full rules on" links
    from .method_copy import rule_label
else:
    from method_copy import rule_label

if __package__:
    from .published import INTERVAL_KEYS, RATIO_FIELDS, announcement, annual_eur_amount, annual_estimate, bound_rounding, budget_ratios, comparable_funding, component_indices, first_designation, milestones, naming_act, significant, status_basis
    from .row_checks import row_display
else:
    from published import INTERVAL_KEYS, RATIO_FIELDS, announcement, annual_eur_amount, annual_estimate, bound_rounding, budget_ratios, comparable_funding, component_indices, first_designation, milestones, naming_act, significant, status_basis
    from row_checks import row_display


DEVELOPMENT_EVENTS = {
    "commitment",
    "proposed",
    "designated",
    "launched",
    "first_output",
    "exploration_started",
    "withdrawn",
    "closed",
}
# Population and region sources apply to every entry: Method names them once, entries do not list them.
GENERIC_FIELDS = ("population", "region")
# Off the card source lists: the downloads and the API carry them; the page shows model access only in Testing and access, with its own source list.
# GDP and the budget ratios join them until the page shows per person (rule budget-ratios).
DOWNLOAD_ONLY_FIELDS = ("testing_capacity", "model_access", "staffing_model", "gdp", *RATIO_FIELDS)
NON_DISPLAY_FIELDS = {
    "id",
    "lineage",
    "source_ids",
    "identifier_source_ids",
    "wikidata",
    "ror",
    "ror_id",
    "iso2",
    "iso3",
    "m49",
    "citations",
    "display",
    "check",
    *DOWNLOAD_ONLY_FIELDS,
}


def funding_selection(records, as_of):
    """One Budget choice: annual comparison, else latest committed own funding,
    else latest own proposal. Equal dates keep permanent record ID order, the
    tie-break that dataset.method's Budget display rule leaves to the code."""
    comparison = comparable_funding(records, as_of)
    candidates = [
        n
        for n, row in enumerate(records)
        if row.get("coverage") == "whole_institute"
        and row.get("kind") in ("one_off", "multi_year", "annual")
        and row.get("amount") is not None
        and row.get("currency")
        and row.get("commitment") in ("adopted", "announced", "proposed")
        and (not row.get("decision_date") or row["decision_date"] <= as_of)
    ]
    candidates.sort(key=lambda n: records[n]["id"])
    candidates.sort(key=lambda n: funding_date(records[n]), reverse=True)
    candidates.sort(key=lambda n: records[n]["commitment"] == "proposed")
    return comparison, comparison if comparison is not None else next(
        iter(candidates), None
    )


def funding_date(record):
    return record.get("decision_date") or (record.get("annual_period") or {}).get(
        "end", ""
    )


def staff_selection(records):
    """Latest total, latest technical, then description; equal dates use IDs."""
    ordered = sorted(range(len(records)), key=lambda n: records[n]["id"])
    ordered.sort(key=lambda n: records[n].get("as_of", ""), reverse=True)
    total = next((n for n in ordered if records[n].get("total") is not None), None)
    technical = next(
        (n for n in ordered if records[n].get("technical") is not None), None
    )
    staff = (
        total
        if total is not None
        else technical
        if technical is not None
        else next(iter(ordered), None)
    )
    return staff, total, technical


def round2(value, qualifier=None):
    """The one rounding rule for every budget and staff figure the page shows: two significant figures.

    Qualifiers that state a bound round the way that keeps the bound true: "125+" shows as "120+", "up to 125" as "130"."""
    return significant(Decimal(str(value)), 2, bound_rounding(qualifier))


def money(value):
    """Short text for an amount already rounded to two significant figures: 640k, 8.1m, 8.0m, 1.2bn.

    A single-digit multiple keeps its second figure ("8.0m", not "8m")."""
    if value != int(value):
        return format(Decimal(str(value)), ",f")
    for size, suffix in ((1_000_000_000, "bn"), (1_000_000, "m"), (1_000, "k")):
        if value >= size:
            short = (Decimal(str(value)) / size).normalize()
            if short < 10 and short == short.to_integral_value():
                short = short.quantize(Decimal("0.1"))
            return format(short, "f") + suffix
    return f"{int(value):,}"


def median(values):
    """Middle value; the mean of the two middle values when the count is even."""
    ordered = sorted(Decimal(str(v)) for v in values)
    if not ordered:
        raise ValueError("median of no values")
    middle = len(ordered) // 2
    return ordered[middle] if len(ordered) % 2 else (ordered[middle - 1] + ordered[middle]) / 2


def changed(value, rounded):
    """True when rounding altered a stated figure, so it must be marked with ~ rather than read as exact."""
    return Decimal(str(value)) != Decimal(str(rounded))


def staff_figure(value, qualifier=None):
    """A staff count at two significant figures with its qualifier; ~ marks a count that rounding changed."""
    rounded = round2(value, qualifier)
    number = format(rounded, ",")
    mark = "~" if changed(value, rounded) else ""
    return {"about": "~" + number, "at_least": mark + number + "+", "at_most": "up to " + mark + number}.get(qualifier, mark + number)


# Period-label wording that already states a commitment, so the qualifier line does not repeat it.
COMMITMENT_STATED = {
    "adopted": r"\b(?:adopted|enacted)\b",
    "announced": r"\bannounced\b",
    "proposed": r"\b(?:propos\w*|request\w*)\b",
}


AMOUNT_PREFIX = {
    "about": "About ",
    "at_most": "Up to ",
    "at_least": "At least ",
    "nearly": "Nearly ",
    "more_than": "More than ",
    "less_than": "Less than ",
}


def original_amount(record, annual):
    """The stated-currency annual figure beside its EUR conversion: " (USD 9.1m)"."""
    currency, stated = record.get("currency"), record.get("annual_amount")
    if not annual or not currency or currency == "EUR" or stated is None:
        return ""
    rounded = round2(stated, record.get("amount_qualifier"))
    return f" ({'~' if changed(stated, rounded) else ''}{currency} {money(rounded)})"


def display_annual_amount(record, rates):
    """Annual labels need primary inputs; missing inputs must not fall back to rounded exports."""
    amount = annual_eur_amount(record, rates)
    if amount is None:
        raise ValueError("annual display needs annual_amount")
    return amount


def allocation_year(annual_period, period=""):
    """The year of one allocation: "2026" for a calendar year, "2026-27" for a fiscal year, written "2026/27" when the period label is."""
    start, end = annual_period["start"][:4], annual_period["end"][:4]
    if start == end:
        return start
    separator = "/" if re.search(r"\d{4}/\d{2}\b", period) else "-"
    return f"{start}{separator}{end[2:]}"


def funding_label(record, annual=None, context=True, *, rates=None):
    """Structured amount and caveats; never extract facts from description prose."""
    annual = annual_estimate(record) if annual is None else annual
    amount = display_annual_amount(record, rates or {}) if annual else record.get("amount")
    bound = record.get("amount_qualifier")
    marker = "~" if annual and record.get("currency") != "EUR" else ""
    if amount is not None:
        rounded = round2(amount, bound)
        marker = marker or ("~" if changed(amount, rounded) else "")
        amount = rounded
    prefix = AMOUNT_PREFIX.get(record.get("amount_qualifier"), "")
    if bound == "about":
        marker = ""
    value = prefix + (
        marker
        + ("EUR " if annual else (record.get("currency") or "") + " ")
        + money(amount)
        + original_amount(record, annual)
        if amount is not None
        else "Not published"
    )
    qualifiers = []
    if annual:
        qualifiers.append(
            "a financial year"
            if "financial year" in (record.get("period") or "").lower()
            else "a year"
        )
        if record.get("annual_basis_code") == "period_mean":
            qualifiers.append("average over period")
    else:
        qualifiers.append(
            {
                "one_off": "one-off",
                "multi_year": "multi-year",
                "annual": "a financial year"
                if "financial year" in (record.get("period") or "").lower()
                else "a year",
            }.get(record.get("kind"), "")
        )
    period = record.get("period") or ""
    if annual and record.get("annual_basis_code") == "allocation_current_year" and record.get("annual_period"):
        # The figure is one year's allocation, not the whole profile's period, whose label is then not shown.
        qualifiers.append(allocation_year(record["annual_period"], period) + " allocation")
        period = ""
    elif period and not re.fullmatch(
        r"(?:per|a|each) (?:financial )?year|annual(?:ly)?", period, re.I
    ):
        qualifiers.append(period)
    # The commitment is shown unless the shown period label already states it ("2027 budget proposal", "FY2026 enacted").
    if record.get("commitment") in COMMITMENT_STATED and not re.search(
        COMMITMENT_STATED[record["commitment"]], period, re.I
    ):
        qualifiers.append(record["commitment"])
    # Figure source type, in plain words, only when the figure is not from a primary source.
    if context:
        qualifiers.append(figure_source(record))
    return value, [text for text in qualifiers if text]


def ratio_citations(funding, fc, jurisdiction, jn, cited, union, rates, path):
    """Evidence for the generated budget ratios: the compared figure's, then the denominators' sources.

    The exchange-rate evidence enters the share of GDP only when the budget and GDP currencies differ."""
    population = jurisdiction.get("population") or {}
    gdp = jurisdiction.get("gdp") or {}
    gdp_ids = union(cited(f"/jurisdictions/{jn}/gdp"), gdp.get("source_ids", []))
    if gdp.get("currency") == funding.get("currency"):
        share = union(fc["annual_amount"], cited(f"{path}/currency"), gdp_ids)
    else:
        rate = next((r for r in rates if r["currency"] == gdp.get("currency")), None)
        share = union(fc["eur_per_year_est"], gdp_ids, rate["citations"]["eur_per_unit"] if rate else [])
    denominators = funding.get("ratio_denominators", {})
    return {
        "eur_per_person_est": union(fc["eur_per_year_est"], population.get("source_ids", []))
        if funding.get("eur_per_person_est") is not None
        else [],
        "share_of_gdp_est": share if funding.get("share_of_gdp_est") is not None else [],
        "ratio_denominators": union(
            population.get("source_ids", []) if "population" in denominators else [],
            gdp_ids if "gdp" in denominators else [],
        ),
    }


def per_person_label(record, jurisdiction, rates):
    """The compared figure per person at two significant figures, from unrounded inputs, keeping the amount's qualifier.

    "~EUR 1.0 per person", "up to ~EUR 0.026 per person"; None without a population. ~ follows the
    funding label: a converted figure or one that rounding changed, except after "about"."""
    value = budget_ratios(record, jurisdiction, rates)["eur_per_person"]
    if value is None:
        return None
    bound = record.get("amount_qualifier")
    if value:
        rounded = value.quantize(Decimal(1).scaleb(value.adjusted() - 1), rounding=bound_rounding(bound))
        if rounded.adjusted() != value.adjusted():  # 9.96 rounds to 10.0: keep two figures
            rounded = rounded.quantize(Decimal(1).scaleb(rounded.adjusted() - 1))
    else:
        rounded = value
    marker = "~" if record.get("currency") != "EUR" or rounded != value else ""
    prefix = AMOUNT_PREFIX.get(bound, "").lower()
    if bound == "about":
        marker = ""
    return f"{prefix}{marker}EUR {format(rounded, ',f')} per person"


FIGURE_SOURCE = {"secondary_sources": "secondary sources", "press_report": "press report", "our_estimate": "our estimate"}


def figure_source(record) -> str:
    """The plain source-type label of a budget or staff figure; empty for a primary (high-confidence) figure."""
    if record.get("confidence") == "medium":
        return FIGURE_SOURCE["secondary_sources"]
    if record.get("confidence") == "low":
        return FIGURE_SOURCE[record["figure_source"]]
    return ""


UPPER_BOUNDS, LOWER_BOUNDS = {"at_most", "less_than", "nearly"}, {"at_least", "more_than"}


def median_bound(qualifiers):
    """The bound the counts-row median keeps (dataset.method budget-ratios): "at_most" when any input is an upper bound, "at_least" for a lower bound, else None.

    A median over upper bounds is itself an upper bound and rounds up; over lower bounds, a lower bound rounding down. With both no bound holds, and there is no rule for it."""
    upper, lower = bool(qualifiers & UPPER_BOUNDS), bool(qualifiers & LOWER_BOUNDS)
    if upper and lower:
        raise ValueError("median budget mixes upper and lower bounds; dataset.method has no rule for it")
    return "at_most" if upper else "at_least" if lower else None


def summary_figures(data):
    """Median and highest budget and staff figures for the counts row, from the sets the page already charts."""
    jurisdictions = {j["id"]: j for j in data.get("jurisdictions", [])}
    rates = {r["currency"]: r["eur_per_unit"] for r in data.get("dataset", {}).get("exchange_rates", [])}

    def summary(rows, text):
        # rows: (value, jurisdiction id, institute id, shown text of that value)
        if len(rows) < 2:
            return None
        top = max(rows, key=lambda r: (r[0], r[2]))
        return {
            "n": len(rows),
            "median": text(median(r[0] for r in rows)),
            "highest": top[3],
            "highest_jurisdiction_id": top[1],
        }

    budgets, staff, bounds = [], [], set()
    for institute in data.get("institutes", []):
        # The EU is not a state, so it is in none of the counts.
        if jurisdictions.get(institute["jurisdiction_id"], {}).get("kind") == "supranational":
            continue
        shown = institute["display"]
        record = shown["funding_records"].get(shown["comparison_funding"])
        if record and record.get("eur_per_year_est") is not None:
            value = display_annual_amount(record, rates)
            text = funding_label(record, annual=True, rates=rates)[0].split(" (")[0]
            if record.get("amount_qualifier") in AMOUNT_PREFIX:
                text = text[0].lower() + text[1:]
            budgets.append((value, institute["jurisdiction_id"], institute["id"], text))
            bounds.add(record.get("amount_qualifier"))
        record = shown["staff_records"].get(shown["total_staff"])
        if record and record.get("total") is not None:
            staff.append(
                (
                    record["total"],
                    institute["jurisdiction_id"],
                    institute["id"],
                    record["display"]["total"],
                )
            )
    bound = median_bound(bounds)
    return {
        "budget": summary(budgets, lambda v: AMOUNT_PREFIX.get(bound, "").lower() + "EUR " + money(round2(v, bound))),
        "headcount": summary(staff, lambda v: format(round2(v), ",")),
    }


def record_sources(record, skip=GENERIC_FIELDS):
    """All catalogue links, kept separate from field evidence.

    The generic population and region fields are skipped on the record itself only, never in nested records."""
    if isinstance(record, list):
        return [sid for item in record for sid in record_sources(item, ())]
    if isinstance(record, dict):
        return [
            sid
            for key, item in record.items()
            if key not in ("display", *skip, *DOWNLOAD_ONLY_FIELDS)
            for sid in (
                item
                if key == "source_ids"
                else [
                    sid
                    for field, ids in item.items()
                    if field not in DOWNLOAD_ONLY_FIELDS
                    for sid in ids
                ]
                if key == "citations"
                else record_sources(item, ())
            )
        ]
    return []


def first_sentence(text):
    text = str(text or "").strip()
    for match in re.finditer(r"[.!?][\"')\]]*(?=\s|$)", text):
        sentence = text[: match.end()]
        bare = re.sub(r"[\"')\]]+$", "", sentence)
        if re.search(
            r"\b(?:Mr|Mrs|Ms|Dr|Prof|St|e\.g|i\.e)\.$", bare, re.I
        ) or re.search(r"\b(?:[A-Za-z]\.)+$", bare):
            continue
        return sentence
    return text


def recent_developments(jurisdictions, union):
    groups = {}
    for jurisdiction in jurisdictions:
        for event in sorted(
            jurisdiction.get("timeline", []), key=lambda event: event["id"]
        ):
            if event["event"] not in DEVELOPMENT_EVENTS:
                continue
            key = (
                jurisdiction["id"],
                event["date"],
                None
                if re.fullmatch(r"\d{4}-\d{2}-\d{2}", event["date"])
                else event["id"],
            )
            if key not in groups:
                groups[key] = {
                    "jurisdiction": jurisdiction["id"],
                    "name": jurisdiction.get("name", jurisdiction["id"]),
                    "date": event["date"],
                    "events": [],
                    "texts": [],
                    "source_ids": [],
                    "order": event["id"],
                }
            group = groups[key]
            group["events"].append(event["id"])
            group["texts"].append(
                (event.get("short") or "").strip()
                or first_sentence(event.get("description"))
            )
            citations = event.get("citations", {})
            group["source_ids"] = union(
                group["source_ids"],
                *(
                    citations.get(k, [])
                    for k in ("description", "short", "date", "event")
                ),
            )
    # Same-day clauses and partial-date ties use stable event IDs.
    ordered = sorted(groups.values(), key=lambda g: (g["jurisdiction"], g["order"]))
    ordered.sort(key=lambda g: g["date"], reverse=True)
    for group in ordered:
        group["citations"] = {"display": list(group["source_ids"])}
    return ordered[:10]


MONTH_DAYS = Decimal("30.4375")  # average Gregorian month
ANNOUNCEMENT_WORD = {"commitment": "announcement", "proposed": "proposal"}


def interval_phrase(span, milestone):
    """Card wording for an exact interval only; a range stays in the downloads.

    Under 60 days in days, otherwise about N months (days / 30.4375, half up).
    Signed: a negative interval reads "before".
    """
    if span is None or span["days_min"] != span["days_max"]:
        return None
    days = span["days_min"]
    count = abs(days)
    if not days:
        return f"same day as {milestone}"
    if count < 60:
        amount = f"{count} day" if count == 1 else f"{count} days"
    else:
        months = int((Decimal(count) / MONTH_DAYS).quantize(Decimal(1), ROUND_HALF_UP))
        amount = f"about {months} months"
    return f"{amount} {'before' if days < 0 else 'after'} {milestone}"


def derive_display(published_data):
    """Return citations and selections for every renderer from one evidence map."""
    data = copy.deepcopy(published_data)
    source_order = {s["id"]: n for n, s in enumerate(data.get("sources", []))}
    evidence = {}
    for source in data.get("sources", []):
        for excerpt in source.get("excerpts", []):
            for pointer in excerpt["supports"]:
                if not isinstance(pointer, str) or not pointer.startswith("/"):
                    raise ValueError(
                        "derive_display requires resolved excerpt pointers"
                    )
                # Index ancestors once; field lookup then includes exact and
                # descendant bindings without scanning every excerpt again.
                parts = pointer.split("/")
                for end in range(2, len(parts) + 1):
                    evidence.setdefault("/".join(parts[:end]), set()).add(source["id"])

    def union(*groups):
        return sorted(
            {sid for group in groups for sid in group},
            key=lambda sid: (source_order.get(sid, len(source_order)), sid),
        )

    def cited(path):
        return union(evidence.get(path, ()))

    def walk(record, path=""):
        if isinstance(record, list):
            for n, value in enumerate(record):
                walk(value, f"{path}/{n}")
        elif isinstance(record, dict):
            fields = [k for k in record if k not in ("citations", "display")]
            for key in fields:
                walk(record[key], f"{path}/{key.replace('~', '~0').replace('/', '~1')}")
            if "source_ids" in record:
                citations = {
                    key: cited(f"{path}/{key.replace('~', '~0').replace('/', '~1')}")
                    for key in fields
                    if key not in ("source_ids", "identifier_source_ids")
                }
                citations["display"] = union(
                    *(
                        ids
                        for key, ids in citations.items()
                        if key not in NON_DISPLAY_FIELDS
                    )
                )
                record["citations"] = citations

    walk(data)
    jurisdictions = {
        j["id"]: (n, j) for n, j in enumerate(data.get("jurisdictions", []))
    }
    institutes = {i["id"]: i for i in data.get("institutes", [])}
    rates = data.get("dataset", {}).get("exchange_rates", [])
    rate_values = {r["currency"]: r["eur_per_unit"] for r in rates}
    for n, rate in enumerate(rates):
        rate.setdefault("citations", {})["eur_per_unit"] = cited(
            f"/dataset/exchange_rates/{n}/units_per_eur"
        )

    for n, institute in enumerate(data.get("institutes", [])):
        path = f"/institutes/{n}"
        citations = institute.setdefault("citations", {})
        citations["form"] = union(
            *(
                cited(f"{path}/{key}")
                for key in ("legal_form", "host", "legal_basis", "placement")
            )
        )
        citations["mandate"] = union(
            cited(f"{path}/mandate"), cited(f"{path}/mandate_short")
        )
        citations["inst"] = union(
            *(
                cited(f"{path}/{key}")
                for key in ("name", "short_name", "name_local", "acronym", "names")
            )
        )
        citations["institute"] = list(citations["inst"])
        jn, jurisdiction = jurisdictions[institute["jurisdiction_id"]]
        events = jurisdiction.get("timeline", [])
        designation = first_designation(events)
        # Interval citations: the dates of every record that could be either milestone.
        as_of = data.get("dataset", {}).get("as_of")
        found = milestones(institute, events, as_of)
        # Generated dates cite the timeline event they are read from.
        announced = announcement(events, as_of)
        citations["announced"] = (
            cited(f"/jurisdictions/{jn}/timeline/{announced}/date")
            if announced is not None
            else []
        )
        citations["established"] = (
            cited(f"/jurisdictions/{jn}/timeline/{designation}/date")
            if designation is not None
            else []
        )
        for name in institute.get("names", []):
            act = naming_act(events, name)
            name["citations"]["from"] = cited(f"/jurisdictions/{jn}/timeline/{act}/date")
        milestone_ids = {
            name: union(*(cited(f"/jurisdictions/{jn}/timeline/{k}/date") for k in found[name][1]))
            for name in ("announced", "established")
        }
        milestone_ids["first_qualifying_activity"] = union(
            *(
                cited(f"{path}/outputs/{k}/credit/{field}")
                for k in found["first_qualifying_activity"][1]
                for field in ("institute_id", "date")
            )
        )
        spans = institute.get("intervals") or {}
        for key in INTERVAL_KEYS:
            start, end = key.split("_to_")
            citations[key] = (
                union(milestone_ids[start], milestone_ids[end])
                if spans.get(key) is not None
                else []
            )
        for activity in institute.get("operating", []):
            ids = list(activity["source_ids"])
            activity["citations"] = {"date": ids, "kind": ids, "display": ids}
        for event in events:
            if event["event"] == "first_output":
                ids = list(event["source_ids"])
                event["citations"] = {
                    key: ids
                    for key in ("event", "date", "short", "description", "display")
                }
        funding_rows = institute.get("funding", [])

        def amount_sources(index, field, seen=()):
            if index in seen:
                raise ValueError("funding: cyclic sum_of citation dependency")
            row = funding_rows[index]
            direct = cited(f"{path}/funding/{index}/{field}")
            if "sum_of" in row:
                return union(
                    direct,
                    *(
                        amount_sources(child, field, (*seen, index))
                        for child in component_indices(funding_rows, index)
                    ),
                )
            if (
                field == "annual_amount"
                and row.get("annual_basis_code") == "period_mean"
            ):
                return union(
                    direct,
                    *(
                        cited(f"{path}/funding/{index}/{key}")
                        for key in ("amount", "years", "period")
                    ),
                )
            if (
                field == "annual_amount"
                and row.get("annual_basis_code") == "allocation_current_year"
            ):
                return union(
                    direct,
                    *(
                        cited(f"{path}/funding/{index}/annual_profile/{pn}/amount")
                        for pn, allocation in enumerate(row.get("annual_profile", []))
                        if allocation["start"]
                        <= data["dataset"]["as_of"]
                        <= allocation["end"]
                    ),
                )
            return direct

        for fn, funding in enumerate(funding_rows):
            funding["display"] = {"annual_estimate": annual_estimate(funding)}
            fc = funding.setdefault("citations", {})
            rate = next(
                (r for r in rates if r["currency"] == funding.get("currency")), None
            )
            for field in ("amount", "annual_amount"):
                fc[field] = amount_sources(fn, field)
            if funding.get(
                "annual_basis_code"
            ) == "allocation_current_year" and funding.get("annual_profile"):
                fc["annual_period"] = union(
                    *(
                        cited(f"{path}/funding/{fn}/annual_profile/{pn}/{key}")
                        for pn, allocation in enumerate(funding["annual_profile"])
                        if allocation["start"]
                        <= data["dataset"]["as_of"]
                        <= allocation["end"]
                        for key in ("start", "end")
                    )
                )
            fc["eur_per_year_est"] = (
                union(
                    fc["annual_amount"],
                    cited(f"{path}/funding/{fn}/currency"),
                    rate["citations"]["eur_per_unit"]
                    if rate and funding.get("currency") != "EUR"
                    else [],
                )
                if funding.get("eur_per_year_est") is not None
                else []
            )
            if "eur_per_person_est" in funding:
                fc.update(ratio_citations(funding, fc, jurisdiction, jn, cited, union, rates, f"{path}/funding/{fn}"))
        comparison, budget = funding_selection(
            institute.get("funding", []), data["dataset"]["as_of"]
        )
        staff, total, technical = staff_selection(institute.get("staff", []))
        funding_ids = sorted((row["id"] for row in funding_rows))
        funding_by_id = {row["id"]: row for row in funding_rows}
        funding_ids.sort(
            key=lambda identity: funding_date(funding_by_id[identity]), reverse=True
        )
        staff_rows = institute.get("staff", [])
        for row in staff_rows:
            row["display"] = {
                "total": staff_figure(row["total"], row.get("total_qualifier"))
                if row.get("total") is not None
                else None,
                "technical": staff_figure(row["technical"], row.get("technical_qualifier"))
                if row.get("technical") is not None
                else None,
                "target": staff_figure(row["target"])
                if row.get("target") is not None
                else None,
            }
        staff_by_id = {row["id"]: row for row in staff_rows}
        staff_ids = sorted(staff_by_id)
        staff_ids.sort(
            key=lambda identity: staff_by_id[identity].get("as_of", ""), reverse=True
        )
        operating = institute.get("operating")
        if operating is not None:
            items = sorted(
                operating,
                key=lambda item: (item["date"], item.get("kind", ""), item["lineage"]),
            )
            first = next(iter(items), None)
            operating = {
                "since": first["date"] if first else None,
                "first_lineage": first["lineage"] if first else None,
                "items": items,
            }
        known = None
        phrase = interval_phrase(
            spans.get("established_to_first_qualifying_activity"), "establishment"
        )
        first_activity = found["first_qualifying_activity"][0]
        if phrase and operating:
            day = first_activity[0].isoformat()
            item = next((x for x in operating["items"] if x["date"] == day), None)
            if item is not None:
                known = {"kind": item["kind"], "lineage": item["lineage"], "text": phrase}
        # Under Status since only while that date is the establishment: no closure has reset it.
        since = None
        if found["announced"][1] and not any(e["event"] == "closed" for e in events):
            words = {ANNOUNCEMENT_WORD[events[k]["event"]] for k in found["announced"][1]}
            since = interval_phrase(
                spans.get("announced_to_established"),
                words.pop() if len(words) == 1 else "announcement",
            )
        institute["display"] = {
            "intervals": {"status_since": since, "known_activity": known},
            "comparison_funding": funding_rows[comparison]["id"]
            if comparison is not None
            else None,
            "budget": funding_rows[budget]["id"] if budget is not None else None,
            "staff": staff_rows[staff]["id"] if staff is not None else None,
            "total_staff": staff_rows[total]["id"] if total is not None else None,
            "technical_staff": staff_rows[technical]["id"]
            if technical is not None
            else None,
            "funding_records": funding_by_id,
            "funding_ids": funding_ids,
            "staff_records": staff_by_id,
            "staff_ids": staff_ids,
            "related_funding_ids": [
                identity
                for identity in funding_ids
                if funding_by_id[identity].get("coverage") == "not_institute_specific"
            ],
            "operating": operating,
        }
        rows = row_display(institute, data.get("dataset", {}).get("vocabularies", {}))
        if rows is not None:
            institute["display"]["rows"] = rows

    for jn, jurisdiction in jurisdictions.values():
        path = f"/jurisdictions/{jn}"
        citations = jurisdiction.setdefault("citations", {})
        events = jurisdiction.get("timeline", [])
        _, _, basis = status_basis(events)
        status_ids = (
            union(
                *(
                    cited(f"{path}/timeline/{basis}/{key}")
                    for key in ("date", "event", "withdraws")
                )
            )
            if basis is not None
            else []
        )
        citations["since"] = (
            cited(f"{path}/timeline/{basis}/date")
            if basis is not None and jurisdiction.get("status_since")
            else []
        )
        citations["status_since"] = list(citations["since"])
        citations["status"] = status_ids
        citations["finding"] = cited(f"{path}/summary")
        institute = institutes.get(jurisdiction.get("institute_id"))
        citations["inst"] = institute["citations"]["inst"] if institute else []
        if institute:
            institute["citations"]["status"] = list(citations["status"])
        population = jurisdiction.get("population", {}).get("value")
        jurisdiction["display"] = {
            "small_state": population is not None and population < 10_000_000,
            "advisory": any(
                e["date"] == jurisdiction.get("status_since")
                and e.get("instrument") == "advisory_report"
                for e in jurisdiction.get("timeline", [])
            ),
        }

    def refresh_display(record):
        if isinstance(record, list):
            for child in record:
                refresh_display(child)
        elif isinstance(record, dict):
            for key, child in record.items():
                if key not in ("citations", "display"):
                    refresh_display(child)
            if "citations" in record:
                citations = record["citations"]
                citations["display"] = union(
                    *(
                        ids
                        for key, ids in citations.items()
                        if key not in NON_DISPLAY_FIELDS
                        and (
                            key != "eur_per_year_est"
                            or record.get("display", {}).get("annual_estimate", False)
                        )
                    )
                )

    refresh_display(data)
    for institute in data.get("institutes", []):
        institute["display"]["source_links"] = union(record_sources(institute))
        for row in institute.get("funding", []):
            comparison = institute["display"]["comparison_funding"] == row["id"]
            value, qualifiers = funding_label(
                row,
                annual=comparison,
                rates=rate_values,
            )
            native_value, native_qualifiers = funding_label(
                row, annual=False, context=False
            )
            fields = (
                ["eur_per_year_est", "annual_basis_code"]
                if comparison
                else ["amount", "currency"]
            )
            fields += ["amount_qualifier", "period", "commitment", "kind", "confidence"]
            if comparison and row.get("eur_per_person_est") is not None:
                row["display"]["per_person"] = per_person_label(
                    row, jurisdictions[institute["jurisdiction_id"]][1], rate_values
                )
            row["display"].update(
                value=value,
                qualifiers=qualifiers,
                native_value=native_value,
                native_qualifiers=native_qualifiers,
                budget_citations=union(
                    *(row["citations"].get(field, []) for field in fields)
                ),
            )
    for jurisdiction in data.get("jurisdictions", []):
        jurisdiction["display"]["source_links"] = union(record_sources(jurisdiction))
        # A related body's or closest candidate's own sources, for the marker after its entry (catalogue links, not field evidence).
        for body in [*jurisdiction.get("related_bodies", []), *([jurisdiction["closest_body"]] if "closest_body" in jurisdiction else [])]:
            body["display"] = {"source_links": union(body.get("source_ids", []))}
    for overview in data.get("dataset", {}).get("other_overviews", []):
        overview["display"] = {"source_links": list(overview["source_ids"])}
    metadata = data.setdefault("dataset", {})
    changes = sorted(
        data.get("changelog", []),
        key=lambda row: (
            row["date"],
            tuple(int(part) for part in row["version"].split(".")),
        ),
        reverse=True,
    )
    metadata["display"] = {
        "developments": recent_developments(data.get("jurisdictions", []), union),
        "maintainers": sorted(
            metadata.get("maintainers", []),
            key=lambda person: (person["name"], person.get("affiliation", "")),
        ),
        "changelog": changes,
        "latest_change": next(iter(changes), None),
        "counts": summary_figures(data),
        "rule_labels": {rule["id"]: rule_label(rule) for rule in metadata.get("method", [])},
        "generic_sources": union(
            [
                sid
                for j in data.get("jurisdictions", [])
                for field in GENERIC_FIELDS
                for sid in (j.get(field) or {}).get("source_ids", [])
            ]
        ),
    }
    return data
