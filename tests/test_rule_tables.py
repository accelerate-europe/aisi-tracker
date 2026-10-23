"""Offline report regressions: attribution dates and evidence are not inferred."""

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

import pytest

from scripts.rule_tables import Report, load_decisions, load_uk_audit, main


class ReportTests(unittest.TestCase):
    def report(self, data):
        return Report(data, Path("work/reports/rule-tables.md"), [], None)

    def test_detail_keeps_publication_and_credit_dates_distinct(self):
        data = {
            "sources": [],
            "jurisdictions": [],
            "institutes": [
                {
                    "id": "test",
                    "name": "Test",
                    "outputs": [
                        {
                            "date": "2025-01",
                            "credited_from": "2025-07-18",
                            "title": "Paper",
                            "lineage": "paper",
                            "source_ids": [],
                        }
                    ],
                }
            ],
        }
        text = self.report(data).details()
        self.assertIn("2025-01", text)
        self.assertIn("credited_from: 2025-07-18", text)

    def test_missing_catalogue_item_is_not_an_exclusion(self):
        data = {
            "sources": [],
            "jurisdictions": [],
            "institutes": [{"id": "test", "outputs": []}],
        }
        text = self.report(data).outputs()
        self.assertIn("0 output lineages", text)
        self.assertIn("No item-level exclusion recorded.", text)
        self.assertNotIn("remain excluded", text)

    def test_secondary_governance_source_does_not_count_as_primary(self):
        data = {
            "dataset": {"as_of": "2026-10-17"},
            "sources": [{"id": "s", "type": "secondary"}],
            "jurisdictions": [
                {
                    "id": "ZZ",
                    "name": "Test",
                    "status": "none_identified",
                    "status_confidence": "low",
                    "source_ids": ["s"],
                    "check": {
                        "method": "individual",
                        "last_checked": "2026-10-17",
                        "governance_source_ids": ["s"],
                        "governance_note": "Official landscape reviewed.",
                        "searches": [
                            {
                                "language": "en-GB",
                                "query": "institute",
                                "date": "2026-10-17",
                                "national_or_official": True,
                            }
                        ],
                    },
                }
            ],
        }
        text = self.report(data).negatives()
        self.assertIn("primary governance citations: 0", text)
        self.assertIn("rule maximum low", text)
        data["sources"][0]["type"] = "primary"
        text = self.report(data).negatives()
        self.assertIn("primary governance citations: 1", text)
        self.assertIn("rule maximum medium", text)

    def test_first_output_uses_credit_date_not_earlier_publication(self):
        data = {
            "sources": [],
            "jurisdictions": [
                {
                    "id": "ZZ",
                    "timeline": [
                        {
                            "id": "first",
                            "event": "first_output",
                            "date": "2025-05",
                            "short": "Report published.",
                            "source_ids": [],
                        }
                    ],
                }
            ],
            "institutes": [
                {
                    "id": "test",
                    "jurisdiction_id": "ZZ",
                    "outputs": [
                        {
                            "date": "2025-01",
                            "credited_from": "2025-07",
                            "source_ids": [],
                        },
                        {"date": "2025-05", "source_ids": []},
                    ],
                }
            ],
        }
        text = self.report(data).first_output()
        self.assertIn("Earliest qualifying catalogue credit: 2025-05", text)
        self.assertNotIn("WARNING", text)
        data["jurisdictions"][0]["timeline"][0]["date"] = "2025-01"
        self.assertIn("WARNING", self.report(data).first_output())

    def test_authority_without_optional_designation_fields_is_still_counted(self):
        data = {
            "sources": [],
            "institutes": [],
            "jurisdictions": [
                {
                    "id": "AT",
                    "name": "Austria",
                    "eu_member": True,
                    "related_bodies": [
                        {
                            "kind": "ai_act_authority",
                            "name": "Sector regulator",
                            "role": "Limited competence",
                        },
                        {"kind": "other", "name": "Digital ministry", "role": "Policy"},
                    ],
                }
            ],
        }
        text = self.report(data).authorities()
        self.assertIn("1 bodies. unresolved: 1", text)
        self.assertIn("Sector regulator", text)
        self.assertNotIn("Digital ministry", text)

    def test_budget_table_keeps_future_period_and_current_basis_distinct(self):
        data = {
            "dataset": {"as_of": "2026-10-17"},
            "sources": [],
            "jurisdictions": [],
            "institutes": [
                {
                    "id": "test",
                    "funding": [
                        {
                            "id": "current-budget", "annual_amount": 10.25,
                            "commitment": "announced",
                            "annual_profile": [
                                {
                                    "start": "2026-07-01",
                                    "end": "2027-06-30",
                                    "amount": 10.25,
                                }
                            ],
                            "currency": "AUD",
                            "annual_basis_code": "allocation_current_year",
                            "period": "2026-27",
                        },
                        {
                            "id": "future-budget", "annual_amount": 7,
                            "currency": "AUD",
                            "annual_basis_code": "other",
                            "period": "From 2029-30",
                        },
                    ],
                }
            ],
        }
        data["institutes"][0]["funding"].append(
            {
                "id": "mean-budget", "annual_amount": 8,
                "currency": "AUD",
                "annual_basis_code": "period_mean",
                "period": "2025-29",
                "commitment": "adopted",
            }
        )
        text = self.report(data).budgets()
        columns = text.splitlines()[-1].split("|")
        self.assertIn("10.25 AUD; announced", columns[2])
        self.assertNotIn("2029", columns[2])
        self.assertIn("7 AUD", columns[3])
        self.assertIn("2029", columns[3])
        self.assertNotIn("period_mean", columns[2])
        self.assertIn("period_mean", columns[3])

    def test_explicit_decision_rejects_unknown_citation(self):
        data = {"sources": [], "institutes": [{"id": "test"}], "jurisdictions": []}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "decisions.json"
            path.write_text(
                json.dumps(
                    [
                        {
                            "section": "outputs",
                            "record_id": "test",
                            "what_did_not_count": "Uncredited paper",
                            "reason": "No institute issuer",
                            "source_ids": ["missing"],
                        }
                    ]
                )
            )
            with self.assertRaisesRegex(ValueError, "Unknown review sources"):
                load_decisions(path, data)

    def test_long_cited_reason_keeps_links_only_in_full_detail(self):
        url = "https://example.org/" + "long-publication-path/" * 30
        data = {
            "sources": [{"id": "source", "title": "Primary publication", "url": url}],
            "jurisdictions": [],
            "institutes": [{"id": "test", "outputs": []}],
        }
        decisions = [
            {
                "section": "outputs",
                "record_id": "test",
                "what_did_not_count": "Uncredited publication",
                "reason": "The publication does not name the institute.",
                "source_ids": ["source"],
            }
        ]
        report = Report(data, Path("work/reports/rule-tables.md"), decisions, None)
        compact = report.outputs()
        self.assertIn("source", compact)
        self.assertNotIn("https://example.org", compact)
        self.assertIn("[review decisions](#review-decisions)", compact)
        self.assertIn(f"[source]({url})", report.review_details())


