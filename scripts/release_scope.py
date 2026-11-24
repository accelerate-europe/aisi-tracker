"""List the records and evidence changed since the last release for a scoped review.

  uv run --locked scripts/release_scope.py [--base REF] --seed 1 [--draw 4] --out DIR

scope.json and scope.md contain IDs and pointers only. detail.json contains stored
values and quotations for the comparer. The candidate is the working-tree data.
"""

import argparse
import hashlib
import json
import random
import shlex
import subprocess
import sys
from pathlib import Path

try:
    from .check import REMOVED, binding_changes, diff, excerpt_keys, git, resolve_base
except ImportError:
    from check import REMOVED, binding_changes, diff, excerpt_keys, git, resolve_base

ROOT = Path(__file__).resolve().parents[1]
RECORDS = ("institutes", "jurisdictions")
RELEASE_METADATA = {"version", "as_of", "citation"}


def pointer_parts(pointer: str) -> list[str]:
    return [part.replace("~1", "/").replace("~0", "~")
            for part in pointer.removesuffix(REMOVED).split("/")[1:]]


def pointer_records(data: dict, pointer: str) -> list[tuple[str, str]]:
    parts = pointer_parts(pointer)
    if not parts or parts[0] not in RECORDS:
        return []
    collection, *tail = parts
    rows = data.get(collection, [])
    if tail:
        rows = [rows[int(tail[0])]]
    return [(collection, row["id"]) for row in rows]


def supported_records(data: dict, excerpt: dict) -> set[tuple[str, str]]:
    records = set()
    for ref in excerpt["supports"]:
        if isinstance(ref, str):
            records.update(pointer_records(data, ref))
        elif len(ref) >= 2 and ref[0] in RECORDS:
            records.add((ref[0], ref[1]["id"]))
    return records


def source_ids(value) -> set[str]:
    """Citations on nested acts and outputs count as well as record-level citations."""
    found = set()
    if isinstance(value, dict):
        found.update(value.get("source_ids", []))
        for child in value.values():
            found.update(source_ids(child))
    elif isinstance(value, list):
        for child in value:
            found.update(source_ids(child))
    return found


def excerpt_id(excerpt: dict) -> str:
    # binding_changes exposes this identity in its rows, but has no identity helper.
    return excerpt.get("id") or "sha256:" + hashlib.sha256(excerpt["text"].encode("utf-8")).hexdigest()


def record_id(key: tuple[str, str]) -> dict:
    collection, identity = key
    return {"collection": collection, "id": identity}


