"""Claims: one sentence, one bound quote and a code where a code fits; the structural checks and
the generated institutes[].functions rows.

Design in docs/design/claims-1.1.md (sections 1, 2 and 5; this is build step A1: the kinds
function and fact). A claim sits in jurisdictions[].claims or institutes[].claims. A function
claim on an institute is about the institute; on a jurisdiction it is about the related body its
body_id names. A fact claim has no code and sits on an institute or on a jurisdiction with a status.
Function codes, their tests and groups live only in dataset.vocabularies.function; a retired code
stays there with deprecated and replaced_by. check.functions (institutes only) lists the codes an
institute was coded against: a listed code with no claim is not_found, an unlisted current code
not_coded. The code list and the functions rows are provisional (dataset.provisional).
"""

import json
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
    """Exact leaves that need a bound excerpt: every claim's text."""
    return {
        f"/{collection}/{n}/claims/{k}/text"
        for collection in ("jurisdictions", "institutes")
        for n, record in enumerate(d[collection])
        for k, _ in enumerate(record.get("claims", []))
    }


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
    for code, entry in vocabulary.items():
        for successor in entry.get("replaced_by", []):
            # A successor may itself be retired later (A to B, then B to C); it must stay in the list.
            if successor not in vocabulary or successor == code:
                errors.append(
                    f"dataset.vocabularies.function.{code}: replaced_by {successor!r} is not another code in the list"
                )
    bound: dict[str, int] = {}
    for source in d["sources"]:
        for excerpt in source.get("excerpts", []):
            for pointer in excerpt["supports"]:
                bound[pointer] = bound.get(pointer, 0) + 1
    seen: set[str] = set()
    for collection in ("jurisdictions", "institutes"):
        for n, record in enumerate(d[collection]):
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
                text = claim["text"].strip()
                if not text.rstrip(CLOSERS).endswith(SENTENCE_END) or SENTENCE_BREAK.search(text):
                    errors.append(f"{where}: text is one sentence, ending with its full stop")
                count = bound.get(f"/{collection}/{n}/claims/{k}/text", 0)
                if count > 1:
                    errors.append(
                        f"{where}: text has {count} bound excerpts; a claim has exactly one quote"
                    )
    return errors
