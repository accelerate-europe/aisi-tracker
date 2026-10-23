"""The static per-record API matches the full dataset it is generated from (built from the real data)."""

import hashlib
import json
import os
import sys

import pytest

from scripts import build


@pytest.fixture(scope="module")
def site(tmp_path_factory):
    out = tmp_path_factory.mktemp("api") / "site"
    argv = sys.argv
    sys.argv = ["build.py", "--out", str(out)]
    try:
        build.main()
    finally:
        sys.argv = argv
    return out


def load(path):
    return json.loads(path.read_text(encoding="utf-8"))


def named_sources(node):
    """Hand-written walk: strings in any *source_ids list or anywhere under a citations object."""
    if isinstance(node, dict):
        for key, value in node.items():
            if key.endswith("source_ids") or key == "citations":
                yield from strings(value)
            else:
                yield from named_sources(value)
    elif isinstance(node, list):
        for value in node:
            yield from named_sources(value)


def strings(node):
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for value in node.values():
            yield from strings(value)
    elif isinstance(node, list):
        for value in node:
            yield from strings(value)


def resolve(document, pointer):
    for part in pointer.split("/")[1:]:
        document = document[int(part)] if isinstance(document, list) else document[part.replace("~1", "/").replace("~0", "~")]
    return document


def file_parts(full, kind, index):
    """(aisi.json prefix, file prefix, record) for every record a file holds, worked out by hand."""
    record = full[kind][index]
    parts = [(f"/{kind}/{index}", "/record", record)]
    if kind == "jurisdictions" and record.get("institute_id"):
        (n,) = [n for n, i in enumerate(full["institutes"]) if i["id"] == record["institute_id"]]
        parts.append((f"/institutes/{n}", "/institute/record", full["institutes"][n]))
    return parts


def test_api_files_match_aisi_json(site):
    full = load(site / "data/aisi.json")
    root = site / f"api/v{full['dataset']['version'].split('.')[0]}"
    sources = {s["id"]: s for s in full["sources"]}
    compact = load(root / "jurisdictions.json")["jurisdictions"]
    assert compact == [{"id": j["id"], "name": j["name"], "status": j["status"], "status_since": j.get("status_since"),
                        "institute": j.get("institute_id")} for j in full["jurisdictions"]]
    for kind in ("jurisdictions", "institutes"):
        assert sorted(p.stem for p in (root / kind).iterdir()) == sorted(r["id"] for r in full[kind])
        for index, record in enumerate(full[kind]):
            served = load(root / kind / f"{record['id']}.json")
            parts = file_parts(full, kind, index)
            assert served["aisi_json_pointer"] == parts[0][0] and served["record"] == record
            assert served["dataset"]["version"] == full["dataset"]["version"]
            expected_excerpts = {}
            for source in full["sources"]:
                for excerpt in source.get("excerpts", []):
                    inner = [target + p.removeprefix(prefix) for p in excerpt["supports"]
                             for prefix, target, _ in parts if p == prefix or p.startswith(prefix + "/")]
                    if inner:
                        expected_excerpts.setdefault(source["id"], []).append({**excerpt, "supports": inner})
            named = set().union(*(set(named_sources(r)) for _, _, r in parts))
            expected_ids = (named & set(sources)) | set(expected_excerpts)
            assert [s["id"] for s in served["sources"]] == sorted(expected_ids)
            for entry in served["sources"]:
                original = sources[entry["id"]]
                assert set(entry) <= {"id", "title", "title_en", "url", "archive_url", "excerpts"}
                for key in ("title", "title_en", "url", "archive_url"):
                    assert entry.get(key) == original.get(key)
                assert [dict(sorted(e.items())) for e in entry["excerpts"]] == [dict(sorted(e.items())) for e in expected_excerpts.get(entry["id"], [])]


