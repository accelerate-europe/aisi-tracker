"""Files for agents and scripts, generated from the published dataset by build.py.

- The static per-record API (api/v<major>/), versioned by the schema major in dataset.version.
  Each record file holds the published record unchanged plus the sources it cites, so a reader
  needs one request per state or institute instead of the full aisi.json.
- One orientation text (what the data is, how to read sources, status, confidence, funding and
  partners, licence, citation), written once here and rendered into api/v<major>/index.json and
  llms.txt; the page's "Using the data" block links both.
- The schema.org Dataset JSON-LD in the page head, sitemap.xml, robots.txt and the Atom feed of news and releases (feed.xml,
  from the changelog).
- CITATION.cff and .zenodo.json, the only tracked outputs, written by build.py --update-readme, and
  the Zenodo record metadata scripts/zenodo_metadata.py sets after each release.
"""

from __future__ import annotations

import datetime as dt
import html
import json
import re
from urllib.parse import urlsplit

# Every absolute site URL the build writes derives from this address, ending in "/".
PUBLIC_URL = "https://aisi.accelerate-europe.org/"
SITE_NAME = "AI Security Institutes tracker"
SOCIAL_IMAGE = "social-preview.png"

# Licence names in dataset.license as Frictionless licence objects (datapackage.json uses them too).
LICENSES = {"CC BY 4.0": {"name": "CC-BY-4.0", "path": "https://creativecommons.org/licenses/by/4.0/", "title": "Creative Commons Attribution 4.0"}}


def public_url_errors(url: str | None = None) -> list[str]:
    url = PUBLIC_URL if url is None else url
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.hostname or not url.endswith("/"):
        return [f"PUBLIC_URL {url!r} in scripts/agent_files.py must be an https address ending in /"]
    if parts.hostname.endswith(".invalid"):
        return [f"PUBLIC_URL in scripts/agent_files.py is still the placeholder {url!r}; set the public site address before release"]
    return []


def doi_url(doi: str) -> str:
    """The resolvable link for a DOI, as the citation, page and metadata files give it."""
    return "https://doi.org/" + doi


SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


def api_major(published: dict) -> str:
    """The schema major: the first part of dataset.version (major = breaking schema change)."""
    return published["dataset"]["version"].split(".")[0]


def api_root(published: dict) -> str:
    return f"api/v{api_major(published)}"


def cited_ids(node) -> set[str]:
    """Every source ID a record names: any *source_ids list and the per-field citations."""
    found: set[str] = set()
    if isinstance(node, dict):
        for key, value in node.items():
            if key.endswith("source_ids") and isinstance(value, list):
                found.update(value)
            elif key == "citations" and isinstance(value, dict):
                found.update(s for ids in value.values() for s in ids)
            else:
                found |= cited_ids(value)
    elif isinstance(node, list):
        for value in node:
            found |= cited_ids(value)
    return found


def inside(pointer: str, prefix: str) -> bool:
    return pointer == prefix or pointer.startswith(prefix + "/")


def record_sources(published: dict, parts: list[tuple[str, str, dict]]) -> list[dict]:
    """The sources the records in one file cite, with the excerpts that support a value inside them.

    `parts` pairs each record's aisi.json pointer with its pointer in this file, as
    (`/jurisdictions/36`, `/record`, record). Excerpt `supports` pointers inside a record are
    rewritten to the file (`/jurisdictions/36/status` to `/record/status`); pointers outside every
    record are dropped. Sources are named by id, address and quotations only, never by their private
    copies. Sorted by ID.
    """
    ids = set().union(*(cited_ids(record) for _, _, record in parts))
    sources = []
    for source in published["sources"]:
        excerpts = []
        for excerpt in source.get("excerpts", []):
            supports = [target + p[len(prefix):] for p in excerpt["supports"] for prefix, target, _ in parts if inside(p, prefix)]
            if supports:
                excerpts.append({k: v for k, v in excerpt.items() if k != "supports"} | {"supports": supports})
        if source["id"] in ids or excerpts:
            entry = {k: source[k] for k in ("id", "title", "title_en", "url", "archive_url") if k in source}
            sources.append(entry | {"excerpts": excerpts})
    return sorted(sources, key=lambda s: s["id"])


