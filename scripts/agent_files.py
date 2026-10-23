"""Files for agents and scripts, generated from the published dataset by build.py.

- The static per-record API (api/v<major>/), versioned by the schema major in dataset.version.
  Each record file holds the published record unchanged plus the sources it cites, so a reader
  needs one request per state or institute instead of the full aisi.json.
- One orientation text (what the data is, files and URL patterns, how to read status and
  confidence, licence, citation), written once here and rendered into the page's "Using the
  data" block, the README section "For agents and scripts", api/v<major>/index.json and llms.txt.
- The schema.org Dataset JSON-LD in the page head, sitemap.xml and robots.txt.
- CITATION.cff and .zenodo.json, the only tracked outputs, written by build.py --update-readme.
"""

from __future__ import annotations

import html
import json
import re
from urllib.parse import urlsplit

# Every absolute site URL the build writes derives from this address, ending in "/".
PUBLIC_URL = "https://aisi.accelerate-europe.org/"

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
    "jurisdictions.json": "Every jurisdiction in one compact list: id, name, status, status_since and institute id.",
    "jurisdictions/{id}.json": f"Everything on one jurisdiction ({JURISDICTION_IDS}): its record as published in the full dataset, its institute's record under institute (null when it has none), and the sources both cite with their supporting excerpts.",
    "institutes/{id}.json": "One institute as published in the full dataset, with the sources it cites and their supporting excerpts.",
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
    api = api_root(published)
    statuses = list(dataset["status_definitions"])
    licence = LICENSES[dataset["license"]]
    return [
        ("What this is", f"{dataset['description']} Published by {dataset['publisher']}; version {dataset['version']}, data as of {dataset['as_of']}."),
        ("Files and addresses",
         "On the published site, the full dataset is `data/aisi.json`, every record with its sources and check dates; `data/aisi.schema.json` defines every field, "
         "and `data/datapackage.json` lists every file, including one CSV per table. "
         f"For one state, `{api}/jurisdictions/{{id}}.json` ({JURISDICTION_IDS}) holds its record and its institute's with the sources they cite; "
         f"for one institute, `{api}/institutes/{{id}}.json` holds the record with its sources; `{api}/jurisdictions.json` lists every jurisdiction with its status, and `{api}/index.json` describes these files. "
         "Paths are relative to the site address."),
        ("Status",
         f"`status` is one of {', '.join(statuses[:-1])} or {statuses[-1]}, defined in `dataset.status_definitions`. "
         "`status_since` is the date the current status began, meaning the earliest event in the unbroken run of events at that status. "
         "none_identified means our searches found no qualifying government step, not that none exists. "
         "Bodies a reader might mistake for an institute, such as AI Act authorities, are listed under each jurisdiction's `related_bodies`."),
        ("Head, reporting line and national partners",
         "Each institute can carry `head`, `reports_to` and `national_partners`: who leads it, which ministry-level body it answers to and which national bodies it works with. "
         "Each has a `state`: stated, not_published or not_applicable. not_published means we searched and found nothing published, not that nothing exists. "
         "`head.person_id` names an entry of the institute's `people`. A partner has `what` it is, a `relation` (member, works_with or planned, defined in `dataset.vocabularies.partner_relation`) and `how`, one sentence written from its quoted source; "
         "`cyber_agency` marks the national cyber security agency, the only body searched for every institute, so a missing partner of any other kind says nothing. "
         "The card's Partners line shows at most six partners per institute; its \"How they work together\" fold, the data and the downloads hold all of them. "
         "The CSVs are `institutes.csv` (head and reporting-line columns) and `national_partners.csv`."),
        ("Confidence and missing values",
         "`status_confidence` and `confidence` rate the evidence as high, medium or low; the schema defines each level, and `dataset.method` sets the rules for status confidence. "
         f"{method_rule(dataset, 'missing-values')} Some fields give null, omission or an empty list a specific meaning, such as not applicable; their schema descriptions say which."),
        ("Sources",
         "`citations` gives, for each field, the IDs of the sources whose quoted excerpt supports it. "
         "Each source has its address, an archive copy where one exists, and its excerpts, with an English gloss for other languages. "
         "`check.last_checked` is the recorded date of a record's last full re-check (Freshness in `dataset.method`); "
         "where `check.last_checked_basis` is historical_upper_bound, that date is only an upper bound."),
        ("Licence", f"Code: MIT (`LICENSE`). Data, page text and documentation: {dataset['license']} "
         f"(`LICENSE-DATA`; {licence['title']}, {licence['path']}), with attribution. {dataset['license_note']}"),
        ("Cite as", dataset["citation"]),
    ]


def plain(text: str) -> str:
    return text.replace("`", "")


def definition_markdown(dataset: dict) -> str:
    """The README's definition block: the dataset's definition, as on the page, with its link to the full rule."""
    return f"{dataset['definition']} [What counts]({PUBLIC_URL}method.html#qualifying-body)"


