"""Load-bearing words in the editorial rules survive edits (process review, 24 Oct 2026: readability rewrites
softened 'never' and 'only' in about half of the rewritten rules)."""

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "protected_words.json"


def word_counts(text: str, words: list[str]) -> dict[str, int]:
    return {w: len(re.findall(rf"\b{re.escape(w)}\b", text, flags=re.IGNORECASE)) for w in words}


def lost_words(method: list[dict], fixture: dict) -> list[str]:
    rules = {r["id"]: r["text"] for r in method}
    lost = []
    for rule_id, pinned in sorted(fixture["rules"].items()):
        if rule_id not in rules:
            lost.append(f"{rule_id}: rule removed; remove its entry in {FIXTURE.name} with the owner's approval")
            continue
        now = word_counts(rules[rule_id], fixture["words"])
        lost += [f"{rule_id}: '{w}' {n} -> {now[w]}" for w, n in sorted(pinned.items()) if now[w] < n]
    return lost


def test_rules_keep_their_protected_words():
    method = json.loads((ROOT / "data" / "aisi.json").read_text(encoding="utf-8"))["dataset"]["method"]
    assert lost_words(method, json.loads(FIXTURE.read_text(encoding="utf-8"))) == []


def test_a_softened_prohibition_is_caught():
    fixture = {"words": ["never", "only"], "rules": {"x": {"never": 1, "only": 1}}}
    assert lost_words([{"id": "x", "text": "Never infer. Use only codes."}], fixture) == []
    assert lost_words([{"id": "x", "text": "Do not infer. Use only codes."}], fixture) == ["x: 'never' 1 -> 0"]
    assert lost_words([{"id": "x", "text": "Never infer. Use the codes; whenever unsure, ask."}], fixture) == ["x: 'only' 1 -> 0"]
    assert lost_words([], fixture)[0].startswith("x: rule removed")