def test_state_file_embeds_its_institute_and_every_pointer_resolves_inside_it(site):
    full = load(site / "data/aisi.json")
    root = site / f"api/v{full['dataset']['version'].split('.')[0]}"
    embedded = 0
    for index, record in enumerate(full["jurisdictions"]):
        served = load(root / "jurisdictions" / f"{record['id']}.json")
        assert list(served) == ["dataset", "aisi_json_pointer", "record", "institute", "sources"]
        if not record.get("institute_id"):
            assert served["institute"] is None
        else:
            embedded += 1
            institute = served["institute"]
            pointer = institute["aisi_json_pointer"]
            assert resolve(full, pointer) == institute["record"] == load(root / "institutes" / f"{record['institute_id']}.json")["record"]
            assert (institute["record"]["id"], institute["record"]["jurisdiction_id"]) == (record["institute_id"], record["id"])
        for _, target, _ in file_parts(full, "jurisdictions", index):
            assert resolve(served, target) is not None
        for entry in served["sources"]:
            for excerpt in entry["excerpts"]:
                for pointer in excerpt["supports"]:
                    (prefix, target) = next((p, t) for p, t, _ in sorted(file_parts(full, "jurisdictions", index), key=lambda x: -len(x[1])) if pointer == t or pointer.startswith(t + "/"))
                    assert resolve(served, pointer) == resolve(full, prefix + pointer.removeprefix(target)), (record["id"], pointer)
    assert embedded == len(full["institutes"])


def test_state_file_keeps_flag_only_sources_and_no_private_copies(site):
    from scripts.display import DOWNLOAD_ONLY_FIELDS

    full = load(site / "data/aisi.json")
    root = site / f"api/v{full['dataset']['version'].split('.')[0]}"
    # Local paths, the snapshot setting and, when configured, the private store's own path.
    private_markers = ["/Users/", "file://", "AISI_SNAPSHOTS"]
    if os.environ.get("AISI_SNAPSHOTS"):
        private_markers.append(os.path.expanduser(os.environ["AISI_SNAPSHOTS"]).rstrip("/"))
    checked = 0
    for institute in full["institutes"]:
        flag_only = set().union(*(set(named_sources(institute.get(f, {}))) for f in DOWNLOAD_ONLY_FIELDS)) - set(institute["display"]["source_links"])
        flag_only &= {s["id"] for s in full["sources"]}
        served = load(root / "jurisdictions" / f"{institute['jurisdiction_id']}.json")
        assert flag_only <= {s["id"] for s in served["sources"]}, institute["id"]
        checked += len(flag_only)
        text = json.dumps(served, ensure_ascii=False)
        assert '"snapshot"' not in text and "sha256" not in text.replace("sha256:", "")
        assert not any(marker in text for marker in private_markers), institute["id"]
    assert checked, "no flag-only source found to check"


def test_api_major_follows_the_dataset_version(site):
    from scripts.agent_files import api_files

    full = load(site / "data/aisi.json")
    full["dataset"]["version"] = "7.2.0"
    paths = list(api_files(full))
    assert paths and all(path.startswith("api/v7/") for path in paths)
    assert "api/v7/jurisdictions/GB.json" in paths


def test_api_record_carries_generated_intervals(site):
    full = load(site / "data/aisi.json")
    served = load(site / f"api/v{full['dataset']['version'].split('.')[0]}/institutes/gb-aisi.json")
    assert served["record"]["intervals"]["announced_to_established"] == {"days_min": 7, "days_max": 7}

def test_api_index_lists_every_file_pattern_and_record(site):
    full = load(site / "data/aisi.json")
    major = full["dataset"]["version"].split(".")[0]
    index = load(site / f"api/v{major}/index.json")
    assert index["api_version"] == major
    assert index["institute_ids"] == [i["id"] for i in full["institutes"]]
    here = site / f"api/v{major}"
    for entry in index["files"]:
        for path in [entry["path"]] if "{id}" not in entry["path"] else [
                entry["path"].replace("{id}", r["id"]) for r in full[entry["path"].split("/")[0]]]:
            assert (here / path).is_file(), path
    for path in index["full_dataset"].values():
        assert (here / path).resolve().is_file(), path
    assert index["status_definitions"] == full["dataset"]["status_definitions"]


