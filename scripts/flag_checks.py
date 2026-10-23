"""Testing capacity, model access and staffing model: invariants, required evidence
pointers and the derived model access value.

Design in docs/design/schema-1.0.md ("Testing capacity and model access", "Staffing
model"); the editorial rules are dataset.method (Testing capacity and model access;
Staffing model). Testing capacity is a list of
modes, each with its own sources and bound excerpt, or one state. Model access records
dated access events, a legal power and developer agreements; its value and latest event
date are derived here, never stored. Labels and definitions live only in
dataset.vocabularies. Staffing model is one code with its sources, bound to an excerpt from
a source published in 2023 or later. All three fields are in the downloads and the API. The
page's Testing and access groups show the derived model access value and, for institutes with
no access found, the testing capacity, cited from that section's own source list; staffing
model is downloads and API only.
"""

import calendar
import datetime as dt
import re

FIELDS = ("testing_capacity", "model_access")
# Stable codes the checks depend on, in vocabulary order (the authored and exported order).
TESTING_MODES = ("in_house", "via_other_bodies")
TESTING_STATES = ("planned", "not_published")
EVENT_KINDS = ("pre_release", "deeper_than_public")
# Derived model access values, in precedence order.
ACCESS_VALUES = (
    "recent_access",
    "earlier_access",
    "by_law",
    "access_agreed",
    "none_found",
)
RECENT_MONTHS = 12
# The frontier set is Epoch AI's (dataset.method); its rows are cited from Epoch's own data.
EPOCH_PUBLISHER = "Epoch AI"
# Leading developer at an event date: an Epoch frontier model released on or after this day,
# and an Epoch notable model in the twelve months before the event (dataset.method).
LEADING_SINCE = dt.date(2023, 1, 1)
LEADING_KEYS = ("epoch_model_date", "epoch_notable_model", "epoch_notable_date")
# Every institute carries both fields; the schema requires them too.
FLAGS_REQUIRED = True
# Staffing model codes, in vocabulary order; the last two are states, the rest models.
STAFFING_CODES = (
    "own_staff",
    "member_bodies",
    "core_with_partners",
    "host_unit",
    "planned",
    "not_published",
)
# Only a federation of existing bodies is staffed by its member bodies (as partner relation member).
MEMBER_BODIES_FORM = "federation_of_existing_bodies"
# Relevance cutoff (dataset.method, Staffing model): a staffing arrangement is a current fact,
# so a source published before this year never supports the value.
STAFFING_SINCE_YEAR = 2023
# Every institute carries it; the schema requires it too.
STAFFING_REQUIRED = True
# "Article 92", "Art. 5", "§ 3", "Section 12", "제5조", "第5条".
PROVISION_LOCATOR = re.compile(
    r"(?:\b(?:article|art\.|artikel|articolo|artículo|section|sec\.|clause|paragraph|para\.)\s*\d"
    r"|§\s*\d|제?\s*\d+\s*조|第?\s*\d+\s*条)",
    re.IGNORECASE,
)


def first_day(day: str) -> dt.date:
    """The earliest calendar day a full or partial date can mean."""
    parts = [int(part) for part in day.split("-")] + [1, 1]
    return dt.date(*parts[:3])


def last_day(day: str) -> dt.date:
    """The latest calendar day a full or partial date can mean."""
    parts = [int(part) for part in day.split("-")]
    if len(parts) == 3:
        return dt.date(*parts)
    if len(parts) == 2:
        return dt.date(parts[0], parts[1], calendar.monthrange(*parts)[1])
    return dt.date(parts[0], 12, 31)


def year_before(as_of: str | dt.date) -> dt.date:
    """The same calendar day twelve months earlier (29 February becomes 28 February)."""
    today = as_of if isinstance(as_of, dt.date) else dt.date.fromisoformat(as_of)
    year = today.year - RECENT_MONTHS // 12
    return today.replace(
        year=year, day=min(today.day, calendar.monthrange(year, today.month)[1])
    )


def access_summary(access: dict, as_of: str) -> dict:
    """Derived value and latest event date. An event is recent only when it certainly falls
    within the twelve months up to as_of: a partial date counts from its first possible day.
    Latest event: the latest first possible day, then the more precise date."""
    events = access.get("access_events", [])
    # ISO dates order as strings, a partial date before every fuller date it contains.
    latest = max((e["date"] for e in events), default=None)
    if latest is not None:
        value = (
            "recent_access"
            if first_day(latest) >= year_before(as_of)
            else "earlier_access"
        )
    elif access.get("legal_power"):
        value = "by_law"
    elif access.get("developer_agreements"):
        value = "access_agreed"
    else:
        value = "none_found"
    return {"value": value, "latest_event": latest}


