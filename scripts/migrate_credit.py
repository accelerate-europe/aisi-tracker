"""Add reviewed output credit and its evidence; write only a named separate file.

uv run scripts/migrate_credit.py --data data/aisi.json \
    --evidence work/credit-additions.json --out work/aisi-credit.json
The evidence file contains assignments selected by institute ID and output lineage,
plus new source records. Editorial decisions must agree with the input dataset.
"""

import argparse
import copy
import json
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker

if __package__:
    from .references import pointer_ancestors, stable_selector
else:
    from references import pointer_ancestors, stable_selector

ROOT = Path(__file__).resolve().parents[1]


def migrate(document, evidence):
    """Apply all reviewed assignments atomically and without changing input objects."""
    data = copy.deepcopy(document)
    schema = json.loads((ROOT / "schema/aisi.schema.json").read_text())
    source_validator = Draft202012Validator(
        {"$defs": schema["$defs"], **schema["properties"]["sources"]["items"]},
        format_checker=FormatChecker(),
    )
    sources = {source["id"]: source for source in data["sources"]}
    if len(sources) != len(data["sources"]):
        raise ValueError("duplicate source ID")
    for source in evidence["new_sources"]:
        source_validator.validate(source)
        if source.get("excerpts"):
            raise ValueError("new source excerpts must come from reviewed assignments")
        if source["id"] in sources:
            current = {
                k: v for k, v in sources[source["id"]].items() if k != "excerpts"
            }
            if current != {k: v for k, v in source.items() if k != "excerpts"}:
                raise ValueError(f"source metadata collision: {source['id']}")
        else:
            new = copy.deepcopy(source)
            data["sources"].append(new)
            sources[new["id"]] = new

    for update in evidence.get("source_context_updates", []):
        source = sources[update["source_id"]]
        actual = (source.get("output_role"), source.get("output_lineage"))
        expected = (update["expected_role"], update["expected_lineage"])
        if actual != ("context", None):
            if actual != expected:
                raise ValueError(f"source role changed since review: {source['id']}")
            source["output_role"] = "context"
            source.pop("output_lineage", None)

    outputs = {}
    for i, institute in enumerate(data["institutes"]):
        for j, output in enumerate(institute.get("outputs", [])):
            key = (institute["id"], output["lineage"])
            if key in outputs:
                raise ValueError(f"ambiguous output selector: {key}")
            outputs[key] = (output, f"/institutes/{i}/outputs/{j}")
    assignments = evidence["assignments"]
    keys = [
        (a["selector"]["institute_id"], a["selector"]["output_lineage"])
        for a in assignments
    ]
    if len(set(keys)) != len(keys) or set(keys) != set(outputs):
        raise ValueError("assignments must cover every output exactly once")
    stable = any(
        isinstance(target, list)
        for source in data["sources"]
        for excerpt in source.get("excerpts", [])
        for target in excerpt["supports"]
    )
    for key, assignment in zip(keys, assignments):
        output, pointer = outputs[key]
        for field in ("attribution", "qualifies_as"):
            if output[field] != assignment[field]:
                raise ValueError(f"editorial decision changed for {key}: {field}")
        credit = assignment["credit"]
        if set(credit) != {"institute_id", "date"} or credit["institute_id"] != key[0]:
            raise ValueError(f"invalid credit for {key}")
        if "credit" in output and output["credit"] != credit:
            raise ValueError(f"credit collision for {key}")
        output["credit"] = copy.deepcopy(credit)
        covered = set()
        for item in assignment["evidence"]:
            if item.get("verification", {}).get("status") != "matched":
                raise ValueError(f"quote has not passed offline verification: {key}")
            source = sources[item["source_id"]]
            targets = []
            for target in item["stable_supports"]:
                if (target["institute_id"], target["output_lineage"]) != key or target[
                    "path"
                ] not in {"credit/institute_id", "credit/date"}:
                    raise ValueError(
                        f"evidence target does not match assignment: {key}"
                    )
                covered.add(target["path"])
                path = f"{pointer}/{target['path']}"
                owner = next(
                    node
                    for node in reversed(pointer_ancestors(data, path))
                    if isinstance(node, dict) and "source_ids" in node
                )
                if source["id"] not in owner["source_ids"]:
                    owner["source_ids"].append(source["id"])
                targets.append(stable_selector(data, path) if stable else path)
            excerpts = source.setdefault("excerpts", [])
            excerpt = next(
                (
                    old
                    for old in excerpts
                    if old["text"] == item["text"]
                    and old.get("url", source["url"]) == item.get("url", source["url"])
                ),
                None,
            )
            if excerpt is None:
                excerpt = {
                    k: item[k]
                    for k in ("text", "accessed", "url", "gloss")
                    if item.get(k) is not None
                }
                excerpt["supports"] = []
                excerpts.append(excerpt)
            for target in targets:
                if target not in excerpt["supports"]:
                    excerpt["supports"].append(target)
        if covered != {"credit/institute_id", "credit/date"}:
            raise ValueError(f"both credit leaves need evidence: {key}")
    return data


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    if args.out.resolve() in {args.data.resolve(), args.evidence.resolve()}:
        parser.error(
            "write to a separate output, review, then replace the authored file"
        )
    data = migrate(
        json.loads(args.data.read_text(encoding="utf-8")),
        json.loads(args.evidence.read_text(encoding="utf-8")),
    )
    args.out.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


if __name__ == "__main__":
    main()