def record_file(published: dict, kind: str, index: int) -> dict:
    """One record file. A jurisdiction file also embeds its institute (null when it has none), so one
    download holds everything on a state; its excerpt pointers resolve under /institute/record."""
    record = published[kind][index]
    prefix = f"/{kind}/{index}"
    dataset = published["dataset"]
    result = {
        "dataset": {"name": dataset["name"], "version": dataset["version"], "as_of": dataset["as_of"],
                    "license": dataset["license"], "license_note": dataset["license_note"], "citation": dataset["citation"]}
        | ({"doi": dataset["doi"]} if dataset.get("doi") else {}),
        "aisi_json_pointer": prefix,
        "record": record,
    }
    parts = [(prefix, "/record", record)]
    if kind == "jurisdictions":
        result["institute"] = None
        if record.get("institute_id"):
            matches = [n for n, i in enumerate(published["institutes"]) if i["id"] == record["institute_id"]]
            if len(matches) != 1:
                raise ValueError(f"jurisdiction {record['id']}: institute {record['institute_id']!r} matches {len(matches)} records")
            pointer = f"/institutes/{matches[0]}"
            institute = published["institutes"][matches[0]]
            result["institute"] = {"aisi_json_pointer": pointer, "record": institute}
            parts.append((pointer, "/institute/record", institute))
    result["sources"] = record_sources(published, parts)
    return result


def compact_jurisdiction(j: dict) -> dict:
    return {"id": j["id"], "name": j["name"], "status": j["status"], "status_since": j.get("status_since"),
            "institute": j.get("institute_id")}


JURISDICTION_IDS = "ISO 3166-1 alpha-2 code, EU for the European Union"
API_FILES = {
    "index.json": "This index: files, URL patterns and how to read the records.",
    "jurisdictions.json": "Every jurisdiction in one compact list: id, name, status, status_since and AISI id (institute).",
    "jurisdictions/{id}.json": f"Everything on one jurisdiction ({JURISDICTION_IDS}): its record as published in the full dataset, its AISI's record under institute (null when there is no AISI), and the sources both cite with their supporting excerpts.",
    "institutes/{id}.json": "One AISI as published in the full dataset, with the sources it cites and their supporting excerpts.",
}


def method_rule(dataset: dict, rule_id: str) -> str:
    """One dataset.method rule, quoted from its home with its name dropped and its first letter capitalised."""
    rules = [r["text"] for r in dataset["method"] if r["id"] == rule_id]
    if len(rules) != 1:
        raise ValueError(f"dataset.method needs exactly one {rule_id} rule for the orientation text")
    lead_in = re.match(r"[A-Z][^:.]{0,60}: ", rules[0])
    if not lead_in:
        raise ValueError(f"dataset.method rule {rule_id} must open with its name and a colon for the orientation text")
    rule = rules[0][lead_in.end():]
    return rule[0].upper() + rule[1:]


