"""Branch previews, using small built fixtures and in-memory lease races only."""

import ast
import copy
import datetime as dt
import re
import shutil
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit
from xml.etree import ElementTree as ET

import pytest
from bs4 import BeautifulSoup
from jsonschema import Draft202012Validator

from scripts import build, preview

SHA = "a" * 40
OTHER_SHA = "b" * 40
BUILT = "2026-11-16T13:05:00Z"
RUN = preview.SOURCE_REPO + "/actions/runs/100"
CITATION = "Example tracker, version 1.2.3. https://doi.org/10.5281/zenodo.12345"
ROOT = Path(__file__).resolve().parents[1]


def snapshot(root):
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()}


@pytest.fixture
def dist(tmp_path):
    root = tmp_path / "dist"
    (root / "data").mkdir(parents=True)
    (root / "api/v1/jurisdictions").mkdir(parents=True)
    (root / "api/v1/institutes").mkdir()
    dataset = {"version": "1.2.3", "as_of": "2026-11-16", "description": "Example dataset", "citation": CITATION,
               "doi": "10.5281/zenodo.12345", "contact": "feedback@example.org"}
    source = {"dataset": dataset, "jurisdictions": [], "sources": [], "changelog": [{"version": "1.2.2"}]}
    schema = {"type": "object", "properties": {"dataset": {"type": "object", "additionalProperties": False,
              "required": ["version", "citation", "description"],
              "properties": {key: {"type": "string"} for key in dataset}}}}
    schema["properties"]["dataset"]["properties"]["version"]["pattern"] = r"^\d+\.\d+\.\d+$"
    schema["description"] = "A DOI link starts with https://doi.org/; Zenodo uses 10.5281/."
    for path, value in {"data/aisi.json": source, "data/aisi.schema.json": schema,
                        "data/page.json": {**source, "build_mark": "old-mark"},
                        "data/datapackage.json": {"version": "1.2.3", "id": "https://doi.org/10.5281/zenodo.12345",
                                                  "resources": [{"path": "aisi.json"}, {"path": "tiny.csv"}]},
                        "api/v1/index.json": {"dataset": dataset, "orientation": [{"heading": "Cite as", "text": CITATION}]},
                        "api/v1/jurisdictions.json": {"dataset": dataset},
                        "api/v1/jurisdictions/FR.json": {"dataset": dataset, "record": {"id": "FR"}},
                        "api/v1/institutes/fr.json": {"dataset": dataset, "record": {"id": "fr"}}}.items():
        preview.write_json(root / path, value)
    page = ('<!doctype html><html data-prerendered><head>'
            f'<link rel="canonical" href="{preview.LIVE_URL}">'
            f'<meta property="og:url" content="{preview.LIVE_URL}">'
            f'<meta property="og:image" content="{preview.LIVE_URL}social-preview.png">'
            f'<meta name="twitter:image" content="{preview.LIVE_URL}social-preview.png">'
            '<meta name="aisi-page-data" content="data/page.json?v=old-mark" data-package="data/datapackage.json?v=old" data-build="old-mark">'
            f'<script type="application/ld+json">{{"url": "{preview.LIVE_URL}", "identifier": "https://doi.org/10.5281/zenodo.12345"}}</script>'
            '</head><body><p id="cite">Old citation <a href="https://doi.org/10.5281/zenodo.12345">DOI</a></p>'
            '<template id="page-template"><p id="cite"></p></template>'
            '<script>var doiUrl = m.doi ? "https://doi.org/" + m.doi : "";</script></body></html>')
    for path in ("index.html", "method.html", "api/v1/index.html"):
        (root / path).write_text(page)
    (root / "404.html").write_text('<html><head><link rel="icon" href="/favicon.svg"></head><body><a href="/">Home</a></body></html>')
    (root / "favicon.svg").write_text('<svg xmlns="http://www.w3.org/2000/svg"/>')
    (root / "data/tiny.csv").write_text("id,version\nexample,2\n")
    (root / "llms.txt").write_text(f"# Tracker\n## Cite as\n{CITATION}\n[Page]({preview.LIVE_URL})\n[Data]({preview.LIVE_URL}data/aisi.json)\n[API]({preview.LIVE_URL}api/v1/index.json)\n")
    (root / "feed.xml").write_text(f'''<feed xmlns="http://www.w3.org/2005/Atom"><id>tag:aisi.accelerate-europe.org,2026:feed</id><title>Tracker</title><link href="{preview.LIVE_URL}feed.xml"/><entry><id>tag:aisi.accelerate-europe.org,2026:release/1.2.3</id><link href="{preview.LIVE_URL}#changes"/></entry></feed>''')
    (root / "sitemap.xml").write_text(preview.LIVE_URL)
    (root / "robots.txt").write_text("Allow: /\nSitemap: " + preview.LIVE_URL + "sitemap.xml")
    return root


