"""Generated reader text for a migrated record: summary, notes, body roles, closest-body note and undisclosed funding.

Design in docs/design/claims-1.1.md, section 3 (build step B). A migrated record is a jurisdiction whose authored
input has no summary; only it is generated, and a record with an authored summary builds as it always has. Words in
the patterns are fixed here; everything that varies comes from fields, claims and check. Negative findings come from
pattern 1, dated by check.last_checked, or the structured none_disclosed funding state. Expiry (section 4) is step C:
a claim marked expired (claims[].expired,
set by rereads.join in published.publish, which then words the record again) is skipped, with one exception: the
closest body's lead claim, expired because its source is offline or could not be re-read, stays in pattern 2 and the
closest-body note with its re-read state after it (owner, 15 November, "Keep last quote, marked"; section 4).

A state without a status: pattern 1, then the first of patterns 2, 3 and 4 that applies. A state with a status:
pattern 5, then pattern 6 when its institute has a mandate_short.
"""

import datetime as dt

MONTHS = ("January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
          "November", "December")
LANGUAGE_NAMES = {
    "am": "Amharic", "ar": "Arabic", "az": "Azerbaijani", "bg": "Bulgarian", "bm": "Bambara", "bn": "Bengali",
    "bs": "Bosnian", "ca": "Catalan", "cnr": "Montenegrin", "cs": "Czech", "da": "Danish", "de": "German",
    "dv": "Dhivehi", "dz": "Dzongkha", "el": "Greek", "en": "English", "es": "Spanish", "et": "Estonian",
    "fa": "Persian", "fi": "Finnish", "fr": "French", "ha": "Hausa", "he": "Hebrew", "hr": "Croatian",
    "ht": "Haitian Creole", "hu": "Hungarian", "hy": "Armenian", "id": "Indonesian", "is": "Icelandic",
    "it": "Italian", "ja": "Japanese", "ka": "Georgian", "kk": "Kazakh", "km": "Khmer", "ko": "Korean",
    "ky": "Kyrgyz", "lo": "Lao", "lt": "Lithuanian", "lv": "Latvian", "mk": "Macedonian", "mn": "Mongolian",
    "mos": "Mossi", "ms": "Malay", "my": "Burmese", "nb": "Norwegian Bokmål", "ne": "Nepali", "nl": "Dutch",
    "nn": "Norwegian Nynorsk", "no": "Norwegian", "pl": "Polish", "ps": "Pashto", "pt": "Portuguese",
    "rn": "Kirundi", "ro": "Romanian", "ru": "Russian", "si": "Sinhala", "sk": "Slovak", "sl": "Slovenian",
    "sq": "Albanian", "sr": "Serbian", "sv": "Swedish", "sw": "Swahili", "ta": "Tamil", "tg": "Tajik",
    "th": "Thai", "ti": "Tigrinya", "tk": "Turkmen", "tr": "Turkish", "uk": "Ukrainian", "ur": "Urdu",
    "uz": "Uzbek", "vi": "Vietnamese", "zh": "Chinese",
}
CHECK_TOPICS = {
    "status": "status", "head": "head", "reports_to": "reporting line", "national_partners": "partners",
    "staff": "staff", "funding": "budget", "agreements": "agreements", "model_access": "model access",
    "testing_capacity": "testing capacity", "outputs": "published work", "closest_body": "closest body",
    "related_bodies": "AI Act authorities and related bodies",
}
# Pattern 2: the body's stage after its name, where it does not exist yet. The stage is the body's, not our choice of
# it, so no stage word stands before "closest body" (owner, 17 November: "Proposed closest body" read as our proposal).
STAGE_TAIL = {"exists": "", "planned": ", planned and not yet set up", "proposed": ", proposed and not yet set up",
              "under_study": ", under study and not yet set up"}
# Counts inside a sentence: words for one to nine, figures from 10 (owner, 17 November).
COUNT_WORDS = ("zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine")
# Pattern 4: AI Act roles in the order the line names them.
ROLE_WORDS = {"single_point_of_contact": "single point of contact",
              "market_surveillance": "market-surveillance authority", "notifying": "notifying authority"}
# Pattern 5: the institute's legal form; the host in brackets for a unit, so no article is needed before it.
FORM_WORDS = {"new_body": "a new body", "unit_in_existing_body": "a unit within an existing body",
              "federation_of_existing_bodies": "a network of existing bodies", "university_centre": "a university centre"}
