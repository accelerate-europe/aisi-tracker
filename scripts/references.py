"""Stable authored selectors; RFC 6901 pointers belong to materialised downloads."""

import copy
import hashlib
import json
import re

if __package__:
    from .shared_claims import resolve
else:
    from shared_claims import resolve


IDENTITY_KEYS = {
    "outputs": "lineage",
    "exchange_rates": "currency",
    "memberships": "body",
    "modes": "code",  # institutes[].testing_capacity.modes
}
ID_COLLECTIONS = {
    "funding",
    "staff",
    "people",
    "names",
    "agreements",
    "related_bodies",
    "annual_profile",
    "entries",  # institutes[].national_partners.entries
    "access_events",  # institutes[].model_access.access_events
    "developer_agreements",  # institutes[].model_access.developer_agreements
}


def excerpt_identity(excerpt):
    """Source-scoped quotation identity; binding edits do not change its hash."""
    return excerpt.get("id") or "sha256:" + hashlib.sha256(
        excerpt["text"].encode("utf-8")
    ).hexdigest()


def record_label(document, pointer):
    """Human-readable stable record path, separate from its snapshot pointer."""
    nodes = pointer_ancestors(document, pointer)
    parts = []
    for parent, value, token in zip(nodes, nodes[1:], pointer[1:].split("/")):
        if isinstance(parent, list):
            collection = parts[-1]
            identity = value.get(IDENTITY_KEYS.get(collection, "id"))
            if collection == "searches":
                identity = json.dumps(
                    {key: value.get(key, "") for key in ("query", "date", "language")},
                    ensure_ascii=False, sort_keys=True,
                )
            parts[-1] += f"[{identity or 'identity missing'}]"
        else:
            parts.append(token.replace("~1", "/").replace("~0", "~"))
    return "/".join(parts)


def pointer_ancestors(document, pointer):
    """Resolve an RFC 6901 pointer, rejecting malformed escapes and array indices."""
    if not pointer.startswith("/") or re.search(r"~(?![01])", pointer):
        raise ValueError("expected a root-relative RFC 6901 pointer")
    nodes = [document]
    for token in pointer[1:].split("/"):
        key = token.replace("~1", "/").replace("~0", "~")
        node = nodes[-1]
        if isinstance(node, list):
            if not re.fullmatch(r"0|[1-9][0-9]*", key):
                raise ValueError("invalid array index")
            node = node[int(key)]
        elif isinstance(node, dict):
            node = node[key]
        else:
            raise ValueError("cannot traverse a scalar")
        nodes.append(node)
    return nodes


def stable_selector(document, pointer):
    """Convert a valid pointer to identity matches, never to positional steps."""
    nodes = pointer_ancestors(document, pointer)
    selector = []
    for parent, value, token in zip(nodes, nodes[1:], pointer[1:].split("/")):
        if isinstance(parent, list):
            previous = (
                selector[-1] if selector and isinstance(selector[-1], str) else None
            )
            key = IDENTITY_KEYS.get(previous, "id")
            if not isinstance(value, dict) or key not in value:
                raise ValueError(
                    f"{pointer}: referenced array member needs a stable identity"
                )
            selector.append({key: value[key]})
        else:
            selector.append(token.replace("~1", "/").replace("~0", "~"))
    resolve(document, selector)  # Also rejects ambiguous identities.
    return selector


def support_pointer(document, reference):
    """Legacy pointers are accepted only during the staged authored migration."""
    if isinstance(reference, str):
        return reference
    if not reference:
        raise ValueError("reference needs at least one selector step")
    previous = None
    for step in reference:
        if isinstance(step, dict) and set(step) != {IDENTITY_KEYS.get(previous, "id")}:
            raise ValueError(
                f"reference needs a stable identity for {previous}, got {step}"
            )
        previous = step if isinstance(step, str) else None
    return resolve(document, reference)[2]


def materialise_references(document):
    """Copy and resolve after all records are in their published positions."""
    data = copy.deepcopy(document)
    references = [
        ref
        for source in data.get("sources", [])
        for excerpt in source.get("excerpts", [])
        for ref in excerpt["supports"]
    ]
    stable = any(isinstance(ref, list) for ref in references)
    if stable and any(isinstance(ref, str) for ref in references):
        raise ValueError(
            "authored references cannot mix selectors and positional pointers"
        )

    def identities(node):
        if isinstance(node, dict):
            for key, value in node.items():
                if key in ID_COLLECTIONS and isinstance(value, list):
                    ids = [row.get("id") for row in value if isinstance(row, dict)]
                    present = [identity for identity in ids if identity is not None]
                    if len(set(present)) != len(present):
                        raise ValueError(f"{key}: duplicate stable record identity")
                    if stable and len(present) != len(ids):
                        raise ValueError(
                            f"{key}: record needs permanent id; use collection name plus a new UUID hex value"
                        )
                identities(value)
        elif isinstance(node, list):
            for value in node:
                identities(value)

    identities(data)
    if stable:
        for claim in data.get("dataset", {}).get("shared_claims", []):
            for target in claim["targets"]:
                support_pointer(data, target)
    for source in data.get("sources", []):
        for excerpt in source.get("excerpts", []):
            excerpt["supports"] = [
                support_pointer(data, ref) for ref in excerpt["supports"]
            ]
    for institute in data.get("institutes", []):
        funding = institute.get("funding", [])
        for record in funding:
            if "sum_of" in record:
                components = record["sum_of"]
                positional = any(isinstance(key, int) for key in components)
                if positional and (
                    stable or any(isinstance(key, str) for key in components)
                ):
                    raise ValueError(
                        "sum_of: stable references require component IDs, never positional indices"
                    )
                identities = []
                for key in components:
                    if isinstance(key, int):  # Transitional input only.
                        if not 0 <= key < len(funding):
                            raise ValueError("sum_of: invalid component index")
                        identities.append(funding[key].get("id", key))
                        continue
                    matches = [
                        n for n, row in enumerate(funding) if row.get("id") == key
                    ]
                    if len(matches) != 1:
                        raise ValueError(
                            f"{institute['id']}: sum_of {key!r} matched {len(matches)} records"
                        )
                    identities.append(key)
                record["sum_of"] = identities
    return data


