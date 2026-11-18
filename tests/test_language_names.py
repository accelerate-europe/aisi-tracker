"""Languages print by their plain name, never a variant or script; exact codes stay in the data and downloads.

The page (QUOTE_LANGUAGES in site/index.html) and the summary generator (scripts/summaries.py LANGUAGE_NAMES) hold
the same names, and every language a translated quote or anchor comes from has one."""

import json
import re
from pathlib import Path

from scripts.summaries import LANGUAGE_NAMES

ROOT = Path(__file__).resolve().parents[1]
VARIANTS = ("Bokmål", "Nynorsk", "Simplified", "Traditional", "Latin", "Cyrillic")


def page_names() -> dict[str, str]:
    html = (ROOT / "site" / "index.html").read_text(encoding="utf-8")
    body = re.search(r"var QUOTE_LANGUAGES = \{(.*?)\};", html).group(1)
    return dict(re.findall(r'"?([a-z]+)"?: "([^"]+)"', body))


def test_page_and_generator_share_names():
    assert page_names() == LANGUAGE_NAMES


def test_names_are_plain():
    names = set(LANGUAGE_NAMES.values())
    for code, name in LANGUAGE_NAMES.items():
        assert "(" not in name and not any(v in name for v in VARIANTS), f"{code}: {name} names a variant"
        assert not any(name.startswith(other + " ") for other in names if other != name), f"{code}: {name} is a variant of another name"
    assert {LANGUAGE_NAMES[c] for c in ("no", "nb", "nn")} == {"Norwegian"}


def test_every_translated_quote_language_is_named():
    data = json.loads((ROOT / "data" / "aisi.json").read_text(encoding="utf-8"))
    sources = {s["id"]: s for s in data["sources"]}
    missing = set()
    for s in data["sources"]:
        for e in s.get("excerpts", []):
            if e.get("gloss"):
                missing.add(s.get("language"))
            for a in e.get("anchors", []):
                if a.get("gloss"):
                    missing.add(sources.get(a.get("source_id"), s).get("language"))
    assert not missing - LANGUAGE_NAMES.keys(), sorted(map(str, missing - LANGUAGE_NAMES.keys()))
