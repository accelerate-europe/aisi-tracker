"""Pure authored-fact materialisation, before download pointer resolution.

Editorial output qualification remains authored. Legacy repeated leaves are checked
until migration; strict mode forbids them. Partial-date event order is preserved.
"""

import copy
import math
from datetime import date, timedelta
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP, localcontext
from functools import cmp_to_key

if __package__:
    from .flag_checks import access_summary
else:
    from flag_checks import access_summary  # type: ignore[import-not-found, no-redef]

AUTHORING_STRICT = True  # Production accepts primary facts and stable bindings only.

IMPLIES = {
    "exploration_started": "exploring",
    "proposed": "proposed",
    "commitment": "planned",
    "designated": "established",
}
RANK = {
    "none_identified": 0,
    "exploring": 1,
    "proposed": 2,
    "planned": 3,
    "established": 4,
}


def output_activity_kind(output: dict) -> str | None:
    """Apply the reviewed output decision; type or title alone cannot establish it.

    The qualification policy is in dataset.method and the output schema. Every
    item records qualifies_as and its reason, including explicit exclusions.
    """
    return {
        "published_work": "published_work",
        "joint_testing": "joint_testing",
        "none": None,
    }[output["qualifies_as"]]


def known_activity(institute: dict) -> list[dict]:
    """Earliest reviewed output per kind, using its first institute-credit date.

    Unpadded ISO strings preserve precision: 2025 precedes 2025-03 as a
    presentation convention, not a claim about overlapping date intervals.
    Unique lineage keys break item ties; lexical kind order breaks clause ties.
    """
    earliest = {}
    for output in sorted(
        institute.get("outputs", []),
        key=lambda o: (credit_date(o), o["lineage"]),
    ):
        kind = output_activity_kind(output)
        if kind and kind not in earliest:
            earliest[kind] = {
                "kind": kind,
                "date": credit_date(output),
                "lineage": output["lineage"],
                "source_ids": list(output["source_ids"]),
            }
    return sorted(earliest.values(), key=lambda e: (e["date"], e["kind"]))


def status_basis(events: list[dict]) -> tuple[str, str | None, int | None]:
    """Current status, date and source act in the authored event order."""
    withdrawn = {e.get("withdraws") for e in events if e["event"] == "withdrawn"}
    status, since, basis = "none_identified", None, None
    for n, e in enumerate(events):
        if e["id"] in withdrawn:
            continue
        if e["event"] == "closed":
            status, since, basis = "none_identified", None, n
            continue
        new = IMPLIES.get(e["event"])
        if new and RANK[new] > RANK[status]:
            status, since, basis = new, e["date"], n
    if basis is None:
        basis = next(
            (
                n
                for n in reversed(range(len(events)))
                if events[n]["event"] == "withdrawn"
            ),
            None,
        )
    return status, since, basis


def first_designation(events: list[dict]) -> int | None:
    """First surviving designation; closure preserves the historical date."""
    withdrawn = {event.get("withdraws") for event in events if event["event"] == "withdrawn"}
    return next(
        (n for n, event in enumerate(events)
         if event["event"] == "designated" and event["id"] not in withdrawn),
        None,
    )


def timeline_status(events: list[dict]) -> tuple[str, str | None]:
    """Withdrawn acts drop out; closure resets the uninterrupted status run."""
    status, since, _ = status_basis(events)
    return status, since


def significant(value: Decimal, digits: int, rounding=ROUND_HALF_UP) -> int | float:
    """Round decimal arithmetic once, half up unless told otherwise; retain zero and integral results."""
    if not value.is_finite() or value < 0:
        raise ValueError("expected a finite non-negative amount")
    if not value:
        return 0
    rounded = value.quantize(
        Decimal(1).scaleb(value.adjusted() - digits + 1), rounding=rounding
    )
    return int(rounded) if rounded == rounded.to_integral_value() else float(rounded)