def orientation(published: dict) -> list[tuple[str, str]]:
    """The one orientation text as (heading, text) pairs; `backticks` mark file paths and field names."""
    dataset = published["dataset"]
    statuses = list(dataset["status_definitions"])
    licence = LICENSES[dataset["license"]]
    as_of = dt.date.fromisoformat(dataset["as_of"])
    return [
        ("What this is", f"{dataset['description']} Published by {dataset['publisher']}; version {dataset['version']}, data as of {as_of.day} {as_of:%B %Y}."),
        ("What's in the data",
         "- Jurisdiction (`jurisdictions`): the AISI status of one of the 193 UN member states, Taiwan or the EU.\n"
         f"- AISI (`institutes`): a body that meets the definition below, or a named body planned or formally proposed for that role. {dataset['definition']}\n"
         "- Related body (a jurisdiction's `related_bodies`): a body that could be mistaken for an AISI, such as an AI Act authority. Its record says why it does not qualify.\n"
         "- Output (an AISI's `outputs`): a report, joint test, guidance, evaluation, tool or dataset credited to the AISI or a predecessor. Joint work appears under each of the involved AISIs.\n"
         "- Agreement (an AISI's `agreements`): an agreement that names the AISI as a party or implementer. A company's pledge to give the AISI model access is listed too. Each is signed, announced or pledged (`state`).\n"
         "- Network milestone (`dataset.network_milestones`): an event of the international network of AISIs, such as its founding or a joint testing exercise. It does not by itself show which members took part.\n"
         "- Source (`sources`): a document we cite, with the passages we quote from it. Each quoted passage names the facts it supports (`supports`).\n"
         "- Claim (a jurisdiction's or an AISI's `claims`): one sentence with the one quoted passage that supports it, and a code where a code fits.\n\n"
         "Jurisdiction and AISI IDs never change. New codes in `dataset.vocabularies` can appear in a minor version; removing or renaming one is a major version, "
         "except fields and vocabularies listed in `dataset.provisional`."),
        ("Sources and dates",
         "The sources behind each field are listed by ID in `citations`; an empty list means we have no quoted passage for that field. "
         "Each source has a title, its address (`url`), a copy saved in a web archive where one exists (`archive_url`) and the passages we quote from it (`excerpts`), "
         "each with an English translation when the source is in another language (`gloss`). The data date is `dataset.as_of`. "
         "To let others check an answer drawn from this data, give the data date and each source's title and address. "
         "Each jurisdiction and AISI records the date of its last full re-check (`check.last_checked`; see the freshness rule in `dataset.method`). "
         "Where `check.last_checked_basis` is historical_upper_bound, the last check was on or before that date."),
        ("Status",
         f"A jurisdiction's status (`status`) is one of {', '.join(statuses[:-1])} or {statuses[-1]}, defined in `dataset.status_definitions`. "
         "Where recorded, the date the current status began (`status_since`) is as precise as its source (a year, a month or a day)."),
        ("Searches that found nothing",
         "These four values mean our searches found nothing. They do not show that nothing exists.\n\n"
         "- None identified (`none_identified`): no current qualifying government step towards an AISI.\n"
         "- Not published (`not_published`): no published information for that field.\n"
         "- None found (`none_found`): no matching evidence for that field, such as model access.\n"
         "- None disclosed (a funding entry of `kind` none_disclosed): no published budget.\n\n"
         f"{method_rule(dataset, 'missing-values')} Some fields give null, omission or an empty list another meaning, such as not applicable; their schema descriptions say which."),
        ("Confidence",
         "We rate the evidence as high, medium or low. Every jurisdiction's status has a rating (`status_confidence`), set by the status confidence rule in `dataset.method`. "
         "Funding amounts, staff counts and leadership entries have one where recorded (`confidence`); the schema defines its levels."),
        ("Funding and staff",
         "An AISI's funding (`funding`) and staff (`staff`) lists include every recorded figure, including historical figures and proposals, so the first or last entry is not necessarily the current one. "
         "The `display` fields pick the current figures, the ones the page shows, by ID (null when none qualifies):\n\n"
         "- `display.budget`: the funding entry shown as the budget\n"
         "- `display.comparison_funding`: the annual funding entry used to compare AISIs; its euro estimate is `eur_per_year_est`\n"
         "- `display.staff`: the staff entry shown on the page\n"
         "- `display.total_staff` and `display.technical_staff`: the latest total and technical staff counts\n\n"
         "Not every amount is the AISI's own adopted budget. A figure can be another body's or a wider programme's money (`coverage`: not_institute_specific), "
         "only part of the budget (`coverage`: component) or proposed rather than adopted (`commitment`: proposed)."),
        ("Leadership and national partners",
         "An AISI's head (`head`), the ministry-level body it reports to (`reports_to`) and the national bodies it works with (`national_partners`) each have a state: stated, not published or not applicable (`state`). "
         "The head's name and title are in the AISI's list of named leaders, such as directors and their deputies (`people`, linked by `head.person_id`). "
         "Each partner's relation is member (a constituent of a federated AISI), works with, or planned (`relation`). "
         "Only the national cyber security agency, marked `cyber_agency`, is searched for at every AISI, so a missing partner of another kind does not mean there is none."),
        ("Functions",
         "An AISI's functions (`functions`) give each current (not retired) code in `dataset.vocabularies.function` once, in that order, with a value (`value`): "
         "mandate (a text assigns it), activity (a text shows it done), both, not_found (checked, no claim found) or not_coded (not yet checked against that code). "
         "The claims behind a value are listed by ID (`claim_ids`). A function claim on a jurisdiction is about the related body its `body_id` names. "
         "The functions and their code list are provisional (`dataset.provisional`): codes may be added, split, renamed or removed in a minor version, "
         "and a retired code stays in the list with `deprecated` and `replaced_by`."),
        ("Licence", f"Code: MIT (`LICENSE`). Data, page text and documentation: {dataset['license']} "
         f"(`LICENSE-DATA`; {licence['title']}, {licence['path']}), with attribution. {dataset['license_note']}"),
        ("Cite as", dataset["citation"]),
    ]


def plain(text: str) -> str:
    return text.replace("`", "")


def definition_markdown(dataset: dict) -> str:
    """The README's definition block: the dataset's definition, as on the page, with its link to the full rule."""
    return f"{dataset['definition']} [What counts]({PUBLIC_URL}method.html#qualifying-body)"


def orientation_html(published: dict) -> str:
    """The page's "Using the data": one muted line after the downloads linking llms.txt and the API index (the full text is in llms.txt and the API index)."""
    api = api_root(published)
    return ('<p class="small" id="using-data">Scripts and AI agents: <a href="llms.txt">llms.txt</a> '
            f'and the <a href="{html.escape(api)}/index.json">API index</a></p>')