def access_label(access: dict, vocabularies: dict) -> str:
    """Reader label, from the vocabulary: 'Yes, evidence from 2024' fills in the year."""
    label = vocabularies["model_access"][access["value"]]["label"]
    return label.replace("<year>", (access.get("latest_event") or "")[:4])


def required_flag_pointers(d: dict) -> set[str]:
    """Exact leaves that need a bound excerpt."""
    required: set[str] = set()
    for n, institute in enumerate(d["institutes"]):
        testing = institute.get("testing_capacity")
        base = f"/institutes/{n}/testing_capacity"
        if testing:
            required.update(
                f"{base}/modes/{k}/code" for k in range(len(testing.get("modes", [])))
            )
            if testing.get("state") == "planned":
                required.add(f"{base}/state")
        staffing = institute.get("staffing_model")
        if staffing and staffing["code"] != "not_published":
            required.add(f"/institutes/{n}/staffing_model/code")
        access = institute.get("model_access")
        if not access:
            continue
        base = f"/institutes/{n}/model_access"
        for k in range(len(access.get("access_events", []))):
            required.update(
                f"{base}/access_events/{k}/{key}"
                for key in (
                    "model",
                    "developer",
                    "date",
                    "kind",
                    "epoch_model",
                    *LEADING_KEYS,
                )
                if key in access["access_events"][k]
            )
        for k, _ in enumerate(access.get("developer_agreements", [])):
            required.update(
                f"{base}/developer_agreements/{k}/{key}"
                for key in ("agreement_id", "epoch_model")
            )
        if access.get("legal_power"):
            required.update(
                f"{base}/legal_power/{key}" for key in ("provision", "conditions")
            )
    return required


def name_forms(institute: dict) -> set[str]:
    """The institute's own names as recorded: name, acronym, short and local names, former
    names and acronyms given in brackets in them."""
    names = [
        institute.get(key, "")
        for key in ("name", "acronym", "short_name", "name_local")
    ] + [n.get("name", "") for n in institute.get("names", [])]
    forms: set[str] = set()
    for name in names:
        name = re.sub(r"\s*\((?:planned|proposed)\)", "", name).strip()
        forms.update(f.strip() for f in re.findall(r"\(([^()]+)\)", name))
        forms.add(re.sub(r"\s*\([^()]*\)", "", name).strip())
    return {f for f in forms if len(f) >= 3}


def institute_forms(institutes: list[dict]) -> dict[str, tuple[set[str], set[str]]]:
    """Per institute: all its name forms, and those no other institute shares. A shared form
    ("AISI", "CAISI", "AI Security Institute") names it only in a source from its own jurisdiction."""
    forms = {i["id"]: {f.casefold() for f in name_forms(i)} for i in institutes}
    shared = {
        f
        for iid, own in forms.items()
        for f in own
        if any(f in other for oid, other in forms.items() if oid != iid)
    }
    return {iid: (own, own - shared) for iid, own in forms.items()}


def find(form: str, text: str) -> list[tuple[int, int]]:
    start = r"(?<!\w)" if form[0].isascii() else ""
    end = r"(?!\w)" if form[-1].isascii() else ""
    return [
        m.span()
        for m in re.finditer(start + re.escape(form) + end, text, re.IGNORECASE)
    ]


def names_institute(text: str, own: set[str], others: set[str]) -> bool:
    """True when the text names the institute by one of its own forms, and that mention is
    not part of another institute's longer name ("AI Safety Institute" in "Singapore AI
    Safety Institute")."""
    covered = [span for form in others for span in find(form, text)]
    return any(
        not any(a <= s and e <= b and (a, b) != (s, e) for a, b in covered)
        for form in own
        for s, e in find(form, text)
    )


def parent_names(institute: dict) -> set[str]:
    """Bodies that are never the institute: host, reporting line and national partners."""
    reports = institute.get("reports_to", {})
    names = {institute.get("host", "")}
    names.update(
        part for key in ("name", "name_en") for part in reports.get(key, "").split(";")
    )
    for entry in institute.get("national_partners", {}).get("entries", []):
        names.update(entry.get(key, "") for key in ("name", "name_en", "acronym"))
    return {
        re.sub(r"^the\s+", "", n.strip(), flags=re.IGNORECASE).casefold() for n in names
    } - {""}


