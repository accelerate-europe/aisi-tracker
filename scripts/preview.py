"""Mark an existing build and publish disposable branch previews.

The live build never imports this module. Transform writes a separate directory;
publish prepares a complete candidate before changing a checked-out preview tree.
"""

from __future__ import annotations

import argparse
import copy
import datetime as dt
import html
import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import quote, urlencode, urljoin, urlsplit
from xml.etree import ElementTree as ET
from zoneinfo import ZoneInfo

# Each preview text says what it stands in for and what happens to it at release (owner, 17 Nov 2026).
NOTICE = "This build is unpublished. Do not cite it."
# Page elements the preview replaces as a whole (by id), with the text it puts there. check_preview excludes exactly
# these from the comparison with the build, and nothing the preview merely marks.
REPLACED = {"cite": NOTICE}
# While News lines are drafts for the owner, the preview says so above the list; empty once they are approved.
NEWS_DRAFT_NOTE = "Draft wording: every News line below is a draft for review and may change before release."


def replaced_selector(ids: list[str]) -> str:
    """The build-side exclusion for the preview's replaced elements; refuses any id not listed in REPLACED, so the
    preview cannot exempt its own changes by marking page content as chrome."""
    unknown = sorted(set(ids) - set(REPLACED))
    if unknown:
        raise ValueError(f"preview chrome replaces page elements not listed in REPLACED: {unknown}")
    return "".join(f", #{name}" for name in sorted(set(ids)))
PREVIEW_URL = "https://accelerate-europe.github.io/aisi-tracker-preview/"
SOURCE_REPO = "https://github.com/accelerate-europe/aisi-tracker"
PREVIEW_REPO = "git@github.com:accelerate-europe/aisi-tracker-preview.git"
LIVE_URL = "https://aisi.accelerate-europe.org/"
ROOT_FILES = {"index.html", "404.html", "robots.txt", ".nojekyll", "manifest.json", "keep.txt"}
RESERVED = ROOT_FILES | {"b", ".git", ".github", "cname", ".", "..", ""}
BUDGET = 900_000_000
FROZEN_DAYS = 21  # an unkept frozen build goes after this many days
FROZEN_COUNT = 10  # at most this many unkept frozen builds stay, the newest by first publish
ROBOTS = "User-agent: *\nDisallow: /\n"
KEEP_HEADER = "# One ten-character commit per line. Kept builds are not pruned.\n"


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def timestamp(value: str) -> dt.datetime:
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("built and attempt times need an explicit time zone")
    return parsed.astimezone(dt.timezone.utc)


def vienna(value: str) -> str:
    """Vienna time with its zone, so no separate line has to say which: "17 Nov 2026, 01:08 CET" (CEST in summer)."""
    return timestamp(value).astimezone(ZoneInfo("Europe/Vienna")).strftime("%-d %b %Y, %H:%M %Z")


def data_date(value: str) -> str:
    """The data date as the page writes it (14 Nov 2026)."""
    return dt.date.fromisoformat(value).strftime("%-d %b %Y")


def folder_name(branch: str) -> str:
    folder = folder_name_unchecked(branch)
    if folder in RESERVED or folder.startswith(".") or (folder == "next" and branch != "next"):
        raise ValueError(f"preview: reserved folder for branch {branch!r}")
    return folder


def commit_id(sha: str) -> str:
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise ValueError("commit must be a full, lower-case Git SHA")
    return sha[:10]


def preview_base(url: str) -> str:
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.netloc or parsed.query or parsed.fragment or not url.endswith("/"):
        raise ValueError("preview URL must be an https directory URL")
    return url


def frozen_url(page_url: str, logical: str, sha: str) -> str:
    """Python counterpart of the delegated link handler, also used by the tests."""
    depth = logical.count("/")
    site = urljoin(page_url, "../" * depth or "./")
    parent = "../../" if re.search(r"/b/[0-9a-f]{10}/$", urlsplit(site).path) else "../"
    return urljoin(site, parent + f"b/{commit_id(sha)}/{logical}") + ("#" + urlsplit(page_url).fragment if urlsplit(page_url).fragment else "")