def json_text(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"


def compare(old: dict, new: dict, seed: int | None, draw: int) -> tuple[dict, dict]:
    omitted = {"sources", "dataset", "changelog"}
    claims = diff({k: v for k, v in old.items() if k not in omitted},
                  {k: v for k, v in new.items() if k not in omitted})
    dataset_changes = diff(
        {k: v for k, v in old.get("dataset", {}).items() if k not in RELEASE_METADATA},
        {k: v for k, v in new.get("dataset", {}).items() if k not in RELEASE_METADATA},
        "/dataset",
    )
    source_changes = diff(old.get("sources", []), new.get("sources", []), "/sources")
    reasons, grouped_claims = {}, {}
    for pointer, _, _ in claims:
        data = old if pointer.endswith(REMOVED) else new
        for key in pointer_records(data, pointer):
            reasons.setdefault(key, set()).add(pointer)
            grouped_claims.setdefault(key, set()).add(pointer)

    sources_before = {s["id"]: s for s in old.get("sources", [])}
    sources_after = {s["id"]: s for s in new.get("sources", [])}
    source_pointers, source_keys = {}, {}
    for pointer, _, _ in source_changes:
        data = old if pointer.endswith(REMOVED) else new
        _, index, *tail = pointer_parts(pointer)
        source = data["sources"][int(index)]
        sid = source["id"]
        source_pointers.setdefault(sid, set()).add(pointer)
        source_keys.setdefault(sid, set()).update(tail[:1] if tail else source.keys())

    # Both sides matter: a removed citation must still bring its former record into scope.
    for data in (old, new):
        for source in data.get("sources", []):
            pointers = source_pointers.get(source["id"], set())
            if pointers:
                for excerpt in source.get("excerpts", []):
                    for key in supported_records(data, excerpt):
                        reasons.setdefault(key, set()).update(pointers)
        for collection in RECORDS:
            for record in data.get(collection, []):
                for sid in source_ids(record) & source_pointers.keys():
                    reasons.setdefault((collection, record["id"]), set()).update(source_pointers[sid])

    changed_quotes = excerpt_keys(new) - excerpt_keys(old)
    bindings = binding_changes(old, new)
    changed_excerpts = {(row["source_id"], row["excerpt_id"]) for row in bindings}
    for source in new.get("sources", []):
        for excerpt in source.get("excerpts", []):
            # Ask the shared helper about each row so anchor and copy rules stay in one place.
            keys = excerpt_keys({"sources": [{"id": source["id"], "excerpts": [excerpt]}]})
            if keys & changed_quotes:
                changed_excerpts.add((source["id"], excerpt_id(excerpt)))

    excerpt_rows = {}
    for side, data in (("before", old), ("after", new)):
        for source in data.get("sources", []):
            for excerpt in source.get("excerpts", []):
                key = (source["id"], excerpt_id(excerpt))
                if key in changed_excerpts:
                    entry = excerpt_rows.setdefault(key, {"before": [], "after": []})
                    entry[side].append(excerpt)

    ordered = sorted(reasons)
    chosen = sorted(random.Random(seed).sample(ordered, min(draw, len(ordered))))
    scope = {
        "records": [{**record_id(key), "reasons": sorted(reasons[key])} for key in ordered],
        "claims": [{**record_id(key), "pointers": sorted(pointers)}
                   for key, pointers in sorted(grouped_claims.items())],
        "excerpts": [{"source_id": sid, "excerpt_id": eid} for sid, eid in sorted(changed_excerpts)],
        "sources": [{"id": sid, "change": "added" if sid not in sources_before else
                     "removed" if sid not in sources_after else "changed", "keys": sorted(source_keys[sid])}
                    for sid in sorted(source_pointers)],
        "verify_sources": sorted({key[0] for key in changed_quotes} | (sources_after.keys() - sources_before.keys())),
        "dataset_changes": sorted(pointer for pointer, _, _ in dataset_changes),
        "seed": seed,
        "draw": draw,
        "blind_draw": [record_id(key) for key in chosen],
    }
    detail = {
        "changes": [{"pointer": p, "before": b, "after": a}
                    for p, b, a in sorted(claims + source_changes + dataset_changes, key=lambda row: row[0])],
        "excerpts": [{"source_id": sid, "excerpt_id": eid,
                      **{side: sorted(rows, key=json_text) for side, rows in entry.items()}}
                     for (sid, eid), entry in sorted(excerpt_rows.items())],
        "bindings": bindings,
    }
    return scope, detail


def summary(scope: dict) -> str:
    counts = {name: len(scope[name]) for name in
              ("records", "excerpts", "sources", "verify_sources", "dataset_changes", "blind_draw")}
    counts["claims"] = sum(len(row["pointers"]) for row in scope["claims"])
    return "; ".join(f"{counts[name]} {name}" for name in
                     ("records", "claims", "excerpts", "sources", "verify_sources", "dataset_changes", "blind_draw"))


def markdown(scope: dict) -> str:
    lines = ["# Release scope", "", f"Base: `{scope['base_ref']}` (`{scope['base_sha']}`).",
             f"HEAD: `{scope['head_sha']}`.", "", summary(scope), "",
             "detail.json contains stored values and full quotations for the comparer only."]
    if not any(scope[name] for name in ("records", "claims", "excerpts", "sources", "dataset_changes")):
        lines += ["", f"nothing changed since {scope['base_ref']}"]
    for title, name, field in (("Records", "records", "reasons"), ("Claims", "claims", "pointers")):
        lines += ["", f"## {title} ({len(scope[name])})", ""]
        lines += [f"- `{row['collection']}/{row['id']}`: " + ", ".join(f"`{p}`" for p in row[field])
                  for row in scope[name]] or ["None."]
    lines += ["", f"## Excerpts ({len(scope['excerpts'])})", ""]
    lines += [f"- `{row['source_id']}` / `{row['excerpt_id']}`" for row in scope["excerpts"]] or ["None."]
    lines += ["", f"## Sources ({len(scope['sources'])})", ""]
    lines += [f"- `{row['id']}` ({row['change']}): " + ", ".join(f"`{key}`" for key in row["keys"])
              for row in scope["sources"]] or ["None."]
    lines += ["", f"## Quotation verification ({len(scope['verify_sources'])} sources)", ""]
    if scope["verify_sources"]:
        command = ["uv", "run", "--locked", "scripts/verify_excerpts.py"]
        for sid in scope["verify_sources"]:
            command += ["--source", sid]
        lines += ["```sh", shlex.join(command), "```"]
    else:
        # An unfiltered verifier command would silently expand an empty scope to all sources.
        lines += ["No quotations need verification."]
    lines += ["", "## Dataset wording changes", "", "Owner wording approval is needed for these pointers.", ""]
    lines += [f"- `{pointer}`" for pointer in scope["dataset_changes"]] or ["None."]
    lines += ["", f"## Blind re-check ({len(scope['blind_draw'])})", "", f"Seed: {scope['seed']}; requested: {scope['draw']}."]
    if len(scope["records"]) < scope["draw"]:
        lines += [f"Only {len(scope['records'])} records changed; taking them all."]
    lines += [""]
    lines += [f"- `{row['collection']}/{row['id']}`" for row in scope["blind_draw"]] or ["None."]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None, root: Path = ROOT) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base", help="base ref (default: last v* release tag reachable from HEAD)")
    parser.add_argument("--seed", type=int, help="random seed, required when --draw is positive")
    parser.add_argument("--draw", type=int, default=4, help="number of records to draw (default: 4)")
    parser.add_argument("--out", type=Path, required=True, help="new or empty output directory")
    args = parser.parse_args(argv)
    if args.draw < 0 or (args.draw > 0 and args.seed is None):
        parser.error("--draw must be non-negative; --seed is required when --draw is positive")
    try:
        if args.out.exists() and (not args.out.is_dir() or any(args.out.iterdir())):
            raise ValueError(f"output directory must be new or empty: {args.out}")
        base_ref = args.base
        if base_ref is None:
            base_ref = git(root, "describe", "--tags", "--abbrev=0", "--match", "v[0-9]*", "HEAD", check=False).strip()
            if not base_ref:
                raise ValueError("HEAD has no reachable v* release tag; pass --base REF")
            base_sha = resolve_base(root, f"refs/tags/{base_ref}", release=True)
        else:
            base_sha = resolve_base(root, base_ref, release=True)
        metadata = {"base_ref": base_ref, "base_sha": base_sha, "head_sha": git(root, "rev-parse", "HEAD").strip()}
        old = json.loads(git(root, "show", f"{base_sha}:data/aisi.json"))
        new = json.loads((root / "data/aisi.json").read_text(encoding="utf-8"))
        scope, detail = compare(old, new, args.seed, args.draw)
        scope.update(metadata)
        detail.update(metadata)
        outputs = {"scope.json": json_text(scope), "scope.md": markdown(scope), "detail.json": json_text(detail)}
        args.out.mkdir(parents=True, exist_ok=True)
        for name, content in outputs.items():
            (args.out / name).write_text(content, encoding="utf-8")
    except (OSError, ValueError, KeyError, IndexError, TypeError, subprocess.CalledProcessError) as exc:
        message = exc.stderr.strip() if isinstance(exc, subprocess.CalledProcessError) else str(exc)
        print(f"release_scope: {message}", file=sys.stderr)
        return 2
    print(summary(scope))
    if not detail["changes"] and not detail["bindings"] and not scope["excerpts"]:
        print(f"nothing changed since {base_ref}")
    if len(scope["records"]) < args.draw:
        print(f"Only {len(scope['records'])} records changed; taking them all.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
