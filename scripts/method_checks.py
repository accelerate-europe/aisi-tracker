"""Checks for the revised public research method, independent of source retrieval."""

import calendar
import datetime as dt
import re

if __package__:
    from .method_copy import check_method_copy
    from .references import materialise_references
    from .published import IMPLIES, announcement, credit_date, first_designation, prepared_facts, status_basis
    from .reader_text import check_reader_text  # type: ignore[import-not-found]
    from .row_checks import check_rows, required_row_pointers
    from .flag_checks import check_flags, required_flag_pointers
else:
    from method_copy import check_method_copy
    from references import materialise_references
    from published import IMPLIES, announcement, credit_date, first_designation, prepared_facts, status_basis
    from reader_text import check_reader_text  # type: ignore[import-not-found, no-redef]
    from row_checks import check_rows, required_row_pointers  # type: ignore[import-not-found, no-redef]
    from flag_checks import check_flags, required_flag_pointers  # type: ignore[import-not-found, no-redef]


def required_excerpt_pointers(d: dict, *, authored: dict | None = None) -> set[str]:
    """Exact claim leaves, never satisfied by a quote attached only to a parent."""
    authored = d if authored is None else authored
    authored_jurisdictions = {j["id"]: j for j in authored["jurisdictions"]}
    authored_institutes = {i["id"]: i for i in authored["institutes"]}
    required: set[str] = set()
    for n, rate in enumerate(d.get("dataset", {}).get("exchange_rates", [])):
        required.update(
            f"/dataset/exchange_rates/{n}/{field}"
            for field in ("units_per_eur", "period/start", "period/end")
        )
    for n, record in enumerate(d["jurisdictions"]):
        prefix = f"/jurisdictions/{n}"
        status_authored = "status" in authored_jurisdictions[record["id"]]
        if status_authored and record["status"] != "none_identified":
            required.update(f"{prefix}/{key}" for key in ("status", "status_since"))
        if "closest_body" in record:
            required.update(
                f"{prefix}/closest_body/{key}" for key in ("name", "stage", "note")
            )
        institute = authored_institutes.get(record.get("institute_id"))
        primary_institute = institute is not None and "status" not in institute
        if not status_authored or primary_institute:
            events = record.get("timeline", [])
            _, _, basis = status_basis(events)
            if basis is not None:
                required.update(f"{prefix}/timeline/{basis}/{key}" for key in ("date", "event"))
            designation = first_designation(events)
            if designation is not None and record.get("institute_id"):
                required.update(f"{prefix}/timeline/{designation}/{key}" for key in ("date", "event"))
        authored_events = {e["id"]: e for e in authored_jurisdictions[record["id"]].get("timeline", [])}
        for k, event in enumerate(record.get("timeline", [])):
            if event["event"] == "first_output" and event["id"] in authored_events:
                required.update(f"{prefix}/timeline/{k}/{key}" for key in ("date", "description"))
    for n, record in enumerate(d["institutes"]):
        prefix = f"/institutes/{n}"
        for k, output in enumerate(record.get("outputs", [])):
            if "credit" in output:
                required.update(f"{prefix}/outputs/{k}/credit/{key}" for key in ("institute_id", "date"))
            elif output.get("credited_from"):
                required.add(f"{prefix}/outputs/{k}/credited_from")
        if "status" in authored_institutes[record["id"]] and record["status"] != "none_identified":
            required.add(f"{prefix}/status")
        if record.get("legal_basis"):
            required.update(f"{prefix}/legal_basis/{key}" for key in ("type", "state"))
        for collection, keys in (
            ("funding", ("amount", "annual_amount")),
            ("staff", ("total", "technical", "target")),
            ("agreements", ("counterparty", "state", "date", "scope", "participation")),
        ):
            for k, item in enumerate(record.get(collection, [])):
                item_keys = keys
                if (
                    collection == "funding"
                    and item.get("annual_basis_code") == "period_mean"
                ):
                    item_keys = ("amount", "years", "period")
                if (
                    collection == "funding"
                    and item.get("annual_basis_code") == "allocation_current_year"
                    and item.get("annual_profile")
                ):
                    item_keys = ("amount",)
                if collection == "funding" and "sum_of" in item:
                    # Arithmetic is checked by the build; quotations cover each
                    # component's amount leaves, which remain required below.
                    item_keys = ()
                required.update(
                    f"{prefix}/{collection}/{k}/{key}"
                    for key in item_keys
                    if item.get(key) is not None
                )
                if collection == "funding":
                    required.update(
                        f"{prefix}/funding/{k}/annual_profile/{pn}/amount"
                        for pn, allocation in enumerate(item.get("annual_profile", []))
                        if allocation.get("amount") is not None
                    )
    required.update(required_row_pointers(d))
    required.update(required_flag_pointers(d))
    return required