def transform(dist, out, **kwargs):
    return preview.transform(dist, out, **({"branch": "next", "sha": SHA, "built": BUILT, "run_url": RUN} | kwargs))


def test_transform_untouched_input_and_one_way_build_dependency(dist, tmp_path):
    before = snapshot(dist)
    target = tmp_path / "marked"
    transform(dist, target)
    assert snapshot(dist) == before
    imports = ast.parse((ROOT / "scripts/build.py").read_text())
    for node in ast.walk(imports):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            assert "preview" not in ast.unparse(node)
    assert not preview.oracle(target, "10.5281/zenodo.12345")
    assert not (target / "sitemap.xml").exists()
    assert (target / "robots.txt").read_text() == preview.ROBOTS
    assert (target / "data/tiny.csv").read_bytes() == (dist / "data/tiny.csv").read_bytes()


@pytest.mark.parametrize("extra_allowed", [True, False])
def test_contract_semver_notice_schema_and_cache_marks(dist, tmp_path, extra_allowed):
    schema = preview.read_json(dist / "data/aisi.schema.json")
    schema["properties"]["dataset"]["additionalProperties"] = extra_allowed
    preview.write_json(dist / "data/aisi.schema.json", schema)
    out = tmp_path / "marked"
    transform(dist, out)
    source = preview.read_json(out / "data/aisi.json")
    Draft202012Validator(schema).validate(source)
    for path in ("data/aisi.json", "data/page.json", "api/v1/index.json", "api/v1/jurisdictions.json", "api/v1/jurisdictions/FR.json", "api/v1/institutes/fr.json"):
        value = preview.read_json(out / path)
        assert value["dataset"]["version"] == "1.2.3"
        assert "doi" not in value["dataset"]
        assert value["dataset"]["citation"] == preview.NOTICE
        if path.startswith("api/") or extra_allowed:
            assert value["dataset"]["preview"] == {"notice": preview.NOTICE, "branch": "next", "commit": SHA, "built": BUILT}
        else:
            assert "preview" not in value["dataset"]
            assert value["dataset"]["description"].startswith(preview.NOTICE)
    assert source["changelog"] == [{"version": "1.2.2"}]
    package = preview.read_json(out / "data/datapackage.json")
    assert package["version"] == "1.2.3" and "id" not in package
    assert package["preview"]["commit"] == SHA
    assert preview.read_json(out / "api/v1/index.json")["orientation"][0]["text"] == preview.NOTICE
    soup = BeautifulSoup((out / "index.html").read_text(), "html.parser")
    meta = soup.select_one('meta[name="aisi-page-data"]')
    mark = build.content_mark(build.page_data(source), "1.2.3")
    assert meta["data-build"] == mark == preview.read_json(out / "data/page.json")["build_mark"]
    assert meta["content"] == f"data/page.json?v={mark}"
    assert meta["data-package"] == build.versioned("data/datapackage.json", (out / "data/datapackage.json").read_text(), "1.2.3")
    assert (out / "data/page.json").read_text() == build.page_data(source, mark)


def test_banner_template_and_all_pages(dist, tmp_path):
    out = tmp_path / "marked"
    transform(dist, out, branch='work/<safe>&test', feedback_mail="override@example.org")
    for path in out.rglob("*.html"):
        soup = BeautifulSoup(path.read_text(), "html.parser")
        assert soup.body.find(recursive=False)["class"] == ["preview-banner"]
        assert soup.select_one('meta[name="robots"]')["content"] == "noindex, nofollow"
        assert not soup.select('link[rel="canonical"], meta[property="og:url"], script[type="application/ld+json"]')
        assert soup.select_one(".preview-banner [data-preview-path]")["data-preview-path"] == path.relative_to(out).as_posix()
        assert "16 Nov 2026, 14:05 Europe/Vienna" in soup.select_one(".preview-banner").get_text()
        assert soup.select_one(".preview-banner a[href^='mailto:']")["href"].startswith("mailto:override@example.org?")
        assert "work/<safe>&test" in soup.select_one(".preview-banner").get_text()
        assert soup.select_one("safe") is None
        for cite in soup.find_all(id="cite"):
            assert cite.string == preview.NOTICE
        for template in soup.select("#page-template"):
            assert template.find(recursive=False)["class"] == ["preview-banner"]
        assert "'contextmenu'" in str(soup.head) and "'hashchange'" in str(soup.head)
    assert 'href="/' not in (out / "404.html").read_text()