def eur_per_unit(units_per_eur: int | float) -> int | float:
    """EUR per unit of a currency from its ECB units-per-euro rate, stored at 12 significant figures."""
    with localcontext() as context:
        context.prec = 50
        return significant(Decimal(1) / Decimal(str(units_per_eur)), 12)


def exact_annual_amount(funding: dict) -> Decimal | None:
    """The annual amount in its own currency before the float export: a period mean
    is amount / years in decimal arithmetic, as materialise_facts derives it."""
    with localcontext() as context:
        context.prec = 50
        if (
            funding.get("annual_basis_code") == "period_mean"
            and "sum_of" not in funding
            and funding.get("amount") is not None
            and funding.get("years")
        ):
            return Decimal(str(funding["amount"])) / Decimal(str(funding["years"]))
        amount = funding.get("annual_amount")
        return Decimal(str(amount)) if amount is not None else None


def annual_eur_amount(funding: dict, rates: dict) -> Decimal | None:
    """Unrounded annual conversion, shared by display, the six-figure export and the budget ratios."""
    amount = exact_annual_amount(funding)
    if amount is None:
        return None
    currency = funding["currency"]
    if currency != "EUR" and currency not in rates:
        raise ValueError(f"no exchange rate for {currency}")
    rate = 1 if currency == "EUR" else rates[currency]
    with localcontext() as context:
        context.prec = 50
        return amount * Decimal(str(rate))


def annual_eur_estimate(funding: dict, rates: dict) -> int | float | None:
    """Six-significant-figure download precision, never an input to display rounding.

    A stated bound rounds outward so it stays true (bound_rounding), as the budget ratios do."""
    amount = annual_eur_amount(funding, rates)
    with localcontext() as context:
        context.prec = 50
        return (
            significant(amount, 6, bound_rounding(funding.get("amount_qualifier")))
            if amount is not None
            else None
        )


# Qualifiers that state a bound round the way that keeps the bound true: "125+" shows as "120+", "up to 125" as "130".
LOWER_BOUND = ("at_least", "more_than")
UPPER_BOUND = ("at_most", "less_than", "nearly")


def annual_estimate(funding):
    value = funding.get("eur_per_year_est")
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and funding.get("kind") in ("annual", "multi_year")
    )


def comparable_funding(records, as_of):
    """Return the selected array index: committed periods overlapping as_of's year."""
    year = as_of[:4]
    candidates = [
        n
        for n, f in enumerate(records)
        if annual_estimate(f)
        and f.get("coverage") == "whole_institute"
        and f.get("annual_period")
        and f["annual_period"]["start"] <= f"{year}-12-31"
        and f["annual_period"]["end"] >= f"{year}-01-01"
        and (not f.get("decision_date") or f["decision_date"] <= as_of)
        and f.get("commitment") in ("adopted", "announced")
    ]

    def rank(f):
        period = f["annual_period"]
        return 0 if period["start"] > as_of else 1 if period["end"] < as_of else 2

    def compare(a, b):
        return (a > b) - (a < b)

    def order(an, bn):
        a, b = records[an], records[bn]
        ar, br = rank(a), rank(b)
        if ar != br:
            return br - ar
        if ar == 2 and (a.get("annual_basis_code") == "period_mean") != (
            b.get("annual_basis_code") == "period_mean"
        ):
            return 1 if a.get("annual_basis_code") == "period_mean" else -1
        period_order = (
            compare(a["annual_period"]["start"], b["annual_period"]["start"])
            if ar == 0
            else compare(b["annual_period"]["end"], a["annual_period"]["end"])
        )
        if period_order:
            return period_order
        if (a["commitment"] == "adopted") != (b["commitment"] == "adopted"):
            return -1 if a["commitment"] == "adopted" else 1
        return compare(
            b.get("decision_date", ""), a.get("decision_date", "")
        ) or compare(a["id"], b["id"])

    return min(candidates, key=cmp_to_key(order), default=None)