STATUS_WORDS = {"planned": "Planned", "proposed": "Proposed", "exploring": "Exploring"}
UNDISCLOSED_FUNDING = {
    "whole_institute": "No budget published for the institute as a whole.",
    "component": "No budget published for this part of the institute.",
    "not_institute_specific": "No budget published for this wider funding.",
}
# The re-read states under which an expired lead claim is kept, marked, in the page's freshness wording (section 4).
# A quote no longer found in a readable source is not kept: the fact may have changed (hub default, for the owner).
KEPT_STATES = {"source_gone": "Source offline since {since}; last found {last_found}.",
               "blocked_or_failed": "Source could not be re-read since {since}; last found {last_found}."}


class GeneratorError(ValueError):
    """A migrated record that the patterns cannot word."""


def long_date(day: str) -> str:
    """'2 November 2023'; a partial date reads 'July 2025' or '2025'."""
    parts = day.split("-")
    if len(parts) == 1:
        return parts[0]
    month = MONTHS[int(parts[1]) - 1]
    if len(parts) == 2:
        return f"{month} {parts[0]}"
    return f"{dt.date.fromisoformat(day).day} {month} {parts[0]}"


def month_of(day: str) -> str:
    """'October 2026' for any date in that month."""
    year, month = day.split("-")[:2]
    return f"{MONTHS[int(month) - 1]} {year}"


def words(items: list[str]) -> str:
    """'a', 'a and b', 'a, b and c'."""
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]


def count(n: int) -> str:
    """A count inside a sentence: a word for one to nine, a figure from 10."""
    return COUNT_WORDS[n] if 0 < n < 10 else str(n)


def sentence(text: str) -> str:
    """Text ending with one full stop (a closing quotation mark may follow it; a bracket ends a name, not a sentence)."""
    text = text.strip()
    return text if text.rstrip("\"'’”").endswith((".", "!", "?")) else text + "."


def live(claims: list[dict]) -> list[dict]:
    return [c for c in claims if not c.get("expired")]


def role_claims(record: dict, body_id: str | None) -> list[dict]:
    """A related body's role claims, in authored order, expired ones left out."""
    if body_id is None:
        return []
    return live([c for c in record.get("claims", []) if c["kind"] == "function" and c.get("body_id") == body_id])


def role_text(record: dict, body_id: str | None) -> str:
    """A related body's role: its role claims' sentences; empty when it has none."""
    return " ".join(c["text"].strip() for c in role_claims(record, body_id))


def kept_lead(record: dict) -> dict | None:
    """The closest body's lead claim when it has expired and is kept, marked: its source is offline or unreadable."""
    closest = record.get("closest_body") or {}
    lead = [c for c in record.get("claims", []) if c["kind"] == "function" and c.get("lead")
            and closest.get("body_id") is not None and c.get("body_id") == closest["body_id"]]
    if lead and lead[0].get("expired") and lead[0].get("reread", {}).get("state") in KEPT_STATES:
        return lead[0]
    return None


def marked(claim: dict) -> str:
    """A kept lead claim's sentence and its re-read state."""
    reread = claim["reread"]
    state = KEPT_STATES[reread["state"]].format(since=long_date(reread["since"]), last_found=long_date(reread["last_found"]))
    return f"{claim['text'].strip()} {state}"


def shown_lead(record: dict) -> dict | None:
    """The closest body's claim that pattern 2 quotes: the kept lead, else the live lead."""
    closest = record.get("closest_body") or {}
    if kept := kept_lead(record):
        return kept
    lead = [c for c in role_claims(record, closest.get("body_id")) if c.get("lead")]
    return lead[0] if lead else None


def closest_texts(record: dict, *, without: dict | None = None) -> list[str]:
    """The closest body's role sentences in authored order, the kept lead in its place marked, one claim left out."""
    closest = record["closest_body"]
    kept = kept_lead(record)
    return [marked(c) if c is kept else c["text"].strip() for c in record.get("claims", [])
            if c["kind"] == "function" and c.get("body_id") == closest["body_id"] and (c is kept or not c.get("expired"))
            and c is not without]


def closest_note(record: dict) -> str:
    """The closest body's note: its role sentences other than the one the summary quotes (hub default of 15 November,
    awaiting the owner: the card does not say the lead twice); its name as a sentence when none is left, so the
    published note stays non-empty (schema), and the page shows no note that is only the name."""
    return " ".join(closest_texts(record, without=shown_lead(record))) or sentence(record["closest_body"]["name"])


def closest_warning(record: dict) -> str | None:
    """For the release warnings and the next sweep: a migrated record whose closest body has no live role claim."""
    closest = record.get("closest_body")
    if closest is None or "body_id" not in closest or role_claims(record, closest["body_id"]):
        return None
    where = f"{record['id']}: closest body {closest['name']}"
    if kept := kept_lead(record):
        return f"{where}: its lead claim {kept['id']} expired ({kept['reread']['state']} since {kept['reread']['since']}) and is shown marked; re-check it in the next sweep"
    return f"{where} has no live role claim and is shown by name alone; find a quote in the next sweep"