def test_absolute_urls_and_feed_ids_point_to_frozen_files(dist, tmp_path):
    out = tmp_path / "marked"
    transform(dist, out)
    prefix = preview.PREVIEW_URL + f"b/{SHA[:10]}/"
    text = (out / "llms.txt").read_text()
    links = re.findall(r"\((https://[^)]+)\)", text)
    feed = ET.parse(out / "feed.xml")
    ns = "{http://www.w3.org/2005/Atom}"
    links += [n.attrib["href"] for n in feed.iter(ns + "link")]
    links += [n.text for n in feed.iter(ns + "id")]
    assert preview.NOTICE in feed.find(ns + "title").text
    for url in links:
        assert url.startswith(prefix)
        path = out / urlsplit(url[len(prefix):]).path
        assert (path / "index.html").is_file() if path.is_dir() else path.is_file()
    assert preview.NOTICE in text and CITATION not in text


@pytest.mark.parametrize("branch,folder", [("next", "next"), ("CODE/Feature + 1", "code-feature---1"), ("code-1.1.0-preview", "code-1.1.0-preview")])
def test_folder_names(branch, folder):
    assert preview.folder_name(branch) == folder


@pytest.mark.parametrize("branch", ["b", "B", "index.html", "404.html", "manifest.json", "keep.txt", "robots.txt", ".nojekyll", ".git", ".", "..", "", "Next", "CNAME"])
def test_reserved_folders(branch):
    with pytest.raises(ValueError, match="reserved"):
        preview.folder_name(branch)


@pytest.mark.parametrize("logical,url_tail", [("index.html", ""), ("index.html", "index.html"), ("method.html", "method.html"), ("api/v1/index.html", "api/v1/"), ("api/v1/index.html", "api/v1/index.html")])
@pytest.mark.parametrize("placement", ["next", "b/aaaaaaaaaa"])
def test_frozen_links_directory_and_explicit_pages(logical, url_tail, placement):
    url = preview.PREVIEW_URL + placement + "/" + url_tail + "?old=1#c-fr"
    assert preview.frozen_url(url, logical, SHA) == preview.PREVIEW_URL + "b/aaaaaaaaaa/" + logical + "#c-fr"


@pytest.mark.parametrize("relative", [".", "child"])
def test_transform_refuses_input_or_child_output(dist, relative):
    with pytest.raises(ValueError, match="separate"):
        transform(dist, dist / relative)


def test_transform_does_not_publish_unknown_identifiers(dist, tmp_path):
    (dist / "unhandled.txt").write_text("https://zenodo.org/records/12345")
    with pytest.raises(ValueError, match="published identifier remains"):
        transform(dist, tmp_path / "marked")
    assert not (tmp_path / "marked").exists()


@pytest.fixture
def marked(tmp_path):
    def make(branch="next", sha=SHA, run=100, built=BUILT):
        root = tmp_path / f"marked-{branch.replace('/', '-')}-{run}-{sha[:10]}"
        root.mkdir()
        preview.write_json(root / "preview-build.json", {"branch": branch, "commit": sha, "run_url": preview.SOURCE_REPO + f"/actions/runs/{run}",
                                                        "built": built, "preview_url": preview.PREVIEW_URL, "as_of": "2026-11-16"})
        (root / "index.html").write_text(f"branch {branch} run {run}")
        return root
    return make


def publish(tree, marked=None, **kwargs):
    options = {"branch": "next", "sha": SHA, "run_id": 100, "run_attempt": 1, "run_url": RUN,
               "attempted": BUILT, "branches": {"next", "work"}}
    options.update(kwargs)
    options["run_url"] = preview.SOURCE_REPO + f"/actions/runs/{options['run_id']}"
    return preview.publish(tree, marked, **options)


