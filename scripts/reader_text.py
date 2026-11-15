"""Select authored dataset prose without changing recorded evidence or names."""

import re

if __package__:
    from .claim_checks import QUOTED, quoted_spans
else:
    from claim_checks import QUOTED, quoted_spans  # type: ignore[import-not-found, no-redef]

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
        # A claim's text, a development's act and an authority's latest step are our prose around the source's
        # words: the quoted spans are the source's and are masked, the rest is checked (worklist item 26).
        claim = len(path) == 5 and path[0] in {"jurisdictions", "institutes"} and path[2] == "claims" and field in {"text", "act"}
        step = len(path) == 6 and path[0] == "jurisdictions" and path[2:5:2] == ("related_bodies", "designation_step") and field == "text"
        if claim or step:
            yield path, QUOTED.sub('"…"', obj)
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


_MONTH = r"(?:January|February|March|April|May|June|July|August|September|October|November|December)"
_SHORT_MONTH = r"(?:Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sept?|Oct|Nov|Dec)\.?"
_MONTH_NUMBER = {m: n for n, m in enumerate(
    "jan feb mar apr may jun jul aug sep oct nov dec".split(), start=1)}

# Mechanical classes: phrases with one reading, errors in a migrated record (docs/design/claims-1.1.md, section 5).
# Workshop words: how we worked, not what the source says ("PDF pages 145 and 158", "in the reviewed passages").
# check.log is the home of reading notes and is exempt; everywhere else they are residue.
RESIDUE = re.compile(
    r"\bPDF pages?\b|\breviewed passages?\b|\bsubsequently retrieved\b|\bin the retrieved\b|\b(?:this|the) record\b"
    r"|\bthe cited sources?\b|\bthe (?:only )?bodies found (?:are|were|is)\b|\bon its summary page\b",
    re.I,
)
# Internal date-and-slug pass ids, with an optional nearby research-pass label, must not reach readers.
RESEARCH_PASS_ID = re.compile(
    r"(?:\bresearch pass\b(?:\s+\w+){0,3}[\s:]+)?"
    r"\b\d{4}-\d{2}-\d{2}-[a-z0-9]+(?:-[a-z0-9]+)*\b",
    re.I,
)
# A month written short in prose ("26 Nov 2024"); the data writes "26 November 2024".
SHORT_MONTH = re.compile(r"\b\d{1,2} " + _SHORT_MONTH + r" \d{4}\b")
# Future or pending wording that points at a date already past: "several chapters commence 28 October 2026", "its
# report is due 13 Nov 2026", "the law was still in draft in July 2026". The date must belong to the pending action:
# at most two words and a preposition after the wording ("will take effect in October 2026"), never across a comma
# or sentence end, and not after "before", "since", "from" or the like ("will evaluate models trained before 1 January
# 2025", "will use data from July 2026"). A bare "commence", "take effect" or "enter into force" counts only on its own,
# not as an infinitive another phrase governs ("was scheduled to take effect in July 2026": a plan in the past tense).
STALE_TRIGGER = re.compile(
    r"\b(?:will|(?:is|are) (?:scheduled|expected|due|set|planned) to|(?:is|are) to (?:be|take|enter|start|begin)"
    r"|(?:is|are) due|due by|(?<!to )(?:commences?|enters? into force|takes? effect)"
    r"|(?:still|remains?|remained) (?:in draft|pending|incomplete|under (?:consultation|discussion|review)))\b",
    re.I,
)
_CLAUSE_DATE = re.compile(
    r"^(?: enter into force| take effect)?(?: (?!(?:before|after|since|than|until|trained|of|from)\b)[a-z-]+){0,2}?(?: (?:on|in|by|for|at))? "
    r"(?:(\d{1,2}) )?(" + _MONTH + "|" + _SHORT_MONTH + r") (\d{4})\b", re.I)
# A plan told in the past tense ("full operation was scheduled for November"): the reader cannot tell whether it
# happened. A warning: with no past date given, the wording alone does not show it is stale.
PAST_PLAN = re.compile(r"\b(?:was|were) (?:scheduled|expected|due|planned) (?:for|to)\b", re.I)

