"""Draft checks for repeated facts; no network or repository writes.

Source output_lineage marks a single document lineage, never a catalogue/index.
Shared claims bind only explicitly reviewed repetitions. They store no value.
"""

from collections import defaultdict


def resolve(data, selector):
    """Resolve stable path steps and return value, nearest cited record, JSON pointer."""
    value, owner, parts = data, None, []
    for step in selector:
        if isinstance(value, dict) and (
            "source_ids" in value or (len(parts) == 2 and parts[0] == "sources")
        ):
            owner = value
        if isinstance(step, dict):
            if not isinstance(value, list) or not step:
                raise ValueError(
                    "selector match requires a nonempty match object and array"
                )
            matches = [
                (n, item)
                for n, item in enumerate(value)
                if isinstance(item, dict)
                and all(item.get(k) == v for k, v in step.items())
            ]
            if len(matches) != 1:
                raise ValueError(f"selector {step} matched {len(matches)} records")
            n, value = matches[0]
            parts.append(str(n))
        else:
            if not isinstance(value, dict) or step not in value:
                raise ValueError(f"missing selector field {step}")
            value = value[step]
            parts.append(step.replace("~", "~0").replace("/", "~1"))
    if isinstance(value, dict) and "source_ids" in value:
        owner = value
    return value, owner, "/" + "/".join(parts)


def _outputs(data):
    return [(i, o) for i in data.get("institutes", []) for o in i.get("outputs", [])]


def _bound_claims(data):
    """Yield registry entries and resolved targets; errors remain explicit."""
    for claim in data.get("dataset", {}).get("shared_claims", []):
        resolved, errors = [], []
        for selector in claim.get("targets", []):
            try:
                resolved.append(resolve(data, selector))
            except (ValueError, TypeError, KeyError) as exc:
                errors.append(str(exc))
        yield claim, resolved, errors


def _number_identity(data, pointer):
    """Read numeric identity from its actual funding/staff context, not the selector."""
    parts = pointer.strip("/").split("/")
    if len(parts) < 5 or parts[0] != "institutes":
        raise ValueError("number target must be an institute funding or staff field")
    institute = data["institutes"][int(parts[1])]
    section, index, field = parts[2], int(parts[3]), parts[-1]
    record = institute[section][index]
    if (
        section == "staff"
        and len(parts) == 5
        and field in {"total", "technical", "target"}
    ):
        unit, period = "people", record.get("as_of")
    elif section == "funding":
        unit = record.get("currency")
        if len(parts) == 7 and parts[4] == "annual_profile" and field == "amount":
            window = record["annual_profile"][int(parts[5])]
            period = f"{window['start']}/{window['end']}"
        elif len(parts) == 5 and field in {
            "amount",
            "annual_amount",
            "eur_per_year_est",
        }:
            if field == "eur_per_year_est":
                unit = "EUR"
            if (
                field in {"annual_amount", "eur_per_year_est"}
                and record.get("kind") != "annual"
                and record.get("annual_basis_code") == "allocation_current_year"
            ):
                window = record.get("annual_period", {})
                if not window.get("start") or not window.get("end"):
                    raise ValueError("current annual amount needs an annual_period")
                period = f"{window['start']}/{window['end']}"
            else:
                period = record.get("period")
        else:
            raise ValueError("number target is not a supported funding amount field")
    else:
        raise ValueError("number target is not a supported funding or staff field")
    if not unit or not period:
        raise ValueError("number target lacks unit or period context")
    return {"subject": institute["id"], "unit": unit, "period": period}