# One quiet band in the page's own tokens (ground, accent, ink, link, meta type), with fallbacks for the pages that
# have their own small stylesheet (API landing page, preview root). Its content sits in the column of the header
# below: the site's .gutter/.measure on the tracker and method pages, the page's main column elsewhere (--pv-measure,
# --pv-gutter). One line; below 40rem of column (phones; the API page's column is 42rem) the identity moves to a second line under
# the label and actions. A long branch name is cut with an ellipsis (full name in the text and its title), never wrapped.
BANNER_CSS = """
.preview-banner { box-sizing: border-box; margin: 0; color: var(--ink, inherit);
  background: color-mix(in srgb, var(--accent, #BE773D) 12%, var(--ground, Canvas));
  border-bottom: 1px solid color-mix(in srgb, var(--accent, #BE773D) 45%, var(--ground, Canvas));
  font: var(--type-meta, .875rem/1.5 "Segoe UI", Arial, sans-serif); text-align: left; }
.preview-banner .pv-col { box-sizing: border-box; max-width: var(--pv-measure); margin-inline: auto; padding-inline: var(--pv-gutter); }
.preview-banner .measure, .preview-banner .pv-col { container: pv / inline-size; }
.preview-banner .pv-row { display: grid; grid-template-columns: auto minmax(0, 1fr) auto; grid-template-areas: "title id actions";
  column-gap: var(--sp-4, 1rem); align-items: baseline; padding-block: var(--sp-2, .5rem); }
.preview-banner .pv-title { grid-area: title; font-weight: 600; white-space: nowrap; }
.preview-banner .pv-id { grid-area: id; color: var(--muted, inherit); display: flex; min-width: 0; white-space: nowrap; }
.preview-banner .pv-branch { overflow: hidden; text-overflow: ellipsis; min-width: 2ch; flex: 0 1 auto; }
.preview-banner .pv-build { flex: none; }
.preview-banner .pv-actions { grid-area: actions; display: flex; gap: var(--sp-4, 1rem); align-items: baseline; white-space: nowrap; }
.preview-banner .pv-toggle { font: inherit; color: var(--link, LinkText); background: none; border: 0; padding: 0; margin: 0; cursor: pointer;
  text-decoration: underline; text-decoration-thickness: var(--underline-width, auto); text-underline-offset: var(--underline-offset, .2em);
  text-decoration-color: var(--underline-colour, currentColor); }
.preview-banner .pv-toggle::after { content: " +"; }
.preview-banner .pv-toggle[aria-expanded="true"]::after { content: " \\2212"; }
.preview-banner .pv-toggle:focus-visible { outline: 2px solid var(--focus, var(--link, currentColor)); outline-offset: 2px; }
.preview-banner .pv-details { list-style: none; margin: 0; padding: 0 0 var(--sp-2, .5rem); }
.preview-banner .pv-details[hidden] { display: none; }
/* Preview chrome inside the page: what the preview adds or replaces, in the band's look, so page and preview stay
   distinguishable. */
.cite.pv-chrome, .pv-draft-note.pv-chrome { background: color-mix(in srgb, var(--accent, #BE773D) 12%, var(--ground, Canvas)); padding-block: var(--sp-2, .5rem); user-select: auto; }
/* The marker: once the band has scrolled away, one short fixed line in the corner keeps the build identifiable on
   screen and in screenshots. Nothing else on the page is fixed there (the theme switch is in the header). */
.preview-banner .pv-marker { position: fixed; right: max(var(--sp-4, 1rem), env(safe-area-inset-right)); bottom: max(var(--sp-4, 1rem), env(safe-area-inset-bottom));
  z-index: 40; display: inline-flex; align-items: baseline; max-width: min(60vw, 20rem); box-sizing: border-box; margin: 0;
  padding: .125rem var(--sp-3, .75rem); border: 1px solid color-mix(in srgb, var(--accent, #BE773D) 45%, var(--ground, Canvas)); border-radius: 999px;
  background: color-mix(in srgb, var(--accent, #BE773D) 12%, var(--ground, Canvas)); color: var(--ink, CanvasText);
  font: var(--type-meta, .875rem/1.5 "Segoe UI", Arial, sans-serif); white-space: nowrap; cursor: pointer; }
.preview-banner .pv-marker[hidden], .preview-banner .pv-marker.pv-away { display: none; }
.preview-banner .pv-marker:focus-visible { outline: 2px solid var(--focus, var(--link, currentColor)); outline-offset: 2px; }
@media print { .preview-banner .pv-marker { display: none; } }
@container pv (max-width: 40rem) {
  .preview-banner .pv-row { grid-template-columns: minmax(0, 1fr) auto; grid-template-areas: "title actions" "id id"; }
  .preview-banner .pv-actions { justify-self: end; }
}
"""