def minimal_data():
    return {
        "dataset": {"version": "1.0.0", "as_of": "2026-10-17"},
        "sources": [],
        "institutes": [],
        "jurisdictions": [],
    }


def decision(section, record):
    return {
        "section": section,
        "record_id": record,
        "what_did_not_count": "Explicit exclusion",
        "reason": "Reviewed reason",
    }


def test_main_authority_and_negative_rows_render_decisions_without_country_fact():
    data = minimal_data()
    data["jurisdictions"] = [
        {
            "id": "AT",
            "name": "Austria",
            "eu_member": True,
            "status": "none_identified",
            "source_ids": [],
            "check": {"method": "individual", "last_checked": "2026-10-17"},
        }
    ]
    report = Report(
        data,
        Path("report.md"),
        [decision("ai_act_basis", "AT"), decision("negative_confidence", "AT")],
        None,
    )
    for text in [report.authorities(), report.negatives()]:
        assert "Explicit exclusion" in text and "Reviewed reason" in text
    report.decisions = []
    assert "Article 74(8)" not in report.authorities()


def test_authority_scope_includes_eea_but_not_other_states():
    data = minimal_data()
    data["jurisdictions"] = [
        {"id": code, "name": code} for code in ["IS", "LI", "NO", "US"]
    ]
    text = Report(data, Path("report.md"), [], None).authorities()
    assert all(f"| {code} {code} |" in text for code in ["IS", "LI", "NO"])
    assert "| US US |" not in text


@pytest.mark.parametrize(
    ("section", "record"),
    [
        ("outputs", "US"),
        ("ai_act_basis", "US"),
        ("negative_confidence", "US"),
        ("missing", "unit"),
    ],
)
def test_decision_section_must_apply_to_record(tmp_path, section, record):
    data = minimal_data()
    data["institutes"] = [{"id": "unit"}]
    data["jurisdictions"] = [{"id": "US", "status": "established"}]
    path = tmp_path / "decisions.json"
    path.write_text(json.dumps([decision(section, record)]))
    with pytest.raises(ValueError, match="section or record"):
        load_decisions(path, data)


def test_missing_decision_keys_and_missing_evidence_fail_cleanly(tmp_path):
    data = minimal_data()
    data["institutes"] = [{"id": "unit"}]
    path = tmp_path / "decisions.json"
    for row in [
        {},
        {**decision("outputs", "unit"), "audit_path": str(tmp_path / "absent")},
    ]:
        path.write_text(json.dumps([row]))
        with pytest.raises(ValueError):
            load_decisions(path, data)