# Judgement classes: listed for the reviewer as release warnings, never errors (section 5: "a word list cannot catch
# it reliably, so the build only lists claims with negative words to help the reviewer").
NEGATIVE_WORD = re.compile(r"\b(?:not|no|none|never|without|neither|nor)\b", re.I)
# An opener without its later partner in the same quoted span may signal a cut construction.
CORRELATIVES = (
    (re.compile(r"\bnot only\b", re.I), re.compile(r"\bbut\b", re.I)),
    (re.compile(r"\beither\b", re.I), re.compile(r"\bor\b", re.I)),
    (re.compile(r"\bneither\b", re.I), re.compile(r"\bnor\b", re.I)),
    (re.compile(r"\bboth\b", re.I), re.compile(r"\band\b", re.I)),
)
VERDICT = re.compile(
    r"\bnot (?:a|an) (?:government|qualifying|national)[^.;]{0,40}(?:assignment|mandate|designation|role|institute)"
    r"|\bdo(?:es)? not (?:assign|qualify|supply|establish|count|meet|amount|determine)"
    r"|\b(?:is|are) not (?:counted|treated) as\b"
    r"|\bno (?:qualifying|national|government)[^.;]{0,50}(?:role|mandate|assignment|designation|institute|body) (?:is|was|has been) (?:assigned|identified|found|established)",
    re.I,
)
NOT_TAIL = re.compile(r",\s+(?:not|rather than)\s+(?:a |an |the |its |their )?[\w' -]{3,60}[.;]")
# "National", "advanced" and "severe-risk" (and "frontier") stated as a bar the body fails ("no national
# advanced-model evaluation role is assigned"; owner, 15 Nov 2026: not bars, removed at migration). A warning: about
# one hit in five only says what a search looked for or found ("No national AI strategy … was found").
BAR_WORD = re.compile(
    r"\b(?:no|not|without|never|neither|nor|lacks?)\b[^.;]{0,80}?\b(?:national|advanced|severe-risk|frontier)\b[^.;]{0,50}?"
    r"\b(?:mandate|role|remit|assignment|designation|evaluation|evaluator|institute|body|tasks?)\b"
    r"|\b(?:national|advanced|severe-risk|frontier)\b[^.;]{0,60}?\b(?:mandate|role|remit|assignment|designation)\b[^.;]{0,40}?"
    r"\b(?:was|were|is|are|has been)\s+not\b",
    re.I,
)
BARE_ACT = re.compile(r"(?<![\w(])(?:Resolution|Decree|Ordinance|Bill) (?:No\.? ?)?\d[\w/.-]*")
# A check.log entry says what we did; one with a verb of the world and none of searching or reading states a finding.
WORLD_VERB = re.compile(r"\b(?:designates?|assigns?|adopted|announced|established|enacted|signed|approved|took effect|commences?|is the|are the)\b", re.I)
METHOD_VERB = re.compile(r"\b(?:search(?:ed|es)?|read|re-read|opened|could not|blocked|retriev\w*|checked|looked|quer(?:y|ied|ies)|found no|not found|archived|snapshot|saved)\b", re.I)

LINT_CLASSES = {
    "workshop words": "error",
    "research pass id": "error",
    "short month": "error",
    "future wording about a past date": "error",
    "negative wording in a claim": "warning",
    "open correlative in a quote": "warning",
    "our verdict in prose": "warning",
    "', not X' tail": "warning",
    "verdict repeated in one record": "warning",
    "plan in the past tense": "warning",
    "bare act number": "warning",
    "national, advanced or severe-risk as a bar": "warning",
    "finding in check.log": "warning",
}


def _past(day: str | None, month: str, year: str, as_of: str) -> bool:
    """Whether the date (a whole month when there is no day) lies before as_of."""
    number = _MONTH_NUMBER[month[:3].lower()]
    if day:
        return f"{year}-{number:02d}-{int(day):02d}" < as_of
    return f"{year}-{number:02d}" < as_of[:7]


def stale(text: str, as_of: str) -> str | None:
    """The future or pending wording in text that points at a date before as_of, or None."""
    for trigger in STALE_TRIGGER.finditer(text):
        date = _CLAUSE_DATE.match(text[trigger.end():])
        if date and _past(date.group(1), date.group(2), date.group(3), as_of):
            return text[trigger.start(): trigger.end() + date.end()].strip()
    return None


def _lint_leaves(data: dict, migrated: set[str]):
    """(record id, migrated?, path, text, kind) for the reader text of jurisdictions and institutes: authored prose
    (claim texts with their quoted spans masked) and check.log. In a migrated record the summary, notes, roles and
    closest note are generated, so they are left out here: their words come from claims and structured check fields."""
    owner = {j.get("institute_id"): j["id"] for j in data.get("jurisdictions", []) if j.get("institute_id")}
    for path, value in reader_text(data):
        if path[0] not in {"jurisdictions", "institutes"} or not page_facing(path, data):
            continue
        record = data[path[0]][path[1]]
        jid = record["id"] if path[0] == "jurisdictions" else owner.get(record["id"], record["id"])
        is_migrated = jid in migrated
        generated = path[0] == "jurisdictions" and (
            path[2:] in {("summary",), ("notes",), ("closest_body", "note")}
            or (len(path) == 5 and path[2] == "related_bodies" and path[4] == "role")
        )
        if is_migrated and generated:
            continue
        kind = "claim" if len(path) == 5 and path[2] == "claims" else "prose"
        yield jid, is_migrated, path, value, kind
    for index, j in enumerate(data.get("jurisdictions", [])):
        for k, entry in enumerate((j.get("check") or {}).get("log", [])):
            yield j["id"], j["id"] in migrated, ("jurisdictions", index, "check", "log", k, "text"), entry["text"], "log"
        for k, entry in enumerate((j.get("check") or {}).get("passes", [])):
            for n, place in enumerate(entry["places"]):
                yield j["id"], j["id"] in migrated, ("jurisdictions", index, "check", "passes", k, "places", n), place, "prose"