# The frozen address is set on load, on every hash change and just before any use of the link (click, middle click,
# context menu, focus, hover), so a copied link carries the current card anchor too. Hydration replaces the banner
# element, hence the delegated listeners.
BANNER_JS = """
(function () {
  function frozen(link) {
    const logical = link.dataset.previewPath;
    const depth = logical.split('/').length - 1;
    const site = new URL('../'.repeat(depth) || './', location.href);
    const parent = /\\/b\\/[0-9a-f]{10}\\/$/.test(site.pathname) ? '../../' : '../';
    const target = new URL(parent + 'b/' + link.dataset.previewFrozen + '/' + logical, site);
    target.hash = location.hash;
    link.href = target.href;
  }
  function all() { document.querySelectorAll('[data-preview-frozen]').forEach(frozen); }
  ['click', 'auxclick', 'contextmenu', 'focusin', 'pointerover'].forEach(function (type) {
    document.addEventListener(type, function (event) {
      const link = event.target.closest && event.target.closest('[data-preview-frozen]');
      if (link) frozen(link);
    }, true);
  });
  document.addEventListener('DOMContentLoaded', all);
  window.addEventListener('hashchange', all);
  // Details: a button with aria-expanded; delegated, since hydration replaces the band.
  document.addEventListener('click', function (event) {
    const toggle = event.target.closest && event.target.closest('.preview-banner .pv-toggle');
    if (!toggle) return;
    const open = toggle.getAttribute('aria-expanded') !== 'true';
    toggle.setAttribute('aria-expanded', String(open));
    toggle.closest('.preview-banner').querySelector('.pv-details').hidden = !open;
  });
  // The marker shows only while the band is out of view; it brings the band back with Details open. It stays hidden
  // without this script, and steps aside while the focused element sits under it.
  function band() { return document.querySelector('body > .preview-banner'); }
  function place() {
    const b = band(), marker = b && b.querySelector('.pv-marker');
    if (!marker) return;
    marker.hidden = b.getBoundingClientRect().bottom > 0;
    marker.classList.remove('pv-away');
    const focused = document.activeElement;
    if (marker.hidden || !focused || focused === document.body || marker.contains(focused)) return;
    const m = marker.getBoundingClientRect(), f = focused.getBoundingClientRect();
    if (f.right > m.left && f.left < m.right && f.bottom > m.top && f.top < m.bottom) marker.classList.add('pv-away');
  }
  let frame = 0;
  function soon() { if (!frame) frame = requestAnimationFrame(function () { frame = 0; place(); }); }
  window.addEventListener('scroll', soon, { passive: true });
  window.addEventListener('resize', soon);
  document.addEventListener('DOMContentLoaded', function () {
    place();
    new MutationObserver(soon).observe(document.body, { childList: true }); // hydration replaces the band
  });
  document.addEventListener('click', function (event) {
    const marker = event.target.closest && event.target.closest('.preview-banner .pv-marker');
    if (!marker) return;
    const b = marker.closest('.preview-banner'), toggle = b.querySelector('.pv-toggle');
    toggle.setAttribute('aria-expanded', 'true');
    b.querySelector('.pv-details').hidden = false;
    window.scrollTo(0, 0);
    b.scrollIntoView({ block: 'start' });
    toggle.focus({ preventScroll: true });
    place();
  });
  document.addEventListener('focusin', place);
  document.addEventListener('focusout', soon);
})();
"""

# The column of a page without the site's .gutter/.measure: its main box (max-width 42rem plus 1.5rem padding on
# each side, content-box) in border-box terms. check_preview compares the left edges.
OWN_COLUMN = "--pv-measure: 45rem; --pv-gutter: 1.5rem"


def banner(meta: dict, logical: str, mail: str, site_column: bool = True) -> str:
    short = commit_id(meta["commit"])
    subject = urlencode({"subject": f"AISI preview {meta['branch']} {short}"}, quote_via=quote)
    esc = html.escape
    outer, inner = ("preview-banner pv-chrome gutter", "measure") if site_column else ("preview-banner pv-chrome", "pv-col")
    style = "" if site_column else f' style="{OWN_COLUMN}"'
    return (f'<aside class="{outer}" aria-label="Preview build"{style}><div class="{inner}">'
            '<div class="pv-row"><span class="pv-title">Preview, not published</span>'
            f'<span class="pv-id"><span class="pv-branch" title="{esc(meta["branch"], quote=True)}">{esc(meta["branch"])}</span>'
            f'<span class="pv-build">&nbsp;· {short} · {vienna(meta["built"])}</span></span>'
            f'<span class="pv-actions"><a href="mailto:{quote(mail, safe="@.+")}?{esc(subject)}">Feedback</a>'
            '<button type="button" class="pv-toggle" aria-expanded="false" aria-controls="preview-details">Details</button></span></div>'
            '<ul class="pv-details" id="preview-details" hidden>'
            f'<li>Data as of {data_date(meta["as_of"])}</li>'
            f'<li><a href="{esc(meta["run_url"], quote=True)}">CI run for this build</a></li>'
            f'<li><a href="#" data-preview-frozen="{short}" data-preview-path="{esc(logical)}">Link to this exact build</a></li>'
            f'<li><a href="{LIVE_URL}" data-preview-published>Published site</a></li></ul></div>'
            f'<button type="button" class="pv-marker" hidden aria-label="Preview · {esc(meta["branch"], quote=True)} · {short}: show the build details">'
            f'Preview ·&nbsp;<span class="pv-branch">{esc(meta["branch"])}</span>&nbsp;· {short}</button></aside>')