def test_success_failure_and_stale_attempts(tmp_path, marked, capsys):
    tree = tmp_path / "tree"
    assert publish(tree, marked())
    original = snapshot(tree / "next")
    frozen = snapshot(tree / "b" / SHA[:10])
    assert publish(tree, run_id=102, sha=OTHER_SHA)
    manifest = preview.read_json(tree / "manifest.json")
    assert manifest["folders"]["next"]["attempt"]["status"] == "build failed"
    assert manifest["folders"]["next"]["current"]["commit"] == SHA
    assert snapshot(tree / "next") == original
    assert snapshot(tree / "b" / SHA[:10]) == frozen
    before = snapshot(tree)
    assert not publish(tree, marked(run=101), run_id=101)
    assert snapshot(tree) == before
    assert "stale" in capsys.readouterr().out
    assert publish(tree, marked(run=102), run_id=102, run_attempt=2)
    assert not publish(tree, run_id=102, run_attempt=1)
    assert preview.read_json(tree / "manifest.json")["folders"]["next"]["run_attempt"] == 2


def test_frozen_first_publish_identity_across_branches(tmp_path, marked):
    tree = tmp_path / "tree"
    publish(tree, marked())
    before = snapshot(tree / "b" / SHA[:10])
    publish(tree, marked("work", run=101), branch="work", run_id=101)
    assert snapshot(tree / "b" / SHA[:10]) == before
    frozen = preview.read_json(tree / "manifest.json")["frozen"][SHA[:10]]
    assert frozen["branch"] == "next" and frozen["run_id"] == 100
    assert (tree / "work/index.html").read_text() == "branch work run 101"


def test_collision_refused_without_tree_mutation(tmp_path, marked):
    tree = tmp_path / "tree"
    assert publish(tree, marked("work/x"), branch="work/x", branches={"work/x"})
    before = snapshot(tree)
    assert not publish(tree, branch="work-x", run_id=101, branches={"work-x"})
    assert snapshot(tree) == before
    other = tmp_path / "empty"
    assert not publish(other, branch="work/x", branches={"work/x", "work-x"})
    assert not other.exists()


def test_failed_first_attempt_has_no_live_or_frozen_folder(tmp_path):
    tree = tmp_path / "tree"
    publish(tree)
    assert not (tree / "next").exists() and not (tree / "b").exists()
    assert "build failed" in (tree / "index.html").read_text()


def retention_fixture():
    def frozen(char, built):
        return {"commit": char * 40, "built": built, "size": 10}
    return {"folders": {"next": {"branch": "next", "size": 10, "current": {"commit": "a" * 40}},
                         "gone": {"branch": "gone", "size": 10, "current": {"commit": "b" * 40}}},
            "frozen": {"a" * 10: frozen("a", "2026-10-01T00:00:00Z"),
                       "b" * 10: frozen("b", "2026-10-01T00:00:00Z"),
                       "c" * 10: frozen("c", "2026-10-01T00:00:00Z"),
                       "d" * 10: frozen("d", "2026-11-10T00:00:00Z"),
                       "e" * 10: frozen("e", "2026-11-11T00:00:00Z")}}


def test_retention_age_keep_live_deleted_and_budget():
    manifest = retention_fixture()
    before = copy.deepcopy(manifest)
    result, removed = preview.retention(manifest, {"c" * 10}, set(), preview.timestamp(BUILT), budget=40)
    assert manifest == before
    assert set(result["folders"]) == {"next"}
    assert set(result["frozen"]) == {"a" * 10, "c" * 10, "e" * 10}
    assert removed == {"gone", "b/" + "b" * 10, "b/" + "d" * 10}
    with pytest.raises(ValueError, match="protected builds"):
        preview.retention(manifest, {"c" * 10}, set(), preview.timestamp(BUILT), budget=29)


def test_age_boundary_and_size_tie_break():
    manifest = retention_fixture()
    manifest["folders"] = {}
    manifest["frozen"] = {sha: e for sha, e in manifest["frozen"].items() if sha.startswith(("b", "c"))}
    exact = preview.timestamp("2026-10-22T00:00:00Z")
    result, removed = preview.retention(manifest, set(), set(), exact, budget=20)
    assert len(result["frozen"]) == 2 and not removed
    result, removed = preview.retention(manifest, set(), set(), exact, budget=10)
    assert set(result["frozen"]) == {"c" * 10}
    result, removed = preview.retention(manifest, set(), set(), exact + dt.timedelta(seconds=1), budget=20)
    assert not result["frozen"]