def llms_txt(published: dict) -> str:
    """llms.txt (llmstxt.org): title, summary, the orientation, then absolute links to the key files."""
    dataset = published["dataset"]
    api = api_root(published)
    # The per-record examples, by key: the UK and its AISI; a build without them lists the patterns unlinked.
    uk = [j for j in published["jurisdictions"] if j["id"] == "GB"]
    gb = uk[0]["id"] if len(uk) == 1 else None
    gb_aisi = uk[0].get("institute_id") if gb else None
    # (name, path, example, description); a URL pattern links its example file, or nothing without one.
    links = [
        ("Full dataset", "data/aisi.json", None, "every jurisdiction, AISI and source in one file"),
        ("Schema", "data/aisi.schema.json", None, "what each field means"),
        ("Data package", "data/datapackage.json", None, "every download, including one CSV per table, with column descriptions"),
        ("Jurisdictions list", f"{api}/jurisdictions.json", None, "each jurisdiction's ID, name, status, status date and AISI ID"),
        ("One jurisdiction", f"{api}/jurisdictions/{{id}}.json", gb and f"{gb}.json",
         "the ID is the two-letter ISO 3166-1 code (GB for the UK, GR for Greece) or EU. Holds the jurisdiction (`record`), its AISI where there is one "
         "(`institute.record`, otherwise `institute` is null) and the sources both cite (`sources`)."),
        ("One AISI", f"{api}/institutes/{{id}}.json", gb_aisi and f"{gb_aisi}.json", "the ID is in the jurisdictions list. Holds the AISI and the sources it cites."),
        ("API index", f"{api}/index.json", None, "file locations, URL patterns, AISI IDs, status definitions and this text, as JSON"),
        ("Page", "./", None, "the tracker page"),
    ]
    items = []
    for name, path, example, text in links:
        if "{id}" in path and not example:
            items.append(f"- {name} (`{path}`): {text}")
        elif example:
            items.append(f"- [{name}]({PUBLIC_URL}{path.replace('{id}.json', example)}) (`{path}`, for example `{example}`): {text}")
        else:
            items.append(f"- [{name}]({PUBLIC_URL}{path.removeprefix('./')}) (`{path}`): {text}")
    sections = orientation(published)
    body = [f"# {dataset['title']}", "", f"> {sections[0][1]}", ""]
    for heading, text in sections[1:]:
        body += [f"## {heading}", "", text, ""]
    body += ["## Files", ""] + items
    return "\n".join(body) + "\n"


def share_tags(title: str, path: str, image_alt: str) -> str:
    """The Open Graph and Twitter tags every page shares (the pages add their own descriptions): title, site name,
    address and the preview image, for the page at PUBLIC_URL + path."""
    def e(text: str) -> str:
        return html.escape(text, quote=True)

    url, image = e(PUBLIC_URL + path), e(PUBLIC_URL + SOCIAL_IMAGE)
    return (f'<meta property="og:title" content="{e(title)}">\n'
            '<meta property="og:type" content="website">\n'
            f'<meta property="og:site_name" content="{e(SITE_NAME)}">\n'
            f'<meta property="og:url" content="{url}">\n'
            f'<meta property="og:image" content="{image}">\n'
            '<meta property="og:image:width" content="1200">\n'
            '<meta property="og:image:height" content="630">\n'
            f'<meta property="og:image:alt" content="{e(image_alt)}">\n'
            '<meta name="twitter:card" content="summary_large_image">\n'
            f'<meta name="twitter:title" content="{e(title)}">\n'
            f'<meta name="twitter:image" content="{image}">')


def image_alt_text(published: dict, image_title: str) -> str:
    """What social-preview.png says (scripts/social_preview.py): the page's heading, publisher and credits."""
    dataset = published["dataset"]
    credit = ", ".join(p["name"] for p in dataset["maintainers"])
    return f"{image_title}. {dataset['publisher']}, compiled by {credit}."


def head_links(published: dict, page_title: str, image_title: str | None = None) -> str:
    """The index page's head insertions: canonical address, share tags (titled page_title; the preview image shows
    image_title, the page's heading) and the alternate links."""
    api = api_root(published)
    url = html.escape(PUBLIC_URL, quote=True)
    return (f'<link rel="canonical" href="{url}">\n'
            + share_tags(page_title, "", image_alt_text(published, image_title or page_title)) + "\n"
            f'<link rel="alternate" type="application/json" href="{api}/index.json" title="Per-record API">\n'
            '<link rel="alternate" type="application/json" href="data/datapackage.json" title="Data package">\n'
            f'<link rel="alternate" type="application/atom+xml" href="{FEED_PATH}" title="News and new versions (Atom feed)">')


def json_ld(published: dict, resources: list[dict]) -> dict:
    """schema.org Dataset for search engines, from the dataset metadata and the datapackage resources."""
    dataset = published["dataset"]
    dates = [e["date"] for j in published["jurisdictions"] for e in j.get("timeline", [])]
    dates += [m["date"] for m in dataset.get("network_milestones", [])]
    publisher = {"@type": "Organization", "name": dataset["publisher"]}
    ld = {
        "@context": "https://schema.org",
        "@type": "Dataset",
        "name": dataset["title"],
        "alternateName": dataset["name"],
        "description": dataset["description"],
        "url": PUBLIC_URL,
        **({"identifier": doi_url(dataset["doi"])} if dataset.get("doi") else {}),
        "version": dataset["version"],
        "dateModified": dataset["as_of"],
        "license": LICENSES[dataset["license"]]["path"],
        "isAccessibleForFree": True,
        "creator": [publisher] + [{"@type": "Person", "name": p["name"]} for p in dataset["maintainers"]],
        "publisher": publisher,
        "spatialCoverage": {"@type": "Place", "name": "World", "description": dataset["scope"]},
        "distribution": [{"@type": "DataDownload", "name": r["title"], "description": r["description"],
                          "encodingFormat": r["mediatype"], "contentUrl": f"{PUBLIC_URL}data/{r['path']}"} for r in resources],
    }
    if dates:  # from the earliest dated event to the data date; partial dates keep their precision
        ld["temporalCoverage"] = f"{min(dates)}/{dataset['as_of']}"
    return ld