def check_shared_claims(data):
    # Publication equality, partial-date credit order and earliest dates live in method_checks.
    errors = []
    sources = {s["id"]: s for s in data.get("sources", [])}
    by_lineage = defaultdict(list)
    # Independent source metadata keeps a renamed output from hiding a shared document.
    by_url = defaultdict(set)
    for source in sources.values():
        if source.get("output_role") == "document":
            if not source.get("output_lineage"):
                errors.append(f"{source['id']}: document source needs output_lineage")
            by_url[source["url"].split("#")[0].rstrip("/")].add(
                source.get("output_lineage")
            )
        elif source.get("output_role") == "context" and source.get("output_lineage"):
            errors.append(f"{source['id']}: context source cannot anchor one document")
    for url, lineages in by_url.items():
        if len(lineages) > 1:
            errors.append(f"same document URL has conflicting lineages: {url}")
    for institute, output in _outputs(data):
        lineage = output.get("lineage")
        if not any(
            sources.get(s, {}).get("output_role") == "document"
            for s in output.get("source_ids", [])
        ):
            errors.append(
                f"{institute['id']}/{lineage}: output lacks a document identity anchor"
            )
        for sid in output.get("source_ids", []):
            if sources.get(sid, {}).get("output_role") not in ("document", "context"):
                errors.append(
                    f"{sid}: output citation needs explicit document/context role"
                )
        by_lineage[lineage].append((institute, output))
        for sid in output.get("source_ids", []):
            declared = sources.get(sid, {}).get("output_lineage")
            if declared is not None and lineage != declared:
                errors.append(
                    f"{institute['id']}: output lineage {lineage} disagrees with document {sid}: {declared}"
                )
    for lineage, rows in by_lineage.items():
        if len(rows) > 1:
            # Repeated records must retain at least one independently declared document anchor.
            for institute, output in rows:
                if not any(
                    sources.get(s, {}).get("output_lineage") == lineage
                    for s in output["source_ids"]
                ):
                    errors.append(
                        f"{institute['id']}/{lineage}: repeated lineage lacks a declared document source"
                    )
    institutes = {i["id"]: i for i in data.get("institutes", [])}
    for jurisdiction in data.get("jurisdictions", []):
        institute = institutes.get(jurisdiction.get("institute_id"), {})
        outputs = institute.get("outputs", [])
        for event in jurisdiction.get("timeline", []):
            if event.get("event") != "first_output":
                continue
            lineage = event.get("output_lineage")
            matches = [o for o in outputs if o.get("lineage") == lineage]
            if len(matches) != 1:
                errors.append(
                    f"{jurisdiction['id']}/{event['id']}: first_output needs one explicit output_lineage"
                )
                continue
            output = matches[0]
            eligible = output.get("credited_from", output["date"])
            if event.get("date") != eligible:
                errors.append(
                    f"{jurisdiction['id']}/{event['id']}: first_output differs from its bound output eligibility date"
                )
            if not set(output.get("source_ids", [])) & set(event.get("source_ids", [])):
                errors.append(
                    f"{jurisdiction['id']}/{event['id']}: first_output has no source shared with its output"
                )
    seen = set()
    for claim, rows, failures in _bound_claims(data):
        cid = claim.get("id", "?")
        if cid in seen:
            errors.append(f"{cid}: duplicate shared claim ID")
        seen.add(cid)
        errors += [f"{cid}: {e}" for e in failures]
        if len(claim.get("targets", [])) < 2:
            errors.append(f"{cid}: shared claim needs at least two targets")
        if len({p for _, _, p in rows}) != len(rows):
            errors.append(f"{cid}: duplicate target cannot establish consistency")
        for key in ("subject", "metric", "unit", "period", "scope"):
            if not claim.get(key):
                errors.append(f"{cid}: missing claim identity {key}")
        if not claim.get("source_ids") or any(
            s not in sources for s in claim["source_ids"]
        ):
            errors.append(f"{cid}: missing or unknown source identity")
        values = []
        for value, owner, pointer in rows:
            evidence = owner.get("source_ids", []) if owner else []
            if pointer.startswith("/sources/") and owner:
                evidence = [owner["id"]]
            if not set(evidence) & set(claim.get("source_ids", [])):
                errors.append(f"{cid}: {pointer} does not cite this claim’s evidence")
            if claim["kind"] == "agreement":
                if not isinstance(value, dict) or "counterparty" not in value:
                    errors.append(f"{cid}: {pointer} is not an agreement")
                    continue
                # Missing dates stay None; absence on only one mirrored record is a conflict.
                values.append((value.get("date"), value.get("state")))
            elif claim["kind"] == "number":
                try:
                    identity = _number_identity(data, pointer)
                except (ValueError, KeyError, IndexError, TypeError) as exc:
                    errors.append(f"{cid}: {pointer}: {exc}")
                else:
                    for key, expected in identity.items():
                        if claim.get(key) != expected:
                            errors.append(
                                f"{cid}: {pointer}: claim {key} {claim.get(key)!r} differs from target {expected!r}"
                            )
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    errors.append(f"{cid}: {pointer} is not a number")
                values.append(value)
            elif claim["kind"] == "date":
                if not isinstance(value, str):
                    errors.append(f"{cid}: {pointer} is not a date string")
                values.append(value)
            else:
                errors.append(f"{cid}: unknown claim kind")
        if values and any(v != values[0] for v in values[1:]):
            errors.append(f"{cid}: repeated {claim['kind']} disagrees: {values}")
    return errors


def shared_claim_coverage(data):
    """Report scope, including every numeric institute leaf left unchecked."""
    covered_numbers, covered_dates, covered_agreements = set(), set(), set()
    for claim, rows, failures in _bound_claims(data):
        if failures:
            continue
        for _, _, pointer in rows:
            if claim["kind"] == "number":
                covered_numbers.add(pointer)
            elif claim["kind"] == "date":
                covered_dates.add(pointer)
            elif claim["kind"] == "agreement":
                covered_agreements.add(pointer)
    numbers = set()

    def walk(node, pointer):
        if isinstance(node, bool):
            return
        if isinstance(node, (int, float)):
            numbers.add(pointer)
        elif isinstance(node, list):
            for n, v in enumerate(node):
                walk(v, f"{pointer}/{n}")
        elif isinstance(node, dict):
            for k, v in node.items():
                # Generated day counts between dated facts are arithmetic, not research claims.
                if k not in {"citations", "display", "intervals"}:
                    walk(v, f"{pointer}/{k}")

    walk(data.get("institutes", []), "/institutes")
    rows = _outputs(data)
    anchored = sum(
        any(
            next(
                (s.get("output_lineage") for s in data["sources"] if s["id"] == sid),
                None,
            )
            == o.get("lineage")
            for sid in o.get("source_ids", [])
        )
        for _, o in rows
    )
    return {
        "pointer_basis": "RFC 6901 pointers into this build's published aisi.json; version-specific locations, not stable record identities.",
        "output_rows": len(rows),
        "document_anchored_output_rows": anchored,
        "global_lineages": len({o.get("lineage") for _, o in rows}),
        "first_output_events_checked": sum(
            e.get("event") == "first_output"
            for j in data.get("jurisdictions", [])
            for e in j.get("timeline", [])
        ),
        "agreement_rows": sum(
            len(i.get("agreements", [])) for i in data.get("institutes", [])
        ),
        "registered_date_leaves": len(covered_dates),
        "registered_agreements": len(covered_agreements),
        "numeric_leaves": len(numbers),
        "registered_numeric_leaves": len(numbers & covered_numbers),
        "unchecked_numeric_leaves": sorted(numbers - covered_numbers),
        "limitations": [
            "Equality of registered facts does not verify source truth.",
            "Date fields from one source are not presumed to describe one event.",
            "Different numeric scopes and unregistered prose numbers are not compared.",
        ],
    }