def mark_html(text: str, logical: str, meta: dict, mail: str, data_urls: tuple[str, str, str]) -> str:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(text, "html.parser")
    if soup.head is None or soup.body is None:
        raise ValueError(f"{logical}: expected a complete HTML page")
    for tag in soup.select('link[rel="canonical"], meta[property="og:url"], meta[property="og:image"], meta[name="twitter:image"], script[type="application/ld+json"], meta[name="robots"]'):
        tag.decompose()
    robots = soup.new_tag("meta", attrs={"name": "robots", "content": "noindex, nofollow"})
    soup.head.append(robots)
    style = soup.new_tag("style")
    style.string = BANNER_CSS
    soup.head.append(style)
    script = soup.new_tag("script")
    script.string = BANNER_JS
    soup.head.append(script)
    # The page shows its own text; what the preview adds or replaces carries the preview-chrome class (band look).
    # CSS selectors follow browser template boundaries; find_all also visits the inert template tree, which
    # hydration puts in place of the saved body.
    for cite in soup.find_all(id="cite"):
        cite.clear()
        cite.append(NOTICE)
        cite["class"] = [*cite.get("class", []), "pv-chrome"]
    # The note sits before the list, which the page script refills; the note itself is static in both copies.
    for news in soup.find_all(id="development-list") if NEWS_DRAFT_NOTE else []:
        note = soup.new_tag("p", attrs={"class": "pv-draft-note pv-chrome small"})
        note.string = NEWS_DRAFT_NOTE
        news.insert_before(note)
    for node in soup.select('meta[name="aisi-page-data"]'):
        node["content"], node["data-package"], node["data-build"] = data_urls
    # The saved body is replaced during hydration. Both copies need the same first element. Pages with the site's
    # header use its column classes; the others get their main column.
    site_column = soup.find(class_="masthead") is not None
    soup.body.insert(0, BeautifulSoup(banner(meta, logical, mail, site_column), "html.parser").aside)
    for template in soup.select('template[id="page-template"]'):
        template.insert(0, BeautifulSoup(banner(meta, logical, mail, site_column), "html.parser").aside)
    if logical == "404.html":
        for node in soup.select('[href="/favicon.svg"], [href="/"]'):
            node["href"] = "favicon.svg" if node["href"].endswith("favicon.svg") else "./"
    # Preview data has no DOI. Remove the dormant live DOI URL constructor as well.
    return str(soup).replace('"https://doi.org/" + m.doi', '""')


def mark_json(value, meta: dict, citation: str, allow_preview: bool = True):
    if isinstance(value, list):
        return [mark_json(item, meta, citation, allow_preview) for item in value]
    if not isinstance(value, dict):
        return NOTICE if isinstance(value, str) and value == citation else value
    # The dataset's own DOI sits next to its citation (agent_files.py); a DOI elsewhere, such as a cited paper's, stays.
    result = {key: mark_json(item, meta, citation, allow_preview) for key, item in value.items() if not (key == "doi" and "citation" in value)}
    if isinstance(result.get("dataset"), dict):
        dataset = result["dataset"]
        dataset["citation"] = NOTICE
        if allow_preview:
            dataset["preview"] = meta
        elif "description" in dataset:
            dataset["description"] = f"{NOTICE} {dataset['description']}"
    return result


def mark_feed(text: str, frozen: str, citation: str) -> str:
    ns = "{http://www.w3.org/2005/Atom}"
    ET.register_namespace("", ns[1:-1])
    root = ET.fromstring(text.replace(LIVE_URL, frozen).replace(citation, NOTICE))
    for node in root.iter(ns + "id"):
        old = node.text or ""
        if old.startswith("tag:" + urlsplit(LIVE_URL).hostname):
            node.text = frozen + "feed.xml#" + quote(old.split(":", 2)[-1], safe="")
    title = root.find(ns + "title")
    if title is not None:
        title.text = f"{title.text} (unpublished preview build)"
    return '<?xml version="1.0" encoding="utf-8"?>\n' + ET.tostring(root, encoding="unicode") + "\n"


def oracle(dist: Path, doi: str) -> list[str]:
    """The release's identifiers must not survive (its DOI, Zenodo, the live host), except schema explanations and
    the banner's link to the published site. Other DOIs, such as a cited paper's, are sources and stay."""
    from bs4 import BeautifulSoup

    errors = []
    forbidden = re.compile("|".join([r"10\.5281/zenodo", r"zenodo\.org", r"aisi\.accelerate-europe\.org"] + ([re.escape(doi)] if doi else [])), re.I)
    for path in sorted(dist.rglob("*")):
        if not path.is_file() or path.name == "aisi.schema.json":
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        if path.suffix == ".html":
            soup = BeautifulSoup(text, "html.parser")
            for link in soup.find_all("a", attrs={"data-preview-published": True}):
                # BeautifulSoup excludes template text from get_text(); inspect the known link's contents.
                if link.find_parent(class_="preview-banner") and link.get("href") == LIVE_URL and link.decode_contents().lower() == "published site":
                    link.decompose()
            text = str(soup)
        if match := forbidden.search(text):
            context = text[max(0, match.start() - 50):match.end() + 50]
            errors.append(f"{path.relative_to(dist)}: published identifier remains: {context!r}")
    return errors


def regular_tree(root: Path) -> None:
    if root.is_symlink() or any(p.is_symlink() for p in root.rglob("*")):
        raise ValueError(f"symlinks are not allowed in {root.name}")


