"""Owner decision D1: European title, worldwide coverage in the search and social descriptions."""

import re
from pathlib import Path

from bs4 import BeautifulSoup

from scripts.check_page import check_meta_descriptions

ROOT = Path(__file__).resolve().parents[1]
DATA = {"jurisdictions": [{"kind": "state"}] * 193 + [{"kind": "territory"}, {"kind": "supranational"}]}


def head():
    source = (ROOT / "site/index.html").read_text(encoding="utf-8")
    return source[: source.index("</head>")] + "</head>"


def test_page_head_passes_and_each_mutation_fails():
    html = head()
    assert check_meta_descriptions(BeautifulSoup(html, "html.parser"), DATA) == []
    data = {"jurisdictions": DATA["jurisdictions"][1:]}
    assert len(check_meta_descriptions(BeautifulSoup(html, "html.parser"), data)) == 3
    no_twitter = re.sub(r'<meta name="twitter:description"[^>]*>', "", html)
    assert check_meta_descriptions(BeautifulSoup(no_twitter, "html.parser"), DATA) == [
        "meta[name=\"twitter:description\"] must appear once and say 'all 193 UN member states plus Taiwan and the EU AI Office, with Europe in detail'"
    ]
    retitled = html.replace("<title>AI Security Institutes</title>", "<title>AI Security Institutes in Europe</title>")
    assert check_meta_descriptions(BeautifulSoup(retitled, "html.parser"), DATA) == ["page title is not 'AI Security Institutes'"]
