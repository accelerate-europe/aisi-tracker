"""Report disagreements between credited outputs, agreements and function entries, never a gate.

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
# The international_cooperation test names agreements with foreign counterparts; every recorded agreement is
# foreign (domestic_interagency is always false), and only a signed one with a government or institute counts.
AGREEMENT_COUNTERPARTIES = ("government", "institute")


def output_detail(output: dict) -> dict:
    return {
        "date": output["date"],
        "title": output["title"],
        "lineage": output["lineage"],
        "review": "reviewed" if "functions" in output else "unreviewed",
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
    agreement_without_entry = []
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
            reviewed = "functions" in output
            counts["credited_outputs"] += 1
            counts["reviewed" if reviewed else "unreviewed"] += 1
            codes = output["functions"] if reviewed else UNREVIEWED_FUNCTIONS.get(output["type"], ())
            for code in codes:
                if code not in live_codes:
                    shown.setdefault(code, []).append(output_detail(output))
        for code in sorted(shown):
            shown_without_entry.append({"institute_id": institute["id"], "code": code, "outputs": shown[code]})
        signed = sorted(
            (a for a in institute.get("agreements", [])
             if a.get("state") == "signed" and a.get("counterparty_type") in AGREEMENT_COUNTERPARTIES),
            key=lambda a: (a.get("date", ""), a["id"]),
        )
        if signed and "international_cooperation" not in live_codes:
            agreement_without_entry.append({
                "institute_id": institute["id"], "code": "international_cooperation",
                "agreements": [{"id": a["id"], "date": a.get("date", "undated"), "counterparty": a["counterparty"]} for a in signed],
            })

        for claim in live:
            pointer = support_pointer(data, ["institutes", {"id": institute["id"]}, "claims", {"id": claim["id"]}, "text"])
            sources = bindings.get(pointer, set())
            if len(sources) != 1:
                continue  # Missing or ambiguous text bindings are handled by validation.
            source_id = next(iter(sources))
            # A source such as a briefing note may back several outputs: flag only when every output it backs
            # has been judged and none shows the entry's function.
            citing = [output for output in outputs if source_id in output["source_ids"]]
            judged = bool(citing) and all("functions" in output for output in citing)
            disagreeing = [output_detail(output) for output in citing] if judged and not any(
                claim["code"] in output["functions"] for output in citing) else []
            if disagreeing:
                entry_not_shown.append({
                    "institute_id": institute["id"], "code": claim["code"], "claim_id": claim["id"],
                    "source_id": source_id, "outputs": disagreeing,
                })
    counts.update(shown_without_entry=len(shown_without_entry), entry_not_shown=len(entry_not_shown),
                  agreement_without_entry=len(agreement_without_entry))
    return {"counts": counts, "shown_without_entry": shown_without_entry, "entry_not_shown": entry_not_shown,
            "agreement_without_entry": agreement_without_entry}


def report_lines(report: dict) -> list[str]:
    """The CLI and build share the same counts and warning lines.

    A dataset with no credited output and no unmatched agreement has nothing to report.
    """
    counts = report["counts"]
    if not counts["credited_outputs"] and not counts["agreement_without_entry"]:
        return []
    lines = [
        f"function gaps: {counts['credited_outputs']} credited outputs; {counts['reviewed']} reviewed, "
        f"{counts['unreviewed']} unreviewed; {counts['shown_without_entry']} shown without an entry; "
        f"{counts['entry_not_shown']} entries not shown by their output; "
        f"{counts['agreement_without_entry']} institutes with a signed agreement but no cooperation entry"
    ]
    for direction, description in (
        ("shown_without_entry", "shown without an entry"),
        ("entry_not_shown", "entry not shown by its output"),
    ):
        for row in report[direction]:
            entry = f" (claim {row['claim_id']}, source {row['source_id']})" if "claim_id" in row else ""
            outputs = "; ".join(f"{o['date']} {o['title']} ({o['review']})" for o in row["outputs"])
            lines.append(f"function gaps: {row['institute_id']} {row['code']}: {description}{entry}; {outputs}")
    for row in report["agreement_without_entry"]:
        agreements = "; ".join(f"{a['date']} {a['counterparty']} ({a['id']})" for a in row["agreements"])
        lines.append(f"function gaps: {row['institute_id']} {row['code']}: signed agreement without an activity entry; {agreements}")
    return lines


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--json", action="store_true", help="print the report as JSON")
    args = parser.parse_args()
    report = function_gaps(publish(json.loads(DATA.read_text(encoding="utf-8"))))
    print(json.dumps(report, ensure_ascii=False, indent=2) if args.json else "\n".join(report_lines(report)))


if __name__ == "__main__":
    main()