def negative_confidence_limit(record: dict, sources: dict, as_of: str) -> str:
    """Maximum supported confidence; calendar anniversary is inclusive."""
    check = record["check"]
    searches = [
        q
        for q in check.get("searches", [])
        if q.get("language")
        and q.get("query", "").strip()
        and q.get("date", "9999") <= as_of
    ]
    local_search = any(q.get("national_or_official") for q in searches)
    english_search = any(q["language"].split("-")[0].casefold() == "en" for q in searches)
    governance = set(check.get("governance_source_ids", [])) & set(record["source_ids"])
    primary_governance = any(sources.get(sid, {}).get("type") == "primary" for sid in governance)
    if (
        check.get("method") != "individual"
        or not english_search
        or not local_search
        or not primary_governance
        or not check.get("governance_note", "").strip()
    ):
        return "low"
    absence = check.get("explicit_absence", {})
    source = sources.get(absence.get("source_id"), {})
    today = dt.date.fromisoformat(as_of)
    cutoff = today.replace(
        year=today.year - 1,
        day=min(today.day, calendar.monthrange(today.year - 1, today.month)[1]),
    )
    if (
        source.get("type") == "primary"
        and str(cutoff) <= absence.get("date", "") <= as_of
        and source.get("published") == absence.get("date")
        and absence.get("source_id") in record["source_ids"]
    ):
        return "high"
    return "medium"


def valid_event_short(value) -> bool:
    """One plain line, with at most 12 whitespace-delimited words."""
    return (
        isinstance(value, str)
        and bool(value.strip())
        and value == value.strip()
        and len(value.splitlines()) == 1
        and len(value.split()) <= 12
    )


NO_COPY_NOTE = re.compile(
    r"no usable source copy was retained|no complete response snapshot was preserved"
    r"|complete response snapshot was not preserved"
    r"|without a preserved response snapshot",
    re.IGNORECASE,
)


GENERIC_SHORT_FORMS = {"AISI", "AISIS", "ASI"}


def check_naming(d: dict) -> list[str]:
    """One recorded short form per institute: unique, and never a bare generic acronym."""
    errors, seen = [], {}
    for institute in d["institutes"]:
        short = institute.get("short_name", "").strip()
        if not short:
            errors.append(f"{institute['id']}: institute needs a short_name")
            continue
        if re.sub(r"[^A-Za-z]", "", short).upper() in GENERIC_SHORT_FORMS:
            errors.append(
                f"{institute['id']}: short_name {short!r} is a bare generic acronym; "
                "add the country or use the body's own acronym"
            )
        if short.casefold() in seen:
            errors.append(
                f"{institute['id']}: short_name {short!r} is also used by {seen[short.casefold()]}"
            )
        seen[short.casefold()] = institute["id"]
    return errors


NON_LATIN = re.compile(r"[\u0370-\u03ff\u0400-\u052f\u0530-\u058f\u0590-\u06ff\u0900-\u0dff\u0e00-\u0eff\u10a0-\u10ff\u1f00-\u1fff\u3040-\u30ff\u3400-\u9fff\uac00-\ud7af]")


def check_body_names(d: dict) -> list[str]:
    """A related body named in a non-Latin script carries an English name; an English name is never empty or a copy."""
    errors = []
    for j in d["jurisdictions"]:
        for body in j.get("related_bodies", []):
            english = body.get("name_en")
            if NON_LATIN.search(body["name"]) and not english:
                errors.append(f"{j['id']} {body['name']}: a body named in a non-Latin script needs name_en")
            if english is not None and english.strip() == body["name"].strip():
                errors.append(f"{j['id']} {body['name']}: name_en repeats the name; omit it when the name is English")
    return errors