def authored_schema(download_schema, *, strict=False):
    """One field dictionary; authored references and generated fields differ."""
    schema = copy.deepcopy(download_schema)
    supports = schema["properties"]["sources"]["items"]["properties"]["excerpts"][
        "items"
    ]["properties"]["supports"]
    supports["items"] = {
        "anyOf": [supports["items"], {"$ref": "#/$defs/shared_claim_selector"}]
    }
    total = schema["$defs"]["funding"]["properties"]["sum_of"]
    total["items"] = {"anyOf": [{"type": "integer", "minimum": 0}, {"type": "string", "minLength": 1}]}
    total["description"] = "Stable funding component IDs within this institute; positional indices are accepted only by the legacy migration adapter."
    jurisdictions = schema["properties"]["jurisdictions"]["items"]
    institutes = schema["properties"]["institutes"]["items"]
    outputs = institutes["properties"]["outputs"]["items"]
    derived = [
        (jurisdictions, ("status", "status_since")),
        (institutes, ("status", "announced", "established", "operating", "intervals")),
        (schema["$defs"]["funding"], ("eur_per_year_est", "eur_per_person_est", "share_of_gdp_est", "ratio_denominators")),
        (schema["properties"]["dataset"]["properties"]["exchange_rates"]["items"], ("eur_per_unit",)),
        (outputs, ("credited_from",)),
        (institutes["properties"]["names"]["items"], ("from",)),
        (schema["$defs"]["model_access"], ("value", "latest_event")),
    ]
    for record, keys in derived:
        record["required"] = [key for key in record.get("required", []) if key not in keys]
        if strict:
            for key in keys:
                record["properties"][key] = False
    funding = schema["$defs"]["funding"]
    computed_funding = (
        ({"required": ["sum_of"]}, ("amount", "annual_amount")),
        ({"properties": {"annual_basis_code": {"const": "period_mean"}}, "required": ["annual_basis_code"]}, ("annual_amount",)),
        ({"properties": {"annual_basis_code": {"const": "allocation_current_year"}, "annual_profile": {"minItems": 1}}, "required": ["annual_basis_code", "annual_profile"]}, ("annual_amount", "annual_period")),
    )
    computed_condition = {"anyOf": [condition for condition, _ in computed_funding]}
    dependencies = funding.pop("dependentRequired")
    funding["dependentRequired"] = {
        field: [key for key in required if key != "annual_amount"]
        for field, required in dependencies.items()
    }
    # Only computed records may omit annual_amount when a basis is authored.
    funding.setdefault("allOf", []).append({
        "if": {"not": computed_condition},
        "then": {"dependentRequired": dependencies},
    })
    for rule in list(funding.get("allOf", [])):
        required = rule.get("then", {}).get("required", [])
        if "annual_amount" in required:
            original_condition = copy.deepcopy(rule["if"])
            rule["then"]["required"] = [key for key in required if key != "annual_amount"]
            funding["allOf"].append({
                "if": {"allOf": [original_condition, {"not": computed_condition}]},
                "then": {"required": ["annual_amount"]},
            })
    def generated(node):
        if isinstance(node, dict):
            props = node.get("properties", {})
            for key in ("citations", "display"):
                if key in props:
                    props[key] = False
            for child in list(node.values()):
                generated(child)
        elif isinstance(node, list):
            for child in node:
                generated(child)
    generated(schema)
    if strict:
        supports["items"] = {"$ref": "#/$defs/shared_claim_selector"}
        total["items"] = {"type": "string", "minLength": 1}
        outputs["required"] = list(dict.fromkeys(outputs["required"] + ["credit"]))
        event = jurisdictions["properties"]["timeline"]["items"]
        event["properties"]["event"] = {"allOf": [event["properties"]["event"], {"not": {"const": "first_output"}}]}
        for condition, fields in computed_funding:
            funding.setdefault("allOf", []).append({"if": condition, "then": {"properties": dict.fromkeys(fields, False)}})
    return schema
