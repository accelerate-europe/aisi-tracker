"""Read an explicit build; expectations come from this snapshot's authored data."""

import json
from pathlib import Path

import pytest
from bs4 import BeautifulSoup

CONTENT = Path(__file__).resolve().parent
ROOT = CONTENT.parents[1]


def pytest_addoption(parser):
    parser.addoption("--site", type=Path, metavar="DIR", help="pre-rendered site to check")


def pytest_ignore_collect(collection_path, config):
    # Keep broad/default unit collection out of this package without changing the
    # parent conftest. Explicit content paths still collect, even without --site.
    if collection_path.is_relative_to(CONTENT):
        explicit = any(
            (config.invocation_params.dir / arg.split("::", 1)[0]).resolve().is_relative_to(CONTENT)
            for arg in config.args
        )
        return None if explicit else True
    return None


@pytest.fixture(scope="session")
def site(pytestconfig):
    path = pytestconfig.getoption("site")
    if path is None:
        pytest.fail("content checks require --site DIR pointing to a pre-rendered build", pytrace=False)
    path = path.resolve()
    missing = [name for name in ("index.html", "data/aisi.json") if not (path / name).is_file()]
    if missing:
        pytest.fail(f"missing build files in {path}: {', '.join(missing)}", pytrace=False)
    return path


@pytest.fixture(scope="session")
def saved_html(site):
    return (site / "index.html").read_text(encoding="utf-8")


@pytest.fixture(scope="session")
def saved_soup(saved_html):
    return BeautifulSoup(saved_html, "html.parser")


@pytest.fixture(scope="session")
def published(site):
    return json.loads((site / "data/aisi.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="session")
def authored():
    return json.loads((ROOT / "data/aisi.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="session", autouse=True)
def current_build(saved_soup, published, authored):
    differences = []
    for field in ("version", "as_of"):
        actual = published.get("dataset", {}).get(field)
        expected = authored["dataset"][field]
        if actual != expected:
            differences.append(f"dataset.{field}: built {actual!r}, authored {expected!r}")
    if saved_soup.select_one("html[data-prerendered]") is None:
        differences.append("index.html is missing html[data-prerendered]")
    if differences:
        pytest.fail("stale or incomplete build: " + "; ".join(differences), pytrace=False)