def json_ld_script(published: dict, resources: list[dict]) -> str:
    # "<" inside a script element could end it early or open a comment; JSON allows the escape.
    body = json.dumps(json_ld(published, resources), ensure_ascii=False, indent=1).replace("<", "\\u003c")
    return f'<script type="application/ld+json">\n{body}\n</script>'


# Crawlers that read pages for AI search, assistants or training, named so the welcome is explicit.
AI_CRAWLERS = (
    "GPTBot", "OAI-SearchBot", "ChatGPT-User", "ClaudeBot", "Claude-SearchBot", "Claude-User", "anthropic-ai",
    "PerplexityBot", "Perplexity-User", "Google-Extended", "Applebot-Extended", "CCBot", "meta-externalagent",
    "Amazonbot", "DuckAssistBot", "MistralAI-User", "cohere-ai",
)


def robots_txt() -> str:
    """Every crawler may read everything; AI crawlers are named explicitly. Crawlers read it only at a host's root."""
    groups = [f"User-agent: {agent}\nAllow: /" for agent in AI_CRAWLERS] + ["User-agent: *\nAllow: /"]
    return "\n\n".join(groups) + f"\n\nSitemap: {PUBLIC_URL}sitemap.xml\n"


def sitemap_xml(published: dict, paths: list[str]) -> str:
    """The HTML pages among the built files (the page at its directory URL, method.html, the API landing page) except
    the noindex error page, dated as_of, sorted. Fonts, scripts, data and licences are not pages."""
    as_of = published["dataset"]["as_of"]
    pages = sorted({path.removesuffix("index.html") for path in paths if (path == "" or path.endswith(".html")) and path != "404.html"})
    entries = "".join(f"<url><loc>{html.escape(PUBLIC_URL + path)}</loc><lastmod>{as_of}</lastmod></url>\n" for path in pages)
    return f'<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n{entries}</urlset>\n'


FEED_PATH = "feed.xml"


# Atom ids are permanent (RFC 4287 4.2.6): a tag URI minted once, independent of PUBLIC_URL, so a move of the site
# or an edit of the text never makes a feed reader show a release twice.
FEED_ID = "tag:aisi.accelerate-europe.org,2026:feed"


def feed_entry_id(version: str) -> str:
    """A release's permanent Atom id, fixed by its version."""
    return f"tag:aisi.accelerate-europe.org,2026:release/{version}"


def feed_news_id(development: dict) -> str:
    """A News item's permanent Atom id, from the key that groups it (scripts/display.py recent_developments): the state
    and a full date, or for a month or year date the state, the date and its permanent event id. A later event on the
    same day joins the item and keeps its id; a corrected date makes a new item."""
    tail = development["date"] if len(development["date"]) == 10 else f"{development['date']}/{development['order']}"
    return f"tag:aisi.accelerate-europe.org,2026:news/{development['jurisdiction']}/{tail}"


def feed_day(date: str) -> str:
    """An Atom timestamp for a full or partial date: a month or a year counts from its first day."""
    return {10: date, 7: date + "-01", 4: date + "-01-01"}[len(date)] + "T00:00:00Z"


def feed_entries(published: dict) -> list[dict]:
    """The feed's entries, newest first: one per release (the changelog) and one per News item
    (dataset.display.developments, as the page's News section shows them). Ties: News items first, then by id."""
    esc = lambda text: html.escape(text, quote=True)  # noqa: E731
    sources = {x["id"]: x for x in published.get("sources", [])}
    entries = []
    for c in published["changelog"]:
        content = (f"<p>{esc(c['summary'])}</p>" if c.get("summary") else "") + "<ul>" + "".join(f"<li>{esc(x)}</li>" for x in c["changes"]) + "</ul>"
        entries.append({"kind": "release", "id": feed_entry_id(c["version"]), "title": f"Version {c['version']}", "updated": feed_day(c["date"]),
                        "link": PUBLIC_URL + "#changes", "summary": c.get("summary"), "content": content, "key": [int(p) for p in c["version"].split(".")]})
    for g in published["dataset"].get("display", {}).get("developments", []):
        text = " ".join(t for t in g["texts"] if t)
        cited = [sources[i] for i in g.get("citations", {}).get("display", []) if i in sources]
        content = f"<p>{esc(text)}</p>" + ("<p>Sources: " + "; ".join(f'<a href="{esc(x["url"])}">{esc(x["title"])}</a>' for x in cited if x.get("url")) + "</p>" if cited else "")
        entries.append({"kind": "news", "id": feed_news_id(g), "title": f"{g['name']}: {text}", "updated": feed_day(g["date"]),
                        "link": PUBLIC_URL + "#c-" + g["jurisdiction"].lower(), "summary": text, "content": content, "key": []})
    entries.sort(key=lambda e: (e["updated"], e["kind"] == "news", e["key"], e["id"]), reverse=True)
    return entries


