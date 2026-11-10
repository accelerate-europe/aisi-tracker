"""The test environment itself: what conftest removes from the host before any test runs."""

import os
import re
from pathlib import Path

from tests import conftest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
# Read by the scripts but owned by the git isolation fixture or by git itself, not host settings to drop.
# AISI_INTERNAL stays: it is how a public checkout's tests find the private data/internal files, and the tests that
# need them skip only when they are absent there.
# The browser lock settings are set for every test by conftest.isolated_browser_lock (XDG_RUNTIME_DIR is read only
# when AISI_BROWSER_LOCK is unset), so a test never shares the host's lock.
NOT_HOST_SETTINGS = {"GIT_INDEX_FILE", "GIT_AUTHOR_DATE", "AISI_INTERNAL", "AISI_BROWSER_LOCK", "AISI_BROWSER_LOCK_HELD",
                     "XDG_RUNTIME_DIR"}


def test_every_setting_the_scripts_read_is_removed_for_tests():
    """Finds names read as environ["X"], environ.get("X") or getenv("X") (spaces allowed), and every AISI_* literal,
    which covers names held in constants such as RUN_DATE_ENV. It cannot see a non-AISI name built at run time."""
    names = set()
    for path in SCRIPTS.glob("*.py"):
        text = path.read_text()
        names |= set(re.findall(r"""(?:environ\s*(?:\.\s*get\s*\(|\[)|getenv\s*\()\s*["']([A-Z][A-Z0-9_]+)["']""", text))
        names |= set(re.findall(r"\b(AISI_[A-Z0-9_]+)\b", text))
    missing = sorted(names - set(conftest.HOST_ENV) - NOT_HOST_SETTINGS)
    assert not missing, f"add to conftest.HOST_ENV (or NOT_HOST_SETTINGS with a reason): {missing}"


def test_no_host_setting_reaches_a_test():
    assert not [name for name in conftest.HOST_ENV if name in os.environ]