# Timeline events that move a status (dataset.vocabularies.timeline_event), and the leaves a primary excerpt must bind.
STATUS_EVENTS = (*IMPLIES, "withdrawn", "closed")
PRIMARY_LEAVES = {event: ("date", "event") for event in STATUS_EVENTS} | {"launched": ("date",)}


def materialised_view(d: dict) -> dict | None:
    """The data with generated facts and RFC 6901 support pointers, as check_method_rules reads it; None if broken."""
    try:
        if any("status" not in j for j in d["jurisdictions"]):
            d = prepared_facts(d)
        return materialise_references(d)
    except (ValueError, KeyError, IndexError, TypeError, ArithmeticError):
        return None


# Events whose act no primary text could be retrieved for (rule status-evidence), keyed by jurisdiction and event id,
# with the cited secondary source whose access note says what was tried. Each must still be flagged; a stale entry fails.
PRIMARY_SOURCE_EXCEPTIONS = {
    ("KR", "kr-2024-05-22-commitment"): "kr-inews24-2024",
}


def primary_source_flags(d: dict) -> list[tuple[str, str, list[str], str]] | None:
    """Status-changing timeline events, and launch dates, whose date or act no primary source carries.

    A primary source carries the act through an excerpt bound to `event`, and the date through an excerpt bound to
    `date` or by its own `published` date when that equals the event's date at the same precision (rule
    status-evidence). Returns (jurisdiction id, event id, cited source ids, line) in jurisdiction then timeline
    order, each line naming its kind in brackets; None when the facts cannot be prepared, which
    check_method_rules reports.
    """
    d = materialised_view(d)
    if d is None:
        return None
    as_of = d["dataset"]["as_of"]
    institutes = {i["id"]: i for i in d["institutes"]}
    types = {s["id"]: s.get("type") for s in d["sources"]}
    published = {s["id"]: s.get("published") for s in d["sources"]}
    primary_bound: dict[str, set[str]] = {}
    for source in d["sources"]:
        if source.get("type") == "primary":
            for excerpt in source.get("excerpts", []):
                for pointer in excerpt["supports"]:
                    primary_bound.setdefault(pointer, set()).add(source["id"])
    flags = []
    for n, record in enumerate(d["jurisdictions"]):
        events = record.get("timeline", [])
        institute = institutes.get(record.get("institute_id"))
        _, _, basis = status_basis(events)
        roles = {}
        if basis is not None:
            confidence = f", at {record['status_confidence']} confidence" if record.get("status_confidence") else ""
            roles[basis] = [f"sets the current status, {record['status']}{confidence}"]
        if institute is not None:
            for k, label in ((first_designation(events), "established"), (announcement(events, as_of), "announced")):
                if k is not None:
                    roles.setdefault(k, []).append(f"sets {institute['id']}'s {label} date")
        for k, event in enumerate(events):
            leaves = PRIMARY_LEAVES.get(event["event"])
            if leaves is None:
                continue
            role = "; ".join(roles.get(k, ["no status effect" if event["event"] == "launched" else "another status act; no generated date rests on it"]))
            where = f"{record['id']} {event['id']} ({event['event']}, {event['date']}; {role})"
            cited = event.get("source_ids", [])
            flag = (record["id"], event["id"], cited)
            if not any(types.get(sid) == "primary" for sid in cited):
                listed = ", ".join(f"{sid} ({types.get(sid) or 'unknown'})" for sid in cited) or "nothing"
                flags.append((*flag, f"[no primary source cited] {where}: cites {listed}"))
                continue
            dated = any(types.get(sid) == "primary" and published.get(sid) == event["date"] for sid in cited)
            missing = [
                leaf for leaf in leaves
                if not primary_bound.get(f"/jurisdictions/{n}/timeline/{k}/{leaf}") and not (leaf == "date" and dated)
            ]
            if len(missing) == len(leaves):
                flags.append((*flag, f"[primary source cited, no primary excerpt bound] {where}: no primary excerpt binds its {' or '.join(leaves)}"))
            elif missing:
                flags.append((*flag, f"[primary excerpt misses a leaf] {where}: no primary excerpt binds its {' or '.join(missing)}"))
    return flags