def feed_xml(published: dict) -> str:
    """The Atom feed of news and releases: one entry per News item and one per changelog entry, newest first, ids permanent."""
    dataset = published["dataset"]
    esc = lambda text: html.escape(text, quote=True)  # noqa: E731
    entries = feed_entries(published)
    body = "".join(
        "<entry>\n"
        f"<id>{e['id']}</id>\n"
        f"<title>{esc(e['title'])}</title>\n"
        f"<updated>{e['updated']}</updated>\n"
        f'<link rel="alternate" type="text/html" href="{esc(e["link"])}"/>\n'
        + (f"<summary>{esc(e['summary'])}</summary>\n" if e.get("summary") else "")
        + f'<content type="html">{esc(e["content"])}</content>\n'
        "</entry>\n" for e in entries)
    return ('<?xml version="1.0" encoding="utf-8"?>\n<feed xmlns="http://www.w3.org/2005/Atom">\n'
            f"<id>{FEED_ID}</id>\n"
            f"<title>{esc(dataset['title'])}: news and releases</title>\n"
            f"<updated>{max(e['updated'] for e in entries)}</updated>\n"
            f"<author><name>{esc(dataset['publisher'])}</name></author>\n"
            f'<link rel="self" type="application/atom+xml" href="{esc(PUBLIC_URL + FEED_PATH)}"/>\n'
            f'<link rel="alternate" type="text/html" href="{esc(PUBLIC_URL)}"/>\n'
            f"<rights>{esc(dataset['license'])}</rights>\n"
            + body + "</feed>\n")


CFF_HEADER = "# Generated from data/aisi.json by scripts/build.py --update-readme; do not edit.\n"


def citation_message(dataset: dict) -> str:
    """The licence sentence and the canonical citation: the CFF message and the Zenodo notes."""
    return f"Data, page text and documentation: {dataset['license']} (LICENSE-DATA). Code: MIT (LICENSE). {dataset['license_note']} If you use this dataset, please cite it as: {dataset['citation']}"


def split_name(name: str) -> tuple[str, str]:
    """Given names and family name: the family name is the last word."""
    given, _, family = name.rpartition(" ")
    return given, family


def citation_cff(published: dict) -> str:
    """CITATION.cff (type dataset) from the dataset citation metadata: the publisher as an entity
    author and each maintainer as a person, the canonical citation verbatim."""
    import yaml  # build.py's dependency; other scripts import this module without it

    dataset = published["dataset"]
    people = []
    for maintainer in dataset["maintainers"]:
        given, family = split_name(maintainer["name"])
        people.append(({"given-names": given} if given else {}) | {"family-names": family}
                      | ({"affiliation": maintainer["affiliation"]} if maintainer.get("affiliation") else {})
                      | ({"orcid": "https://orcid.org/" + maintainer["orcid"]} if maintainer.get("orcid") else {}))
    cff = {
        "cff-version": "1.2.0",
        "message": citation_message(dataset),
        "type": "dataset",
        "title": dataset["title"],
        "abstract": dataset["description"],
        "version": dataset["version"],
        # The concept DOI, which always resolves to the latest version (Zenodo mints a version DOI per release).
        **({"doi": dataset["doi"], "identifiers": [{"type": "doi", "value": dataset["doi"], "description": "Concept DOI; resolves to the latest version"}]} if dataset.get("doi") else {}),
        # The current version's changelog date: build.py checks the last entry is this version, and
        # check_metadata that its date equals as_of, which release day sets to the release date.
        "date-released": published["changelog"][-1]["date"],
        "authors": [{"name": dataset["publisher"]}, *people],
        **({"contact": [{"name": dataset["publisher"], "email": dataset["contact"]}]} if dataset.get("contact") else {}),
        # One SPDX id: Zenodo's GitHub import rejects a list ("Citation metadata load failed"). The code's MIT licence is named in the message and LICENSE.
        "license": LICENSES[dataset["license"]]["name"],
        "url": PUBLIC_URL,
        **({"repository-code": dataset["repository"]} if dataset.get("repository") else {}),
    }
    return CFF_HEADER + yaml.safe_dump(cff, sort_keys=False, allow_unicode=True, width=1000)


