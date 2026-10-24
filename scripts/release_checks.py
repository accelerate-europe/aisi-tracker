"""Release metadata and repository prose checks, shared by check and build."""

import datetime as dt
import json
import os
import re
import subprocess
import tempfile
from pathlib import Path

try:
    from .agent_files import doi_url
    from .reader_text import reader_text
except ImportError:
    from agent_files import doi_url
    from reader_text import reader_text


CITATION_START = b"<!-- citation:start -->"
CITATION_END = b"<!-- citation:end -->"
QUOTATIONS_NOTICE = (
    "Quotations from sources remain with their rights holders and are reproduced "
    "as quotations; the licence covers our own compilation, text and data."
)


def check_readme_licence(readme: str) -> list[str]:
    """Both README licence statements must preserve the source-rights notice."""
    sections = {
        "agents block": re.search(
            r"<!-- agents:start -->(.*?)<!-- agents:end -->", readme, re.S
        ),
        "Licences section": re.search(
            r"^## Licences\s*\n(.*?)(?=^## |\Z)", readme, re.M | re.S
        ),
    }
    return [
        f"README.md {label}: missing quotations licence notice"
        for label, match in sections.items()
        if match is None or QUOTATIONS_NOTICE not in match.group(1)
    ]


def check_readme_doi(readme: str, doi: str | None) -> list[str]:
    """With dataset.doi set, the README introduction (before the first section) links it."""
    if not doi:
        return []
    intro = re.split(r"^## ", readme, maxsplit=1, flags=re.M)[0]
    link = f"[{doi}]({doi_url(doi)})"
    return [] if link in intro else [f"README.md introduction: missing the DOI link {link}"]


def today() -> dt.date:
    """The machine's local date; a separate function so tests can fix it."""
    return dt.date.today()


def check_metadata(data: dict) -> list[str]:
    errors = []
    metadata = data["dataset"]
    if QUOTATIONS_NOTICE not in metadata.get("license_note", ""):
        errors.append("dataset.license_note: missing quotations licence notice")
    # The whole version token: 'version 1.0.1' must not match inside 'version 1.0.10'.
    if not re.search(r"(?<!\w)version " + re.escape(metadata["version"]) + r"(?![\w.+-]*\w)", metadata["citation"]):
        errors.append("dataset.citation does not name the current version")
    if metadata.get("doi") and not metadata["citation"].endswith(" " + doi_url(metadata["doi"])):
        errors.append("dataset.citation must end with the DOI link https://doi.org/ followed by dataset.doi")
    if not data.get("changelog") or data["changelog"][-1]["date"] != metadata["as_of"]:
        errors.append("dataset.as_of must equal the last changelog date")
    try:
        as_of = dt.date.fromisoformat(metadata["as_of"])
    except ValueError:
        return errors  # The ordinary date check reports malformed dates.
    if as_of > today():
        errors.append(f"dataset.as_of {as_of.isoformat()} is after today ({today().isoformat()})")
    labels = [
        as_of.isoformat(),
        f"{as_of.day} {as_of:%B %Y}",
        f"{as_of.day} {as_of:%b %Y}",
    ]
    if not any(
        re.search(r"(?<!\w)" + re.escape(label) + r"(?!\w)", metadata["citation"])
        for label in labels
    ):
        errors.append("dataset.citation must contain the as_of date")
    return errors


INSTRUMENT_STATUS_MAX_AGE = dt.timedelta(days=183)  # six months, per dataset.method (rescinded-instruments)


def check_instrument_status_dates(data: dict) -> list[str]:
    """A legal instrument's status cannot have been verified in the future."""
    return [
        f"source {source['id']}: instrument_status_checked {source['instrument_status_checked']} is after today ({today().isoformat()})"
        for source in data["sources"]
        if source.get("instrument_status_checked", "") > today().isoformat()
    ]


FLAG_FIELDS = ("model_access", "testing_capacity", "staffing_model")


def utc_date(timestamp: str) -> str:
    """The UTC calendar date of an RFC 3339 timestamp (T, Z in either case); the schema check reports a malformed one."""
    try:
        moment = dt.datetime.fromisoformat(re.sub(r"[Zz]$", "+00:00", timestamp).upper())
    except ValueError:
        return timestamp[:10]  # still compared, so a malformed timestamp cannot hide a late date
    if moment.tzinfo is None:
        return moment.date().isoformat()
    return moment.astimezone(dt.timezone.utc).date().isoformat()


