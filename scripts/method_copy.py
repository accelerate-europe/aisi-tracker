"""Full method rendering and explicit acknowledgements of reader-copy dependencies."""

import argparse
import hashlib
import html
import json
import re
import unicodedata
from pathlib import Path


def fingerprint(text: str) -> str:
    """SHA-256 of NFKC text with whitespace collapsed and trimmed; case is significant."""
    normal = " ".join(unicodedata.normalize("NFKC", text).split())
    return hashlib.sha256(normal.encode("utf-8")).hexdigest()


def check_method_copy(dataset: dict) -> list[str]:
    rules = {r["id"]: r["text"] for r in dataset["method"]}
    errors = []
    if len(rules) != len(dataset["method"]):
        errors.append("dataset.method: duplicate rule id")
    seen, covered = set(), set()
    for summary in dataset.get("method_summaries", []):
        sid = summary["id"]
        if sid in seen:
            errors.append(f"method summary {sid}: duplicate summary id")
        seen.add(sid)
        for rid, digest in summary["rules"].items():
            covered.add(rid)
            if rid not in rules:
                errors.append(f"method summary {sid}: unknown rule {rid}")
            elif digest != fingerprint(rules[rid]):
                errors.append(
                    f"method rule {rid}: summary {sid} fingerprint is stale; reread the summary, then run scripts/method_copy.py refresh {sid}"
                )
    excluded = set(dataset.get("method_no_reader_summary", []))
    for rid in sorted(excluded - rules.keys()):
        errors.append(f"method rule {rid}: unknown no-reader-summary id")
    for rid in sorted(excluded & covered):
        errors.append(
            f"method rule {rid}: both summarised and listed without reader summary"
        )
    for rid in sorted(rules.keys() - covered - excluded):
        errors.append(
            f"method rule {rid}: needs a reader summary or explicit no-reader-summary entry"
        )
    return errors


def refresh(dataset: dict, summary_id: str) -> None:
    """Acknowledge one summary only, never silently acknowledge other stale summaries."""
    matches = [s for s in dataset["method_summaries"] if s["id"] == summary_id]
    if len(matches) != 1:
        raise ValueError(
            f"Expected one summary named {summary_id}, found {len(matches)}"
        )
    rules = {r["id"]: r["text"] for r in dataset["method"]}
    (summary,) = matches
    missing = sorted(summary["rules"].keys() - rules.keys())
    if missing:
        raise ValueError(f"Summary {summary_id} names unknown rules: {', '.join(missing)}")
    summary["rules"] = {rid: fingerprint(rules[rid]) for rid in summary["rules"]}


def rule_label(rule: dict) -> str:
    """Heading for a full rule: its own lead-in name ("Freshness: ..." or "Qualifying body. ...")
    when it has one of at most six words, otherwise the id in words."""
    match = re.match(r"([A-Z][A-Za-z0-9 ]{0,60}?)[.:] ", rule["text"])
    if match and len(match.group(1).split()) <= 6:
        return match.group(1)
    return rule["id"].replace("-", " ").capitalize()


RUN_IN = re.compile(r"(?<=\. )(?=[A-Z][A-Za-z()'-]*(?: [A-Za-z()'-]+){0,4}: )")  # a sentence led by a term of at most five words and a colon ("Ladder: ", "Own staff: ")

# The theme toggle of the main page's section bar, for this page alone: the same three steps and stored choice (principle: Theme).
THEME_SCRIPT = (
    '<script>(function () { var b = document.getElementById("theme-toggle"), NEXT = { system: "light", light: "dark", dark: "system" }, NAME = { system: "follows system", light: "light", dark: "dark" };'
    ' function now() { var t = document.documentElement.getAttribute("data-theme"); return t === "light" || t === "dark" ? t : "system"; }'
    ' function sync() { var t = now(); b.setAttribute("aria-label", "Theme: " + NAME[t]); b.title = "Theme: " + NAME[t] + ". Click for " + NEXT[t]; }'
    ' b.addEventListener("click", function () { var next = NEXT[now()]; if (next === "system") document.documentElement.removeAttribute("data-theme"); else document.documentElement.setAttribute("data-theme", next);'
    ' try { if (next === "system") localStorage.removeItem("aisi-theme"); else localStorage.setItem("aisi-theme", next); } catch (e) {} sync(); }); sync(); })();</script>'
)