def primary_source_errors(d: dict, exceptions: dict | None = None) -> list[str]:
    """Flags not listed in PRIMARY_SOURCE_EXCEPTIONS, and listed exceptions that are stale or have no access note.

    Lifted from the shelved Schema 2.0 draft ("second-hand copies never carry a status change"); an error since
    1.0.10, once the owner had seen what it flagged and the data was fixed.
    """
    exceptions = PRIMARY_SOURCE_EXCEPTIONS if exceptions is None else exceptions
    try:
        flags = primary_source_flags(d)
    except (ValueError, KeyError, IndexError, TypeError, ArithmeticError) as exc:
        # Never a silent pass: an impossible date, for one, is also reported by the consistency checks.
        return [f"status evidence: not checked ({type(exc).__name__}: {exc})"]
    if flags is None:
        return []
    notes = {s["id"]: s.get("access_note", "").strip() for s in d["sources"]}
    present = {j["id"] for j in d["jurisdictions"]}
    errors, flagged = [], set()
    for jid, eid, cited, line in flags:
        source = exceptions.get((jid, eid))
        if source is None:
            errors.append(f"status evidence (rule status-evidence) {line}")
            continue
        flagged.add((jid, eid))
        if source not in cited or not notes.get(source):
            errors.append(f"status evidence: accepted exception {jid} {eid} needs its cited source {source} with an access note saying what was tried")
    # An entry for a jurisdiction the data does not hold (a test fixture) is not checked.
    for jid, eid in sorted(k for k in exceptions.keys() - flagged if k[0] in present):
        errors.append(f"status evidence: accepted exception {jid} {eid} is no longer flagged; remove it from PRIMARY_SOURCE_EXCEPTIONS")
    return errors


def primary_source_warnings(d: dict, exceptions: dict | None = None) -> list[str]:
    """The accepted exceptions, for the check report's release warnings; everything else flagged is an error."""
    exceptions = PRIMARY_SOURCE_EXCEPTIONS if exceptions is None else exceptions
    return [
        f"primary source [accepted exception] {line.split('] ', 1)[1]}"
        for jid, eid, _, line in primary_source_flags(d) or []
        if (jid, eid) in exceptions
    ]


# Free-text leaves of jurisdictions and institutes, by key, that state facts without a required excerpt.
FREE_TEXT_KEYS = ("summary", "notes", "note", "description", "mandate")
STAGE_IN_NAME = re.compile(r"\b(planned|proposed|draft|under study)\b", re.IGNORECASE)
# What a sentence can carry that a source must back, recognisable without reading the prose: a date, a figure,
# or a named body (an acronym or an institutional noun).
CHECKABLE = re.compile(
    r"\d|\b[A-Z]{2,}\b|\b(January|February|March|April|May|June|July|August|September|October|November|December"
    r"|Ministry|Minister|Authority|Agency|Office|Council|Centre|Center|Institute|Commission|Committee"
    r"|Department|Directorate|Board|University|Parliament|Cabinet|Senate|Assembly)\b"
)
SENTENCE_END = re.compile(r"(?<=[.;!?])\s+")
UNQUOTED_KINDS = (
    "related body with no source",
    "related body role with no excerpt",
    "stage in a body's name with no excerpt on the name",
    "AI Act authority designation_state with no excerpt",
    "free text with no excerpt",
)


def checkable_sentences(text: str) -> int:
    """Sentences that carry a date, a figure or a named body."""
    return sum(bool(CHECKABLE.search(sentence)) for sentence in SENTENCE_END.split(text))