ZENODO_KEYWORDS = ["AI Security Institute", "AI Safety Institute", "AISI", "AI governance", "AI policy", "AI safety", "government institutions"]


def zenodo_related(dataset: dict, version: str) -> list[tuple[str, str, str]]:
    """Related identifiers of a Zenodo version as (relation, URL, resource type): the page and,
    with a repository, the release tag v<version> (e.g. v1.0.0)."""
    related = [("isDocumentedBy", PUBLIC_URL, "other")]
    if dataset.get("repository"):
        related.append(("isSupplementTo", f"{dataset['repository']}/tree/v{version}", "software"))
    return related


def zenodo_json(published: dict) -> str:
    """.zenodo.json (Zenodo's legacy deposit metadata), which Zenodo reads in place of CITATION.cff
    when archiving a GitHub release. Same dataset fields as citation_cff, so the two cannot drift.
    Creators are persons only (Zenodo's loader treats every creator as a person); the publisher is
    the imprint, and scripts/zenodo_metadata.py adds it as the first creator after release
    (zenodo_rdm_metadata). No publication_date: Zenodo takes the release date."""
    dataset = published["dataset"]
    creators = []
    for maintainer in dataset["maintainers"]:
        given, family = split_name(maintainer["name"])
        creators.append({"name": f"{family}, {given}" if given else family}
                        | ({"affiliation": maintainer["affiliation"]} if maintainer.get("affiliation") else {})
                        | ({"orcid": maintainer["orcid"]} if maintainer.get("orcid") else {}))
    related = [{"relation": relation, "identifier": identifier, "resource_type": kind}
               for relation, identifier, kind in zenodo_related(dataset, dataset["version"])]
    deposit = {
        "upload_type": "dataset",
        "title": dataset["title"],
        "version": dataset["version"],
        "description": dataset["description"],
        "notes": citation_message(dataset),
        "creators": creators,
        "imprint_publisher": dataset["publisher"],
        # One SPDX id: a list makes Zenodo fail the release.
        "license": LICENSES[dataset["license"]]["name"],
        "keywords": ZENODO_KEYWORDS,
        "related_identifiers": related,
    }
    return json.dumps(deposit, indent=2, ensure_ascii=False) + "\n"


# Rights on the Zenodo record, as Zenodo licence vocabulary ids: the data licence, then MIT for the
# code (named in citation_message). The legacy .zenodo.json can carry only the first.
CODE_LICENSE_ID = "mit"


def zenodo_rdm_metadata(dataset: dict, version: str) -> dict:
    """The fields scripts/zenodo_metadata.py sets on a Zenodo version after release, in Zenodo's
    InvenioRDM record shape: what .zenodo.json cannot express (the publisher as an organisational
    creator, so the record's citation matches the canonical one, and a second licence for the code)
    plus the other fields .zenodo.json sets from the same dataset fields. Vocabulary values carry
    only their id. Title, description and notes are left to the GitHub import."""
    creators = [{"person_or_org": {"type": "organizational", "name": dataset["publisher"]}}]
    for maintainer in dataset["maintainers"]:
        given, family = split_name(maintainer["name"])
        person = {"type": "personal", "family_name": family} | ({"given_name": given} if given else {})
        if maintainer.get("orcid"):
            person["identifiers"] = [{"scheme": "orcid", "identifier": maintainer["orcid"]}]
        creators.append({"person_or_org": person} | ({"affiliations": [{"name": maintainer["affiliation"]}]} if maintainer.get("affiliation") else {}))
    return {
        "resource_type": {"id": "dataset"},
        "creators": creators,
        "publisher": dataset["publisher"],
        "version": version,
        "rights": [{"id": LICENSES[dataset["license"]]["name"].lower()}, {"id": CODE_LICENSE_ID}],
        "subjects": [{"subject": keyword} for keyword in ZENODO_KEYWORDS],
        "related_identifiers": [{"identifier": identifier, "scheme": "url", "relation_type": {"id": relation.lower()}, "resource_type": {"id": kind}}
                                for relation, identifier, kind in zenodo_related(dataset, version)],
    }


def api_index(published: dict) -> dict:
    dataset = published["dataset"]
    root = api_root(published)
    return {
        "api_version": api_major(published),
        "dataset": {"name": dataset["name"], "title": dataset["title"], "version": dataset["version"],
                    "as_of": dataset["as_of"], "license": dataset["license"], "license_note": dataset["license_note"], "citation": dataset["citation"]}
        | ({"doi": dataset["doi"]} if dataset.get("doi") else {}),
        "versioning": f"{root}/ follows the major of dataset.version, which changes only with a breaking schema change; a breaking change will get a new path, and this one is to stay online unchanged.",
        "files": [{"path": path, "description": text} for path, text in API_FILES.items()],
        "institute_ids": [i["id"] for i in published["institutes"]],
        "full_dataset": {"path": "../../data/aisi.json", "schema": "../../data/aisi.schema.json", "datapackage": "../../data/datapackage.json"},
        "notes": [
            "Paths are relative to this file.",
            "record is the record exactly as in the full dataset (full_dataset.path) at aisi_json_pointer; field meanings are in full_dataset.schema; institute in jurisdictions.json is record.institute_id.",
            "record.citations lists, per field, the source IDs that support it; sources gives each cited source with its quotations, and each excerpt's supports are JSON pointers into this file (under /record, or /institute/record for the institute a jurisdiction file embeds).",
        ],
        "orientation": [{"heading": h, "text": t} for h, t in orientation(published)],
        "status_definitions": dataset["status_definitions"],
    }


