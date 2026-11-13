"""Check that the repository follows docs/dependencies.md: every script runs from a lock, the locks agree, and every
documented command uses the one locked form. Static, standard library only: check.py runs it in its lint step.

Usage: uv run --locked scripts/dependency_checks.py [ROOT]   (exit 1 with one line per finding)
"""
from __future__ import annotations

import re
import sys
import tomllib
from pathlib import Path

HEADER = re.compile(r"^# /// script\s*$(.*?)^# ///\s*$", re.M | re.S)
IMPORTS_PLAYWRIGHT = re.compile(r"^\s*(?:from|import)\s+playwright\b", re.M)
# Dependencies whose exact version a header must name (a browser or geodata stack), so a relock cannot drift.
HEAVY = {"playwright", "geopandas", "matplotlib", "pyogrio"}
REQUIREMENT = re.compile(r"^\s*([A-Za-z0-9_.-]+)(\[[^\]]*\])?\s*(.*)$")

# Where commands are written for people, agents and machines. The rule page itself quotes the forbidden forms.
COMMAND_FILES = ("README.md", "AGENTS.md", "CLAUDE.md", "docs/**/*.md", ".github/workflows/*.yml", ".githooks/*", "scripts/*.py",
                 "tests/*.py")
# The rule, this check and its tests quote the forbidden forms.
EXEMPT = {"docs/dependencies.md", "scripts/dependency_checks.py", "tests/test_dependency_checks.py"}
UV_RUN = re.compile(r"\buv run\b(.*)")
# uv run options that take a value, so the value is not mistaken for the command.
VALUE_OPTIONS = {"--python", "-p", "--with", "--group", "--directory", "--project", "--extra", "--env-file", "--package"}
UV_RUN_LIST = re.compile(r"""\[\s*["']uv["']\s*,\s*["']run["']\s*,([^\]]*)\]""")
# The git hooks pipe check.py into a bare interpreter: standard library only, nothing to pin.
STDIN_FORM = re.compile(r"\buv run -q --no-project --python '>=3\.11' python -(?:\s|$)")
NO_PROJECT = re.compile(r"(?<![\w-])--no-project(?![\w-])")
FORBIDDEN = (
    (NO_PROJECT, "--no-project runs without the lock (only the hooks' stdin form may)"),
    (re.compile(r"(?<![\w-])--with(?![\w-])"), "--with installs a package outside any lock"),
    (re.compile(r"\bpip install\b"), "pip install installs a package outside any lock"),
    (re.compile(r"(?<![\w-])--group(?![\w-])"), "--group: the default groups are the lock's; name no group"),
    (re.compile(r"\buvx\b|\buv tool run\b"), "uvx and uv tool run install a tool outside any lock"),
    (re.compile(r"(?<![\w/-])python3? scripts/"), "python scripts/: run the script with uv run --locked"),
    (re.compile(r"\buv sync\b(?!.*--locked)"), "uv sync without --locked"),
)


def header(text: str) -> dict | None:
    """The PEP 723 metadata of a script, or None without a header."""
    match = HEADER.search(text)
    if not match:
        return None
    body = "\n".join(line[2:] if line.startswith("# ") else line[1:] for line in match.group(1).strip("\n").splitlines())
    return tomllib.loads(body)


def lock_versions(path: Path) -> dict[str, set[str]]:
    """Each locked package's versions (a lock split by Python version holds several); the project itself is left out."""
    versions: dict[str, set[str]] = {}
    for package in tomllib.loads(path.read_text(encoding="utf-8")).get("package", []):
        if "version" in package and not {"virtual", "editable"} & set(package.get("source", {})):
            versions.setdefault(package["name"], set()).add(package["version"])
    return versions


def script_findings(root: Path) -> list[str]:
    """Header and lock go together; a browser script has both; heavy dependencies are pinned exactly and match the lock."""
    findings = []
    scripts = root / "scripts"
    for path in sorted(scripts.glob("*.py")):
        text = path.read_text(encoding="utf-8")
        name = f"scripts/{path.name}"
        lock = path.with_name(path.name + ".lock")
        try:
            meta = header(text)
        except tomllib.TOMLDecodeError as exc:
            findings.append(f"{name}: unreadable PEP 723 header ({exc})")
            continue
        if IMPORTS_PLAYWRIGHT.search(text) and meta is None:
            findings.append(f"{name}: imports Playwright but has no PEP 723 header and lock (docs/dependencies.md)")
            continue
        if meta is None:
            if lock.exists():
                findings.append(f"{name}.lock: a lock without a PEP 723 header in its script")
            continue
        if not lock.exists():
            findings.append(f"{name}: a PEP 723 header without its lock; run uv lock --script {name}")
            continue
        locked = lock_versions(lock)
        for requirement in meta.get("dependencies", []):
            match = REQUIREMENT.match(requirement)
            package, spec = match.group(1).lower(), match.group(3).strip()
            if package not in HEAVY:
                continue
            pinned = re.fullmatch(r"==\s*([\w.]+)", spec)
            if not pinned:
                findings.append(f"{name}: header must pin {package} exactly (==), not {spec or 'without a version'}")
            elif locked.get(package) != {pinned.group(1)}:
                findings.append(f"{name}.lock: {package} {sorted(locked.get(package, set()))} does not match the header's =={pinned.group(1)}; relock")
    for lock in sorted(scripts.glob("*.py.lock")):
        if not lock.with_name(lock.name.removesuffix(".lock")).exists():
            findings.append(f"scripts/{lock.name}: a lock without its script")
    return findings