def test_budget_failure_keeps_tree_and_keep_file_unchanged(tmp_path, marked):
    tree = tmp_path / "tree"
    publish(tree, marked())
    (tree / "keep.txt").write_text("# Keep this review\n" + SHA[:10] + "\n")
    before = snapshot(tree)
    with pytest.raises(ValueError, match="budget"):
        publish(tree, marked(run=101), run_id=101, budget=1)
    assert snapshot(tree) == before


def test_publish_prunes_directories_and_keeps_next(tmp_path, marked):
    tree = tmp_path / "tree"
    publish(tree, marked("work"), branch="work")
    publish(tree, marked(sha=OTHER_SHA, run=101), sha=OTHER_SHA, run_id=101, branches={"next"}, attempted="2026-12-30T00:00:00Z")
    assert not (tree / "work").exists()
    assert not (tree / "b" / SHA[:10]).exists()
    assert (tree / "next").is_dir()
    assert (tree / "b" / OTHER_SHA[:10]).is_dir()


@pytest.mark.parametrize("race", ["keep", "other-branch", "newer-run"])
def test_lease_retry_recomputes_fresh_tree(tmp_path, marked, race):
    remote = tmp_path / "remote"
    publish(remote, marked())
    pending = marked(run=101, sha=OTHER_SHA)
    calls = []

    @contextmanager
    def fetch():
        tree = tmp_path / f"clone-{len(calls)}"
        shutil.copytree(remote, tree)
        yield tree, len(calls)

    def apply(tree, transformed):
        return publish(tree, transformed, sha=OTHER_SHA, run_id=101, attempted="2026-12-30T00:00:00Z")

    def push(tree, tip):
        calls.append(tip)
        if len(calls) == 1:
            if race == "keep":
                (remote / "keep.txt").write_text(SHA[:10] + " # web edit\n")
            elif race == "other-branch":
                publish(remote, marked("work", run=102), branch="work", run_id=102)
            else:
                publish(remote, marked(run=102), run_id=102)
            return False
        for path in remote.iterdir():
            shutil.rmtree(path) if path.is_dir() else path.unlink()
        shutil.copytree(tree, remote, dirs_exist_ok=True)
        return True

    changed = preview.push(pending, fetch_tree=fetch, apply=apply, push_tree=push)
    manifest = preview.read_json(remote / "manifest.json")
    if race == "newer-run":
        assert not changed and calls == [0]
        assert manifest["folders"]["next"]["run_id"] == 102
    else:
        assert changed and calls == [0, 1]
        if race == "keep":
            assert (remote / "keep.txt").read_text() == SHA[:10] + " # web edit\n"
            assert SHA[:10] in manifest["frozen"]
        else:
            assert manifest["folders"]["work"]["run_id"] == 102


def test_retry_limit(tmp_path):
    calls = []

    @contextmanager
    def fetch():
        calls.append(len(calls))
        yield tmp_path, "tip"

    with pytest.raises(RuntimeError, match="three times"):
        preview.push(None, fetch_tree=fetch, apply=lambda *_: True, push_tree=lambda *_: False)
    assert len(calls) == 3


def test_keep_and_root_pages():
    assert preview.kept_commits("# comment\naaaaaaaaaa # keep\n\n") == {"a" * 10}
    with pytest.raises(ValueError, match="keep.txt"):
        preview.kept_commits("../../escape")
    for text in (preview.index_html({"folders": {}, "frozen": {}}, set()), preview.not_found_html()):
        soup = BeautifulSoup(text, "html.parser")
        assert soup.select_one('meta[name="robots"]')["content"] == "noindex, nofollow"
    assert "This frozen build was removed" in preview.not_found_html()
    assert 'href="/aisi-tracker-preview/next/"' in preview.not_found_html()


def test_only_the_dataset_doi_is_removed():
    """A DOI beside the dataset citation goes; a cited paper's DOI is a source and stays, and the oracle allows it."""
    value = {"dataset": {"citation": CITATION, "doi": "10.5281/zenodo.12345"},
             "sources": [{"doi": "10.48550/arXiv.2601.15679", "url": "https://doi.org/10.48550/arXiv.2601.15679"}]}
    marked = preview.mark_json(value, {"notice": preview.NOTICE}, CITATION, allow_preview=False)
    assert "doi" not in marked["dataset"]
    assert marked["sources"][0]["doi"] == "10.48550/arXiv.2601.15679"