def transform(dist: Path, out: Path, *, branch: str, sha: str, run_url: str, built: str,
              as_of: str | None = None, feedback_mail: str | None = None, preview_url: str = PREVIEW_URL) -> dict:
    # Import only in the project environment; the browser checker uses its own locked environment.
    if __package__:
        from .build import content_mark, page_data, versioned
    else:
        from build import content_mark, page_data, versioned
    from jsonschema import Draft202012Validator

    dist, out = dist.resolve(), out.resolve()
    if out == dist or dist in out.parents or out in dist.parents or out.exists():
        raise ValueError("transform needs a new output directory separate from its input")
    regular_tree(dist)
    folder_name(branch)
    short = commit_id(sha)
    frozen = preview_base(preview_url) + f"b/{short}/"
    source = read_json(dist / "data/aisi.json")
    dataset = source["dataset"]
    meta = {"notice": NOTICE, "branch": branch, "commit": sha, "built": built}
    timestamp(built)
    extra = {**meta, "as_of": dataset["as_of"], "version": dataset["version"], "run_url": run_url, "preview_url": preview_url}
    if as_of and as_of != dataset["as_of"]:
        raise ValueError("--as-of disagrees with the built dataset")
    mail = feedback_mail or dataset.get("contact")
    if not mail or re.search(r"[\s?&#]", mail):
        raise ValueError("feedback mail must be a plain address")
    schema = read_json(dist / "data/aisi.schema.json")
    candidate = copy.deepcopy(source)
    candidate["dataset"]["preview"] = meta
    allow_preview = not list(Draft202012Validator(schema).iter_errors(candidate))
    transformed = mark_json(source, meta, dataset["citation"], allow_preview)
    Draft202012Validator(schema).validate(transformed)
    if not allow_preview:
        print("preview: schema rejects dataset.preview; notice is in citation and description")
    mark = content_mark(page_data(transformed), dataset["version"])
    page_text = page_data(transformed, mark)
    package = mark_json(read_json(dist / "data/datapackage.json"), meta, dataset["citation"])
    package.pop("id", None)
    package["preview"] = meta
    package_text = json.dumps(package, ensure_ascii=False, indent=2) + "\n"
    urls = (f"data/page.json?v={mark}", versioned("data/datapackage.json", package_text, dataset["version"]), mark)
    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="preview-transform-", dir=out.parent) as tmp:
        target = Path(tmp) / "site"
        shutil.copytree(dist, target)
        (target / "sitemap.xml").unlink(missing_ok=True)
        write_json(target / "data/aisi.json", transformed)
        (target / "data/page.json").write_text(page_text, encoding="utf-8")
        (target / "data/datapackage.json").write_text(package_text, encoding="utf-8")
        for path in sorted((target / "api").rglob("*.json")):
            write_json(path, mark_json(read_json(path), meta, dataset["citation"]))
        for path in sorted(target.rglob("*.html")):
            path.write_text(mark_html(path.read_text(encoding="utf-8"), path.relative_to(target).as_posix(), extra, mail, urls), encoding="utf-8")
        llms = target / "llms.txt"
        llms.write_text(llms.read_text(encoding="utf-8").replace(dataset["citation"], NOTICE).replace(LIVE_URL, frozen), encoding="utf-8")
        # Plain text identifies the preview in its version sentence.
        released = f"version {dataset['version']}, data as of"
        text = llms.read_text(encoding="utf-8")
        if released not in text:
            raise ValueError(f"llms.txt: expected {released!r} to mark as a preview")
        llms.write_text(text.replace(released, "unpublished preview build; " + released), encoding="utf-8")
        feed = target / "feed.xml"
        feed.write_text(mark_feed(feed.read_text(encoding="utf-8"), frozen, dataset["citation"]), encoding="utf-8")
        (target / "robots.txt").write_text(ROBOTS, encoding="utf-8")
        write_json(target / "preview-build.json", extra)
        problems = oracle(target, dataset.get("doi", ""))
        if problems:
            raise ValueError("\n".join(problems))
        shutil.move(target, out)
    return extra


def kept_commits(text: str) -> set[str]:
    kept = set()
    for line in text.splitlines():
        sha = line.partition("#")[0].strip()
        if sha and not re.fullmatch(r"[0-9a-f]{10}", sha):
            raise ValueError(f"keep.txt: expected ten lower-case hex characters, got {sha!r}")
        if sha:
            kept.add(sha)
    return kept


def retention(manifest: dict, kept: set[str], branches: set[str], now: dt.datetime,
              budget: int = BUDGET, overhead: int = 0) -> tuple[dict, set[str]]:
    """Choose removals without touching files. Sizes are measured again on each publish."""
    result = copy.deepcopy(manifest)
    removed = set()
    for folder, entry in list(result["folders"].items()):
        if entry["branch"] != "next" and entry["branch"] not in branches:
            removed.add(folder)
            del result["folders"][folder]
    live = {entry["current"]["commit"][:10] for entry in result["folders"].values() if entry.get("current")}
    protected = kept | live
    frozen = result["frozen"]
    # Timestamp, then SHA: a deterministic tie-break independent of directory order.
    order = sorted(frozen, key=lambda sha: (timestamp(frozen[sha]["built"]), sha))
    for sha in order:
        if sha not in protected and now - timestamp(frozen[sha]["built"]) > dt.timedelta(days=FROZEN_DAYS):
            removed.add("b/" + sha)
            del frozen[sha]
    # At most FROZEN_COUNT unkept builds, newest by first publish; kept and live builds neither go nor count.
    unprotected = [sha for sha in order if sha in frozen and sha not in protected]
    for sha in unprotected[:max(0, len(unprotected) - FROZEN_COUNT)]:
        removed.add("b/" + sha)
        del frozen[sha]
    size = overhead + sum(e.get("size", 0) for e in result["folders"].values()) + sum(e["size"] for e in frozen.values())
    for sha in order:
        if size <= budget:
            break
        if sha in frozen and sha not in protected:
            size -= frozen.pop(sha)["size"]
            removed.add("b/" + sha)
    if size > budget:
        raise ValueError(f"preview: protected builds and root files use {size} bytes, above the {budget}-byte budget; reduce keep.txt or live folders")
    return result, removed


