"""Informational source-origin checks for each independently cited record."""

import json
from pathlib import Path

if __package__:
    from .references import record_label
else:
    from references import record_label

CONTEXT_FIELDS = {"population", "region", "searches"}


def cited_records(data: dict):
    """Yield primary-record citations, inheriting jurisdiction and context only.

    Published display metadata and generated activity/event copies do not create
    additional source-origin claims; their output records are checked once.
    """

    def walk(node, pointer, jurisdiction, context):
        if isinstance(node, dict):
            if node.get("event") == "first_output":
                return
            for key, value in node.items():
                if (key == "source_ids" or key.endswith("_source_ids")) and value:
                    yield {
                        "record_id": record_label(data, pointer) + ("/" + key if key != "source_ids" else ""),
                        "pointer": pointer + ("/" + key if key != "source_ids" else ""),
                        "jurisdiction": jurisdiction,
                        "context": {
                            "identifier_source_ids": "identifiers",
                            "independent_source_ids": "historical_independence",
                        }.get(key, context),
                        "source_ids": value,
                        "citation_field": key,
                    }
            for key, value in node.items():
                if key not in {"source_ids", "citations", "display", "operating"} and not key.endswith("_source_ids"):
                    escaped = key.replace("~", "~0").replace("/", "~1")
                    yield from walk(
                        value,
                        f"{pointer}/{escaped}",
                        jurisdiction,
                        key if key in CONTEXT_FIELDS else context,
                    )
        elif isinstance(node, list):
            for n, value in enumerate(node):
                yield from walk(value, f"{pointer}/{n}", jurisdiction, context)

    for collection in ("jurisdictions", "institutes"):
        for n, record in enumerate(data[collection]):
            jurisdiction = (
                record["id"]
                if collection == "jurisdictions"
                else record["jurisdiction_id"]
            )
            yield from walk(record, f"/{collection}/{n}", jurisdiction, "claim")


def source_origin_report(data: dict) -> dict:
    sources = {s["id"]: s for s in data["sources"]}
    rows = []
    for record in cited_records(data):
        cited = [sources[sid] for sid in record["source_ids"] if sid in sources]
        if any(s.get("origin") == record["jurisdiction"] for s in cited):
            continue
        if not cited:
            continue
        rows.append(
            {
                **record,
                "origins": sorted({s["origin"] for s in cited if s.get("origin")}),
                "unknown_origin_ids": [
                    s["id"] for s in cited if s.get("origin") is None
                ],
                "publishers": list(dict.fromkeys(s["publisher"] for s in cited)),
            }
        )
    return {
        "note": "Informational only. Each row lacks a same-origin citation on that cited record. A domestic citation on a parent does not cover a nested claim. Contextual statistics, identifiers, historical independence checks and search-query logs are listed separately; these are not claim-policy failures. An international report or a foreign party's announcement may be the available primary evidence. Review relevance and add a domestic primary or English companion when available, not merely to clear a flag.",
        "claim_gaps": [r for r in rows if r["context"] == "claim"],
        "contextual_gaps": [r for r in rows if r["context"] != "claim"],
        "unknown_origins": [
            s["id"] for s in sources.values() if s.get("origin") is None
        ],
    }


def write_source_origin_report(data: dict, directory: Path) -> dict:
    report = source_origin_report(data)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "source-origins.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    lines = [
        "# Source-origin review",
        "",
        report["note"],
        "",
        "| Jurisdiction | Record | Origins | Citations |",
        "| --- | --- | --- | --- |",
    ]
    for row in report["claim_gaps"]:
        lines.append(
            f"| {row['jurisdiction']} | `{row['record_id']}` (`{row['pointer']}`) | {', '.join(row['origins']) or 'Unknown'} | {', '.join(row['source_ids'])} |"
        )
    lines += [
        "",
        f"Contextual citation groups: {len(report['contextual_gaps'])}; see the JSON report. Unknown source origins: {', '.join(report['unknown_origins']) or 'none'}.",
    ]
    (directory / "source-origins.md").write_text(
        "\n".join(lines) + "\n", encoding="utf-8"
    )
    return report

