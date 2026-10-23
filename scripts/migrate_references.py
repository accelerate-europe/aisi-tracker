"""One-time migration of positional references; writes only the named output file.

uv run python scripts/migrate_references.py data/aisi.json work/aisi-stable.json
IDs are opaque, permanent and unique within their collection. Never regenerate
an existing ID on a rename, correction or reordering, or reuse a deleted ID.
"""

import argparse
import copy
import json
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

if __package__:
    from .references import ID_COLLECTIONS, stable_selector, materialise_references
    from .shared_claims import resolve
else:
    from references import ID_COLLECTIONS, stable_selector, materialise_references
    from shared_claims import resolve


def migrate(document):
    data = copy.deepcopy(document)

    def identify(node, path=""):
        if isinstance(node, dict):
            for key, value in node.items():
                if key in ID_COLLECTIONS and isinstance(value, list):
                    for n, record in enumerate(value):
                        if isinstance(record, dict) and "id" not in record:
                            seed = f"aisi:{path}/{key}/{n}:" + json.dumps(
                                record, sort_keys=True, ensure_ascii=False
                            )
                            record["id"] = f"{key}-{uuid5(NAMESPACE_URL, seed).hex}"
                identify(value, f"{path}/{key}")
        elif isinstance(node, list):
            for n, value in enumerate(node):
                identify(value, f"{path}/{n}")

    identify(data)
    for source in data.get("sources", []):
        for excerpt in source.get("excerpts", []):
            excerpt["supports"] = [
                stable_selector(data, p) if isinstance(p, str) else p
                for p in excerpt["supports"]
            ]
    for institute in data.get("institutes", []):
        records = institute.get("funding", [])
        for record in records:
            if "sum_of" in record:
                record["sum_of"] = [
                    records[n]["id"] if isinstance(n, int) else n
                    for n in record["sum_of"]
                ]
    for claim in data.get("dataset", {}).get("shared_claims", []):
        claim["targets"] = [
            stable_selector(data, resolve(data, selector)[2])
            for selector in claim["targets"]
        ]
    materialise_references(data)  # Fail before writing if any identity is ambiguous.
    return data


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    if args.input.resolve() == args.output.resolve():
        parser.error(
            "write to a separate output, review, then replace the authored file"
        )
    data = migrate(json.loads(args.input.read_text(encoding="utf-8")))
    args.output.write_text(
        json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


if __name__ == "__main__":
    main()