def lock_findings(root: Path) -> list[str]:
    """One version per package across all locks, direct and transitive (compared as each lock's set of versions)."""
    locks = [root / "uv.lock", *sorted((root / "scripts").glob("*.py.lock"))]
    seen: dict[str, dict[str, set[str]]] = {}
    for lock in locks:
        if lock.exists():
            for package, versions in lock_versions(lock).items():
                seen.setdefault(package, {})[lock.relative_to(root).as_posix()] = versions
    findings = []
    for package, by_lock in sorted(seen.items()):
        if len({frozenset(v) for v in by_lock.values()}) > 1:
            listed = "; ".join(f"{lock} {', '.join(sorted(v))}" for lock, v in sorted(by_lock.items()))
            findings.append(f"{package}: locks disagree ({listed}); relock so every lock resolves one version")
    return findings


def command_files(root: Path) -> list[Path]:
    files = {path for pattern in COMMAND_FILES for path in root.glob(pattern) if path.is_file()}
    return sorted(path for path in files if path.relative_to(root).as_posix() not in EXEMPT)


def split_uv_run(rest: str) -> tuple[list[str], str]:
    """The options after `uv run` and the command they run, from the rest of a line."""
    tokens = [token.strip("`'\"),;") for token in rest.replace("\\n", " ").split()]  # a "\n" in a string literal ends a word
    options, i = [], 0
    while i < len(tokens) and tokens[i].startswith("-"):
        options.append(tokens[i])
        i += 2 if tokens[i] in VALUE_OPTIONS else 1
    return options, tokens[i] if i < len(tokens) else ""


def uv_run_findings(options: list[str], target: str) -> list[str]:
    if "--locked" not in options:
        return ["uv run without --locked"]
    if target == "python":
        return ["uv run ... python: run the script or tool itself (python ignores a script's header and lock)"]
    return []


def line_findings(line: str) -> list[str]:
    """Problems with the commands on one line of a doc, workflow, hook or script."""
    if STDIN_FORM.search(line):  # the hooks' form: --no-project is its point; anything else is still checked
        return [why for pattern, why in FORBIDDEN if pattern is not NO_PROJECT and pattern.search(line)]
    found = [why for pattern, why in FORBIDDEN if pattern.search(line)]
    for match in UV_RUN.finditer(line):
        found += uv_run_findings(*split_uv_run(match.group(1)))
    return found


def list_findings(text: str) -> list[tuple[int, str]]:
    """Problems with ["uv", "run", ...] argument lists in Python source, which may span lines: (line number, why)."""
    found = []
    for match in UV_RUN_LIST.finditer(text):
        items = re.findall(r"""["']([^"']*)["']""", match.group(1))
        options = [item for item in items if item.startswith("-")]
        target = next((item for item in items if not item.startswith("-")), "")
        found += [(text.count("\n", 0, match.start()) + 1, why) for why in uv_run_findings(options, target)]
    return found


def command_findings(root: Path) -> list[str]:
    findings = []
    for path in command_files(root):
        text = path.read_text(encoding="utf-8")
        found = [(number, why) for number, line in enumerate(text.splitlines(), 1) for why in line_findings(line)]
        if path.suffix == ".py":
            found += list_findings(text)
        for number, why in sorted(found):
            findings.append(f"{path.relative_to(root).as_posix()}:{number}: {why} (docs/dependencies.md)")
    return findings


def findings(root: Path) -> list[str]:
    return script_findings(root) + lock_findings(root) + command_findings(root)


def main(argv: list[str]) -> int:
    root = Path(argv[1]) if len(argv) > 1 else Path(__file__).resolve().parent.parent
    found = findings(root)
    for line in found:
        print(line, file=sys.stderr)
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