def dates_after_as_of(data: dict) -> list[str]:
    """Recorded work dated after dataset.as_of: flag searches, source, snapshot and excerpt access, instrument status checks.

    The data cannot be "as of" a date before work it records. `check.py --release`
    fails on these; an ordinary check lists them as release warnings, since
    the release-day cut-off sets as_of to the release date. Record check dates
    (check.last_checked, partial, searches) fail in check_method_rules at any time.
    Ordered by record or source id, then position.
    """
    as_of = data["dataset"]["as_of"]
    found = []
    for institute in sorted(data["institutes"], key=lambda i: i["id"]):
        for field in FLAG_FIELDS:
            for query in (institute.get(field) or {}).get("searches", []):
                if query.get("date", "") > as_of:
                    found.append(f"{institute['id']}: {field} search {query['date']} is after dataset.as_of {as_of}")
    for source in sorted(data["sources"], key=lambda s: s["id"]):
        if source.get("accessed", "") > as_of:
            found.append(f"source {source['id']}: accessed {source['accessed']} is after dataset.as_of {as_of}")
        retrieved = (source.get("snapshot") or {}).get("retrieved")
        if retrieved and utc_date(retrieved) > as_of:
            found.append(f"source {source['id']}: snapshot retrieved {retrieved} is after dataset.as_of {as_of}")
        for n, excerpt in enumerate(source.get("excerpts", [])):
            if excerpt.get("accessed", "") > as_of:
                found.append(f"source {source['id']}: excerpt {n} accessed {excerpt['accessed']} is after dataset.as_of {as_of}")
        if source.get("instrument_status_checked", "") > as_of:
            found.append(
                f"source {source['id']}: instrument_status_checked {source['instrument_status_checked']} is after dataset.as_of {as_of}"
            )
    return found


def instrument_status_warnings(data: dict, *, release: bool = False) -> list[str]:
    """Legal-text sources whose status (in force, or rescinded or replaced) is unverified or older than six months.

    Age is measured at the release date (today) under `--release`, otherwise at
    dataset.as_of; on release day the two are the same date. Warnings for the
    check report, never failures. Ordered by source id.
    """
    try:
        as_of = dt.date.fromisoformat(data["dataset"]["as_of"])
    except ValueError:
        return []  # check_metadata and the date checks report a malformed as_of.
    reference, label = (today(), "the release date") if release else (as_of, "dataset.as_of")
    warnings = []
    for source in sorted(data["sources"], key=lambda s: s["id"]):
        if source.get("legal_text") is not True:
            continue
        checked = source.get("instrument_status_checked")
        if checked is None:
            warnings.append(f"source {source['id']}: status never verified (no instrument_status_checked)")
            continue
        try:
            age = reference - dt.date.fromisoformat(checked)
        except ValueError:
            continue  # the date checks report a malformed date.
        if age > INSTRUMENT_STATUS_MAX_AGE:
            warnings.append(
                f"source {source['id']}: status last verified {checked}, "
                f"{age.days} days before {label} {reference.isoformat()} (limit {INSTRUMENT_STATUS_MAX_AGE.days})"
            )
    return warnings


def generated_block(readme: bytes, content: str, name: str) -> bytes:
    """Replace one named, standalone generated block, retaining other bytes."""
    start_marker = f"<!-- {name}:start -->".encode()
    end_marker = f"<!-- {name}:end -->".encode()
    encoded = content.encode("utf-8")
    if any(marker in encoded for marker in (start_marker, end_marker)):
        raise ValueError(f"The canonical {name} cannot contain {name} markers")
    for marker in (start_marker, end_marker):
        if (
            readme.count(marker) != 1
            or sum(line.rstrip(b"\r") == marker for line in readme.split(b"\n")) != 1
        ):
            raise ValueError(f"README.md needs one pair of standalone {name} markers")
    start = readme.index(start_marker) + len(start_marker)
    end = readme.index(end_marker)
    if start > end:
        raise ValueError(f"README.md {name} markers are reversed")
    newline = b"\r\n" if readme[start : start + 2] == b"\r\n" else b"\n"
    return readme[:start] + newline * 2 + encoded + newline * 2 + readme[end:]


def generated_citation(readme: bytes, citation: str) -> bytes:
    return generated_block(readme, citation, "citation")


def generated_counts(readme: bytes, published: dict) -> bytes:
    """Current coverage from published records; changelog history is untouched."""
    jurisdictions = published["jurisdictions"]
    institutes = published["institutes"]
    metadata = published["dataset"]
    negatives = [j for j in jurisdictions if j["status"] == "none_identified"]
    confidence_counts = [f"{sum(j.get('status_confidence') == level for j in negatives)} {level}" for level in ("high", "medium", "low")]
    unrecorded = sum("status_confidence" not in j for j in negatives)
    if unrecorded:
        confidence_counts.append(f"{unrecorded} not recorded")
    content = (
        f"Version {metadata['version']}, data as of {metadata['as_of']}. "
        f"{len(jurisdictions)} jurisdictions, {len(institutes)} institutes and {len(published['sources'])} sources. "
        f"The Europe view covers {sum(bool(j['europe_scope']) for j in jurisdictions)} states.\n\n"
        f"{sum(len(i.get('outputs', [])) for i in institutes)} output records, "
        f"{sum(len(i.get('agreements', [])) for i in institutes)} agreements and "
        f"{len(metadata.get('network_milestones', []))} network milestones. "
        "Negative findings by confidence: "
        + ", ".join(confidence_counts)
        + "."
    )
    return generated_block(readme, content, "counts")


