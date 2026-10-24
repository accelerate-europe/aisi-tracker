"""Hand-worked contracts for the figure-source labels: indirect or secondary (medium), secondary sources, press report, our estimate."""

import copy
import json
from pathlib import Path

import pytest

from scripts import build
from scripts.check_page import figure_grade
from scripts.display import figure_source

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    "record,label",
    [
        ({"confidence": "high"}, ""),
        ({"confidence": "medium"}, "indirect or secondary"),
        ({"confidence": "low", "figure_source": "secondary_sources"}, "secondary sources"),
        ({"confidence": "low", "figure_source": "press_report"}, "press report"),
        ({"confidence": "low", "figure_source": "our_estimate"}, "our estimate"),
    ],
)
def test_producer_and_gate_agree_on_each_label(record, label):
    assert figure_source(record) == label
    assert figure_grade(record) == (label or None)


def test_real_low_figures_name_their_source_and_the_check_catches_mutations():
    data = json.loads((ROOT / "data/aisi.json").read_text())
    assert build.check_figure_sources(data) == []
    low = [(i, r) for i in data["institutes"] for k in ("funding", "staff") for r in i.get(k, []) if r["confidence"] == "low"]
    assert {r["figure_source"] for _, r in low} == {"secondary_sources", "press_report", "our_estimate"}

    broken = copy.deepcopy(data)
    record = next(r for i in broken["institutes"] for r in i.get("staff", []) if r["confidence"] == "low")
    del record["figure_source"]
    assert any("figure_source is required for low confidence and only then" in e for e in build.check_figure_sources(broken))

    broken = copy.deepcopy(data)
    record = next(r for i in broken["institutes"] for r in i.get("funding", []) if r["confidence"] == "high")
    record["figure_source"] = "press_report"
    assert any("figure_source is required for low confidence and only then" in e for e in build.check_figure_sources(broken))

    broken = copy.deepcopy(data)
    record = next(r for i in broken["institutes"] for r in i.get("funding", []) if "sum_of" in r and r["confidence"] == "low")
    record["figure_source"] = "press_report"
    assert any("a low-confidence sum of components is our_estimate" in e for e in build.check_figure_sources(broken))