# The preview root's pages (index, 404): the tracker's palette and the same band as the previews, over one column.
ROOT_CSS = """
:root { color-scheme: light; --ground: #FAF7F2; --ink: #1A1A1A; --muted: #6E6760; --link: #8F5A23; --accent: #BE773D; --line: #E8E3DD; }
@media (prefers-color-scheme: dark) { :root { color-scheme: dark; --ground: #17191A; --ink: #F3F0EB; --muted: #B0ADA7; --link: #E9AA79; --accent: #E9AA79; --line: #2A2D2F; } }
body { margin: 0; background: var(--ground); color: var(--ink); font: 1rem/1.5 "Segoe UI", Arial, sans-serif; }
main { box-sizing: border-box; max-width: var(--pv-measure); margin-inline: auto; padding: 1.5rem var(--pv-gutter) 3rem; overflow-wrap: anywhere; }
a { color: var(--link); text-underline-offset: .2em; }
td, th { text-align: left; vertical-align: top; padding: .5rem .5rem .5rem 0; border-bottom: 1px solid var(--line); }
table { width: 100%; table-layout: fixed; border-collapse: collapse; }
"""
ROOT_COLUMN = "--pv-measure: 75rem; --pv-gutter: 1.5rem"


def root_page(title: str, body: str, script: str = "") -> str:
    band = ('<header class="preview-banner"><div class="pv-col"><div class="pv-row"><span class="pv-title">Preview, not published</span>'
            '<span class="pv-id">Previews of the AISI tracker</span></div></div></header>')
    return ('<!doctype html><html lang="en"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            '<meta name="robots" content="noindex, nofollow">'
            f'<title>{html.escape(title)}</title><style>' + ROOT_CSS + BANNER_CSS + '</style></head>'
            f'<body style="{ROOT_COLUMN}">' + band + '<main>' + body + '</main>' + (f'<script>{script}</script>' if script else '') + '</body></html>\n')


def index_html(manifest: dict, kept: set[str]) -> str:
    esc = html.escape
    rows = []
    for folder, entry in sorted(manifest["folders"].items()):
        attempt, current = entry["attempt"], entry.get("current")
        name = esc(entry["branch"])
        branch = f'<a href="{folder}/">{name}</a>' if current else name
        build = f'{current["commit"][:10]}<br>{vienna(current["built"])}' if current else "No build yet"
        rows.append(f'<tr><td>{branch}</td><td>{build}</td><td>{esc(attempt["status"])}<br>{attempt["commit"][:10]}<br>{vienna(attempt["time"])}<br>'
                    f'<a data-run="{entry["run_id"]}" href="{esc(attempt["run_url"])}">CI run</a></td></tr>')
    frozen = []
    for sha, entry in sorted(manifest["frozen"].items(), key=lambda item: (item[1]["built"], item[0]), reverse=True):
        expiry = (timestamp(entry["built"]) + dt.timedelta(days=FROZEN_DAYS)).date().isoformat()
        life = "Kept" if sha in kept else f"Until {expiry} at most, sooner by the rule above"
        if sha not in kept and any(e.get("current", {}).get("commit", "")[:10] == sha for e in manifest["folders"].values()):
            life = "Stays while it is a branch's current build"
        frozen.append(f'<tr><td><a href="b/{sha}/">{sha}</a></td><td>{esc(entry["branch"])}<br>{vienna(entry["built"])}<br>'
                      f'<a href="{esc(entry["run_url"])}">CI run</a></td><td>{life}</td></tr>')
    script = """
document.querySelectorAll('[data-run]').forEach(async function (link) {
  try {
    const response = await fetch('https://api.github.com/repos/accelerate-europe/aisi-tracker/actions/runs/' + link.dataset.run);
    if (!response.ok) return;
    const run = await response.json();
    link.textContent = run.status !== 'completed' ? 'CI running' : run.conclusion === 'success' ? 'CI passed' : 'CI failed';
  } catch (_) { /* The run link remains usable when the public API is unavailable. */ }
});
"""
    return root_page("AISI tracker previews", '<h1>Branch previews</h1>'
                     '<table><thead><tr><th>Branch</th><th>Current build</th><th>Last attempt</th></tr></thead><tbody>' + ''.join(rows) + '</tbody></table>'
                     '<h2>Frozen builds</h2><p>'
                     f'The {FROZEN_COUNT} newest unkept frozen builds stay, each for {FROZEN_DAYS} days at most, and fewer if space runs out; '
                     'kept builds and the current build of each branch stay and do not count. '
                     'To keep one, add its ten-character commit to <a href="https://github.com/accelerate-europe/aisi-tracker-preview/edit/gh-pages/keep.txt">keep.txt in the preview repository</a>.</p>'
                     '<table><thead><tr><th>Commit</th><th>Built from</th><th>Retention</th></tr></thead><tbody>' + ''.join(frozen) + '</tbody></table>', script)