def test_negative_languages_and_limit_use_eligible_queries_and_same_sources():
    data = minimal_data()
    data["sources"] = [{"id": "s", "type": "primary"}]
    data["jurisdictions"] = [
        {
            "id": "ZZ",
            "name": "Z",
            "status": "none_identified",
            "status_confidence": "high",
            "source_ids": [],
            "check": {
                "method": "individual",
                "last_checked": "2026-10-17",
                "governance_source_ids": ["s"],
                "governance_note": "Reviewed",
                "searches": [
                    {"language": "fr", "query": "future", "date": "2027-01-01"},
                    {"language": "de", "query": "", "date": "2025-01-01"},
                    {
                        "language": "en",
                        "query": "institute",
                        "date": "2025-01-01",
                        "national_or_official": True,
                    },
                ],
            },
        }
    ]
    text = Report(data, Path("report.md"), [], None).negatives()
    assert "primary governance citations: 0" in text and "rule maximum low" in text
    assert "Eligible query languages: en." in text
    assert "exceeds rule maximum" in text


def test_uk_audit_validation_and_pending_inclusions(tmp_path):
    path = tmp_path / "uk.json"
    path.write_text(json.dumps([{"decision": "reinstate"}]))
    with pytest.raises(ValueError, match="Invalid UK audit"):
        load_uk_audit(path)
    rows = [
        {
            "title": "Missing paper",
            "decision": "reinstate",
            "lineage": "missing",
            "reason": "Own publication",
        },
        {
            "title": "Present paper",
            "decision": "already_represented",
            "lineage": "present",
            "reason": "Retained",
        },
    ]
    path.write_text(json.dumps(rows))
    data = minimal_data()
    data["institutes"] = [
        {
            "id": "gb-aisi",
            "outputs": [{"title": "Retitled paper", "lineage": "present"}],
        }
    ]
    report = Report(data, tmp_path / "report.md", [], path)
    assert report.uk_pending() == rows[:1]
    assert "Pending inclusion decisions absent from catalogue: 1" in report.outputs()
    assert "Pending UK inclusion: Missing paper" in report.review_details()


def test_cli_render_identifies_every_input_and_absent_inputs(tmp_path, monkeypatch):
    data_path = tmp_path / "data.json"
    decisions = tmp_path / "decisions.json"
    audit = tmp_path / "uk.json"
    output = tmp_path / "report.md"
    data_path.write_text(json.dumps(minimal_data()))
    decisions.write_text("[]")
    audit.write_text("[]")
    args = ["rule_tables.py", "--data", str(data_path), "--output", str(output)]
    monkeypatch.setattr(sys, "argv", args)
    main()
    text = output.read_text()
    assert "Decisions: not supplied." in text and "UK audit: not supplied." in text
    monkeypatch.setattr(
        sys, "argv", args + ["--decisions", str(decisions), "--uk-audit", str(audit)]
    )
    main()
    text = output.read_text()
    for path in [data_path, decisions, audit]:
        assert str(path.resolve()) in text
        assert hashlib.sha256(path.read_bytes()).hexdigest() in text
    decisions.write_text("[ ]")
    main()
    assert output.read_text() != text


def test_long_decisions_do_not_hide_computed_main_table_facts():
    data = minimal_data()
    data["jurisdictions"] = [
        {
            "id": "AT",
            "name": "Austria",
            "eu_member": True,
            "status": "none_identified",
            "source_ids": [],
            "related_bodies": [
                {
                    "kind": "ai_act_authority",
                    "name": "Pending regulator",
                    "designation_state": "proposed",
                    "designation_basis": "proposal",
                }
            ],
            "check": {
                "method": "individual",
                "last_checked": "2026-10-17",
                "searches": [
                    {
                        "query": "AI institute",
                        "language": "de",
                        "date": "2026-10-17",
                        "national_or_official": True,
                    }
                ],
            },
        }
    ]
    decisions = [
        decision(section, "AT") for section in ["ai_act_basis", "negative_confidence"]
    ]
    for item in decisions:
        item.update(
            reason="Long review reason " * 100,
            what_did_not_count="Lengthy exclusion " * 100,
        )
    report = Report(data, Path("report.md"), decisions, None)
    authority = report.authorities()
    assert "proposal: 1" in authority and "Pending regulator" in authority
    assert "Authority designation alone does not qualify as an AISI." in authority
    negative = report.negatives()
    assert "Eligible query languages: de" in negative
    assert "English query: False; national/official query: True" in negative
    assert "Search or primary-governance conditions incomplete." in negative
