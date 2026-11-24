"""Report disagreements between credited outputs and function entries, never a gate.

Run from the repo root: uv run --locked scripts/function_gaps.py [--json]
"""

import argparse
import json
from pathlib import Path

if __package__:
    from .claim_checks import OUTPUT_FUNCTIONS
    from .published import publish
    from .references import support_pointer
else:
    from claim_checks import OUTPUT_FUNCTIONS
    from published import publish
    from references import support_pointer

DATA = Path(__file__).resolve().parent.parent / "data" / "aisi.json"
# A report is only a candidate for research until judged; it must not alter the since rule.
UNREVIEWED_FUNCTIONS = {**OUTPUT_FUNCTIONS, "report": ("research",)}


def output_detail(output: dict) -> dict:
    return {
        "date": output["date"],
        "title": output["title"],
        "lineage": output["lineage"],
        "review": "reviewed" if "shows" in output else "unreviewed",
    }


def function_gaps(published_or_authored_data: dict) -> dict:
    """Pure report using supplied expiry flags (absent means live), without inferring them.

    Accepts stable selectors or published pointers. Rows sort by institute ID, code and
    claim ID; outputs by publication date and unique lineage. The caller publishes first
    when tracked re-read states must be applied.
    """
    data = published_or_authored_data
    bindings: dict[str, set[str]] = {}
    for source in data.get("sources", []):
        for excerpt in source.get("excerpts", []):
            for ref in excerpt["supports"]:
                pointer = support_pointer(data, ref)
                bindings.setdefault(pointer, set()).add(source["id"])

    shown_without_entry = []
    entry_not_shown = []
    counts = {"credited_outputs": 0, "reviewed": 0, "unreviewed": 0}
    for institute in sorted(data.get("institutes", []), key=lambda i: i["id"]):
        outputs = sorted(
            (o for o in institute.get("outputs", []) if o.get("credit")),
            key=lambda o: (o["date"], o["lineage"]),
        )
        live = sorted(
            (c for c in institute.get("claims", [])
             if c.get("kind") == "function" and c.get("basis") == "activity" and not c.get("expired")),
            key=lambda c: (c["code"], c["id"]),
        )
        live_codes = {c["code"] for c in live}
        shown: dict[str, list[dict]] = {}
        for output in outputs:
            reviewed = "shows" in output
            counts["credited_outputs"] += 1
            counts["reviewed" if reviewed else "unreviewed"] += 1
            codes = output["shows"] if reviewed else UNREVIEWED_FUNCTIONS.get(output["type"], ())
            for code in codes:
                if code not in live_codes:
                    shown.setdefault(code, []).append(output_detail(output))
        for code in sorted(shown):
            shown_without_entry.append({"institute_id": institute["id"], "code": code, "outputs": shown[code]})

        for claim in live:
            pointer = support_pointer(data, ["institutes", {"id": institute["id"]}, "claims", {"id": claim["id"]}, "text"])
            sources = bindings.get(pointer, set())
            if len(sources) != 1:
                continue  # Missing or ambiguous text bindings are handled by validation.
            source_id = next(iter(sources))
            # A source such as a briefing note may back several outputs: flag only when every output it backs
            # has been judged and none shows the entry's function.
            citing = [output for output in outputs if source_id in output["source_ids"]]
            judged = bool(citing) and all("shows" in output for output in citing)
            disagreeing = [output_detail(output) for output in citing] if judged and not any(
                claim["code"] in output["shows"] for output in citing) else []
            if disagreeing:
                entry_not_shown.append({
                    "institute_id": institute["id"], "code": claim["code"], "claim_id": claim["id"],
                    "source_id": source_id, "outputs": disagreeing,
                })
    counts.update(shown_without_entry=len(shown_without_entry), entry_not_shown=len(entry_not_shown))
    return {"counts": counts, "shown_without_entry": shown_without_entry, "entry_not_shown": entry_not_shown}


def report_lines(report: dict) -> list[str]:
    """The CLI and build share the same counts and warning lines."""
    counts = report["counts"]
    lines = [
        f"function gaps: {counts['credited_outputs']} credited outputs; {counts['reviewed']} reviewed, "
        f"{counts['unreviewed']} unreviewed; {counts['shown_without_entry']} shown without an entry; "
        f"{counts['entry_not_shown']} entries not shown by their output"
    ]
    for direction, description in (
        ("shown_without_entry", "shown without an entry"),
        ("entry_not_shown", "entry not shown by its output"),
    ):
        for row in report[direction]:
            entry = f" (claim {row['claim_id']}, source {row['source_id']})" if "claim_id" in row else ""
            outputs = "; ".join(f"{o['date']} {o['title']} ({o['review']})" for o in row["outputs"])
            lines.append(f"function gaps: {row['institute_id']} {row['code']}: {description}{entry}; {outputs}")
    return lines


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--json", action="store_true", help="print the report as JSON")
    args = parser.parse_args()
    report = function_gaps(publish(json.loads(DATA.read_text(encoding="utf-8"))))
    print(json.dumps(report, ensure_ascii=False, indent=2) if args.json else "\n".join(report_lines(report)))


if __name__ == "__main__":
    main()