def not_found_html(preview_url: str = PREVIEW_URL) -> str:
    root = html.escape(urlsplit(preview_base(preview_url)).path)
    script = """
const match = location.pathname.match(/\\/b\\/([0-9a-f]{10})(?:\\/|$)/);
if (match) {
  document.getElementById('missing').textContent = 'This frozen build was removed';
  const link = document.createElement('a');
  link.href = 'https://github.com/accelerate-europe/aisi-tracker/commit/' + match[1];
  link.textContent = match[1]; document.getElementById('commit').append(link);
}
"""
    return root_page("Preview not found", '<h1 id="missing">Preview not found</h1><p id="commit"></p>'
                     f'<p><a href="{root}">Preview index</a> · <a href="{root}next/">next</a></p>', script)


def tree_size(path: Path) -> int:
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file()) if path.exists() else 0


def validate_manifest(manifest: dict) -> None:
    for folder, entry in manifest["folders"].items():
        if folder_name(entry["branch"]) != folder:
            raise ValueError("manifest contains an unsafe folder")
    for sha, entry in manifest["frozen"].items():
        if commit_id(entry["commit"]) != sha:
            raise ValueError("manifest contains an unsafe frozen path")


def publish(tree: Path, transformed: Path | None, *, branch: str, sha: str, run_id: int,
            run_attempt: int, run_url: str, attempted: str, branches: set[str],
            preview_url: str = PREVIEW_URL, budget: int = BUDGET) -> bool:
    tree = tree.resolve()
    regular_tree(tree)
    folder, short = folder_name(branch), commit_id(sha)
    now = timestamp(attempted)
    manifest_path = tree / "manifest.json"
    manifest = read_json(manifest_path) if manifest_path.exists() else {"folders": {}, "frozen": {}}
    validate_manifest(manifest)
    previous = manifest["folders"].get(folder)
    collisions = {name for name in branches if folder_name_unchecked(name) == folder and name != branch}
    if collisions or (previous and previous["branch"] != branch) or (not previous and (tree / folder).exists()):
        print(f"preview: folder collision for {branch!r}; nothing published")
        return False
    if previous and (run_id, run_attempt) <= (previous["run_id"], previous["run_attempt"]):
        print(f"preview: stale or repeated run {run_id}/{run_attempt} for {branch}; recorded {previous['run_id']}/{previous['run_attempt']}; nothing published")
        return False
    if branch != "next" and branch not in branches:
        print(f"preview: branch {branch!r} was deleted; nothing published")
        return False
    if transformed:
        transformed = transformed.resolve()
        regular_tree(transformed)
        meta = read_json(transformed / "preview-build.json")
        if (meta["branch"], meta["commit"], meta["run_url"], meta["preview_url"]) != (branch, sha, run_url, preview_url):
            raise ValueError("transformed build provenance does not match the publish")
    tree.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="preview-publish-", dir=tree.parent) as tmp:
        candidate = Path(tmp) / "tree"
        shutil.copytree(tree, candidate, ignore=shutil.ignore_patterns(".git"))
        keep = candidate / "keep.txt"
        if not keep.exists():
            keep.write_text(KEEP_HEADER, encoding="utf-8")
        kept = kept_commits(keep.read_text(encoding="utf-8"))
        entry = copy.deepcopy(previous) if previous else {"branch": branch}
        entry.update(run_id=run_id, run_attempt=run_attempt,
                     attempt={"commit": sha, "run_url": run_url, "time": attempted, "status": "built" if transformed else "build failed"})
        if transformed:
            current = {**meta, "run_id": run_id, "run_attempt": run_attempt}
            entry["current"] = current
            destination = candidate / folder
            if destination.exists():
                shutil.rmtree(destination)
            shutil.copytree(transformed, destination)
            frozen = candidate / "b" / short
            if short in manifest["frozen"]:
                if manifest["frozen"][short]["commit"] != sha or not frozen.is_dir():
                    raise ValueError("frozen SHA collision or missing frozen copy")
            elif frozen.exists():
                raise ValueError("unrecorded frozen copy; refusing to overwrite provenance")
            else:
                shutil.copytree(transformed, frozen)
                manifest["frozen"][short] = current
        manifest["folders"][folder] = entry
        for name, item in manifest["folders"].items():
            item["size"] = tree_size(candidate / name)
        for name, item in manifest["frozen"].items():
            item["size"] = tree_size(candidate / "b" / name)
        # Root output is small, but counts towards the same budget. Recompute after pruning too.
        (candidate / "robots.txt").write_text(ROBOTS, encoding="utf-8")
        (candidate / ".nojekyll").touch()
        (candidate / "404.html").write_text(not_found_html(preview_url), encoding="utf-8")
        write_json(candidate / "manifest.json", manifest)
        (candidate / "index.html").write_text(index_html(manifest, kept), encoding="utf-8")
        managed_size = sum(e["size"] for e in manifest["folders"].values()) + sum(e["size"] for e in manifest["frozen"].values())
        manifest, removed = retention(manifest, kept, branches, now, budget, tree_size(candidate) - managed_size)
        for name in sorted(removed):
            path = candidate / name
            if path.exists():
                shutil.rmtree(path)
        write_json(candidate / "manifest.json", manifest)
        (candidate / "index.html").write_text(index_html(manifest, kept), encoding="utf-8")
        if tree_size(candidate) > budget:
            raise ValueError("preview: generated root files exceed the size budget")
        for path in tree.iterdir():
            if path.name != ".git":
                shutil.rmtree(path) if path.is_dir() else path.unlink()
        shutil.copytree(candidate, tree, dirs_exist_ok=True)
    return True