def check_flags(d: dict) -> list[str]:
    """Cross-field checks JSON Schema cannot express; d has resolved excerpt pointers."""
    errors: list[str] = []
    vocabularies = d["dataset"].get("vocabularies", {})
    institutes = d["institutes"]
    for field, codes in (
        ("testing_capacity", (*TESTING_MODES, *TESTING_STATES)),
        ("model_access", ACCESS_VALUES),
        ("staffing_model", STAFFING_CODES),
    ):
        if field in vocabularies and set(vocabularies[field]) != set(codes):
            errors.append(
                f"dataset.vocabularies.{field} must define exactly {', '.join(codes)}"
            )
    authored = any(field in i for i in institutes for field in FIELDS)
    if authored or FLAGS_REQUIRED:
        for institute in institutes:
            missing = [field for field in FIELDS if field not in institute]
            if missing:
                errors.append(
                    f"{institute['id']}: testing_capacity and model_access are added to every institute together; missing {', '.join(missing)}"
                )
    if STAFFING_REQUIRED or any("staffing_model" in i for i in institutes):
        errors += [
            f"{i['id']}: staffing_model is missing; every institute needs it"
            for i in institutes
            if "staffing_model" not in i
        ]
    if not authored:
        return errors
    sources = {s["id"]: s for s in d["sources"]}
    bound: dict[str, list[tuple[dict, dict]]] = {}
    for source in d["sources"]:
        for excerpt in source.get("excerpts", []):
            for pointer in excerpt["supports"]:
                if isinstance(pointer, str):
                    bound.setdefault(pointer, []).append((source, excerpt))
    forms = institute_forms(institutes)
    as_of = d["dataset"]["as_of"]
    for n, institute in enumerate(institutes):
        iid = institute["id"]
        own, unshared = forms[iid]
        other_forms = {f for oid, (fs, _) in forms.items() if oid != iid for f in fs}
        others = other_forms - own
        parents = parent_names(institute)

        def attributed(pointer, label, holder):
            """The institute, or a body explicitly acting for it, is the subject of a bound excerpt."""
            excerpts = bound.get(pointer, [])
            if not excerpts:
                return  # reported as a required claim without an excerpt
            named_as = holder.get("named_as", "")
            if named_as:
                alias = named_as.strip().casefold()
                if re.sub(r"^the\s+", "", alias) in parents:
                    errors.append(
                        f"{iid}: {label}: named_as {named_as!r} is the host, the reporting-line body or a partner, never the institute"
                    )
                if not any(
                    names_institute(excerpt["text"], {alias}, others)
                    and (
                        alias not in other_forms
                        or source.get("origin") == institute["jurisdiction_id"]
                    )
                    for source, excerpt in excerpts
                ):
                    errors.append(
                        f"{iid}: {label}: named_as {named_as!r} does not name this institute in a bound excerpt "
                        "(absent, part of another institute's name, or another institute's name in a source from another jurisdiction)"
                    )
            elif not any(
                names_institute(excerpt["text"], local, others)
                or source.get("publisher", "").strip().casefold() in local
                for source, excerpt in excerpts
                for local in [
                    own
                    if source.get("origin") == institute["jurisdiction_id"]
                    else unshared
                ]
            ):
                errors.append(
                    f"{iid}: {label}: no bound excerpt names the institute or comes from its own publication; "
                    "the institute, or a body explicitly acting for it, must be the subject, never its parent body or a partner "
                    "(record another name the excerpt uses in named_as)"
                )

        def frontier(pointer, label, item, keys=("epoch_model",)):
            """Epoch AI's own data, quoted, is the evidence for each Epoch field."""
            source = sources.get(item["epoch_source_id"])
            if source is None or source.get("publisher") != EPOCH_PUBLISHER:
                errors.append(
                    f"{iid}: {label}: epoch_source_id must name a source published by {EPOCH_PUBLISHER}"
                )
            elif item["epoch_source_id"] not in item["source_ids"]:
                errors.append(
                    f"{iid}: {label}: epoch_source_id must be one of its source_ids"
                )
            else:
                for key in keys:
                    leaf = f"{pointer}/{key}"
                    if bound.get(leaf) and not any(
                        s["id"] == item["epoch_source_id"] for s, _ in bound[leaf]
                    ):
                        errors.append(
                            f"{iid}: {label}: {key} needs an excerpt from {item['epoch_source_id']}"
                        )

        testing = institute.get("testing_capacity")
        if testing:
            base = f"/institutes/{n}/testing_capacity"
            stated = [m["code"] for m in testing.get("modes", [])]
            if len(stated) != len(set(stated)):
                errors.append(f"{iid}: testing_capacity.modes repeats a code")
            elif stated != [c for c in TESTING_MODES if c in stated]:
                errors.append(
                    f"{iid}: testing_capacity.modes must be in vocabulary order ({', '.join(TESTING_MODES)})"
                )
            for k, mode in enumerate(testing.get("modes", [])):
                attributed(
                    f"{base}/modes/{k}/code", f"testing_capacity {mode['code']}", mode
                )
            if testing.get("state") == "planned":
                attributed(f"{base}/state", "testing_capacity planned", testing)
        staffing = institute.get("staffing_model")
        if staffing and staffing["code"] != "not_published":
            pointer = f"/institutes/{n}/staffing_model/code"
            label = f"staffing_model {staffing['code']}"
            attributed(pointer, label, staffing)
            for source, _ in bound.get(pointer, []):
                # An undated page counts as of the day it was read (dataset.method).
                dated = source.get("published") or source.get("accessed", "")
                if dated and int(dated[:4]) < STAFFING_SINCE_YEAR:
                    how = "published" if source.get("published") else "undated and read"
                    errors.append(
                        f"{iid}: {label}: {source['id']} was {how} in {dated[:4]}; "
                        f"only a source dated {STAFFING_SINCE_YEAR} or later supports a staffing model (older ones go in the notes)"
                    )
            if (
                staffing["code"] == "member_bodies"
                and institute.get("legal_form") != MEMBER_BODIES_FORM
            ):
                errors.append(
                    f"{iid}: staffing_model member_bodies needs legal_form {MEMBER_BODIES_FORM}"
                )
        access = institute.get("model_access")
        if not access:
            continue
        base = f"/institutes/{n}/model_access"
        for k, event in enumerate(access.get("access_events", [])):
            label = f"model_access event {event['id']}"
            if first_day(event["date"]) > dt.date.fromisoformat(as_of):
                errors.append(
                    f"{iid}: {label}: date {event['date']} is after as_of {as_of}"
                )
            attributed(f"{base}/access_events/{k}/kind", label, event)
            leading = event["epoch_basis"] == "leading_developer"
            frontier(
                f"{base}/access_events/{k}",
                label,
                event,
                ("epoch_model", *LEADING_KEYS) if leading else ("epoch_model",),
            )
            if leading:
                if first_day(event["epoch_model_date"]) < LEADING_SINCE:
                    errors.append(
                        f"{iid}: {label}: a leading developer needs an Epoch frontier model released on or after {LEADING_SINCE}; {event['epoch_model_date']} is earlier"
                    )
                elif last_day(event["epoch_model_date"]) > last_day(event["date"]):
                    errors.append(
                        f"{iid}: {label}: a leading developer needs its Epoch frontier model released by the event date; {event['epoch_model_date']} is later"
                    )
                notable = event["epoch_notable_date"]
                if not (
                    year_before(first_day(event["date"])) <= first_day(notable)
                    and last_day(notable) <= last_day(event["date"])
                ):
                    errors.append(
                        f"{iid}: {label}: a leading developer needs an Epoch notable model in the twelve months before the event; {notable} is outside them"
                    )
        agreements = {a["id"]: a for a in institute.get("agreements", [])}
        for k, item in enumerate(access.get("developer_agreements", [])):
            label = f"model_access agreement {item['id']}"
            agreement = agreements.get(item["agreement_id"])
            if agreement is None:
                errors.append(
                    f"{iid}: {label}: agreement_id {item['agreement_id']!r} is not an agreement of this institute"
                )
            elif not agreement.get("date"):
                errors.append(f"{iid}: {label}: the referenced agreement has no date")
            elif first_day(agreement["date"]) > dt.date.fromisoformat(as_of):
                errors.append(
                    f"{iid}: {label}: the referenced agreement's date {agreement['date']} is after as_of {as_of}"
                )
            elif agreement.get("counterparty_type") != "developer":
                errors.append(
                    f"{iid}: {label}: the agreement's counterparty is not a developer"
                )
            attributed(f"{base}/developer_agreements/{k}/agreement_id", label, item)
            frontier(f"{base}/developer_agreements/{k}", label, item)
        power = access.get("legal_power")
        if power:
            attributed(
                f"{base}/legal_power/provision", "model_access legal_power", power
            )
            if not PROVISION_LOCATOR.search(power["provision"]):
                errors.append(
                    f"{iid}: model_access legal_power: provision must name the article, for example 'Article 92'"
                )
            excerpts = bound.get(f"{base}/legal_power/provision", [])
            act = " ".join(power["act"].split()).casefold()
            if excerpts and not any(
                source.get("type") == "primary"
                and source.get("legal_text") is True
                and any(
                    act in " ".join(source.get(key, "").split()).casefold()
                    for key in ("title", "title_en")
                )
                for source, _ in excerpts
            ):
                errors.append(
                    f"{iid}: model_access legal_power: the provision needs an excerpt from the legal text itself: "
                    f"a primary source marked legal_text whose title names {power['act']!r}; an explainer is not enough"
                )
    return errors
