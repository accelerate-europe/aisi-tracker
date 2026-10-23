"""Head, reporting line and national partners: invariants, evidence pointers and card selection.

Design in docs/design/schema-1.0.md ("Head, reporting line and national partners"); the
editorial rules are in dataset.method (Leadership, Reporting line, National partners).
The fields are optional per institute until the data lands, but once any institute has one
of them every institute has all three (rows are added to every card together).
Reader labels come only from dataset.vocabularies (partner_relation, partner_cyber_none_published).
"""

import re

# Stable codes the checks and the card depend on. Labels never live here.
# Relation order on the card, strongest first; the codes are the schema enum.
RELATIONS = ("member", "works_with", "planned")
CARD_PARTNERS = 6  # the card shows at most this many; the data and downloads keep all
MAX_HOW_WORDS = 25
# Require exact excerpt bindings for every what descriptor and present cyber flag,
# and at most one flagged entry per institute. False is for legacy fixtures only.
STRICT_PARTNER_DETAILS = True
PARTNER_BOUND_FIELDS = ("name", "relation", "how", "cyber_agency", "what")
ROWS = ("head", "reports_to", "national_partners")
NON_LATIN = re.compile(r"[Ͱ-ϿЀ-ԯ԰-֏֐-ۿऀ-෿฀-໿Ⴀ-ჿἀ-῿぀-ヿ㐀-鿿가-힯]")
SENTENCE_BREAK = re.compile(
    r"""(?:(?<=[a-z0-9]{2})|(?<=[A-Z0-9]{2}))["'’”)\]]*[.!?]["'’”)\]]*\s+["'‘“(\[]*[A-Z]"""
)  # across quotes/brackets too; dotted initials such as "U.S." are not word endings
DATED_NOTE = re.compile(r"\b(?:19|20)\d\d\b")


def partner_order(entries: list[dict]) -> list[dict]:
    """Card order: cyber agency first, then member, works with, planned; authored order is the tie-break (sorted is stable)."""
    return sorted(
        entries,
        key=lambda e: (not e.get("cyber_agency"), RELATIONS.index(e["relation"])),
    )


def partner_name(entry: dict) -> str:
    """The name the card shows."""
    return entry.get("acronym") or entry.get("name_en") or entry["name"]


def partner_text(entry: dict, vocabularies: dict) -> str:
    """'ANSSI (national cyber security agency, member)': the relation word comes from the vocabulary only."""
    relation = vocabularies["partner_relation"][entry["relation"]]["label"]
    return f"{partner_name(entry)} ({entry['what']}, {relation})"


def required_row_pointers(d: dict) -> set[str]:
    """Exact claim leaves that need a bound excerpt (unchanged-quotation rule: never a parent)."""
    required: set[str] = set()
    for n, institute in enumerate(d["institutes"]):
        prefix = f"/institutes/{n}"
        for field, value_key in (("head", "person_id"), ("reports_to", "name")):
            row = institute.get(field)
            if not row:
                continue
            if row["state"] == "stated":
                required.add(f"{prefix}/{field}/{value_key}")
        partners = institute.get("national_partners")
        if not partners:
            continue
        for k, entry in enumerate(partners.get("entries", [])):
            base = f"{prefix}/national_partners/entries/{k}"
            required.update(
                f"{base}/{key}"
                for key in PARTNER_BOUND_FIELDS
                if key in entry
                and (STRICT_PARTNER_DETAILS or key not in ("cyber_agency", "what"))
            )
    return required