def folder_name_unchecked(branch: str) -> str:
    return re.sub(r"[^a-z0-9._-]", "-", branch.lower())


def git(*args: str, cwd: Path | None = None, env: dict | None = None, input: str | None = None) -> str:
    return subprocess.run(["git", *args], cwd=cwd, env=env, input=input, text=True, check=True, capture_output=True).stdout.strip()


def lease_lost(remote: str, tip: str) -> bool:
    """A rejected push is a lease race when gh-pages moved, appeared or was deleted since the fetch ("" = absent)."""
    return (remote.split()[0] if remote.strip() else "") != tip


def push(transformed: Path | None, *, fetch_tree, apply, push_tree, attempts: int = 3) -> bool:
    """Retry only a lease race, with a fresh tree and a complete recomputation every time.

    Callbacks keep the race behaviour testable without git or network access.
    """
    for attempt in range(attempts):
        with fetch_tree() as (tree, tip):
            if not apply(tree, transformed):
                return False
            if push_tree(tree, tip):
                return True
        print(f"preview: lease changed; recomputing on a fresh tree ({attempt + 1}/{attempts})")
    raise RuntimeError("preview: lease failed three times; nothing published by this run")


def push_remote(transformed: Path | None, **options) -> bool:
    from contextlib import contextmanager

    # Reserved branches are a logged no-op even when the site job failed.
    folder_name(options["branch"])

    @contextmanager
    def fetch_tree():
        with tempfile.TemporaryDirectory(prefix="preview-clone-") as tmp:
            tree = Path(tmp) / "tree"
            if git("ls-remote", PREVIEW_REPO, "refs/heads/gh-pages"):
                git("clone", "--depth", "1", "--branch", "gh-pages", PREVIEW_REPO, str(tree))
                yield tree, git("rev-parse", "HEAD", cwd=tree)
            else:
                # The first publish: the new repository has no gh-pages yet. An empty lease expects it absent.
                print("preview: gh-pages does not exist yet; starting it")
                git("init", "-q", str(tree))
                git("remote", "add", "origin", PREVIEW_REPO, cwd=tree)
                yield tree, ""

    def apply(tree, marked):
        heads = git("ls-remote", "--heads", SOURCE_REPO + ".git")
        branches = {line.split("\t", 1)[1].removeprefix("refs/heads/") for line in heads.splitlines()}
        return publish(tree, marked, branches=branches, **options)

    def push_tree(tree, tip):
        git("add", "--all", cwd=tree)
        tree_id = git("write-tree", cwd=tree)
        identity = {**os.environ, "GIT_AUTHOR_NAME": "Preview publisher", "GIT_COMMITTER_NAME": "Preview publisher",
                    "GIT_AUTHOR_EMAIL": "preview@users.noreply.github.com", "GIT_COMMITTER_EMAIL": "preview@users.noreply.github.com"}
        commit = git("commit-tree", tree_id, cwd=tree, env=identity, input="Update generated previews\n")
        try:
            git("push", f"--force-with-lease=refs/heads/gh-pages:{tip}", "origin", f"{commit}:refs/heads/gh-pages", cwd=tree)
        except subprocess.CalledProcessError:
            # Authentication and transport failures are not a lease race.
            if lease_lost(git("ls-remote", "origin", "refs/heads/gh-pages", cwd=tree), tip):
                return False
            raise
        return True

    return push(transformed, fetch_tree=fetch_tree, apply=apply, push_tree=push_tree)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    mark = commands.add_parser("transform")
    mark.add_argument("dist", type=Path)
    mark.add_argument("out", type=Path)
    mark.add_argument("--built", required=True)
    mark.add_argument("--as-of")
    mark.add_argument("--feedback-mail")
    for name in ("publish", "push"):
        command = commands.add_parser(name)
        if name == "publish":
            command.add_argument("tree", type=Path)
            command.add_argument("--branches", type=Path, required=True, help="one existing source branch per line")
        command.add_argument("--transformed", type=Path, help="omit to record a failed site build")
        command.add_argument("--run-id", type=int, required=True)
        command.add_argument("--run-attempt", type=int, required=True)
        command.add_argument("--attempted", required=True)
    for command in (mark, *[commands.choices[name] for name in ("publish", "push")]):
        command.add_argument("--branch", required=True)
        command.add_argument("--sha", required=True)
        command.add_argument("--run-url", required=True)
        command.add_argument("--preview-url", default=PREVIEW_URL)
    args = vars(parser.parse_args())
    command = args.pop("command")
    try:
        if command == "transform":
            transform(**args)
        elif command == "publish":
            args["branches"] = set(args["branches"].read_text(encoding="utf-8").splitlines())
            publish(**args)
        else:
            push_remote(**args)
    except ValueError as exc:
        if str(exc).startswith("preview: reserved folder"):
            print(f"{exc}; nothing published")
        else:
            parser.exit(1, f"preview: {exc}\n")


if __name__ == "__main__":
    main()
