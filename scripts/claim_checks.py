"""Claims: one sentence, one bound quote and a code where a code fits; the structural checks and
the generated institutes[].functions rows.

Design in docs/design/claims-1.1.md (sections 1, 2, 3 and 5; build steps A1 and A2). A claim sits
in jurisdictions[].claims or institutes[].claims. A function claim on an institute is about the
institute; on a jurisdiction it is about the related body its body_id names. A fact claim has no
code and sits on an institute or on a jurisdiction with a status. A development claim is a state's
lead development, at most one, on a jurisdiction with none identified: its act and date are bound
too, the act to a primary source of the state, dated within three years before check.last_checked.
When closest_body.body_id names the closest body, its gap needs an excerpt and, if the body has role
claims, exactly one is marked lead and quotes its excerpt's gloss (or the excerpt). Until step B
(the migrated record) those two rules bind only where body_id is set; designation_step is optional.
Function codes, their tests and groups live only in dataset.vocabularies.function; a retired code
stays there with deprecated and replaced_by. check.functions (institutes only) lists the codes an
institute was coded against: a listed code with no claim is not_found, an unlisted current code
not_coded. The code list and the functions rows are provisional (dataset.provisional).
"""

import json
import re
from datetime import date
from pathlib import Path

if __package__:
    from .row_checks import SENTENCE_BREAK
else:
    from row_checks import SENTENCE_BREAK  # type: ignore[import-not-found, no-redef]

SCHEMA = Path(__file__).resolve().parent.parent / "schema" / "aisi.schema.json"
# Generated functions values; the first two are claim bases.
BASES = ("mandate", "activity")
FUNCTION_VALUES = (*BASES, "both", "not_found", "not_coded")
SENTENCE_END = (".", "!", "?")
CLOSERS = "\"'’”)]"  # a sentence may end inside a closing quotation mark or bracket
# The leaves of a claim that need exactly one bound excerpt, by kind.
CLAIM_LEAVES = {"function": ("text",), "fact": ("text",), "development": ("text", "act", "date")}
DEVELOPMENT_YEARS = 3  # the level 2 bar: an act dated within three years before check.last_checked
QUOTED = re.compile(r'"([^"]*)"|“([^”]*)”')


def current_codes(vocabularies: dict) -> list[str]:
    """Function codes in vocabulary order, retired codes left out."""
    return [
        code
        for code, entry in vocabularies.get("function", {}).items()
        if not entry.get("deprecated")
    ]


def function_rows(institute: dict, vocabularies: dict) -> list[dict] | None:
    """One row per current code: the bases of the institute's function claims, else not_found or not_coded.

    None when the dataset defines no function vocabulary.
    """
    if not vocabularies.get("function"):
        return None
    coded = set(institute.get("check", {}).get("functions", {}).get("codes", []))
    claims = [c for c in institute.get("claims", []) if c.get("kind") == "function"]
    rows = []
    for code in current_codes(vocabularies):
        matching = [c for c in claims if c.get("code") == code]
        bases = {c.get("basis") for c in matching}
        if bases >= set(BASES):
            value = "both"
        elif bases & set(BASES):
            value = next(b for b in BASES if b in bases)
        else:
            value = "not_found" if code in coded else "not_coded"
        rows.append(
            {"code": code, "value": value, "claim_ids": [c.get("id") for c in matching]}
        )
    return rows


def required_claim_pointers(d: dict) -> set[str]:
    """Exact leaves that need a bound excerpt.

    Every claim's text, and a development's act and date; the gap of a closest body that names its
    related body (closest_body.body_id); each designation_step's text and date.
    """
    required = set()
    for collection in ("jurisdictions", "institutes"):
        for n, record in enumerate(d[collection]):
            for k, claim in enumerate(record.get("claims", [])):
                keys = CLAIM_LEAVES.get(claim.get("kind"), ("text",))
                required.update(f"/{collection}/{n}/claims/{k}/{key}" for key in keys)
    for n, record in enumerate(d["jurisdictions"]):
        if "body_id" in record.get("closest_body", {}):
            required.add(f"/jurisdictions/{n}/closest_body/gap")
        for k, body in enumerate(record.get("related_bodies", [])):
            if "designation_step" in body:
                required.update(f"/jurisdictions/{n}/related_bodies/{k}/designation_step/{key}" for key in ("text", "date"))
    return required


