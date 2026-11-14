"""Generated reader text for a migrated record: the summary, notes, related-body roles and the closest body's note.

Design in docs/design/claims-1.1.md, section 3 (build step B). A migrated record is a jurisdiction whose authored
input has no summary; only it is generated, and a record with an authored summary builds as it always has. Words in
the patterns are fixed here; everything that varies comes from fields, claims and check. Negative findings come only
from pattern 1, dated by check.last_checked. Expiry (section 4) is step C: a claim marked expired is skipped and
nothing else here reads freshness.

A state without a status: pattern 1, then the first of patterns 2, 3 and 4 that applies. A state with a status:
pattern 5, then pattern 6 when its institute has a mandate_short.
"""

import datetime as dt

MONTHS = ("January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
          "November", "December")
# Pattern 2: the stage prefix, where the body does not exist yet.
STAGE_PREFIX = {"exists": "Closest body", "planned": "Planned closest body", "proposed": "Proposed closest body",
                "under_study": "Closest body under study"}
# Pattern 4: AI Act roles in the order the line names them.
ROLE_WORDS = {"single_point_of_contact": "single point of contact",
              "market_surveillance": "market-surveillance authority", "notifying": "notifying authority"}
# Pattern 5: the institute's legal form; the host in brackets for a unit, so no article is needed before it.
FORM_WORDS = {"new_body": "a new body", "unit_in_existing_body": "a unit within an existing body",
              "federation_of_existing_bodies": "a network of existing bodies", "university_centre": "a university centre"}
STATUS_WORDS = {"planned": "Planned", "proposed": "Proposed", "exploring": "Exploring"}


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


def notes_text(record: dict) -> str:
    """The record's notes: check.log in date order (ties keep the authored order), one paragraph each."""
    log = sorted(enumerate(record.get("check", {}).get("log", [])), key=lambda pair: (pair[1]["date"], pair[0]))
    return "\n\n".join(entry["text"].strip() for _, entry in log)


def pattern_1(record: dict) -> str:
    return f"No qualifying institute identified as of {month_of(record['check']['last_checked'])}."


def pattern_2(record: dict) -> str | None:
    closest = record.get("closest_body")
    if closest is None:
        return None
    line = f"{STAGE_PREFIX[closest['stage']]}: {sentence(closest['name'])}"
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
        line += "; 1 other AI Act body is listed below"
    elif others > 1:
        line += f"; {others} other AI Act bodies are listed below"
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


def generate(record: dict, institute: dict | None, basis_event: dict | None) -> dict:
    """The generated texts of a migrated record: summary, notes, roles by body id, and the closest body's note."""
    closest = record.get("closest_body")
    if closest is not None and "body_id" not in closest:
        raise GeneratorError(f"{record['id']}: a migrated record's closest body names its related body (closest_body.body_id)")
    texts = {
        "summary": summary_text({**record, "status_basis_event": basis_event}, institute),
        "notes": notes_text(record),
        "roles": {b["id"]: role_text(record, b["id"]) for b in record.get("related_bodies", [])},
    }
    if closest is not None:
        note = role_text(record, closest["body_id"])
        if not note:
            raise GeneratorError(
                f"{record['id']}: the closest body has no role claim, so its generated note would be empty; "
                "closest_body.note must not be empty (a name-only closest body is a 1.2.0 question)"
            )
        texts["note"] = note
    return texts