def bound_rounding(qualifier: str | None):
    """Rounding that keeps a stated bound true: down for a lower bound, up for an upper bound, else half up."""
    return (
        ROUND_FLOOR
        if qualifier in LOWER_BOUND
        else ROUND_CEILING
        if qualifier in UPPER_BOUND
        else ROUND_HALF_UP
    )


def budget_ratios(funding: dict, jurisdiction: dict, rates: dict) -> dict:
    """Unrounded per-person and share-of-GDP ratios of one annual figure; None where an input is missing.

    Per person: the annual figure in EUR over the recorded population. Share of
    GDP: the annual figure over GDP in the same currency, so the exchange rate
    never enters when the budget and GDP currencies agree; otherwise both are
    converted to EUR at the same rates. Non-positive denominators and GDP
    currencies without a rate give None. Rule in dataset.method (budget-ratios).
    """
    with localcontext() as context:
        context.prec = 50
        amount = exact_annual_amount(funding)
        eur = annual_eur_amount(funding, rates)
        population = (jurisdiction.get("population") or {}).get("value")
        per_person = (
            eur / Decimal(str(population))
            if eur is not None and population is not None and population > 0
            else None
        )
        gdp = jurisdiction.get("gdp") or {}
        share = None
        if amount is not None and gdp.get("value") is not None and gdp["value"] > 0:
            if gdp.get("currency") == funding.get("currency"):
                share = amount / Decimal(str(gdp["value"]))
            elif gdp.get("currency") == "EUR" or gdp.get("currency") in rates:
                rate = 1 if gdp["currency"] == "EUR" else rates[gdp["currency"]]
                share = eur / (Decimal(str(gdp["value"])) * Decimal(str(rate)))
        return {"eur_per_person": per_person, "share_of_gdp": share}


def ratio_denominators(jurisdiction: dict) -> dict:
    """The recorded denominators a ratio used, with their years; sources are in the record's citations."""
    result = {}
    if (jurisdiction.get("population") or {}).get("value") is not None:
        result["population"] = {key: jurisdiction["population"][key] for key in ("value", "year")}
    if (jurisdiction.get("gdp") or {}).get("value") is not None:
        result["gdp"] = {key: jurisdiction["gdp"][key] for key in ("value", "currency", "year")}
    return result


def first_output(institute: dict) -> dict | None:
    """Earliest institutional credit, including editorially excluded outputs."""
    return min(
        institute.get("outputs", []),
        key=lambda output: (
            credit_date(output),
            output["lineage"],
        ),
        default=None,
    )


ANNOUNCING_EVENTS = ("commitment", "proposed")
RATIO_FIELDS = ("eur_per_person_est", "share_of_gdp_est", "ratio_denominators")
INTERVAL_KEYS = (
    "announced_to_established",
    "established_to_first_qualifying_activity",
    "announced_to_first_qualifying_activity",
)