def test_api_landing_page_links_resolve(site):
    from bs4 import BeautifulSoup

    major = load(site / "data/aisi.json")["dataset"]["version"].split(".")[0]
    here = site / f"api/v{major}"
    page = BeautifulSoup((here / "index.html").read_text(encoding="utf-8"), "html.parser")
    hrefs = [a["href"] for a in page.select("a")]
    assert {"index.json", "jurisdictions.json", "../../data/aisi.json", "../../llms.txt"} <= set(hrefs)
    for href in hrefs:
        target = (here / href).resolve()
        assert (target / "index.html").is_file() if href.endswith("/") else target.is_file(), href
    # Every file item has one shape: the path in code (linked for a file), for a URL pattern
    # ", for example" and a linked example in code, then ": " and a description ending in a full stop.
    items = page.select("main ul > li")
    assert items
    for li in items:
        codes = li.find_all("code", recursive=True)
        text = " ".join(li.get_text().split())
        path = codes[0].get_text()
        assert text.startswith(path), text
        rest = text[len(path):]
        if "{id}" in path:
            example = codes[1].get_text()
            assert codes[1].parent.name == "a" and codes[1].parent["href"] == example, text
            assert rest.startswith(f", for example {example}: "), text
        else:
            assert codes[0].parent.name == "a" and codes[0].parent["href"] == path and len(codes) == 1, text
            assert rest.startswith(": "), text
        assert text.endswith(".") and text.count(": ") >= 1, text


def test_orientation_is_one_text_in_four_places(site):
    from bs4 import BeautifulSoup

    major = load(site / "data/aisi.json")["dataset"]["version"].split(".")[0]
    sections = [(s["heading"], s["text"]) for s in load(site / f"api/v{major}/index.json")["orientation"]]
    assert len(sections) >= 5
    block = BeautifulSoup((site / "index.html").read_text(encoding="utf-8"), "html.parser").select_one("p#using-data")
    assert [a["href"] for a in block.select("a")] == ["llms.txt", f"api/v{major}/index.json"]
    assert " ".join(block.get_text().split()) == "Scripts and AI agents: llms.txt and the API index"
    readme = (build.ROOT / "README.md").read_text(encoding="utf-8")
    agents = readme.split("<!-- agents:start -->")[1].split("<!-- agents:end -->")[0]
    llms = (site / "llms.txt").read_text(encoding="utf-8")
    for heading, text in sections:
        assert f"### {heading}\n\n{text}\n" in agents
        assert text in llms


def test_gate_accepts_the_built_agent_files(site):
    from scripts import check_page

    assert check_page.check_agent_files(site) == []


@pytest.mark.parametrize(("url", "message"), [
    ("https://public-url-not-decided.invalid/", "still the placeholder"),
    ("http://example.org/", "https address ending in /"),
    ("https://example.org/aisi", "https address ending in /"),
    ("https://example.org/aisi/", None),
])
def test_public_url_must_be_a_real_https_address(url, message):
    from scripts import agent_files

    errors = agent_files.public_url_errors(url)
    assert (message is None and errors == []) or (len(errors) == 1 and message in errors[0])


@pytest.mark.parametrize("page", ["<!-- build:head -->", "<!-- build:head --><!-- build:head --><!-- build:using-data -->"])
def test_page_needs_each_build_marker_once(site, page):
    with pytest.raises(ValueError, match="needs exactly one"):
        build.generated_page(page, load(site / "data/aisi.json"), [])


