"""Exercise the content package in a separate pytest process, without a build or browser."""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

CONTENT = Path(__file__).parent / "content"


@pytest.fixture
def content_repo(tmp_path):
    root = tmp_path / "snapshot"
    (root / "tests/content").mkdir(parents=True)
    for name in ("__init__.py", "conftest.py", "page.py", "test_saved_page.py"):  # the step itself; family tests need the real scripts
        shutil.copy(CONTENT / name, root / "tests/content" / name)
    (root / "pytest.ini").write_text("[pytest]\ntestpaths = tests\n", encoding="utf-8")
    authored = {"dataset": {"version": "1.2.3", "as_of": "2026-11-08"},
                "jurisdictions": [{"id": "GB", "kind": "state", "europe_scope": True}, {"id": "AT", "kind": "state", "europe_scope": True}]}
    (root / "data").mkdir()
    (root / "data/aisi.json").write_text(json.dumps(authored), encoding="utf-8")
    site = tmp_path / "site"
    (site / "data").mkdir(parents=True)
    (site / "data/aisi.json").write_text(json.dumps(authored), encoding="utf-8")
    (site / "index.html").write_text(
        '<html data-prerendered><body><p id="updated"><a href="#changes">v1.2.3</a></p>'
        '<article class="card" data-j="GB"></article>'
        '<details class="card mini" data-j="AT"></details>'
        '<table><tbody id="world-rows"><tr data-j="GB"></tr><tr data-j="AT"></tr></tbody></table></body></html>',
        encoding="utf-8",
    )
    return root, site


def run_content(root, *args):
    env = {k: v for k, v in os.environ.items() if k not in ("PYTEST_ADDOPTS", "PYTHONPATH")}
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "--tb=short", *map(str, args)],
        cwd=root, env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=60,
    )


@pytest.mark.parametrize("field, stale", [("version", "0.0.1"), ("as_of", "2000-01-01")])
def test_content_accepts_matching_site_and_rejects_stale(content_repo, field, stale):
    root, site = content_repo
    result = run_content(root, "tests/content", "--site", site)
    assert result.returncode == 0, result.stdout
    assert "2 passed" in result.stdout
    path = site / "data/aisi.json"
    published = json.loads(path.read_text())
    published["dataset"][field] = stale
    path.write_text(json.dumps(published), encoding="utf-8")
    result = run_content(root, "tests/content", "--site", site)
    assert result.returncode == 1, result.stdout
    assert f"dataset.{field}: built '{stale}', authored" in result.stdout


@pytest.mark.parametrize("target", ["tests/content", "tests/content/test_saved_page.py::test_saved_page_is_prerendered_at_the_authored_version"])
def test_content_without_site_fails_instead_of_skipping(content_repo, target):
    root, _ = content_repo
    result = run_content(root, target)
    assert result.returncode == 1, result.stdout
    assert "content checks require --site DIR" in result.stdout and "skipped" not in result.stdout


@pytest.mark.parametrize("missing", ["directory", "index.html", "data/aisi.json"])
def test_content_reports_missing_build_files(content_repo, missing):
    root, site = content_repo
    if missing == "directory":
        site = site / "absent"
    else:
        path = site / missing
        path.rename(path.with_suffix(".absent"))
    result = run_content(root, "tests/content", "--site", site)
    assert result.returncode == 1, result.stdout
    assert "missing build files" in result.stdout
    assert ("index.html" if missing == "directory" else missing) in result.stdout


@pytest.mark.parametrize("replacement", ["", '><body data-prerendered'])
def test_content_requires_prerender_marker_on_html(content_repo, replacement):
    root, site = content_repo
    path = site / "index.html"
    path.write_text(path.read_text().replace(" data-prerendered", replacement), encoding="utf-8")
    result = run_content(root, "tests/content", "--site", site)
    assert result.returncode == 1, result.stdout
    assert "index.html is missing html[data-prerendered]" in result.stdout


def test_session_guard_covers_tests_that_request_no_fixtures(content_repo):
    root, site = content_repo
    (root / "tests/content/test_guard.py").write_text("def test_guard():\n    pass\n", encoding="utf-8")
    path = site / "data/aisi.json"
    path.write_text('{"dataset": {}}', encoding="utf-8")
    result = run_content(root, "tests/content/test_guard.py", "--site", site)
    assert result.returncode == 1, result.stdout
    assert "dataset.version: built None, authored '1.2.3'" in result.stdout
    assert "dataset.as_of: built None, authored '2026-11-08'" in result.stdout


@pytest.mark.parametrize("old, new", [
    ("v1.2.3", "v0.0.1"),
    ('<details class="card mini" data-j="AT"></details>', ""),
    ('<article class="card" data-j="GB"></article>', '<article class="card" data-j="GB"></article>' * 2),
    ('<tr data-j="AT"></tr>', ""),
])
def test_saved_page_checks_authored_version_cards_and_rows(content_repo, old, new):
    root, site = content_repo
    path = site / "index.html"
    path.write_text(path.read_text().replace(old, new), encoding="utf-8")
    result = run_content(root, "tests/content", "--site", site)
    assert result.returncode == 1, result.stdout
    assert "1 failed" in result.stdout


@pytest.mark.parametrize("targets", [[], ["tests"]])
def test_unit_collection_does_not_import_content_tests(content_repo, targets):
    root, _ = content_repo
    (root / "tests/test_unit.py").write_text("def test_unit():\n    pass\n", encoding="utf-8")
    (root / "tests/content/test_must_not_import.py").write_text(
        'raise AssertionError("unit collection imported a content test")\n', encoding="utf-8",
    )
    result = run_content(root, *targets, "--collect-only")
    assert result.returncode == 0, result.stdout
    assert "tests/test_unit.py::test_unit" in result.stdout
    assert "tests/content/" not in result.stdout and "1 test collected" in result.stdout


def test_without_templates_drops_only_the_inert_copy():
    from content.page import without_templates

    html = '<html data-prerendered><body><p id="x">shown</p><template id="page-template"><p id="x">inert</p></template></body></html>'
    out = without_templates(html)
    assert "shown" in out and "inert" not in out and "data-prerendered" in out


def test_a_template_copy_is_not_counted(content_repo):
    root, site = content_repo
    path = site / "index.html"
    # The page template holds a second copy of the cards; counted, they would fail "exactly one card each".
    path.write_text(path.read_text().replace("</body>", '<template id="page-template"><article class="card" data-j="GB"></article></template></body>'), encoding="utf-8")
    result = run_content(root, "tests/content", "--site", site)
    assert result.returncode == 0, result.stdout
