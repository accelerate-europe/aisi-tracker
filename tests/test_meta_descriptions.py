"""Non-empty titles and data-derived coverage in descriptions."""

import re
from pathlib import Path

from bs4 import BeautifulSoup

from scripts.check_page import check_meta_descriptions

ROOT = Path(__file__).resolve().parents[1]
TITLE = "A reworded tracker title"
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
        "meta[name=\"twitter:description\"] must appear once and say 'Covers all 193 UN member states, Taiwan and the EU, with sources and open data'"
    ]
    retitled = re.sub(r"<title>.*?</title>", f"<title>{TITLE}</title>", html)
    assert check_meta_descriptions(BeautifulSoup(retitled, "html.parser"), DATA) == []
    for title in ("", "<title> \n </title>"):
        broken = re.sub(r"<title>.*?</title>", title, html)
        assert check_meta_descriptions(BeautifulSoup(broken, "html.parser"), DATA) == ["page title is missing or empty"]


def test_share_tags_and_page_size_budget(tmp_path):
    from scripts import agent_files, check_page

    address = agent_files.PUBLIC_URL
    tags = agent_files.share_tags(TITLE, "", "Alt text")
    soup = BeautifulSoup("<head>" + tags + '<meta property="og:description" content="d"><meta name="twitter:description" content="d"></head>', "html.parser")
    assert check_page.check_share_tags(soup, "index.html", TITLE, address) == []
    soup.select_one('meta[property="og:site_name"]')["content"] = "A reworded site name"
    assert check_page.check_share_tags(soup, "index.html", TITLE, address) == []
    for selector in ('meta[property="og:site_name"]', 'meta[property="og:title"]', 'meta[name="twitter:title"]'):
        for content in (None, " \n "):
            broken = BeautifulSoup(str(soup), "html.parser")
            node = broken.select_one(selector)
            if content is None:
                node.attrs.pop("content")
            else:
                node["content"] = content
            assert any("must appear once and not be empty" in error for error in check_page.blocking(check_page.check_share_tags(broken, "index.html", TITLE, address)))
    for selector in ('meta[name="twitter:title"]', 'meta[name="twitter:image"]', 'meta[property="og:site_name"]'):
        broken = BeautifulSoup(str(soup), "html.parser")
        broken.select_one(selector).decompose()
        assert check_page.blocking(check_page.check_share_tags(broken, "index.html", TITLE, address))
    budget = check_page.PAGE_BYTES_BUDGET
    opening, closing = b'<section><div><h2 id="world">', b"</div><section>nested</section></section>"

    def serve(body_bytes: int, after_bytes: int = 0, world: bool = True) -> list[str]:
        head = opening if world else b'<div><h2 id="world">'
        (tmp_path / "index.html").write_bytes(b"<p>x</p>" + head + b"w" * body_bytes + closing + b"<section>" + b"a" * after_bytes + b'<h2 id="data"></section>')
        return check_page.check_page_size(tmp_path)

    fixed = len(b"<p>x</p>" + opening + closing)
    assert serve(budget - fixed) == []  # the world section ends exactly at the budget
    assert len(serve(budget - fixed + 1)) == 1  # one byte beyond
    assert len(serve(budget * 2)) == 1  # an oversized world body
    assert serve(100, after_bytes=budget * 2) == []  # a large section after it only warns
    (error,) = serve(100, world=False)  # the heading keeps its id but its section is gone
    assert "cannot find where the world section" in error
    for broken in (b'<section><h2 id="world">never closes',
                   b'<section>intro</section><h2 id="world">outside any section</h2><section>x</section>',  # heading outside a section after a closed one
                   "<section>é</section>".encode()):  # no heading at all
        (tmp_path / "index.html").write_bytes(broken)
        assert "cannot find" in check_page.check_page_size(tmp_path)[0]
    nested = b"\x85\x0c\n" + b'<section><section>intro</section><h2 id="world">World</h2>' + b"w" * 50 + b"</section><section>after</section>"
    (tmp_path / "index.html").write_bytes("é\n".encode() + nested)  # multibyte text and a newline before it
    assert check_page.world_section_end("é\n".encode() + nested) == len("é\n".encode()) + nested.index(b"</section><section>after") + len(b"</section>")