def unquoted_claims(d: dict) -> dict[str, list[str]]:
    """Facts stated in fields that need no excerpt, with none bound: related-body roles and stages, notes and summaries.

    Returns the items per kind of UNQUOTED_KINDS, in jurisdiction then institute order. A leaf counts as quoted
    when any excerpt binds it exactly; whether the excerpt carries every sentence is left to review. Free text
    is grouped by field family (for example "jurisdictions[].notes"); each item names how many of its
    sentences carry a date, a figure or a named body.
    """
    found: dict[str, list[str]] = {kind: [] for kind in UNQUOTED_KINDS}
    d = materialised_view(d)
    if d is None:
        return found
    bound = {pointer for s in d["sources"] for e in s.get("excerpts", []) for pointer in e["supports"]}

    def free_text(value, pointer: str, family: str, owner: str, key: str = "") -> None:
        if isinstance(value, dict):
            for child, item in value.items():
                if child != "related_bodies":
                    free_text(item, f"{pointer}/{child}", f"{family}.{child}", owner, child)
        elif isinstance(value, list):
            for k, item in enumerate(value):
                free_text(item, f"{pointer}/{k}", f"{family}[]", owner, key)
        elif isinstance(value, str) and key in FREE_TEXT_KEYS and pointer not in bound:
            found["free text with no excerpt"].append(f"{family}: {owner} ({checkable_sentences(value)} checkable sentences)")

    for n, record in enumerate(d["jurisdictions"]):
        for k, body in enumerate(record.get("related_bodies", [])):
            prefix, label = f"/jurisdictions/{n}/related_bodies/{k}", f"{record['id']} {body['name']}"
            if not body.get("source_ids"):
                found["related body with no source"].append(label)
                continue
            if f"{prefix}/role" not in bound:
                found["related body role with no excerpt"].append(label)
            if STAGE_IN_NAME.search(body["name"]) and f"{prefix}/name" not in bound:
                found["stage in a body's name with no excerpt on the name"].append(label)
            if "designation_state" in body and f"{prefix}/designation_state" not in bound:
                found["AI Act authority designation_state with no excerpt"].append(f"{label} ({body['designation_state']})")
        free_text({key: value for key, value in record.items() if key != "check"}, f"/jurisdictions/{n}", "jurisdictions[]", record["id"])
    for n, record in enumerate(d["institutes"]):
        free_text({key: value for key, value in record.items() if key != "check"}, f"/institutes/{n}", "institutes[]", record["id"])
    return found


UNQUOTED_REPORT = "unquoted-claims.md"


def unquoted_warnings(found: dict[str, list[str]]) -> list[str]:
    """One Release warnings line per kind with a count, so the backlog's size shows before any rule is set."""
    lines = []
    for kind, items in found.items():
        if not items:
            continue
        if kind == "free text with no excerpt":
            families: dict[str, list[str]] = {}
            for item in items:
                families.setdefault(item.split(":", 1)[0], []).append(item)
            detail = "; ".join(f"{family} {len(rows)}" for family, rows in sorted(families.items(), key=lambda pair: (-len(pair[1]), pair[0])))
            sentences = sum(int(item.rsplit("(", 1)[1].split()[0]) for item in items)
            lines.append(f"unquoted [{kind}]: {len(items)} leaves, {sentences} sentences in them with a date, figure or named body ({detail}); full list in {UNQUOTED_REPORT} beside the release warnings")
        else:
            lines.append(f"unquoted [{kind}]: {len(items)} (first: {'; '.join(items[:3])}); full list in {UNQUOTED_REPORT} beside the release warnings")
    return lines


def unquoted_report(found: dict[str, list[str]]) -> str:
    """The full list behind the count lines, for the report directory."""
    lines = ["# Unquoted claims", "", "Facts stated in fields that need no excerpt, with no excerpt bound (warning only).", ""]
    for kind, items in found.items():
        lines += [f"## {kind} ({len(items)})", ""] + [f"- {item}" for item in items] + [""]
    return "\n".join(lines)