def lint_reader_text(data: dict, migrated: set[str]) -> list[tuple[str, str, str, str]]:
    """Every hit of the lint classes as (class, record id, pointer, matched text). Severity per class is in
    LINT_CLASSES; an error class is an error only in a migrated record (lint_errors) and is counted as a release
    warning elsewhere until 1.2.0 migrates the rest (lint_warnings)."""
    as_of = data["dataset"]["as_of"]
    hits, verdicts = [], {}
    for jid, _, path, text, kind in _lint_leaves(data, migrated):
        pointer = "/" + "/".join(str(key).replace("~", "~0").replace("/", "~1") for key in path)
        if kind != "log" and (found := RESIDUE.search(text)):
            hits.append(("workshop words", jid, pointer, found.group(0)))
        if found := RESEARCH_PASS_ID.search(text):
            hits.append(("research pass id", jid, pointer, found.group(0)))
        if found := SHORT_MONTH.search(text):
            hits.append(("short month", jid, pointer, found.group(0)))
        # A timeline event is worded as of its own date ("will enter into force on 21 February 2024"), so it is exempt.
        if "timeline" not in path and (found := stale(text, as_of)):
            hits.append(("future wording about a past date", jid, pointer, found))
        if kind == "claim" and (found := NEGATIVE_WORD.search(text)):
            hits.append(("negative wording in a claim", jid, pointer, found.group(0)))
        if kind == "claim":
            # reader_text masks source wording for the other classes; use its shared quote extraction here.
            original = data
            for key in path:
                original = original[key]
            for span in quoted_spans(original):
                for opening, closing in CORRELATIVES:
                    for found in opening.finditer(span):
                        if not closing.search(span, found.end()):
                            hits.append(("open correlative in a quote", jid, pointer, f"{found.group(0)}: {span[:80]}"))
        if found := PAST_PLAN.search(text):
            hits.append(("plan in the past tense", jid, pointer, found.group(0)))
        if kind != "log":  # our sentences, authored prose and claim texts outside their quotes
            for found in VERDICT.finditer(text):
                hits.append(("our verdict in prose", jid, pointer, found.group(0)))
                verdicts[jid] = verdicts.get(jid, 0) + 1
            if found := NOT_TAIL.search(text):
                hits.append(("', not X' tail", jid, pointer, found.group(0).strip(", ")))
            if found := BARE_ACT.search(text):
                hits.append(("bare act number", jid, pointer, found.group(0)))
        if found := BAR_WORD.search(text):
            hits.append(("national, advanced or severe-risk as a bar", jid, pointer, found.group(0)))
        if kind == "log" and WORLD_VERB.search(text) and not METHOD_VERB.search(text):
            hits.append(("finding in check.log", jid, pointer, text[:80]))
    hits += [("verdict repeated in one record", jid, f"/jurisdictions/{jid}", f"{n} verdicts") for jid, n in verdicts.items() if n > 1]
    return hits


def lint_errors(data: dict, migrated: set[str]) -> list[str]:
    """The mechanical lint classes in migrated records: build errors."""
    return [f"{pointer}: {cls}: {found!r}" for cls, jid, pointer, found in lint_reader_text(data, migrated)
            if LINT_CLASSES[cls] == "error" and jid in migrated]


def lint_warnings(data: dict, migrated: set[str]) -> list[str]:
    """One Release warnings line per class with its count: the error classes outside migrated records (the backlog
    until 1.2.0) and the judgement classes everywhere (for the reviewer)."""
    by_class: dict[str, list[tuple[str, str, str]]] = {}
    for cls, jid, pointer, found in lint_reader_text(data, migrated):
        if LINT_CLASSES[cls] == "error" and jid in migrated:
            continue
        by_class.setdefault(cls, []).append((jid, pointer, found))
    lines = []
    for cls in LINT_CLASSES:
        rows = by_class.get(cls)
        if rows:
            first = "; ".join(f"{pointer} {found!r}" for _, pointer, found in rows[:3])
            lines.append(f"reader text [{cls}]: {len(rows)} in {len({jid for jid, _, _ in rows})} records (first: {first}); full list in {READER_TEXT_REPORT} beside the release warnings")
    return lines


READER_TEXT_REPORT = "reader-text.md"


def lint_report(data: dict, migrated: set[str]) -> str:
    """Every lint hit behind the warning lines, by class, for the report directory and the reviewer."""
    lines = ["# Reader text", "", "The reader-text lint (scripts/reader_text.py): error classes are errors only in a migrated record; every other hit is listed here for the reviewer.", ""]
    hits = lint_reader_text(data, migrated)
    for cls, severity in LINT_CLASSES.items():
        rows = [(jid, pointer, found) for c, jid, pointer, found in hits if c == cls and not (severity == "error" and jid in migrated)]
        if rows:
            lines += [f"## {cls} ({len(rows)})", ""] + [f"- {jid} `{pointer}`: {found}" for jid, pointer, found in rows] + [""]
    return "\n".join(lines)


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