def test_social_metadata_urls_and_shipped_image(site):
    from bs4 import BeautifulSoup
    from PIL import Image
    from scripts import agent_files

    page = BeautifulSoup((site / "index.html").read_text(), "html.parser")
    assert [node["content"] for node in page.select('meta[name="twitter:card"]')] == [
        "summary_large_image"
    ]
    assert [node["href"] for node in page.select('link[rel="canonical"]')] == [
        agent_files.PUBLIC_URL
    ]
    assert [node["content"] for node in page.select('meta[property="og:url"]')] == [
        agent_files.PUBLIC_URL
    ]
    assert [node["content"] for node in page.select('meta[property="og:image"]')] == [
        agent_files.PUBLIC_URL + "social-preview.png"
    ]
    with Image.open(site / "social-preview.png") as image:
        assert image.format == "PNG"
        assert image.size == (1200, 630)
        assert image.info["Title"] == page.title.get_text()
    assert (
        agent_files.PUBLIC_URL + "social-preview.png"
        in (site / "sitemap.xml").read_text()
    )


def test_head_urls_follow_public_url(dataset, monkeypatch):
    from bs4 import BeautifulSoup
    from scripts import agent_files

    monkeypatch.setattr(agent_files, "PUBLIC_URL", "https://example.org/tracker/")
    head = BeautifulSoup(agent_files.head_links(dataset, "Example <title>"), "html.parser")
    credit = ", ".join(m["name"] for m in dataset["dataset"]["maintainers"])
    assert head.select_one('meta[property="og:image:alt"]')["content"] == f"Example <title>. {dataset['dataset']['publisher']}, compiled by {credit}."
    assert (
        head.select_one('link[rel="canonical"]')["href"]
        == "https://example.org/tracker/"
    )
    assert (
        head.select_one('meta[property="og:url"]')["content"]
        == "https://example.org/tracker/"
    )
    assert (
        head.select_one('meta[property="og:image"]')["content"]
        == "https://example.org/tracker/social-preview.png"
    )


def test_cff_repository_code_uses_dataset_metadata(dataset):
    import yaml
    from scripts import agent_files

    dataset["dataset"]["repository"] = "https://github.com/example/tracker"
    cff = yaml.safe_load(agent_files.citation_cff(dataset))
    assert cff["repository-code"] == "https://github.com/example/tracker"
    assert cff["type"] == "dataset"
    assert cff["license"] == "CC-BY-4.0"  # a single SPDX string: Zenodo rejects a list
    assert "license-url" not in cff
    assert "CC BY 4.0 (LICENSE-DATA). Code: MIT (LICENSE)." in cff["message"]
    assert cff["date-released"] == dataset["changelog"][-1]["date"] == dataset["dataset"]["as_of"]
    assert dataset["dataset"]["license_note"] in cff["message"]
    del dataset["dataset"]["repository"]
    assert "repository-code" not in yaml.safe_load(agent_files.citation_cff(dataset))


def test_licence_notice_in_generated_files(dataset, build_env, monkeypatch):
    import yaml
    from conftest import write_json

    notice = (
        "Quotations from sources remain with their rights holders and are reproduced "
        "as quotations; the licence covers our own compilation, text and data."
    )
    write_json(build.DATA, dataset)
    monkeypatch.setattr(sys, "argv", ["build.py"])
    build.main()
    site = build.DIST
    assert load(site / "data/aisi.json")["dataset"]["license_note"] == notice
    assert load(site / "data/datapackage.json")["license_note"] == notice
    assert notice in (site / "llms.txt").read_text()
    assert notice in yaml.safe_load((build.ROOT / "CITATION.cff").read_text())["message"]
    for path in (site / "api").rglob("*.json"):
        assert load(path)["dataset"]["license_note"] == notice, path