def check_method_rules(d: dict, *, authored: dict | None = None) -> list[str]:
    """Validate declared evidence metadata; judgement and quotations need review."""
    authored = d if authored is None else authored
    status_evidence = primary_source_errors(d)
    if any("status" not in j for j in d["jurisdictions"]):
        try:
            d = prepared_facts(d)
        except (ValueError, KeyError, IndexError, TypeError, ArithmeticError) as exc:
            return [f"authored facts or references: {exc}"]
    d = materialise_references(d)
    errors = check_method_copy(d["dataset"]) + check_reader_text(d) + check_naming(d) + check_body_names(d)
    errors += status_evidence
    errors += check_rows(d)
    errors += check_flags(d)
    sources = {s["id"]: s for s in d["sources"]}
    institutes = {i["id"]: i for i in d["institutes"]}
    as_of = d["dataset"]["as_of"]
    covered = {
        p
        for s in sources.values()
        for e in s.get("excerpts", [])
        for p in e["supports"]
    }
    for pointer in sorted(required_excerpt_pointers(d, authored=authored) - covered):
        errors.append(f"required claim has no excerpt: {pointer}")
    for source in sources.values():
        if source.get("snapshot") and NO_COPY_NOTE.search(source.get("access_note", "")):
            errors.append(
                f"source {source['id']}: access_note says no copy was kept "
                "but a snapshot is recorded"
            )
        glosses: dict[str, str] = {}
        for excerpt in source.get("excerpts", []):
            gloss = excerpt.get("gloss", "").strip().casefold()
            if gloss and gloss in glosses and glosses[gloss] != excerpt["text"]:
                errors.append(
                    f"source {source['id']}: identical glosses on different excerpts"
                )
            glosses[gloss] = excerpt["text"]
    for record in [*d["jurisdictions"], *d["institutes"]]:
        check = record["check"]
        governance = check.get("governance_source_ids", [])
        if ("governance_source_ids" in check or "governance_note" in check) and (
            not governance or not check.get("governance_note", "").strip()
        ):
            errors.append(f"{record['id']}: governance sources and note must occur together")
        for sid in governance:
            if sid not in record["source_ids"] or sources.get(sid, {}).get("type") != "primary":
                errors.append(f"{record['id']}: governance evidence must be a cited primary source: {sid}")
        if check["last_checked"] > as_of:
            errors.append(f"{record['id']}: full check is after dataset.as_of")
        for item in check.get("partial", []):
            if item["date"] > as_of:
                errors.append(f"{record['id']}: partial check is after dataset.as_of")
        for query in check.get("searches", []):
            if query["date"] > as_of:
                errors.append(f"{record['id']}: search is after dataset.as_of")
    for milestone in d["dataset"].get("network_milestones", []):
        if not valid_event_short(milestone.get("short")):
            errors.append(f"network milestone {milestone['id']}: short must be one nonblank line of at most 12 words")
    for jn, j in enumerate(d["jurisdictions"]):
        for event in j.get("timeline", []):
            if j["status"] != "none_identified" or "short" in event:
                if not valid_event_short(event.get("short")):
                    errors.append(f"{j['id']} timeline {event['id']}: short must be one nonblank line of at most 12 words")
        if j["status"] == "none_identified":
            limit = negative_confidence_limit(j, sources, as_of)
            if {"low": 0, "medium": 1, "high": 2}[j.get("status_confidence", "low")] > {
                "low": 0,
                "medium": 1,
                "high": 2,
            }[limit]:
                errors.append(
                    f"{j['id']}: negative confidence exceeds {limit} evidence limit"
                )
            if j.get("status_confidence", "low") == "high":
                pointer = f"/jurisdictions/{jn}/check/explicit_absence"
                sid = j["check"].get("explicit_absence", {}).get("source_id")
                if not any(
                    pointer in e["supports"]
                    for e in sources.get(sid, {}).get("excerpts", [])
                ):
                    errors.append(
                        f"{j['id']}: explicit absence needs its own official excerpt"
                    )
        for body in j.get("related_bodies", []):
            if body.get("kind") not in {"ai_act_authority", "other"}:
                errors.append(f"{j['id']} {body['name']}: related body needs kind")
            if body.get("kind") != "ai_act_authority":
                if any(key in body for key in (
                    "designation_state", "authority_roles", "designation_basis", "designation_note"
                )):
                    errors.append(f"{j['id']} {body['name']}: authority metadata requires ai_act_authority kind")
                continue
            if not body.get("source_ids"):
                errors.append(f"{j['id']} {body['name']}: authority needs source_ids")
            if body.get("counts_as_aisi") is not False:
                errors.append(f"{j['id']} {body['name']}: authority needs counts_as_aisi false")
            if not body.get("designation_basis"):
                errors.append(
                    f"{j['id']} {body['name']}: authority needs designation_basis"
                )
            if (
                body.get("designation_state") == "designated"
                and body.get("designation_basis") == "none_yet"
            ):
                errors.append(
                    f"{j['id']} {body['name']}: designation needs an enacted basis"
                )
            if (
                j.get("eu_member") or j["id"] in {"IS", "LI", "NO"}
            ) and "eu-ai-act-authorities-2026" not in body.get("source_ids", []):
                errors.append(
                    f"{j['id']} {body['name']}: authority needs Commission list citation"
                )
    lineage_dates = {}
    for i in d["institutes"]:
        outputs = i.get("outputs", [])
        lineages = [o["lineage"] for o in outputs if o.get("lineage")]
        if len(set(lineages)) != len(lineages):
            errors.append(f"{i['id']}: duplicate output lineage")
        for output in outputs:
            lineage = output.get("lineage")
            if lineage in lineage_dates and lineage_dates[lineage] != output["date"]:
                errors.append(f"{i['id']}: shared output lineage {lineage} has inconsistent dates")
            if lineage:
                lineage_dates[lineage] = output["date"]
            credit = credit_date(output)
            if credit and credit[:min(len(credit), len(output["date"]))] < output["date"][:min(len(credit), len(output["date"]))]:
                errors.append(f"{i['id']}: institutional credit cannot precede lineage publication")
            if "credit" in output and output["credit"].get("institute_id") != i["id"]:
                errors.append(f"{i['id']}: output credit institute must match its institute")
            if not output.get("lineage") or not output.get("attribution"):
                errors.append(
                    f"{i['id']}: output needs lineage and institutional attribution"
                )
        for agreement in i.get("agreements", []):
            if (
                not agreement.get("participation")
                or agreement.get("domestic_interagency") is not False
            ):
                errors.append(
                    f"{i['id']}: agreement needs qualifying participation and domestic exclusion"
                )
            peer = agreement.get("peer_institute_id")
            if agreement["counterparty_type"] == "institute":
                if peer not in institutes or peer == i["id"]:
                    errors.append(
                        f"{i['id']}: institute counterparty must identify a peer institute"
                    )
            elif peer:
                errors.append(
                    f"{i['id']}: non-institute counterparty cannot have peer_institute_id"
                )
        for funding in i.get("funding", []):
            code = funding.get("annual_basis_code")
            profile = funding.get("annual_profile", [])
            if profile:
                current = [p for p in profile if p["start"] <= as_of <= p["end"]]
                if len(current) > 1:
                    errors.append(
                        f"{i['id']}: overlapping current fiscal profile allocations"
                    )
                elif current and code != "allocation_current_year":
                    errors.append(
                        f"{i['id']}: annual amount must use current fiscal profile allocation and period"
                    )
                if any(p["start"] > p["end"] for p in profile):
                    errors.append(f"{i['id']}: invalid fiscal profile period")
            if funding.get("annual_amount") is None:
                continue
            code = funding.get("annual_basis_code")
            if not code:
                errors.append(f"{i['id']}: annual amount needs annual_basis_code")
            elif code == "period_mean":
                amount, years = funding.get("amount"), funding.get("years")
                if (
                    amount is None
                    or not years
                    or not funding.get("period")
                ):
                    errors.append(
                        f"{i['id']}: period_mean requires amount, positive years and period"
                    )
            elif code == "allocation_current_year":
                period = funding.get("annual_period", {})
                if not period.get("start", "9999") <= as_of <= period.get("end", ""):
                    errors.append(
                        f"{i['id']}: annual allocation must cover dataset.as_of"
                    )
        for person in i.get("people", []):
            if not person.get("title_status"):
                errors.append(
                    f"{i['id']} {person['name']}: leadership needs title_status"
                )
            if person.get("title_status") == "unpublished" and (
                person["role"] != "lead"
                or "no formal title published" not in person.get("notes", "")
            ):
                errors.append(
                    f"{i['id']} {person['name']}: unpublished title requires lead and no formal title published note"
                )
    return errors