def orientation_markdown(published: dict) -> str:
    """README block: one bold-free paragraph per heading, as level-3 headings."""
    return "\n\n".join(f"### {heading}\n\n{text}" for heading, text in orientation(published))


def orientation_html(published: dict) -> str:
    """The page's "Using the data": one muted line after the downloads linking llms.txt and the API index (the full text is in llms.txt and the README)."""
    api = api_root(published)
    return ('<p class="small" id="using-data">Scripts and AI agents: <a href="llms.txt">llms.txt</a> '
            f'and the <a href="{html.escape(api)}/index.json">API index</a></p>')


def llms_txt(published: dict) -> str:
    """llms.txt (llmstxt.org): title, summary, the orientation, then absolute links to the key files."""
    dataset = published["dataset"]
    api = api_root(published)
    links = [
        ("Full dataset", "data/aisi.json", "every record with its sources, confidence and check dates"),
        ("Schema", "data/aisi.schema.json", "defines every field"),
        ("Data package", "data/datapackage.json", "every file, including one CSV per table, with field descriptions"),
        ("API index", f"{api}/index.json", "the per-record files and how to read them"),
        ("Jurisdictions", f"{api}/jurisdictions.json", "every jurisdiction with its status"),
        ("Page", "", "the tracker page"),
    ]
    sections = orientation(published)
    body = [f"# {dataset['title']}", "", f"> {sections[0][1]}", ""]
    for heading, text in sections[1:]:
        body += [f"## {heading}", "", text, ""]
    body += ["## Files", ""] + [f"- [{name}]({PUBLIC_URL}{path}): {text}" for name, path, text in links]
    return "\n".join(body) + "\n"


def head_links(published: dict, page_title: str) -> str:
    api = api_root(published)
    dataset = published["dataset"]
    # The text drawn on social-preview.png (scripts/social_preview.py): page title, publisher, credits.
    credit = ", ".join(p["name"] for p in dataset["maintainers"])
    image_alt = html.escape(f"{page_title}. {dataset['publisher']}, compiled by {credit}.", quote=True)
    url = html.escape(PUBLIC_URL, quote=True)
    return (f'<link rel="canonical" href="{url}">\n'
            f'<meta property="og:url" content="{url}">\n'
            f'<meta property="og:image" content="{url}social-preview.png">\n'
            '<meta property="og:image:width" content="1200">\n'
            '<meta property="og:image:height" content="630">\n'
            f'<meta property="og:image:alt" content="{image_alt}">\n'
            f'<link rel="alternate" type="application/json" href="{api}/index.json" title="Per-record API">\n'
            '<link rel="alternate" type="application/json" href="data/datapackage.json" title="Data package">')


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
    """The page and built files except the noindex error page, dated as_of, sorted."""
    as_of = published["dataset"]["as_of"]
    entries = "".join(f"<url><loc>{html.escape(PUBLIC_URL + path)}</loc><lastmod>{as_of}</lastmod></url>\n" for path in sorted(paths) if path != "404.html")
    return f'<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n{entries}</urlset>\n'


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


ZENODO_KEYWORDS = ["AI Security Institute", "AI safety", "AI governance", "AI policy", "Europe"]


def zenodo_json(published: dict) -> str:
    """.zenodo.json (Zenodo's legacy deposit metadata), which Zenodo reads in place of CITATION.cff
    when archiving a GitHub release. Same dataset fields as citation_cff, so the two cannot drift.
    Creators are persons only (Zenodo's loader treats every creator as a person); the publisher is
    the imprint. No publication_date: Zenodo takes the release date."""
    dataset = published["dataset"]
    creators = []
    for maintainer in dataset["maintainers"]:
        given, family = split_name(maintainer["name"])
        creators.append({"name": f"{family}, {given}" if given else family}
                        | ({"affiliation": maintainer["affiliation"]} if maintainer.get("affiliation") else {})
                        | ({"orcid": maintainer["orcid"]} if maintainer.get("orcid") else {}))
    related = [{"relation": "isDocumentedBy", "identifier": PUBLIC_URL, "resource_type": "other"}]
    if dataset.get("repository"):
        # The release tag is v<version>, e.g. v1.0.0.
        related.append({"relation": "isSupplementTo", "identifier": f"{dataset['repository']}/tree/v{dataset['version']}", "resource_type": "software"})
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
  <script>try {{ if (localStorage.getItem("aisi-theme") === "dark") document.documentElement.setAttribute("data-theme", "dark"); }} catch (e) {{}}</script>
  <style>
    /* Theme as on the tracker: light by default, dark when the reader chose it there. */
    :root {{ color-scheme: light; --ground: #FAF7F2; --ink: #1A1A1A; --link: #8F5A23; --code: #F0EBE3; }}
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