def rule_paragraphs(text: str) -> list[str]:
    """A rule's text split before each run-in term, so "Ladder:", "Established:" and the like each start a short paragraph; joined with spaces they are the rule's text."""
    return [part.rstrip() for part in RUN_IN.split(text)]


def lead_in(part: str, label: str) -> str:
    """A rule's first paragraph under its heading: a lead-in that repeats the heading ("Freshness: the ...") stays in the text for
    screen readers and copying but is not shown, and the next word takes a capital, so the heading is not said twice (rule 1)."""
    for mark in (": ", ". "):
        if part.startswith(label + mark) and len(part) > len(label) + 2:
            rest = part[len(label) + 2:]
            return f'<span class="vh">{html.escape(label + mark)}</span><span class="cap">{html.escape(rest[0])}</span>{html.escape(rest[1:])}'
    return html.escape(part)


METHOD_TITLE = "Full method | AI Security Institutes tracker"
METHOD_DESCRIPTION = "The full editorial rules behind the AI Security Institutes tracker: what counts as an institute, how status, dates, funding and sources are recorded and checked."


def full_method_page(dataset: dict, site: str, share: str = "") -> str:
    """Static, escaped full rules. Reuse the main page's style rather than fork it. share: the page's Open Graph and Twitter tags."""
    style = re.search(r"<style>.*?</style>", site, re.S)
    theme = re.search(r'<script id="theme-boot">.*?</script>', site, re.S)  # the tracker's theme choice, before the style
    fonts = re.findall(r'<link rel="preload"[^>]+as="font"[^>]*>', site)  # the vendored fonts' @font-face rules travel in the style
    title = "Full method"
    description = html.escape(METHOD_DESCRIPTION, quote=True)
    sections = []
    for rule in dataset["method"]:
        rid = html.escape(rule["id"], quote=True)
        label = html.escape(rule_label(rule))
        sections.append(
            f'<section id="{rid}" aria-labelledby="heading-{rid}"><h2 id="heading-{rid}">{label}</h2><div class="section-body">'
            + "".join(f"<p>{lead_in(part, rule_label(rule)) if n == 0 else html.escape(part)}</p>" for n, part in enumerate(rule_paragraphs(rule["text"])))
            + "</div></section>"
        )
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{html.escape(METHOD_TITLE)}</title>"
        f'<meta name="description" content="{description}">'
        f'<meta property="og:description" content="{description}"><meta name="twitter:description" content="{description}">'
        + share
        + '<link rel="icon" href="favicon.svg" type="image/svg+xml">'
        + (theme.group() if theme else "")
        + "".join(fonts)
        + (style.group() if style else "")
        + '</head><body class="full-method"><header class="masthead">'
        # The main page's section bar: its title links back to the tracker, the theme toggle at its end.
        '<nav class="secnav gutter" aria-label="Site"><div class="measure"><a href="index.html#method-sec" class="navtitle" aria-label="Back to the tracker"><span class="long">AI Security Institutes</span><span class="short">AISIs</span></a>'
        '<button type="button" class="btn ico i-theme theme-toggle" id="theme-toggle" aria-label="Theme: follows system"></button></div></nav>'
        f'<div class="panel gutter"><div class="measure"><h1>{title}</h1></div></div></header>'
        '<main class="gutter"><div class="measure">'
        + "".join(sections)
        + '</div></main><footer class="gutter"><div class="measure" id="foot">'
        f'<p>Code: MIT; data, page text and documentation: {html.escape(dataset["license"])}</p>'
        f'<p>{html.escape(dataset["license_note"])}</p>'
        '<a href="index.html#method-sec">Back to the tracker</a></div></footer>'
        + THEME_SCRIPT
        + "</body></html>\n"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["refresh"])
    parser.add_argument(
        "summary_id", help="Summary you have reread against its full rules"
    )
    parser.add_argument(
        "--data",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "data/aisi.json",
    )
    args = parser.parse_args()
    data = json.loads(args.data.read_text(encoding="utf-8"))
    try:
        refresh(data["dataset"], args.summary_id)
    except (ValueError, KeyError) as exc:
        parser.error(str(exc))
    args.data.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"Refreshed {args.summary_id}; owner-copy approval status is unchanged.")


if __name__ == "__main__":
    main()