def api_landing_html(published: dict) -> str:
    """api/v<major>/index.html: a static host serves this for the bare folder address, so the API
    has a human-readable entry point linking the same files as index.json."""
    dataset = published["dataset"]
    # The first record of each kind as the example link; a kind with no records gets none.
    example = {kind: published[kind][0]["id"] for kind in ("jurisdictions", "institutes") if published[kind]}
    items = []
    for path, text in API_FILES.items():
        kind = path.split("/")[0]
        if path == "index.json":  # API_FILES describes it from inside the JSON ("This index")
            text = "The same overview as JSON: files, URL patterns and how to read the records."
        # One shape for every item: the path in code (linked when it is a file), for a URL pattern
        # ", for example" and a linked example when there is a record, then ": " and the description.
        code = f"<code>{html.escape(path)}</code>"
        if "{id}" not in path:
            head = f'<a href="{html.escape(path)}">{code}</a>'
        elif kind in example:
            href = html.escape(path.replace("{id}", example[kind]))
            head = f'{code}, for example <a href="{href}"><code>{href}</code></a>'
        else:
            head = code
        items.append(f"<li>{head}: {html.escape(text)}</li>")
    title = html.escape(dataset["title"])
    listing = "\n      ".join(items)
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>API v{api_major(published)} | {title}</title>
  <link rel="icon" type="image/svg+xml" href="../../favicon.svg">
  <script>try {{ var t = localStorage.getItem("aisi-theme"); if (t === "dark" || t === "light") document.documentElement.setAttribute("data-theme", t); }} catch (e) {{}}</script>
  <style>
    /* Theme as on the tracker: the system scheme unless the reader chose light or dark there. */
    :root {{ color-scheme: light; --ground: #FAF7F2; --ink: #1A1A1A; --link: #8F5A23; --code: #F0EBE3; }}
    @media (prefers-color-scheme: dark) {{ :root:not([data-theme="light"]) {{ color-scheme: dark; --ground: #17191A; --ink: #F3F0EB; --link: #E9AA79; --code: #26292B; }} }}
    :root[data-theme="dark"] {{ color-scheme: dark; --ground: #17191A; --ink: #F3F0EB; --link: #E9AA79; --code: #26292B; }}
    body {{ margin: 0; background: var(--ground); color: var(--ink); font: 1rem/1.5 "Segoe UI", Arial, sans-serif; }}
    main {{ max-width: 42rem; margin: 8vh auto; padding: 1.5rem; overflow-wrap: anywhere; }}
    a {{ color: var(--link); text-underline-offset: .2em; }}
    a:focus-visible {{ outline: 2px solid var(--link); outline-offset: 4px; }}
    li {{ margin-block: .75rem; }}
    code {{ font: .875em/1.4 ui-monospace, "SF Mono", Menlo, Consolas, monospace; background: var(--code); padding: .1em .3em; border-radius: 3px; }}
  </style>
</head>
<body>
  <main>
    <p><a href="../../">{title}</a></p>
    <h1>API v{api_major(published)}</h1>
    <p>Static JSON files, version {html.escape(dataset["version"])}, data as of {html.escape(dataset["as_of"])}. Each record file holds the record as in the full dataset with the sources it cites.</p>
    <ul>
      {listing}
    </ul>
    <p>The full dataset is <a href="../../data/aisi.json">data/aisi.json</a>. For how to read the records, see <a href="../../llms.txt">llms.txt</a>.</p>
  </main>
</body>
</html>
"""


def api_files(published: dict) -> dict[str, dict]:
    """Every API file by path relative to the site root."""
    root = api_root(published)
    index = api_index(published)
    files = {f"{root}/index.json": index,
             f"{root}/jurisdictions.json": {"dataset": index["dataset"],
                                            "jurisdictions": [compact_jurisdiction(j) for j in published["jurisdictions"]]}}
    for kind in ("jurisdictions", "institutes"):
        for index, record in enumerate(published[kind]):
            if not SAFE_ID.fullmatch(record["id"]):
                raise ValueError(f"{kind} id {record['id']!r} is not a safe file name")
            files[f"{root}/{kind}/{record['id']}.json"] = record_file(published, kind, index)
    return files