def release_tag(data: dict) -> str:
    """The git tag for this release. .zenodo.json links tree/v<version>, and Zenodo reads the file at the tag."""
    return f"v{data['dataset']['version']}"


def check_release_tag(data: dict, zenodo: str, head_tags: list[str], tag_commit: str | None, head: str) -> list[str]:
    """The tag about to be created must match the data version, the committed .zenodo.json and HEAD."""
    tag = release_tag(data)
    errors = []
    zenodo_version = json.loads(zenodo).get("version")
    if zenodo_version != data["dataset"]["version"]:
        errors.append(f".zenodo.json at HEAD has version {zenodo_version!r}, data has {data['dataset']['version']!r}; run scripts/build.py --update-readme and commit")
    errors.extend(f"HEAD carries tag {other}, expected {tag}" for other in head_tags if other.startswith("v") and other != tag)
    if tag_commit is not None and tag_commit != head:
        errors.append(f"tag {tag} already exists on {tag_commit[:12]}, not on HEAD {head[:12]}")
    return errors


def write_citation(path: Path, content: bytes) -> None:
    """Replace a generated tracked file atomically, retaining its file permissions (new files get 0644)."""
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(content)
        temporary.chmod(path.stat().st_mode & 0o777 if path.exists() else 0o644)
        temporary.replace(path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def tracked_markdown(root: Path) -> list[Path]:
    """Use tracked prose in checkouts, or source prose in an unpacked export."""
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "ls-files", "-z", "--", "*.md"],
            capture_output=True,
            check=True,
            env={**os.environ, "LC_ALL": "C"},
        )
    except FileNotFoundError:
        pass  # Git is optional for a source export.
    except subprocess.CalledProcessError as exc:
        if b"not a git repository" not in (exc.stderr or b""):
            raise
    else:
        if result.stdout or (root / ".git").exists():
            return sorted(root / name.decode("utf-8") for name in result.stdout.split(b"\0") if name)

    excluded = {
        ".git", ".venv", "venv", "node_modules", "work", "dist", "build",
        "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".tox", ".nox",
    }

    def unreadable(error):
        raise error

    paths = []
    for directory, dirs, files in os.walk(root, onerror=unreadable):
        dirs[:] = [name for name in dirs if name not in excluded and not name.startswith("tmp")]
        paths.extend(Path(directory) / name for name in files if name.endswith(".md"))
    return sorted(paths)


def check_markdown(root: Path, readme: bytes | None = None) -> list[str]:
    try:
        paths = tracked_markdown(root)
        return [
            f"{path.relative_to(root)}: tracked Markdown contains U+2014"
            for path in paths
            if "\u2014"
            in (
                readme.decode("utf-8")
                if path == root / "README.md" and readme is not None
                else path.read_text(encoding="utf-8")
            )
        ]
    except (OSError, subprocess.SubprocessError, UnicodeError) as error:
        return [f"Cannot check tracked Markdown: {error}"]


