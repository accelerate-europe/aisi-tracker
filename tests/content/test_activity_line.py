"""Every saved card's activity qualifier, including each case that must omit it."""

import pytest
from bs4 import BeautifulSoup

RULE_CASES = ("closest with activity", "closest without activity", "institute", "other or legacy")


def activity_expectation(j, institute, vocabulary):
    """Independent expectations from published claims, never page-generated text."""
    if institute:
        return "institute", "", set()
    if "claims" not in j:
        return "other or legacy", "", set()
    if j["status"] == "none_identified" and j.get("closest_body"):
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
        target = card.select_one(".body-group.closest li")
        if line.parent is not target or target.get("data-body-id") != j["closest_body"]["body_id"]:
            errors.append(f"{j['id']}: activity must belong to the closest body's item")
        previous = line.find_previous_sibling()
        if j["closest_body"]["name"] in j.get("summary", ""):  # summary pattern 2 gives name and stage, so the item opens with the activity
            if previous is not None:
                errors.append(f"{j['id']}: activity must open the closest body's item when the summary names the body")
            continue
        stage ={"planned": "Planned.", "proposed": "Proposed.", "under_study": "Under study."}.get(j["closest_body"]["stage"])
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