@pytest.mark.parametrize(
    ("name", "old", "new", "message"),
    [
        (
            "index.html",
            'href="favicon.svg"',
            'href="/favicon.svg"',
            "index.html: expected relative SVG favicon",
        ),
        (
            "404.html",
            'href="/favicon.svg"',
            'href="favicon.svg"',
            "404.html: expected root-relative SVG favicon",
        ),
        (
            "method.html",
            'href="favicon.svg"',
            'href="/favicon.svg"',
            "method.html: expected relative SVG favicon",
        ),
        (
            "404.html",
            'href="/"',
            'href="index.html"',
            "404.html: missing root recovery link",
        ),
        (
            "404.html",
            'content="noindex"',
            'content="index"',
            "404.html: missing noindex",
        ),
        (
            "404.html",
            "<h1>Page not found</h1>",
            "<h1>Welcome</h1>",
            "404.html: missing Page not found heading",
        ),
        (
            "favicon.svg",
            'viewBox="0 0 32 32"',
            'viewBox="0 0 0 0"',
            "favicon.svg: expected a 32-unit SVG icon",
        ),
        (
            "LICENSE-DATA",
            "Creative Commons Attribution 4.0 International Public License",
            "MIT License",
            "LICENSE-DATA: missing CC BY 4.0 legal code",
        ),
        (
            "LICENSE",
            "Permission is hereby granted",
            "All rights reserved",
            "LICENSE: missing MIT licence text",
        ),
    ],
)
def test_launch_asset_mutations(site, tmp_path, name, old, new, message):
    from scripts import check_page

    for file in ("index.html", "404.html", "method.html", "favicon.svg", "LICENSE", "LICENSE-DATA"):
        (tmp_path / file).write_bytes((site / file).read_bytes())
    assert check_page.check_launch_assets(tmp_path) == []
    path = tmp_path / name
    assert old in path.read_text(encoding="utf-8")
    path.write_text(
        path.read_text(encoding="utf-8").replace(old, new), encoding="utf-8"
    )
    assert message in check_page.check_launch_assets(tmp_path)


@pytest.mark.parametrize("missing", ["404.html", "method.html", "favicon.svg", "LICENSE", "LICENSE-DATA"])
def test_launch_assets_missing_file(site, tmp_path, missing):
    from scripts import check_page

    for file in ("index.html", "404.html", "method.html", "favicon.svg", "LICENSE", "LICENSE-DATA"):
        if file != missing:
            (tmp_path / file).write_bytes((site / file).read_bytes())
    assert any(missing in error for error in check_page.check_launch_assets(tmp_path))


def test_legal_code_packaged_verbatim_and_error_page_not_indexed(site, dataset):
    from scripts import agent_files

    for name in ("LICENSE", "LICENSE-DATA"):
        assert (site / name).read_bytes() == (build.ROOT / name).read_bytes(), name
    sitemap = agent_files.sitemap_xml(
        dataset, ["", "404.html", "LICENSE-DATA", "favicon.svg"]
    )
    assert "404.html" not in sitemap
    assert "LICENSE-DATA" in sitemap and "favicon.svg" in sitemap


@pytest.mark.parametrize(
    "dependency",
    ['<script src="/extra.js"></script>', '<link rel="stylesheet" href="/extra.css">'],
)
def test_error_page_rejects_external_dependencies(site, tmp_path, dependency):
    from scripts import check_page

    for file in ("index.html", "404.html", "method.html", "favicon.svg", "LICENSE", "LICENSE-DATA"):
        (tmp_path / file).write_bytes((site / file).read_bytes())
    assert check_page.check_launch_assets(tmp_path) == []
    path = tmp_path / "404.html"
    path.write_text(
        path.read_text(encoding="utf-8").replace("</head>", dependency + "</head>"),
        encoding="utf-8",
    )
    assert (
        "404.html: recovery page must be self-contained"
        in check_page.check_launch_assets(tmp_path)
    )


def test_legal_code_matches_official_download():
    # Retrieved from https://creativecommons.org/licenses/by/4.0/legalcode.txt.
    expected = "9ba9550ad48438d0836ddab3da480b3b69ffa0aac7b7878b5a0039e7ab429411"
    assert (
        hashlib.sha256((build.ROOT / "LICENSE-DATA").read_bytes()).hexdigest()
        == expected
    )


def test_orientation_rule_needs_its_lead_in(dataset):
    from scripts import agent_files

    rule = next(r for r in dataset["dataset"]["method"] if r["id"] == "missing-values")
    rule["text"] = "Missing values. An empty value means: not known."
    with pytest.raises(ValueError, match="missing-values must open with its name"):
        agent_files.method_rule(dataset["dataset"], "missing-values")