def check_rows(d: dict) -> list[str]:
    """Cross-field checks JSON Schema cannot express; shape and states are the schema's."""
    errors: list[str] = []
    vocabularies = d["dataset"].get("vocabularies", {})
    institutes = d["institutes"]
    authored = {i["id"]: [f for f in ROWS if f in i] for i in institutes}
    if any(authored.values()):
        for iid, have in authored.items():
            missing = [f for f in ROWS if f not in have]
            if missing:
                errors.append(
                    f"{iid}: rows are added to every institute together; missing {', '.join(missing)}"
                )
    for institute in institutes:
        iid = institute["id"]
        head = institute.get("head")
        if head and head["state"] == "stated":
            person = next(
                (
                    p
                    for p in institute.get("people", [])
                    if p.get("id") == head["person_id"]
                ),
                None,
            )
            if person is None:
                errors.append(
                    f"{iid}: head.person_id {head['person_id']!r} is not a person of this institute"
                )
            elif person.get("until"):
                errors.append(
                    f"{iid}: head.person_id {head['person_id']!r} names a former appointment (until {person['until']})"
                )
        reports = institute.get("reports_to")
        if reports and reports["state"] == "stated":
            if NON_LATIN.search(reports["name"]) and not reports.get("name_en"):
                errors.append(
                    f"{iid}: reports_to.name in a non-Latin script needs name_en"
                )
            if reports.get("name_en", "").strip() == reports["name"].strip():
                errors.append(
                    f"{iid}: reports_to.name_en repeats the name; omit it when the name is English"
                )
            if ";" in reports["name"] and "; " not in reports["name"]:
                errors.append(
                    f"{iid}: reports_to.name joins bodies with ';' and no space; use '; '"
                )
        for field in ("head", "reports_to", "national_partners"):
            row = institute.get(field)
            if (
                row
                and row["state"] == "not_applicable"
                and institute["status"] == "established"
            ):
                errors.append(
                    f"{iid}: {field}: not_applicable requires no established institute"
                )
            if (
                row
                and row["state"] == "not_published"
                and not DATED_NOTE.search(row.get("notes", ""))
            ):
                errors.append(
                    f"{iid}: {field}.notes must say what was searched and when (a dated search log)"
                )
        partners = institute.get("national_partners")
        if not partners:
            continue
        entries = partners.get("entries", [])
        if (
            STRICT_PARTNER_DETAILS
            and sum(bool(e.get("cyber_agency")) for e in entries) > 1
        ):
            errors.append(
                f"{iid}: national_partners allows at most one cyber_agency entry"
            )
        ids = [e["id"] for e in entries]
        if len(ids) != len(set(ids)):
            errors.append(f"{iid}: national_partners entry ids are not unique")
        if partners["state"] == "stated" and not any(
            e.get("cyber_agency") for e in entries
        ):
            if not DATED_NOTE.search(partners.get("notes", "")):
                errors.append(
                    f"{iid}: no partner entry has cyber_agency, so national_partners.notes must name the cyber agency searched and the date"
                )
        host = institute["host"].casefold().strip()
        reports = institute.get("reports_to", {})
        parents = {
            part.casefold().strip()
            for key in ("name", "name_en")
            for part in reports.get(key, "").split(";")
        } - {""}
        for entry in entries:
            where = f"{iid}: partner {entry['id']}"
            shown = {
                entry["name"].casefold().strip(),
                entry.get("name_en", "").casefold().strip(),
                entry.get("acronym", "").casefold().strip(),
            }
            if host in shown:
                errors.append(
                    f"{where} is the institute's host; the host is never a partner entry"
                )
            if parents & shown:
                errors.append(
                    f"{where} is a reports_to body; the body the institute reports to is never a partner entry"
                )
            if NON_LATIN.search(entry["name"]) and not entry.get("name_en"):
                errors.append(f"{where}: name in a non-Latin script needs name_en")
            words = len(entry["how"].split())
            if words > MAX_HOW_WORDS:
                errors.append(
                    f"{where}: how has {words} words; one sentence of at most {MAX_HOW_WORDS}"
                )
            if SENTENCE_BREAK.search(entry["how"].strip()):
                errors.append(
                    f"{where}: how is one sentence; it has a sentence break inside"
                )
            if (
                entry["relation"] == "member"
                and institute.get("legal_form") != "federation_of_existing_bodies"
            ):
                errors.append(
                    f"{where}: member is reserved for an institute whose legal_form is federation_of_existing_bodies"
                )
    if any(authored.values()):
        relation_vocabulary = set(vocabularies.get("partner_relation", {}))
        if relation_vocabulary != set(RELATIONS):
            errors.append(
                f"dataset.vocabularies.partner_relation must define exactly {', '.join(RELATIONS)}"
            )
        if not vocabularies.get("partner_cyber_none_published"):
            errors.append(
                "dataset.vocabularies.partner_cyber_none_published is required: the muted tail for an institute without a cyber agency partner"
            )
    return errors


def row_display(institute: dict, vocabularies: dict) -> dict | None:
    """Generated card rows. Returns None when the institute has none of the fields."""
    if not any(f in institute for f in ROWS):
        return None
    out: dict = {}

    def ids(record, *fields):
        seen: list[str] = []
        for field in fields:
            for sid in record.get("citations", {}).get(field, []):
                if sid not in seen:
                    seen.append(sid)
        return seen

    head = institute.get("head")
    if head:
        text = None
        if head["state"] == "stated":
            person = next(
                p for p in institute["people"] if p["id"] == head["person_id"]
            )
            text = f"{person['name']}, {person['role']}"
        out["head"] = {
            "state": head["state"],
            "text": text,
            "cited": ids(head, "person_id") if head["state"] == "stated" else [],
        }
    reports = institute.get("reports_to")
    if reports:
        text = None
        if reports["state"] == "stated":
            text = reports.get("name_en") or reports["name"]
        out["reports_to"] = {
            "state": reports["state"],
            "text": text,
            "cited": ids(reports, "name") if reports["state"] == "stated" else [],
        }
    partners = institute.get("national_partners")
    if partners:
        entries = partner_order(partners.get("entries", []))
        shown = entries[:CARD_PARTNERS]
        items = [
            {
                "id": e["id"],
                "text": partner_text(e, vocabularies),
                "name": partner_name(e),
                "how": e["how"],
                "cited": ids(e, "name", "how"),
            }
            for e in entries
        ]
        # One citation group ends the line, as on every card row; it lists the sources of the shown entries in card order.
        cited_ids = [
            sid
            for e in shown
            for sid in ids(e, *PARTNER_BOUND_FIELDS)
        ]
        out["partners"] = {
            "state": partners["state"],
            "items": items[:CARD_PARTNERS],
            "fold_items": items,
            # Wording lives in the vocabulary, so renaming it renames the tail.
            "tail": vocabularies["partner_cyber_none_published"]
            if partners["state"] == "stated"
            and not any(e.get("cyber_agency") for e in entries)
            else None,
            "cited": list(dict.fromkeys(cited_ids))
            if partners["state"] == "stated"
            else [],
        }
    return out