def years_before(day: str, years: int) -> str:
    """The same calendar day `years` earlier (ISO dates); 29 February becomes 28 February."""
    start = date.fromisoformat(day)
    try:
        return start.replace(year=start.year - years).isoformat()
    except ValueError:
        return start.replace(year=start.year - years, day=28).isoformat()


def quoted_spans(text: str) -> list[str]:
    """The spans of text inside straight or curly double quotation marks."""
    return [a or b for a, b in QUOTED.findall(text)]


def check_development(where: str, record: dict, claim: dict, vocabularies: dict, leaves: dict) -> list[str]:
    """A lead development: code, the clause shape, the act's source and the three-year window.

    leaves maps each of the claim's required leaves to the (source, excerpt) pairs that bind it.
    """
    errors = []
    vocabulary = vocabularies.get("development", {})
    code = claim["code"]
    if code not in vocabulary:
        errors.append(f"{where}: unknown development code {code!r}")
    elif vocabulary[code].get("deprecated"):
        errors.append(f"{where}: development code {code!r} is retired")
    if record.get("status") != "none_identified":
        errors.append(f"{where}: a development sits only on a jurisdiction with none identified")
    text = claim["text"].strip()
    sentence = f"On {claim['date']} {claim['act'].strip()}, which {text}."
    if not text[:1].islower() or text.rstrip(CLOSERS).endswith(SENTENCE_END) or SENTENCE_BREAK.search(sentence):
        errors.append(
            f"{where}: a development's text is the clause that ends 'On {{date}} {{act}}, which {{text}}.': lower-case start, no full stop, one sentence"
        )
    for source, _ in leaves.get("act", []):
        if source.get("type") != "primary" or source.get("origin") != record["id"]:
            errors.append(
                f"{where}: the act's excerpt is from {source['id']}, not a primary source of {record['id']}"
            )
    last_checked = record.get("check", {}).get("last_checked")
    if not last_checked:
        errors.append(f"{where}: a development needs check.last_checked, which its three-year window counts back from")
    elif not years_before(last_checked, DEVELOPMENT_YEARS) <= claim["date"] <= last_checked:
        errors.append(
            f"{where}: a development is dated within {DEVELOPMENT_YEARS} years before check.last_checked ({last_checked}), not {claim['date']}"
        )
    return errors


def check_lead(where: str, claim: dict, leaves: dict) -> list[str]:
    """The lead claim quotes its excerpt: a quoted span in the gloss, or in the excerpt when it has none (case ignored)."""
    spans = quoted_spans(claim["text"])
    for _, excerpt in leaves.get("text", []):
        quote = (excerpt.get("gloss") or excerpt["text"]).casefold()
        if any(span.strip().casefold() in quote for span in spans if span.strip()):
            return []
    return [f"{where}: the lead claim's text quotes, in quotation marks, a span of its excerpt's gloss, or of the excerpt when it has none"]


def schema_node(path: str) -> bool:
    """Whether a dataset.provisional path ('institutes[].functions', 'vocabularies.function') names a schema field."""
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    root = schema["properties"]
    head = path.split(".")[0].removesuffix("[]")
    node = schema if head in root and head != "dataset" else root["dataset"]
    for step in path.split("."):
        name, many = step.removesuffix("[]"), step.endswith("[]")
        while "$ref" in node:
            node = schema["$defs"][node["$ref"].rsplit("/", 1)[1]]
        node = node.get("properties", {}).get(name)
        if node is None:
            return False
        if many:
            while "$ref" in node:
                node = schema["$defs"][node["$ref"].rsplit("/", 1)[1]]
            node = node.get("items")
            if node is None:
                return False
    return True


