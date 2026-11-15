"""Every saved card's activity qualifier, including each case that must omit it."""

import pytest
from bs4 import BeautifulSoup

RULE_CASES = (
    "closest with activity", "closest without activity", "planned or proposed with activity",
    "planned or proposed without activity", "established without work", "established with work", "other or legacy",
)


def activity_expectation(j, institute, vocabulary):
    """Independent expectations from published claims and function rows, never page-generated text."""
    record = institute or j
    if "claims" not in record:
        return "other or legacy", "", set()
    if institute:
        if j["status"] == "established":
            if not institute["display"]["operating"]["items"]:
                return "established without work", "Established. No published work found yet.", set()
            return "established with work", "", set()
        if j["status"] not in ("planned", "proposed"):
            return "other or legacy", "", set()
        ids = {cid for r in institute.get("functions", []) if r["value"] in ("activity", "both") for cid in r["claim_ids"]}
        codes = {r["code"] for r in institute.get("functions", []) if r["value"] in ("activity", "both")}
        claims = [c for c in institute["claims"] if c["id"] in ids and c["kind"] == "function" and c["basis"] == "activity"]
        case = "planned or proposed with activity" if codes else "planned or proposed without activity"
        prefix = "Not yet established. Active in "
    elif j["status"] == "none_identified" and j.get("closest_body"):
        body_id = j["closest_body"].get("body_id")
        claims = [c for c in j["claims"] if c["kind"] == "function" and c["basis"] == "activity"
                  and not c.get("expired") and body_id is not None and c.get("body_id") == body_id]
        codes = {c["code"] for c in claims}
        case = "closest with activity" if codes else "closest without activity"
        prefix = "Not designated. Active in "
    else:
        return "other or legacy", "", set()
    if not codes:
        return case, "", set()
    words = [entry["short"] for code, entry in vocabulary.items() if code in codes]
    if len(words) > 3:
        work = ", ".join(words[:3]) + f" and {len(words) - 3} more"
    elif len(words) > 1:
        work = ", ".join(words[:-1]) + " and " + words[-1]
    else:
        work = words[0]
    return case, prefix + work + ".", {sid for c in claims for sid in c["source_ids"]}


def line_text(node):
    return "".join(str(n) for n in node.find_all(string=True) if n.find_parent(class_="cites") is None).strip()


def check_activity_lines(html, data, case=None):
    soup = BeautifulSoup(html, "html.parser")
    institutes = {i["id"]: i for i in data.get("institutes", [])}
    vocabulary = data.get("dataset", {}).get("vocabularies", {}).get("function", {})
    errors = []
    for j in data.get("jurisdictions", []):
        institute = institutes.get(j.get("institute_id"))
        actual_case, expected, sources = activity_expectation(j, institute, vocabulary)
        if case is not None and actual_case != case:
            continue
        card = soup.find(id="c-" + j["id"].lower())
        if card is None:
            if j.get("europe_scope") or j.get("kind") == "supranational" or j["status"] != "none_identified":
                errors.append(f"{j['id']}: missing card")
            continue
        lines = card.select(".activity-line")
        if len(lines) != bool(expected):
            errors.append(f"{j['id']}: expected {int(bool(expected))} activity lines, found {len(lines)}")
            continue
        if not expected:
            continue
        line, = lines
        if line_text(line) != expected:
            errors.append(f"{j['id']}: activity text differs: {line_text(line)!r} != {expected!r}")
        if {a.get("data-source-id") for a in line.select(".cites .cn")} != sources:
            errors.append(f"{j['id']}: activity citations differ")
        if not {"small", "qualifiers"} <= set(line.get("class", [])) or "det" in line.get("class", []):
            errors.append(f"{j['id']}: activity is not a muted qualifier")
        if institute:
            target = card.select_one('.row[data-track="3"] > .v')
            if line.parent is not target or line.find_next_sibling() is not None:
                errors.append(f"{j['id']}: activity must follow the Status qualifiers in the same value")
        else:
            target = card.select_one(".body-group.closest li")
            if line.parent is not target or target.get("data-body-id") != j["closest_body"]["body_id"]:
                errors.append(f"{j['id']}: activity must belong to the closest body's item")
            previous = line.find_previous_sibling()
            stage = {"planned": "Planned.", "proposed": "Proposed.", "under_study": "Under study."}.get(j["closest_body"]["stage"])
            if stage:
                if previous is None or line_text(previous) != stage:
                    errors.append(f"{j['id']}: activity must follow the stage")
                previous = previous.find_previous_sibling() if previous is not None else None
            if previous is None or "body-name" not in previous.get("class", []):
                errors.append(f"{j['id']}: activity and stage must directly follow the name")
    return errors


FAMILY = {"activity line": check_activity_lines}


@pytest.mark.parametrize("case", RULE_CASES)
def test_activity_line_matches_the_data(case, saved_html, published):
    assert check_activity_lines(saved_html, published, case) == []
