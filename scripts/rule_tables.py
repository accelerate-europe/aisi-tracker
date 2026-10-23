#!/usr/bin/env python3
"""Generate owner-facing rule comparisons from the canonical dataset and explicit review decisions."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from collections import Counter
from pathlib import Path

if __package__:
    from .method_checks import negative_confidence_limit
else:
    from method_checks import negative_confidence_limit

SECTIONS = (
    "outputs",
    "first_output",
    "agreements",
    "budget_basis",
    "negative_confidence",
    "ai_act_basis",
)


if __package__:
    from .published import publish
else:
    from published import publish


def cell(value: object) -> str:
    return str(value).replace("|", "&#124;").replace("\n", " ").strip()


def brief(value: str, limit: int = 240) -> str:
    # Compact cells retain citation labels; appendices retain complete links.
    value = re.sub(r"\[([^\]]+)\]\((?:[^()]|\([^()]*\))*\)", r"\1", value)
    if len(value) <= limit:
        return value
    return value[:limit].rsplit(" ", 1)[0] + "…"


def table(headers: list[str], rows: list[list[str]]) -> str:
    return "\n".join(
        [
            "| " + " | ".join(headers) + " |",
            "| " + " | ".join("---" for _ in headers) + " |",
            *("| " + " | ".join(cell(v) for v in row) + " |" for row in rows),
        ]
    )


def load_decisions(path: Path | None, data: dict) -> list[dict]:
    if path is None:
        return []
    decisions = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(decisions, list):
        raise ValueError("Review decisions must be an array")  # noqa: TRY004 - invalid JSON input value
    institutes = {r["id"] for r in data["institutes"]}
    applicable = {section: institutes for section in SECTIONS[:4]}
    applicable["negative_confidence"] = {
        j["id"] for j in data["jurisdictions"] if j.get("status") == "none_identified"
    }
    applicable["ai_act_basis"] = {
        j["id"]
        for j in data["jurisdictions"]
        if j.get("eu_member") or j["id"] in {"IS", "LI", "NO"}
    }
    sources = {s["id"] for s in data["sources"]}
    for decision in decisions:
        if not isinstance(decision, dict) or decision.get(
            "record_id"
        ) not in applicable.get(decision.get("section"), set()):
            raise ValueError(f"Unknown review section or record: {decision}")
        if not decision.get("reason") or not decision.get("what_did_not_count"):
            raise ValueError("Each review decision needs what_did_not_count and reason")
        if decision.get("audit_path") and not Path(decision["audit_path"]).is_file():
            raise ValueError(f"Missing review evidence: {decision['audit_path']}")
        unknown = set(decision.get("source_ids", [])) - sources
        if unknown:
            raise ValueError(f"Unknown review sources: {sorted(unknown)}")
    return decisions


def load_uk_audit(path: Path | None) -> list[dict]:
    if path is None:
        return []
    rows = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(rows, list):
        raise ValueError("UK audit must be an array")  # noqa: TRY004 - invalid JSON input value
    for row in rows:
        if (
            not isinstance(row, dict)
            or not isinstance(row.get("title"), str)
            or not row["title"].strip()
            or not isinstance(row.get("reason"), str)
            or not row["reason"].strip()
            or row.get("decision")
            not in {"reinstate", "already_represented", "retain_exclusion"}
        ):
            raise ValueError(f"Invalid UK audit entry: {row}")
        if row["decision"] != "retain_exclusion" and (
            not isinstance(row.get("lineage"), str) or not row["lineage"].strip()
        ):
            raise ValueError("Included UK audit entries need lineage")
    return rows


def input_identity(label: str, path: Path | None) -> str:
    if path is None:
        return f"{label}: not supplied."
    return f"{label}: `{path.resolve()}`; SHA-256 `{hashlib.sha256(path.read_bytes()).hexdigest()}`."


class Report:
    def __init__(
        self, data: dict, output: Path, decisions: list[dict], uk_audit: Path | None
    ):
        self.data = data
        self.output = output
        self.sources = {s["id"]: s for s in data["sources"]}
        self.jurisdictions = {j["id"]: j for j in data["jurisdictions"]}
        self.decisions = decisions
        self.uk_audit_path = uk_audit
        self.uk_audit = load_uk_audit(uk_audit)

    def uk_pending(self) -> list[dict]:
        outputs = next(
            (
                i.get("outputs", [])
                for i in self.data["institutes"]
                if i["id"] == "gb-aisi"
            ),
            [],
        )
        lineages = {o.get("lineage") for o in outputs}
        titles = {o.get("title", "").casefold() for o in outputs}
        return [
            r
            for r in self.uk_audit
            if r["decision"] != "retain_exclusion"
            and r["lineage"] not in lineages
            and r["title"].casefold() not in titles
        ]

    def refs(self, ids: list[str]) -> str:
        return "; ".join(
            f"[{sid}]({self.sources[sid]['url']})" for sid in dict.fromkeys(ids)
        )

    def local_link(self, path: str | Path, label: str) -> str:
        target = os.path.relpath(Path(path).resolve(), self.output.parent.resolve())
        return f"[{label}]({target})"

    def review(self, section: str, record_id: str) -> tuple[str, str]:
        selected = [
            d
            for d in self.decisions
            if d["section"] == section and d["record_id"] == record_id
        ]
        excluded, reasons = [], []
        for decision in selected:
            excluded.append(decision["what_did_not_count"])
            reason = decision["reason"]
            if decision.get("source_ids"):
                reason += " " + self.refs(decision["source_ids"])
            if decision.get("audit_path"):
                reason += " " + self.local_link(decision["audit_path"], "Review detail")
            reasons.append(reason)
        return " ".join(excluded) or "No item-level exclusion recorded.", " ".join(
            reasons
        )

    def outputs(self) -> str:
        rows = []
        for institute in self.data["institutes"]:
            rid = institute["id"]
            outputs = institute.get("outputs", [])
            attribution = Counter(
                o.get("attribution", "unrecorded attribution") for o in outputs
            )
            accepted = f"{len(outputs)} output lineages. " + "; ".join(
                f"{k}: {v}" for k, v in sorted(attribution.items())
            )
            excluded, reason = self.review("outputs", rid)
            if rid == "gb-aisi" and self.uk_audit:
                counts = Counter(d["decision"] for d in self.uk_audit)
                rejected = [
                    d for d in self.uk_audit if d["decision"] == "retain_exclusion"
                ]
                excluded += f" {len(rejected)} reviewed UK candidates remain excluded. "
                if rejected:
                    excluded += (
                        "Examples: "
                        + "; ".join(d["title"] for d in rejected[:2])
                        + ". "
                    )
                excluded += (
                    "Audit decisions: "
                    + "; ".join(f"{k}: {v}" for k, v in sorted(counts.items()))
                    + "."
                )
                reason += " " + self.local_link(
                    self.uk_audit_path, "UK paper-by-paper audit"
                )
                pending = self.uk_pending()
                accepted += f" Pending inclusion decisions absent from catalogue: {len(pending)}."
                reason += " Audit decisions are review evidence, not additional catalogue counts."
            boundary = (
                institute.get("notes", "") if not outputs else ""
            )
            reason = " ".join(
                filter(
                    None,
                    [
                        reason,
                        boundary,
                        "Catalogue entries require institutional publication or named credit; document versions share one lineage.",
                    ],
                )
            )
            rows.append(
                [
                    rid,
                    accepted,
                    brief(excluded),
                    brief(reason)
                    + f" [Titles and evidence](#outputs-{rid}); [review decisions](#review-decisions).",
                ]
            )
        return table(
            [
                "Institute",
                "What counted",
                "What did not count",
                "Deciding reason / evidence",
            ],
            rows,
        )

    def first_output(self) -> str:
        rows = []
        for institute in self.data["institutes"]:
            rid = institute["id"]
            j = self.jurisdictions[institute["jurisdiction_id"]]
            events = [e for e in j.get("timeline", []) if e["event"] == "first_output"]
            outputs = institute.get("outputs", [])
            earliest = min(
                (o.get("credited_from", o["date"]) for o in outputs), default=None
            )
            accepted = (
                "; ".join(f"{e['date']}: {e.get('short', e['id'])}" for e in events)
                or "No first-output event recorded."
            )
            if earliest:
                accepted += f" Earliest qualifying catalogue credit: {earliest}."
            excluded, reason = self.review("first_output", rid)
            rule = "Use credited_from when present, otherwise publication date; predecessor credit does not backdate designation."
            if events and earliest and any(e["date"] != earliest for e in events):
                rule += " WARNING: event date and earliest catalogue credit differ."
            rows.append(
                [
                    rid,
                    accepted,
                    brief(excluded),
                    brief(" ".join(filter(None, [reason, rule])))
                    + f" [Output evidence](#outputs-{rid}); [review decisions](#review-decisions).",
                ]
            )
        return table(
            [
                "Institute",
                "What counted",
                "What did not count",
                "Deciding reason / evidence",
            ],
            rows,
        )

    def agreements(self) -> str:
        rows = []
        for institute in self.data["institutes"]:
            rid = institute["id"]
            agreements = institute.get("agreements", [])
            labels = [
                f"{a['counterparty']} ({a['state']}, {a.get('date', 'date not recorded')}; {a.get('participation', 'role not recorded')})"
                for a in agreements
            ]
            states = Counter(a["state"] for a in agreements)
            roles = Counter(
                a.get("participation", "role not recorded") for a in agreements
            )
            accepted = f"{len(agreements)} agreements."
            if agreements:
                accepted += " " + "; ".join(
                    f"{k}: {v}" for k, v in sorted(states.items())
                )
                accepted += ". " + "; ".join(
                    f"{k}: {v}" for k, v in sorted(roles.items())
                )
            if labels:
                accepted += ". Examples: " + "; ".join(labels[:2]) + "."
            excluded, reason = self.review("agreements", rid)
            reason = " ".join(
                filter(
                    None,
                    [
                        reason,
                        "Institute named as a party or implementing body. Conditional cooperation remains conditional; government parties are distinct from institute implementers.",
                    ],
                )
            )
            rows.append(
                [
                    rid,
                    brief(accepted),
                    brief(excluded),
                    brief(reason)
                    + f" [Agreement evidence](#agreements-{rid}); [review decisions](#review-decisions).",
                ]
            )
        return table(
            [
                "Institute",
                "What counted",
                "What did not count",
                "Deciding reason / evidence",
            ],
            rows,
        )

    def rows(self) -> str:
        """Head, reporting line and partners per institute: the state of each row and what the partner rule accepted."""
        rows = []
        for institute in self.data["institutes"]:
            head, reports, partners = (
                institute.get(key) for key in ("head", "reports_to", "national_partners")
            )
            person = next(
                (
                    p
                    for p in institute.get("people", [])
                    if head and p.get("id") == head.get("person_id")
                ),
                None,
            )
            entries = (partners or {}).get("entries", [])
            by_relation = Counter(e["relation"] for e in entries)
            cyber = any(e.get("cyber_agency") for e in entries)
            rows.append(
                [
                    institute["id"],
                    "not recorded"
                    if head is None
                    else head["state"] + (f": {person['name']}" if person else ""),
                    "not recorded"
                    if reports is None
                    else reports["state"] + (f": {reports['name']}" if reports.get("name") else ""),
                    "not recorded"
                    if partners is None
                    else f"{partners['state']}: {len(entries)} entries"
                    + (
                        " (" + "; ".join(f"{k} {v}" for k, v in sorted(by_relation.items())) + ")"
                        if entries
                        else ""
                    )
                    + ("; cyber agency present" if cyber else "; no cyber agency"),
                ]
            )
        return table(["Institute", "Head", "Reports to", "Partners"], rows)

    def budgets(self) -> str:
        rows = []
        as_of = self.data["dataset"]["as_of"]
        for institute in self.data["institutes"]:
            current, other = [], []
            for funding in institute.get("funding", []):
                amount = funding.get("annual_amount")
                if amount is None:
                    continue
                label = f"funding/{funding['id']}: {amount:,} {funding.get('currency', '')}; {funding.get('commitment', 'commitment unrecorded')}; {funding.get('annual_basis_code', 'unrecorded basis')}; {funding.get('period', 'period unrecorded')}"
                profile = [
                    p
                    for p in funding.get("annual_profile", [])
                    if p["start"] <= as_of <= p["end"]
                ]
                window = funding.get("annual_period", {})
                if profile:
                    allocation = profile[0]
                    current.append(
                        f"funding/{funding['id']}: {allocation['amount']:,} {funding.get('currency', '')}; {funding.get('commitment', 'commitment unrecorded')}; allocation_current_year; {allocation['start']}/{allocation['end']}"
                    )
                    if amount != allocation["amount"]:
                        other.append(
                            "WARNING: recorded annual amount differs from current profile. "
                            + label
                        )
                elif (
                    window
                    and window["start"] <= as_of <= window["end"]
                    and funding.get("annual_basis_code") == "allocation_current_year"
                ):
                    current.append(label + f"; {window['start']}/{window['end']}")
                else:
                    other.append(label)
            excluded, reason = self.review("budget_basis", institute["id"])
            rule = "Explicit current fiscal allocation takes precedence over a period mean; other periods and commitments remain labelled, not spending. Comparison rule: dataset.method (Annual funding)."
            rid = institute["id"]
            rows.append(
                [
                    rid,
                    brief(" ".join(current))
                    or "No explicitly dated current allocation; see other annual bases.",
                    brief(" ".join(other)) or "No other annual amount recorded.",
                    brief(excluded),
                    brief(reason + " " + rule)
                    + f" [Amounts, periods and basis](#budget-{rid}); [review decisions](#review-decisions).",
                ]
            )
        return table(
            [
                "Institute",
                "Comparable current allocation",
                "Other annual bases (means, future, other)",
                "What did not count",
                "Deciding reason / evidence",
            ],
            rows,
        )

    def negatives(self) -> str:
        rows = []
        for j in self.data["jurisdictions"]:
            if j["status"] != "none_identified":
                continue
            check = j["check"]
            searches = [
                q
                for q in check.get("searches", [])
                if q.get("language")
                and q.get("query", "").strip()
                and q.get("date", "9999") <= self.data["dataset"]["as_of"]
            ]
            languages = (
                ", ".join(sorted({q["language"] for q in searches})) or "none recorded"
            )
            governance = set(check.get("governance_source_ids", [])) & set(
                j["source_ids"]
            )
            primary = sum(
                self.sources.get(sid, {}).get("type") == "primary" for sid in governance
            )
            limit = negative_confidence_limit(
                j, self.sources, self.data["dataset"]["as_of"]
            )
            counted = f"Recorded {j.get('status_confidence', 'unrecorded')}; rule maximum {limit}. {check['method']}; primary governance citations: {primary}."
            decision, reason = self.review("negative_confidence", j["id"])
            if {"low": 0, "medium": 1, "high": 2}.get(
                j.get("status_confidence"), -1
            ) > {"low": 0, "medium": 1, "high": 2}[limit]:
                counted += " WARNING: recorded confidence exceeds rule maximum."
            excluded = f"Eligible query languages: {languages}. "
            english = any(
                q.get("language", "").split("-")[0].casefold() == "en" for q in searches
            )
            local = any(q.get("national_or_official") for q in searches)
            excluded += f"English query: {english}; national/official query: {local}. "
            excluded += (
                "No qualifying recent official denial."
                if limit == "medium"
                else "Search or primary-governance conditions incomplete."
                if limit == "low"
                else "Recent official denial meets the high-confidence rule."
            )
            rows.append(
                [
                    j["id"] + " " + j["name"],
                    counted,
                    excluded + " " + brief(decision, 160),
                    brief(reason)
                    + f" Checked {check['last_checked']}. [Evidence and limits](#negative-{j['id']}); [review decisions](#review-decisions).",
                ]
            )
        return table(
            [
                "Jurisdiction",
                "What counted",
                "Language / evidence limits",
                "Deciding reason / evidence",
            ],
            rows,
        )

    def authorities(self) -> str:
        rows = []
        for j in self.data["jurisdictions"]:
            if not j.get("eu_member") and j["id"] not in {"IS", "LI", "NO"}:
                continue
            bodies = [
                b
                for b in j.get("related_bodies", [])
                if b.get("kind") == "ai_act_authority"
            ]
            bases = Counter(b.get("designation_basis", "unresolved") for b in bodies)
            states = Counter(b.get("designation_state", "unresolved") for b in bodies)
            accepted = f"{len(bodies)} bodies. " + "; ".join(
                f"{k}: {v}" for k, v in sorted(states.items())
            )
            reason = (
                "; ".join(f"{k}: {v}" for k, v in sorted(bases.items()))
                or "No identified authority; absence is not established."
            )
            exceptions = [
                b["name"] for b in bodies if b.get("designation_state") != "designated"
            ]
            if exceptions:
                reason += (
                    ". Pending or unresolved: "
                    + "; ".join(exceptions[:2])
                    + (
                        f"; {len(exceptions) - 2} more in detail"
                        if len(exceptions) > 2
                        else ""
                    )
                )
            excluded, decision_reason = self.review("ai_act_basis", j["id"])
            reason = reason + " " + brief(decision_reason, 160)
            rows.append(
                [
                    j["id"] + " " + j["name"],
                    accepted,
                    "Authority designation alone does not qualify as an AISI. "
                    + brief(excluded, 160),
                    reason
                    + f" [Authority detail](#authority-{j['id']}); [review decisions](#review-decisions).",
                ]
            )
        return table(
            [
                "Jurisdiction",
                "What counted",
                "What did not count",
                "Deciding reason / evidence",
            ],
            rows,
        )

    def evidence_details(self) -> str:
        chunks = ["## Negative-confidence evidence"]
        for j in self.data["jurisdictions"]:
            if j["status"] != "none_identified":
                continue
            check = j["check"]
            excluded, reason = self.review("negative_confidence", j["id"])
            chunks.append(
                f'<a id="negative-{j["id"]}"></a>\n\n### {j["id"]}: {j["name"]}'
            )
            chunks.append(
                " ".join(
                    filter(
                        None,
                        [
                            check.get("governance_note"),
                            check.get("language_note"),
                            check.get("independence_note"),
                            self.refs(check.get("governance_source_ids", [])),
                            reason,
                            f"Check basis: {check.get('last_checked_basis', 'not recorded')}. {excluded}",
                        ],
                    )
                )
            )
        chunks.append("## AI Act authority details")
        for j in self.data["jurisdictions"]:
            if not j.get("eu_member") and j["id"] not in {"IS", "LI", "NO"}:
                continue
            chunks.append(
                f'<a id="authority-{j["id"]}"></a>\n\n### {j["id"]}: {j["name"]}'
            )
            excluded, reason = self.review("ai_act_basis", j["id"])
            chunks.append(excluded + " " + reason)
            lines = []
            for body in j.get("related_bodies", []):
                if body.get("kind") != "ai_act_authority":
                    continue
                lines.append(
                    f"- {body['name']} (`{j['id']}/related_bodies/{body['id']}`): {body.get('designation_state', 'unresolved')}; {body.get('designation_basis', 'unresolved')}; roles: {', '.join(body.get('authority_roles', [])) or 'not coded'}. {body['role']} {body.get('designation_note', '')} {self.refs(body.get('source_ids', []))}"
                )
            chunks.append(
                "\n".join(lines)
                or "No authority identified in the dataset. "
                + j.get("notes", "")
                + " "
                + self.refs(j["source_ids"])
            )
        return "\n\n".join(chunks)

    def details(self) -> str:
        chunks = ["## Output details"]
        for institute in self.data["institutes"]:
            rid = institute["id"]
            chunks.append(
                f'<a id="outputs-{rid}"></a>\n\n### {rid}: {institute["name"]}'
            )
            outputs = institute.get("outputs", [])
            lines = []
            for o in outputs:
                credit = (
                    f" credited_from: {o['credited_from']}."
                    if o.get("credited_from")
                    else ""
                )
                lines.append(
                    f"- {o['date']};{credit} {o['title']} (`{rid}/outputs/{o['lineage']}`, lineage `{o.get('lineage', 'not recorded')}`). {o.get('attribution_note', '')} {self.refs(o['source_ids'])}"
                )
            chunks.append(
                "\n".join(lines)
                or "No credited output recorded; this is not a finding that the institute produces no work."
            )
        chunks.append("## First-output event details")
        for institute in self.data["institutes"]:
            j = self.jurisdictions.get(institute.get("jurisdiction_id"), {})
            for event in j.get("timeline", []):
                if event["event"] == "first_output":
                    chunks.append(
                        f"- {institute['id']}: {event['date']}, {event['description']} ({event['id']}). {self.refs(event['source_ids'])}"
                    )
        chunks.append("## Budget details")
        for institute in self.data["institutes"]:
            rid = institute["id"]
            chunks.append(
                f'<a id="budget-{rid}"></a>\n\n### {rid}: {institute["name"]}'
            )
            lines = []
            for f in institute.get("funding", []):
                lines.append(
                    f"- `{rid}/funding/{f['id']}`: {f.get('annual_amount', 'no annual amount')} {f.get('currency', '')}; {f.get('period', 'period unrecorded')}; basis {f.get('annual_basis_code', 'not recorded')}; {f.get('commitment', 'commitment unrecorded')}. {f.get('annual_basis', '')} {f['description']} {f.get('notes', '')} {self.refs(f['source_ids'])}"
                )
                for allocation in f.get("annual_profile", []):
                    lines.append(
                        "  - Fiscal profile: "
                        + json.dumps(allocation, ensure_ascii=False)
                    )
            chunks.append("\n".join(lines) or "No funding record.")
        chunks.append("## Agreement details")
        for institute in self.data["institutes"]:
            rid = institute["id"]
            chunks.append(
                f'<a id="agreements-{rid}"></a>\n\n### {rid}: {institute["name"]}'
            )
            lines = []
            for a in institute.get("agreements", []):
                lines.append(
                    f"- {a.get('date', 'Date not recorded')}: {a['counterparty']}, {a['state']} (`{rid}/agreements/{a['id']}`, {a.get('participation', 'role not recorded')}). {a['scope']} {self.refs(a['source_ids'])}"
                )
            chunks.append("\n".join(lines) or "No qualifying agreement recorded.")
        return "\n\n".join(chunks)

    def review_details(self) -> str:
        lines = ['<a id="review-decisions"></a>\n\n## Explicit review decisions']
        for decision in self.decisions:
            line = f"- {decision['section']}, {decision['record_id']}: {decision['what_did_not_count']} {decision['reason']}"
            if decision.get("source_ids"):
                line += " " + self.refs(decision["source_ids"])
            if decision.get("audit_path"):
                line += " " + self.local_link(decision["audit_path"], "Review detail")
            lines.append(line)
        if not self.decisions:
            lines.append(
                "No explicit review-decision file supplied; missing catalogue items are not classified as exclusions."
            )
        if self.uk_audit_path:
            lines.append(
                self.local_link(self.uk_audit_path, "Full UK candidate audit")
                + ": external editorial evidence, not extra catalogue entries."
            )
        for row in self.uk_pending():
            lines.append(
                f"- Pending UK inclusion: {row['title']} (lineage `{row['lineage']}`), decision {row['decision']}; absent from supplied catalogue. {row['reason']}"
            )
        return "\n\n".join(lines)

    def render(self, inputs: str) -> str:
        meta = self.data["dataset"]
        sections = [
            ("Outputs", self.outputs()),
            ("First output", self.first_output()),
            ("Agreements", self.agreements()),
            ("Budget basis", self.budgets()),
            ("Negative confidence", self.negatives()),
            ("AI Act basis", self.authorities()),
            ("Head, reporting line and partners", self.rows()),
        ]
        intro = f"# Rule comparisons\n\nGenerated from dataset {meta['version']}, as of {meta['as_of']}. Inputs: {inputs} Counts describe the supplied catalogue, not all work produced. Detail labels use stable record IDs and documented natural keys. Every institute appears in the first four tables; every none_identified jurisdiction appears in the fifth; the sixth table covers EU members plus Iceland, Liechtenstein and Norway; the last table covers every institute again.\n\nExclusions are shown only when explicitly supplied in review decisions. An unlisted item is not assumed to have been rejected. Canonical notes and source citations retain their scope; this report does not independently reassess confidence or source accuracy. UK audit decisions can precede their application to the catalogue.\n\n"
        return (
            intro
            + "\n\n".join(f"## {heading}\n\n{body}" for heading, body in sections)
            + "\n\n"
            + self.details()
            + "\n\n"
            + self.evidence_details()
            + "\n\n"
            + self.review_details()
            + "\n"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=Path("data/aisi.json"))
    parser.add_argument(
        "--output", type=Path, default=Path("work/reports/rule-tables.md")
    )
    parser.add_argument(
        "--decisions",
        type=Path,
        help="Explicit review decisions JSON; no exclusions are inferred when omitted",
    )
    parser.add_argument(
        "--uk-audit",
        type=Path,
        help="Explicit UK paper-by-paper audit JSON to summarise and link",
    )
    args = parser.parse_args()
    raw = args.data.read_bytes()
    data = publish(json.loads(raw))
    report = Report(
        data, args.output, load_decisions(args.decisions, data), args.uk_audit
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    identities = " ".join(
        input_identity(label, path)
        for label, path in [
            ("Data", args.data),
            ("Decisions", args.decisions),
            ("UK audit", args.uk_audit),
        ]
    )
    args.output.write_text(report.render(identities), encoding="utf-8")
    print(args.output)


if __name__ == "__main__":
    main()