def check_claims(d: dict) -> list[str]:
    """Cross-field checks JSON Schema cannot express; d has resolved excerpt pointers."""
    errors: list[str] = []
    vocabularies = d["dataset"].get("vocabularies", {})
    vocabulary = vocabularies.get("function", {})
    current = set(current_codes(vocabularies))
    for path in d["dataset"].get("provisional", []):
        if not schema_node(path):
            errors.append(f"dataset.provisional: {path!r} names no field in the schema")
    for name in ("function", "development"):
        codes = vocabularies.get(name, {})
        for code, entry in codes.items():
            for successor in entry.get("replaced_by", []):
                # A successor may itself be retired later (A to B, then B to C); it must stay in the list.
                if successor not in codes or successor == code:
                    errors.append(
                        f"dataset.vocabularies.{name}.{code}: replaced_by {successor!r} is not another code in the list"
                    )
    binding: dict[str, list[tuple[dict, dict]]] = {}
    for source in d["sources"]:
        for excerpt in source.get("excerpts", []):
            for pointer in excerpt["supports"]:
                binding.setdefault(pointer, []).append((source, excerpt))
    seen: set[str] = set()
    for collection in ("jurisdictions", "institutes"):
        for n, record in enumerate(d[collection]):
            closest = record.get("closest_body", {}) if collection == "jurisdictions" else {}
            closest_id = closest.get("body_id")
            if closest_id is not None and closest_id not in {b.get("id") for b in record.get("related_bodies", [])}:
                errors.append(f"{record['id']}: closest_body.body_id {closest_id!r} is not a related body of this jurisdiction")
            claims = record.get("claims", [])
            if sum(c["kind"] == "development" for c in claims) > 1:
                errors.append(f"{record['id']}: a jurisdiction has at most one development claim")
            roles = [c for c in claims if c["kind"] == "function" and closest_id is not None and c.get("body_id") == closest_id]
            leads = sum(bool(c.get("lead")) for c in claims)
            if leads > 1:
                errors.append(f"{record['id']}: at most one claim is marked lead")
            elif roles and not leads:
                errors.append(f"{record['id']}: the closest body has role claims, so one of them is marked lead")
            functions = record.get("check", {}).get("functions")
            if functions and collection == "jurisdictions":
                errors.append(
                    f"{record['id']}: check.functions is for institutes only"
                )
            elif functions:
                for code in functions["codes"]:
                    if code not in current:
                        errors.append(
                            f"{record['id']}: check.functions code {code!r} is not a current function code"
                        )
            bodies = {b.get("id") for b in record.get("related_bodies", [])}
            for k, claim in enumerate(record.get("claims", [])):
                where = f"{record['id']} claim {claim['id']}"
                if claim["id"] in seen:
                    errors.append(f"{where}: claim id is used twice; claim ids are never reused")
                seen.add(claim["id"])
                if claim["kind"] == "function":
                    code = claim["code"]
                    if code not in vocabulary:
                        errors.append(f"{where}: unknown function code {code!r}")
                    elif code not in current:
                        errors.append(f"{where}: function code {code!r} is retired")
                    if collection == "jurisdictions" and claim.get("body_id") not in bodies:
                        errors.append(
                            f"{where}: body_id {claim.get('body_id')!r} is not a related body of this jurisdiction"
                        )
                if (
                    claim["kind"] == "fact"
                    and collection == "jurisdictions"
                    and record.get("status") == "none_identified"
                ):
                    errors.append(
                        f"{where}: a fact claim sits only on an institute or a jurisdiction with a status"
                    )
                leaves = {
                    key: binding.get(f"/{collection}/{n}/claims/{k}/{key}", [])
                    for key in CLAIM_LEAVES[claim["kind"]]
                }
                if claim["kind"] == "development":
                    errors += check_development(where, record, claim, vocabularies, leaves)
                else:
                    text = claim["text"].strip()
                    if not text.rstrip(CLOSERS).endswith(SENTENCE_END) or SENTENCE_BREAK.search(text):
                        errors.append(f"{where}: text is one sentence, ending with its full stop")
                if claim.get("lead"):
                    if not any(role is claim for role in roles):
                        errors.append(f"{where}: lead marks a role claim of the closest body (closest_body.body_id)")
                    errors += check_lead(where, claim, leaves)
                for key, excerpts in leaves.items():
                    if len(excerpts) > 1:
                        errors.append(
                            f"{where}: {key} has {len(excerpts)} bound excerpts; a claim has exactly one quote"
                        )
    return errors