DRAFT_WORDING = re.compile(r"\bunreleased\b|\bthis\s+draft\b|\bowner\s+copy\s+edit\s+pending\b", re.I)
# Match tracker version labels and paths, not the CC BY licence or font versions.
PRERELEASE_VERSION = re.compile(
    r"\b(?:version|schema|contract)\s*[-:(`]*\s*v?4\.0(?:\.\d+)?(?!\w|\.\d)"
    r"|\b4\.0(?:\.\d+)?\s+(?:activity\s+)?(?:schema|contract)\b"
    r"|\bapi/v4/",
    re.I,
)
# Internal batch labels and reviewer tooling do not belong in release notes.
CHANGELOG_PROCESS = re.compile(
    r"\bround\s+\d+\b|\b(?-i:[Rr]ound\s+[A-Z])\b|\bnight\s+\d+\b"
    r"|\b(?:page|plain-bug)\s+round\b|\bowner\s+decision\b"
    r"|\b(?:Codex|Fable)\b|\breviewed\s+by\s+(?:a\s+)?(?:different\s+)?model\s+famil(?:y|ies)\b",
    re.I,
)
# Match research job identifiers and reviewer provenance, not research about AI
# models or red-teaming. Source titles, quotations and query strings stay intact.
INTERNAL_PROCESS = re.compile(
    r"\b(?:review|research|audit|search|agent|job|batch)(?:[-_][a-z]+)*[-_]\d+\b"
    r"|\bnight\d+/[a-z][\w-]*"
    r"|(?-i:\b[a-z][a-zA-Z]*(?:Search|Blogs?)\b)"
    r"|\b(?:red[- ]teamed|reviewed|checked|audited|verified)\s+by\s+"
    r"(?:an?\s+)?(?:(?:different|independent|separate|second|another)\s+)*"
    r"(?:"
    # Product names may carry a version or tier; a following name can be a person.
    r"(?:Codex|Claude|Opus|Fable|Sonnet|GPT|Gemini)\b"
    r"(?=\s*(?:[.,;:!?()]|$)|-?\s*\d|\s+(?:Opus|Sonnet|Haiku|Pro|Flash|Ultra)\b|\s+(?:per|in\s+each|for\s+each|across)\b)"
    # Generic actors can precede institutions or human reviewers, such as
    # AI Office staff or model providers, which are not process labels.
    r"|(?:model(?:\s+famil(?:y|ies))?|subagent|agent|LLM|AI)\b"
    r"(?=\s*(?:[.,;:!?)]|$)|\s+(?:per|in\s+each|for\s+each|across)\b)"
    r")",
    re.I,
)


def schema_descriptions(obj, path=()):
    if isinstance(obj, dict):
        for key, value in obj.items():
            if key == "description" and isinstance(value, str):
                yield (*path, key), value
            else:
                yield from schema_descriptions(value, (*path, key))
    elif isinstance(obj, list):
        for index, value in enumerate(obj):
            yield from schema_descriptions(value, (*path, index))


def check_launch_wording(
    data: dict, schema: dict, root: Path, readme: str | None = None
) -> list[str]:
    """Scan release prose, never enum values, source titles or quoted evidence."""
    texts = [("dataset.citation", data["dataset"]["citation"])]
    errors = []
    # Downloads expose audit and access notes too, even when the page hides them.
    # Reuse the authored-prose selector without its page-only restrictions.
    public_prose = [
        ("/" + "/".join(map(str, path)), text)
        for path, text in reader_text(data)
        if path[-1] != "title_en" and "citations" not in path
    ]
    public_prose.extend(
        ("schema/" + "/".join(map(str, path)), text)
        for path, text in schema_descriptions(schema)
    )
    for where, text in public_prose:
        prose = re.sub(r"https?://\S+", "", text)
        if match := INTERNAL_PROCESS.search(prose):
            errors.append(
                f"{where}: remove internal process wording before release ({match.group(0)!r})"
            )
    for entry in data.get("changelog", []):
        fields = [("summary", entry["summary"])] + [
            (f"changes/{index}", text)
            for index, text in enumerate(entry["changes"])
        ]
        for field, text in fields:
            where = f"changelog/{entry['version']}/{field}"
            texts.append((where, text))
            if CHANGELOG_PROCESS.search(text):
                errors.append(f"{where}: remove process wording before release")
    for name in ("README.md", "CLAUDE.md", "AGENTS.md"):
        try:
            text = (
                readme
                if name == "README.md" and readme is not None
                else (root / name).read_text(encoding="utf-8")
            )
            texts.append((name, text))
        except OSError as exc:
            if (isinstance(exc, FileNotFoundError) and name in {"CLAUDE.md", "AGENTS.md"}
                    and not (root / ".git").exists()):
                continue
            errors.append(f"{name}: cannot check release wording: {exc}")
    texts.extend((f"dataset.method/{rule['id']}", rule["text"]) for rule in data["dataset"].get("method", []))
    texts.append(("dataset.method_summary_status", data["dataset"].get("method_summary_status", "")))
    texts.extend((f"dataset.method_summaries/{s['id']}", s["summary"]) for s in data["dataset"].get("method_summaries", []))
    texts.extend(
        ("schema/" + "/".join(map(str, path)), text)
        for path, text in schema_descriptions(schema)
    )
    # Public source documentation ships with the release; internal history does not.
    for path in sorted((root / "docs").rglob("*.md")):
        if "history" not in path.relative_to(root / "docs").parts:
            texts.append((path.relative_to(root).as_posix(), path.read_text(encoding="utf-8")))
    for where, text in texts:
        if DRAFT_WORDING.search(text):
            errors.append(f"{where}: remove draft wording before release")
    for where, text in [*texts, *public_prose]:
        if match := PRERELEASE_VERSION.search(text):
            error = f"{where}: remove internal pre-release version before release ({match.group(0)!r})"
            if error not in errors:
                errors.append(error)
    return errors