def test_oracle_forbids_each_release_identifier():
    """Every form of the release's identity is refused; a cited paper's DOI is a source and is allowed."""
    def problems(tmp, text):
        (tmp / "f.txt").write_text(text, encoding="utf-8")
        return preview.oracle(tmp, "10.5281/zenodo.12345")
    import tempfile
    with tempfile.TemporaryDirectory() as name:
        tmp = Path(name)
        for text in ("see https://doi.org/10.5281/zenodo.12345", "10.5281/zenodo.99999", "https://zenodo.org/records/1",
                     "https://aisi.accelerate-europe.org/method.html", "doi 10.5281/ZENODO.12345"):
            assert problems(tmp, text), text
        assert not problems(tmp, "https://doi.org/10.48550/arXiv.2601.15679")


def test_lease_lost_on_moved_or_deleted_branch():
    assert preview.lease_lost("", "abc")  # gh-pages deleted after the fetch: retry from an empty tree
    assert preview.lease_lost("def\trefs/heads/gh-pages", "abc")
    assert preview.lease_lost("def\trefs/heads/gh-pages", "")  # created by another run after we saw none
    assert not preview.lease_lost("abc\trefs/heads/gh-pages", "abc")  # unchanged: a transport or auth failure


def count_fixture(n, start="2026-11-14T00:00:00Z", live=None):
    """n frozen builds an hour apart (oldest first), all within the age limit at BUILT, each 10 bytes."""
    first = preview.timestamp(start)
    frozen = {f"{i:010x}": {"commit": f"{i:010x}" + "0" * 30, "built": (first + dt.timedelta(hours=i)).isoformat(), "size": 10}
              for i in range(1, n + 1)}
    folders = {"next": {"branch": "next", "size": 10, "current": {"commit": live or "f" * 40}}}
    return {"folders": folders, "frozen": frozen}


def test_count_limit_prunes_the_oldest_unkept_build():
    assert preview.FROZEN_COUNT == 10
    manifest = count_fixture(11)
    result, removed = preview.retention(manifest, set(), set(), preview.timestamp(BUILT), budget=10**9)
    assert removed == {"b/" + f"{1:010x}"}
    assert set(result["frozen"]) == {f"{i:010x}" for i in range(2, 12)}
    result, removed = preview.retention(count_fixture(10), set(), set(), preview.timestamp(BUILT), budget=10**9)
    assert not removed and len(result["frozen"]) == 10


def test_count_limit_never_prunes_or_counts_kept_and_live_builds():
    # The two oldest are kept, the third is next's current build; 11 unkept follow, so only the oldest of those goes.
    manifest = count_fixture(14, live=f"{3:010x}" + "0" * 30)
    kept = {f"{1:010x}", f"{2:010x}"}
    result, removed = preview.retention(manifest, kept, set(), preview.timestamp(BUILT), budget=10**9)
    assert removed == {"b/" + f"{4:010x}"}
    assert kept | {f"{3:010x}"} <= set(result["frozen"])
    assert len([sha for sha in result["frozen"] if sha not in kept | {f"{3:010x}"}]) == preview.FROZEN_COUNT


def test_count_limit_with_age_and_size_rules():
    # Age first: three builds past FROZEN_DAYS go, leaving nine, under the count; nothing else goes.
    manifest = count_fixture(12, start="2026-10-20T00:00:00Z")
    for i in range(4, 13):
        manifest["frozen"][f"{i:010x}"]["built"] = (preview.timestamp("2026-11-14T00:00:00Z") + dt.timedelta(hours=i)).isoformat()
    result, removed = preview.retention(manifest, set(), set(), preview.timestamp(BUILT), budget=10**9)
    assert removed == {"b/" + f"{i:010x}" for i in (1, 2, 3)}
    # Count, then size: of 12 recent builds the count leaves the 10 newest; a budget for next and 8 builds removes 2 more, oldest first.
    result, removed = preview.retention(count_fixture(12), set(), set(), preview.timestamp(BUILT), budget=90)
    assert removed == {"b/" + f"{i:010x}" for i in (1, 2, 3, 4)}
    assert set(result["frozen"]) == {f"{i:010x}" for i in range(5, 13)}