def date_bounds(day: str, as_of: str | None) -> tuple[date, date]:
    """First and last calendar day a recorded date can mean, never a midpoint.

    A month-only or year-only date ends no later than the dataset date: an act
    recorded on as_of happened by then. Full dates are kept as recorded.
    """
    parts = [int(part) for part in day.split("-")]
    if len(parts) == 3:
        return date(*parts), date(*parts)
    if len(parts) == 2:
        year, month = parts
        first = date(year, month, 1)
        last = date(year + month // 12, month % 12 + 1, 1) - timedelta(days=1)
    else:
        first, last = date(parts[0], 1, 1), date(parts[0], 12, 31)
    if as_of:
        last = max(first, min(last, date.fromisoformat(as_of)))
    return first, last


def earliest(dated: list[tuple[int, str]], as_of: str | None) -> tuple[tuple[date, date] | None, list[int]]:
    """Bounds of the earliest of several recorded dates, and the records that could be it.

    From the earliest day any date can mean to the earliest day by which one of
    them certainly happened, so mixed precision never hides an earlier full date.
    Candidates are the records whose first possible day is not after that bound,
    in the given order. The result does not depend on that order.
    """
    bounds = [(n, date_bounds(day, as_of)) for n, day in dated]
    if not bounds:
        return None, []
    certain = min(last for _, (_, last) in bounds)
    lowest = min(first for _, (first, _) in bounds)
    return (lowest, certain), [n for n, (first, _) in bounds if first <= certain]


def announcing(events: list[dict]) -> list[tuple[int, str]]:
    """Commitment and proposal events in timeline order, withdrawn acts included.

    The one reading of the announcement milestone: the intervals and the
    generated institutes[].announced both start here. A designation is never one.
    """
    return [(n, e["date"]) for n, e in enumerate(events) if e["event"] in ANNOUNCING_EVENTS]


def announcement(events: list[dict], as_of: str | None) -> int | None:
    """Event read as the generated announced date: the first, in timeline order,
    of the records that could mark the announcement milestone.

    Date order of the timeline makes that the first commitment or proposal event.
    """
    _, candidates = earliest(announcing(events), as_of)
    return candidates[0] if candidates else None


def naming_act(events: list[dict], name: dict) -> int | None:
    """Timeline event that gave a name: the one its `event` names by permanent ID.

    The generated names[].from is that event's date; None when the ID is not
    in the timeline.
    """
    return next(
        (n for n, e in enumerate(events) if e["id"] == name.get("event")), None
    )


def milestones(institute: dict, events: list[dict], as_of: str | None) -> dict:
    """Announcement, establishment and first qualifying activity: bounds and candidates.

    Announcement: commitment and proposal events (a designation is never read as
    one). Establishment: designations whose act has not been withdrawn. First
    qualifying activity: institute-credit dates of outputs qualifying as Known
    activity. Candidates index the timeline or the output catalogue.
    """
    withdrawn = {e.get("withdraws") for e in events if e["event"] == "withdrawn"}
    return {
        "announced": earliest(announcing(events), as_of),
        "established": earliest(
            [
                (n, e["date"])
                for n, e in enumerate(events)
                if e["event"] == "designated" and e["id"] not in withdrawn
            ],
            as_of,
        ),
        "first_qualifying_activity": earliest(
            [
                (n, credit_date(output))
                for n, output in enumerate(institute.get("outputs", []))
                if output_activity_kind(output)
            ],
            as_of,
        ),
    }


def interval(start: tuple[date, date] | None, end: tuple[date, date] | None) -> dict | None:
    """Signed whole days from start to end, as a range over both dates' readings."""
    if start is None or end is None:
        return None
    return {
        "days_min": (end[0] - start[1]).days,
        "days_max": (end[1] - start[0]).days,
    }


def intervals(institute: dict, events: list[dict], as_of: str | None) -> dict:
    """Intervals between milestones; the activity intervals only while established."""
    found = milestones(institute, events, as_of)
    announced, established = found["announced"][0], found["established"][0]
    activity = (
        found["first_qualifying_activity"][0]
        if institute["status"] == "established"
        else None
    )
    return dict(
        zip(
            INTERVAL_KEYS,
            (
                interval(announced, established),
                interval(established, activity),
                interval(announced, activity),
            ),
        )
    )


def credit_date(output: dict) -> str:
    """Explicit institutional credit; legacy fallback exists only before migration."""
    return (
        output["credit"]["date"]
        if "credit" in output
        else output.get("credited_from", output["date"])
    )


def prepared_facts(data: dict, *, strict: bool | None = None) -> dict:
    """Resolve references after derivation, without generated display metadata."""
    if strict is None:
        strict = AUTHORING_STRICT
    if __package__:
        from .references import materialise_references
    else:
        from references import materialise_references
    legacy = any(
        isinstance(ref, str)
        for source in data.get("sources", [])
        for excerpt in source.get("excerpts", [])
        for ref in excerpt["supports"]
    )
    if strict and legacy:
        raise ValueError("authored evidence requires stable selectors")
    source = data
    if legacy:
        if __package__:
            from .migrate_references import migrate
        else:
            from migrate_references import migrate
        source = migrate(data)
    result = materialise_references(materialise_facts(source, strict=strict))
    assign_credit_provenance(result)
    return result


def publish(data: dict, *, strict: bool | None = None) -> dict:
    """The sole authored-to-download boundary, shared by every consumer."""
    if __package__:
        from .display import derive_display
    else:
        from display import derive_display
    return derive_display(prepared_facts(data, strict=strict))


def assign_credit_provenance(data: dict) -> None:
    """Bind generated fact provenance after reference resolution, before checks."""
    evidence = {}
    for source in data.get("sources", []):
        for excerpt in source.get("excerpts", []):
            for pointer in excerpt["supports"]:
                evidence.setdefault(pointer, set()).add(source["id"])
    source_order = {s["id"]: n for n, s in enumerate(data.get("sources", []))}
    jurisdictions = {j["id"]: j for j in data["jurisdictions"]}
    for index, institute in enumerate(data["institutes"]):
        by_lineage = {}
        for number, output in enumerate(institute.get("outputs", [])):
            if "credit" not in output:
                by_lineage[output["lineage"]] = []
                continue
            prefix = f"/institutes/{index}/outputs/{number}/credit"
            ids = set().union(*(evidence.get(f"{prefix}/{field}", set())
                                for field in ("institute_id", "date")))
            by_lineage[output["lineage"]] = sorted(ids, key=source_order.__getitem__)
        for activity in institute.get("operating", []):
            activity["source_ids"] = list(by_lineage.get(activity["lineage"], []))
        for event in jurisdictions[institute["jurisdiction_id"]].get("timeline", []):
            if event["event"] == "first_output":
                event["source_ids"] = list(by_lineage.get(event.get("output_lineage"), []))


def component_indices(records: list[dict], index: int) -> list[int]:
    """Resolve funding components by authored identity (legacy indices accepted)."""
    result = []
    for key in records[index]["sum_of"]:
        matches = (
            [n for n, row in enumerate(records) if row.get("id") == key]
            if isinstance(key, str)
            else [key]
        )
        if (
            len(matches) != 1
            or type(matches[0]) is not int
            or not 0 <= matches[0] < len(records)
        ):
            raise ValueError("funding: invalid sum_of component identity")
        result.append(matches[0])
    if len(result) < 2 or len(set(result)) != len(result) or index in result:
        raise ValueError("funding: invalid sum_of component indices")
    return result


def current_allocation(funding: dict, as_of: str) -> int:
    matches = [
        n
        for n, p in enumerate(funding["annual_profile"])
        if p["start"] <= as_of <= p["end"]
    ]
    if len(matches) != 1:
        raise ValueError(
            "funding: current fiscal profile must have exactly one allocation covering dataset.as_of"
        )
    return matches[0]


def materialise_facts(data: dict, *, strict: bool = False) -> dict:
    """Copy primary facts and derive repetitions without resolving selectors.

    Transitional inputs may retain derived fields but disagreement fails. Missing
    optional legacy fields are not disagreements. First-output prose is replaced
    by standard copy; its stored date and lineage still have to agree. Strict mode
    rejects every authored repeated field and first-output event.
    """
    result = copy.deepcopy(data)
    errors = []

    def derived(record, key, value, where, *, optional=False):
        if key in record:
            if strict:
                errors.append(f"{where}/{key}: derived field must not be authored")
            elif record[key] != value:
                errors.append(
                    f"{where}/{key}: stored {record[key]!r} disagrees with derived {value!r}"
                )
        if value is None and optional:
            record.pop(key, None)
        else:
            record[key] = value

    rate_rows = result.get("dataset", {}).get("exchange_rates", [])
    rates = {}
    for row in rate_rows:
        rate = eur_per_unit(row["units_per_eur"])
        derived(row, "eur_per_unit", rate, f"exchange_rates/{row['currency']}")
        rates[row["currency"]] = rate

    jurisdictions = {j["id"]: j for j in result.get("jurisdictions", [])}
    for jurisdiction in jurisdictions.values():
        where = f"jurisdictions/{jurisdiction['id']}"
        status, since = timeline_status(jurisdiction.get("timeline", []))
        derived(jurisdiction, "status", status, where)
        derived(jurisdiction, "status_since", since, where, optional=True)

    for institute in result.get("institutes", []):
        where = f"institutes/{institute['id']}"
        jurisdiction = jurisdictions[institute["jurisdiction_id"]]
        timeline = jurisdiction.get("timeline", [])
        derived(institute, "status", jurisdiction["status"], where)
        designation = first_designation(timeline)
        established = timeline[designation]["date"] if designation is not None else None
        for output in institute.get("outputs", []):
            credit = output.get("credit")
            if strict and credit is None:
                errors.append(
                    f"{where}/outputs/{output['lineage']}: explicit institutional credit is required"
                )
            if credit is not None:
                if credit.get("institute_id") != institute["id"]:
                    errors.append(
                        f"{where}/outputs/{output['lineage']}: credit institute must match its institute"
                    )
                date = credit.get("date")
                if not date:
                    errors.append(
                        f"{where}/outputs/{output['lineage']}: credit date is required"
                    )
                    continue
                precision = min(len(date), len(output["date"]))
                if date[:precision] < output["date"][:precision]:
                    errors.append(
                        f"{where}/outputs/{output['lineage']}: institutional credit cannot precede lineage publication"
                    )
                had_legacy_credit = "credited_from" in output
                derived(
                    output,
                    "credited_from",
                    date,
                    f"{where}/outputs/{output['lineage']}",
                )
                if date == output["date"] and not had_legacy_credit:
                    output.pop("credited_from", None)
        derived(institute, "established", established, where, optional=True)
        announced = announcement(timeline, result.get("dataset", {}).get("as_of"))
        derived(
            institute,
            "announced",
            timeline[announced]["date"] if announced is not None else None,
            where,
        )
        for name in institute.get("names", []):
            nwhere = f"{where}/names/{name.get('id', name['name'])}"
            act = naming_act(timeline, name)
            if act is None:
                errors.append(
                    f"{nwhere}: event {name.get('event')!r} is not in the "
                    f"{jurisdiction['id']} timeline"
                )
                continue
            derived(name, "from", timeline[act]["date"], nwhere)
        if "model_access" in institute and "as_of" in result.get("dataset", {}):
            summary = access_summary(institute["model_access"], result["dataset"]["as_of"])
            for key, value in summary.items():
                derived(institute["model_access"], key, value, f"{where}/model_access", optional=True)
        records = institute.get("funding", [])
        finished = set()

        def funding_values(n, visiting=()):
            if n in finished:
                return
            if n in visiting:
                raise ValueError("funding: cyclic sum_of components")
            funding = records[n]
            fwhere = f"{where}/funding/{n}"
            if "sum_of" in funding:
                indices = component_indices(records, n)
                for child in indices:
                    if "amount_qualifier" in records[child]:
                        raise ValueError(
                            f"{institute['id']} funding {funding.get('id', n)}: "
                            "sum_of cannot combine qualified component "
                            f"{records[child].get('id', child)}"
                        )
                    funding_values(child, (*visiting, n))
                for key in ("amount", "annual_amount"):
                    try:
                        total = sum(
                            Decimal(str(records[child][key])) for child in indices
                        )
                    except (KeyError, ValueError, ArithmeticError) as exc:
                        raise ValueError(
                            f"{fwhere}: sum components require numeric {key}"
                        ) from exc
                    value = (
                        int(total)
                        if total == total.to_integral_value()
                        else float(total)
                    )
                    derived(funding, key, value, fwhere)
            elif funding.get("annual_basis_code") == "period_mean":
                amount, years = funding.get("amount"), funding.get("years")
                if amount is None or not years or years <= 0:
                    raise ValueError(
                        f"{fwhere}: period_mean requires amount and positive years"
                    )
                value = Decimal(str(amount)) / Decimal(str(years))
                derived(
                    funding,
                    "annual_amount",
                    int(value) if value == value.to_integral_value() else float(value),
                    fwhere,
                )
            elif funding.get(
                "annual_basis_code"
            ) == "allocation_current_year" and funding.get("annual_profile"):
                allocation = funding["annual_profile"][
                    current_allocation(funding, result["dataset"]["as_of"])
                ]
                derived(funding, "annual_amount", allocation["amount"], fwhere)
                derived(
                    funding,
                    "annual_period",
                    {key: allocation[key] for key in ("start", "end")},
                    fwhere,
                )
            finished.add(n)

        for n in range(len(records)):
            funding_values(n)
        for n, funding in enumerate(records):
            derived(
                funding,
                "eur_per_year_est",
                annual_eur_estimate(funding, rates),
                f"{where}/funding/{n}",
            )
        # Ratios belong to a state's compared figure only (rule budget-ratios); the
        # selection is the one display.funding_selection uses.
        as_of = result.get("dataset", {}).get("as_of")
        compared = (
            comparable_funding(records, as_of)
            if as_of and jurisdiction.get("kind") == "state"
            else None
        )
        for n, funding in enumerate(records):
            fwhere = f"{where}/funding/{n}"
            if n != compared:
                for key in RATIO_FIELDS:
                    if key in funding:
                        errors.append(f"{fwhere}/{key}: generated for the compared figure only")
                continue
            ratios = budget_ratios(funding, jurisdiction, rates)
            rounding = bound_rounding(funding.get("amount_qualifier"))
            for key, value in (
                ("eur_per_person_est", ratios["eur_per_person"]),
                ("share_of_gdp_est", ratios["share_of_gdp"]),
            ):
                derived(
                    funding,
                    key,
                    significant(value, 6, rounding) if value is not None else None,
                    fwhere,
                )
            derived(funding, "ratio_denominators", ratio_denominators(jurisdiction), fwhere)
        activity = known_activity(institute)
        derived(institute, "operating", activity, where)
        derived(
            institute,
            "intervals",
            intervals(institute, timeline, result.get("dataset", {}).get("as_of")),
            where,
        )

        output = first_output(institute)
        stored = [e for e in timeline if e["event"] == "first_output"]
        if len(stored) > 1:
            errors.append(f"{where}: multiple first_output events")
        for event in stored:
            if strict:
                errors.append(f"{where}: first_output event must not be authored")
            elif output is None or (event["date"], event.get("output_lineage")) != (
                credit_date(output),
                output["lineage"],
            ):
                errors.append(
                    f"{where}: stored first_output disagrees with output catalogue"
                )
        timeline = [e for e in timeline if e["event"] != "first_output"]
        if output is not None:
            event = {
                "id": stored[0]["id"]
                if stored
                else f"{jurisdiction['id'].lower()}-first-output",
                "date": credit_date(output),
                "event": "first_output",
                "instrument": "publication",
                "description": f"Earliest identified institutionally credited output: {output['title']}",
                "short": "First published work credited to the institute.",
                "output_lineage": output["lineage"],
                "source_ids": list(output["source_ids"]),
            }
            if any(e["id"] == event["id"] for e in timeline):
                errors.append(
                    f"{where}: generated first_output id collides with an authored event"
                )
            # Insert without sorting existing acts: overlapping partial dates keep
            # the authored sequence, which carries closure/status semantics.
            index = next(
                (n for n, e in enumerate(timeline) if e["date"] > event["date"]),
                len(timeline),
            )
            timeline.insert(index, event)
        if timeline or "timeline" in jurisdiction:
            jurisdiction["timeline"] = timeline
    if errors:
        raise ValueError("\n".join(errors))
    return result