def check_language_names(record: dict) -> None:
    """Every search language needs a reader name, including searches not shown in generated notes."""
    check = record.get("check", {})
    codes = {s["language"] for s in check.get("searches", [])}
    codes.update(code for entry in check.get("passes", []) for code in entry["languages"])
    if unknown := sorted(codes - LANGUAGE_NAMES.keys()):
        raise GeneratorError(f"{record['id']}: unknown check language code: {', '.join(unknown)}")


def language_words(codes: list[str]) -> str:
    """Distinct languages in authored order, with English last."""
    ordered = sorted(dict.fromkeys(codes), key=lambda code: code == "en")
    return words([LANGUAGE_NAMES[code] for code in ordered])


def notes_text(record: dict, as_of: str) -> str:
    """How we checked: full check, a future due date when shown, then searches newest first (ties in authored order).
    The page shows the first three paragraphs and folds only the earlier searches."""
    check_language_names(record)
    check = record.get("check", {})
    paragraphs = []
    last = check.get("last_checked")
    if check.get("method") == "individual":
        # Without a recorded full check, the historical date is only an upper bound.
        when = "on" if check.get("last_checked_basis") == "recorded_full_check" else "on or before"
        line = f"Checked in full {when} {long_date(last)}"
        if searches := check.get("searches"):
            line += f", searching in {language_words([s['language'] for s in searches])}"
        paragraphs.append(line + ".")
    elif check.get("method") == "broad_search":
        paragraphs.append(f"Covered by a search across many states by {long_date(last)}.")
    if (due := record.get("next_check", {}).get("date")) and due > as_of:
        paragraphs.append(f"Next check due by {long_date(due)}.")
    for entry in sorted(check.get("passes", []), key=lambda entry: entry["date"], reverse=True):
        if entry["date"] > last:
            paragraphs.append(f"On {long_date(entry['date'])} we searched {words(entry['places'])} in "
                              f"{language_words(entry['languages'])}, for information on {words([CHECK_TOPICS[key] for key in entry['fields']])}.")
    return "\n\n".join(paragraphs)


def next_check_warning(record: dict, as_of: str) -> str | None:
    """An overdue next check is a release warning, never a generated promise to the reader."""
    if (due := record.get("next_check", {}).get("date")) and due < as_of:
        return f"{record['id']}: next_check {due} is before as_of"
    return None


def pattern_1(record: dict) -> str:
    return f"No qualifying institute identified as of {month_of(record['check']['last_checked'])}."


def pattern_2(record: dict) -> str | None:
    closest = record.get("closest_body")
    if closest is None:
        return None
    line = f"Closest body: {sentence(closest['name'] + STAGE_TAIL[closest['stage']])}"
    if kept := kept_lead(record):
        return f"{line} {marked(kept)}"
    lead = [c for c in role_claims(record, closest.get("body_id")) if c.get("lead")]
    return f"{line} {lead[0]['text'].strip()}" if lead else line


def pattern_3(record: dict) -> str | None:
    development = live([c for c in record.get("claims", []) if c["kind"] == "development"])
    if not development:
        return None
    claim = development[0]
    return f"On {long_date(claim['date'])} {claim['act'].strip()}, which {claim['text'].strip()}."


def lead_authority(authorities: list[dict]) -> dict | None:
    """The authority holding the single point of contact, else the most roles; ties by name. None when no authority
    names a role (an entry without authority_roles is counted among the others but never leads)."""
    named = [b for b in authorities if b.get("authority_roles")]
    if not named:
        return None
    return min(named, key=lambda b: ("single_point_of_contact" not in b["authority_roles"],
                                     -len(b["authority_roles"]), body_name(b)))


def body_name(body: dict) -> str:
    return body.get("name_en") or body["name"]


def pattern_4(record: dict) -> str | None:
    authorities = [b for b in record.get("related_bodies", []) if b["kind"] == "ai_act_authority"]
    body = lead_authority(authorities)
    if body is None:
        return None
    roles = words([ROLE_WORDS[r] for r in ROLE_WORDS if r in body["authority_roles"]])
    state = body.get("designation_state")
    if state == "designated":
        line = f"{body_name(body)} is its AI Act {roles}"
    elif state == "proposed":
        line = f"{body_name(body)} is proposed as its AI Act {roles}"
        if step := body.get("designation_step"):
            line += f" ({step['text'].strip()} on {long_date(step['date'])})"
    else:
        line = f"{body_name(body)} is listed as its AI Act {roles}, but no national designation is confirmed"
    others = len(authorities) - 1
    if others == 1:
        line += "; one other AI Act body is listed below"
    elif others > 1:
        line += f"; {count(others)} other AI Act bodies are listed below"
    return line + "."


