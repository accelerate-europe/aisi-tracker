"""Select authored dataset prose without changing recorded evidence or names."""

import re

# Recorded titles, names, published job titles, quotations, query strings, URLs
# and identifiers are deliberately absent. Translations are our own prose.
PROSE_FIELDS = {
    "summary",
    "description",
    "notes",
    "what",
    "how",
    "note",
    "mandate",
    "mandate_short",
    "role",
    "scope",
    "period",
    "annual_basis",
    "basis",
    "attribution_note",
    "qualification_note",
    "staffing_profile",
    "identifier_notes",
    "language_note",
    "independence_note",
    "governance_note",
    "designation_note",
    "access_note",
    "title_en",
    "gloss",
    "reason",
    "short",
    "method",
    "changes",
    "citation",
    "subject",
    "metric",
    "unit",
}


def reader_text(obj, path=()):
    """Yield (path tokens, value) for authored prose, including list entries."""
    if isinstance(obj, dict):
        for key, value in obj.items():
            yield from reader_text(value, (*path, key))
    elif isinstance(obj, list):
        for index, value in enumerate(obj):
            yield from reader_text(value, (*path, index))
    elif isinstance(obj, str):
        field = next((key for key in reversed(path) if isinstance(key, str)), "")
        if "searches" in path or ("people" in path and field == "role"):
            return
        definitions = (
            len(path) > 1
            and path[0] == "dataset"
            and path[1] in {"status_definitions", "vocabularies", "glossary"}
        )
        if field in PROSE_FIELDS or definitions or path in {("dataset", "title"), ("dataset", "definition")} or (path[:2] == ("dataset", "method") and field == "text"):
            yield path, obj


# These are technical download/audit text, not prose displayed by the page.
def page_facing(path: tuple, data: dict) -> bool:
    return not (
        (path[0] == "sources" and ("excerpts" in path or path[-1] == "access_note"))
        or "check" in path
        or (len(path) == 4 and path[0] == "institutes" and path[2] in {"head", "reports_to", "national_partners"} and path[3] == "notes")  # search logs, never shown on the page
        or path[:2] == ("dataset", "exchange_rates")
        or path[-1] in {"attribution_note", "qualification_note"}
        or path[:2] in {("dataset", "vocabularies"), ("dataset", "shared_claims")}
        or (
            path[0] == "changelog"
            and "changes" in path
            and data["changelog"][path[1]].get("summary")
        )
    )


FIELD_PATH = re.compile(
    r"\b([a-z][a-z_]*)(?:\[\d*\])?\.([a-z][a-z_]*)\b"
    r"|\b([a-z][a-z_]*)(?:\[\d*\])+(?:\.|\[)?"
    r"|(?<!\w)/([a-z][a-z_]*)/",
    re.I,
)


def field_names(obj):
    """Known keys avoid confusing domains and abbreviations with field paths."""
    if isinstance(obj, dict):
        for key, value in obj.items():
            if re.fullmatch(r"[a-z]+(?:_[a-z]+)*", key):
                yield key
            yield from field_names(value)
    elif isinstance(obj, list):
        for value in obj:
            yield from field_names(value)


# Working-round labels and the dates of our own checks written into prose go
# stale and contradict the record's check dates, which live in `check`.
_DATE = (
    r"\d{1,2} (?:January|February|March|April|May|June|July|August|September"
    r"|October|November|December|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sept?|Oct|Nov|Dec) \d{4}"
)
PROCESS_LABEL = re.compile(
    r"\b[Rr]ound \d+(?=:| (?:review|attempt|check|reclassif))"
    r"|\b(?:search(?:ed|es)?|re-?check(?:ed|s)?|checked)\b[^.]{0,60}?\bon " + _DATE
    + r"|\bOn " + _DATE + r",[^.]{0,80}?\b(?:search(?:es|ed)?|re-?check(?:ed|s)?|checked)\b",
    re.I,
)
# A space lost between a word and a number or numeral ("AnnexIII", "17février",
# "на09.07.2026"), as text extraction does when it drops a non-breaking space or tag.
GLUED = re.compile(
    r"(?<![\w/.-])[^\W\d_]{2,}\d{1,2}\.\d{1,2}\.\d{2,4}"
    r"|\b\d{1,2}(?i:janvier|février|mars|avril|mai|juin|juillet|août|septembre|octobre"
    r"|novembre|décembre|January|February|March|April|May|June|July|August|September"
    r"|October|November|December)\b"
    r"|\b(?:Annex|Articles?|Section|Regulation|Law|point)(?=[IVX]+\b|\d)"
)
# Distributive "each" standing in for a pronoun: "the values each supports",
# "each status is dated from its act, and each value links to its source".
EACH_CONSTRUCTION = re.compile(
    r"\bthe [a-z]+ each (?!(?:year|month|week|day|time)s?\b)[a-z]+\b|\beach\b[^.;:]*?,\s+and each\b", re.I
)
# Dated observations of something other than our record check.
DATED_OBSERVATIONS = {"identifier_notes"}


def check_reader_text(data: dict) -> list[str]:
    """Reject copy defects in authored prose, never in verbatim evidence."""
    errors = []
    keys = (
        set(field_names(data))
        | PROSE_FIELDS
        | {
            "dataset",
            "check",
            "jurisdictions",
            "institutes",
            "sources",
            "funding",
            "staff",
            "people",
            "outputs",
            "agreements",
            "operating",
            "timeline",
            "changelog",
            "status_definitions",
            "vocabularies",
            "glossary",
            "excerpts",
            "closest_body",
            "last_checked",
            "annual_amount",
            "status_since",
        }
    )
    for index, source in enumerate(data.get("sources", [])):
        # title_en is authored prose and checked below with the rest.
        if glued := GLUED.search(source.get("title") or ""):
            errors.append(f"/sources/{index}/title: a space is missing in {glued.group(0)!r}")
    for path, value in reader_text(data):
        pointer = "/" + "/".join(
            str(key).replace("~", "~0").replace("/", "~1") for key in path
        )
        # URLs embedded in prose retain their exact spelling, like URL fields.
        prose = re.sub(r"https?://\S+", "", value)
        # Source titles stay exempt even when translated; glosses are authored.
        source_title = path[0] == "sources" and path[-1] == "title_en"
        if "\u2014" in prose and not source_title:
            errors.append(f"{pointer}: authored data text contains U+2014")
        if "  " in value:
            errors.append(f"{pointer}: reader-facing text contains double spaces")
        # URLs may legitimately contain paths; ordinary abbreviations, decimals
        # and domains do not match the field-path patterns.
        dotted = "dataset." in value.lower() or any(
            any(token and token.lower() in keys for token in match.groups())
            for match in FIELD_PATH.finditer(prose)
        )
        if (
            path[0] != "changelog"
            and path[:2] != ("dataset", "other_overviews")
            and path[-1] not in DATED_OBSERVATIONS
            and page_facing(path, data)
            and (label := PROCESS_LABEL.search(prose))
        ):
            errors.append(
                f"{pointer}: reader-facing text names a working round or check date ({label.group(0)!r}); dates of checks belong in check"
            )
        if each := EACH_CONSTRUCTION.search(prose):
            errors.append(f"{pointer}: reword the distributive 'each' construction {each.group(0)!r}")
        if glued := GLUED.search(prose):
            errors.append(f"{pointer}: a space is missing in {glued.group(0)!r}")
        bare = re.search(r"\b[a-z]+(?:_[a-z]+)+\b", prose, re.I)
        if page_facing(path, data) and (dotted or bare):
            errors.append(f"{pointer}: page-facing text contains field-path jargon")
    return errors