def pattern_5(record: dict, institute: dict | None) -> str:
    status = record["status"]
    if status == "established":
        if institute is None or not institute.get("established"):
            raise GeneratorError(f"{record['id']}: an established state needs its institute's established date")
        established = institute["established"]
        preposition = "on" if len(established) == 10 else "in"  # a partial date: "set up in 2026"
        line = f"{institute.get('short_name') or institute['name']} was set up {preposition} {long_date(established)}"
        form = institute.get("legal_form")
        if form in FORM_WORDS:
            line += f" as {FORM_WORDS[form]}"
            if form == "unit_in_existing_body" and institute.get("host"):
                line += f" ({institute['host']})"
        return line + "."
    basis = record.get("status_basis_event")
    if basis is None or not basis.get("short"):
        raise GeneratorError(f"{record['id']}: pattern 5 needs the short line of the event that sets the status")
    return f"{STATUS_WORDS[status]} since {long_date(record['status_since'])}: {basis['short'].strip().rstrip('.')}."


def pattern_6(institute: dict | None) -> str | None:
    text = (institute or {}).get("mandate_short", "").strip()
    return f"It {text[:1].lower()}{text[1:]}" if text else None


def summary_text(record: dict, institute: dict | None = None) -> str:
    """The record's summary from the six patterns.

    record is the materialised jurisdiction; for a state with a status other than established, record carries the
    event that sets its status under status_basis_event (the caller passes it; it is not published).
    """
    if record["status"] == "none_identified":
        follow = pattern_2(record) or pattern_3(record) or pattern_4(record)
        return " ".join(x for x in (pattern_1(record), follow) if x)
    return " ".join(x for x in (pattern_5(record, institute), pattern_6(institute)) if x)


def summary_leaves(record: dict, institute: dict | None, basis: int | None) -> list[tuple[str, str]]:
    """The leaves a generated summary words, as ("jurisdiction" | "institute", path below the record), so the
    summary is cited from their excerpts. Pattern 1 is a finding from check and cites nothing. basis: the index of
    the timeline event that sets the status (published.status_basis), for pattern 5 below established."""
    claims = record.get("claims", [])
    if record["status"] != "none_identified":
        if record["status"] == "established":
            leaves = [("institute", key) for key in ("established", "legal_form", "host")]
        else:
            leaves = [("jurisdiction", f"timeline/{basis}")] if basis is not None else []
        return leaves + ([("institute", "mandate_short")] if pattern_6(institute) else [])
    if record.get("closest_body") is not None:
        lead = shown_lead(record)
        return [("jurisdiction", f"closest_body/{key}") for key in ("name", "stage")] + (
            [("jurisdiction", f"claims/{claims.index(lead)}/text")] if lead is not None else [])
    if development := live([c for c in claims if c["kind"] == "development"]):
        n = claims.index(development[0])
        return [("jurisdiction", f"claims/{n}/{key}") for key in ("act", "date", "text")]
    bodies = record.get("related_bodies", [])
    authority = lead_authority([b for b in bodies if b["kind"] == "ai_act_authority"])
    if authority is None:
        return []
    k = bodies.index(authority)
    return [("jurisdiction", f"related_bodies/{k}/{key}") for key in ("designation_state", "authority_roles", "designation_step")]


def generate(record: dict, institute: dict | None, basis_event: dict | None, *, as_of: str) -> dict:
    """The generated texts of a migrated record, with roles and undisclosed funding descriptions keyed by id."""
    closest = record.get("closest_body")
    if closest is not None and "body_id" not in closest:
        raise GeneratorError(f"{record['id']}: a migrated record's closest body names its related body (closest_body.body_id)")
    texts = {
        "summary": summary_text({**record, "status_basis_event": basis_event}, institute),
        "notes": notes_text(record, as_of),
        "roles": {b["id"]: role_text(record, b["id"]) for b in record.get("related_bodies", [])},
        "funding_descriptions": {f["id"]: UNDISCLOSED_FUNDING[f["coverage"]]
                                 for f in (institute or {}).get("funding", []) if f.get("kind") == "none_disclosed"},
    }
    if closest is not None:
        texts["note"] = closest_note(record)
        if kept_lead(record):  # the body's role keeps every sentence, the kept lead marked
            texts["roles"][closest["body_id"]] = " ".join(closest_texts(record))
    return texts
