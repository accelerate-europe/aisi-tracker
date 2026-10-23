# AI Security Institutes tracker

Which states have an AI Security Institute (AISI), with funding, staff and leadership where published. Every record cites its sources and the date of its last recorded full check, with uncertain historical dates marked; funding, staff and leadership facts also carry a confidence rating. Published by Accelerate Europe under CC BY 4.0.

<!-- definition:start -->

We count a body as an AI Security Institute (AISI) if a national government, or the EU, has tasked it with testing general-purpose or frontier AI models for their capabilities and risks, or has named it its AI safety institute or its AI security institute. A regulator, certification body or cyber security agency does not count for that role alone. [What counts](https://aisi.accelerate-europe.org/method.html#qualifying-body)

<!-- definition:end -->

<!-- counts:start -->

Version 1.0.0, data as of 2026-10-23. 195 jurisdictions, 15 institutes and 1088 sources. The Europe view covers 32 states.

116 output records, 46 agreements and 9 network milestones. Negative findings by confidence: 0 high, 121 medium, 57 low.

<!-- counts:end -->

Covers all UN member states, Taiwan (as a territory) and the EU AI Office. The Europe page includes EU member states, Iceland, Liechtenstein, Norway, the UK and Switzerland (`europe_scope`).

## Setup

Install Git, [uv](https://docs.astral.sh/uv/getting-started/installation/), Python 3.11 or newer, Node.js and Chrome or Chromium. Chrome is required for the build and browser gate; Node runs the JavaScript checks. Run these commands from the repository root.

```sh
uv sync --locked --group dev
uv run scripts/check.py --preflight
uv run scripts/build.py --out dist
python3 -m http.server 8000 --bind 127.0.0.1 --directory dist
```

Open `http://127.0.0.1:8000/`. Stage edits before running `uv run scripts/check.py`, which checks the staged snapshot. Full maintainer checks need the [private source store](docs/source-snapshots.md) through `AISI_SNAPSHOTS`; the public CI runs the fast checks, build and browser gate without private evidence.

## Files

- `data/aisi.json`: the dataset, the single source of truth. Edit data here, never in generated exports.
- `schema/aisi.schema.json`: JSON Schema (2020-12) defining every field, including status, confidence and check-method vocabularies.
- `scripts/slide_map.py`: renders a static map of European status (PNG and SVG, light theme) from `data/aisi.json` for slides; boundaries from Natural Earth 1:50m, cached in `work/cache/` and checked against a pinned SHA-256. `AISI_MAP_FONTS=/path/to/fonts` sets the map’s font directory.
- `.github/workflows/pages.yml`: on every push to any branch, runs `scripts/check.py` and builds the map (a map failure is reported but does not block publishing, since the page does not use the map); on `main` it also rejects draft wording and publishes `dist/` to GitHub Pages (needs a public repository).
- `scripts/build.py`: validates the dataset and writes `dist/` (gitignored): the page, `aisi.json`, one CSV per table including chronological `events.csv`, and a Frictionless `datapackage.json` with field descriptions.
- `site/index.html`: the page, which renders the generated `dist/data/aisi.json`. The build saves it as rendered in headless Chrome (`scripts/prerender.py`), so the published page reads without scripts; at load the script swaps in a fresh render without a visible change. Chrome or Chromium is needed to build.
- `data/reference/roster.csv`: the coverage roster (UN members plus tracked territories) with UN M49 region and sub-region and World Bank population, made by `scripts/make_roster.py`.
- `scripts/archive_sources.py`: legacy manual Wayback lookup/request tool, separate from the private-copy workflow. Its `--apply` mode retrieves and validates capture content before writing `archive_url`; add `--offline` to use cached captures only. Rejected candidates in `data/reference/archive_urls.json` remain lookup history.
- `docs/design/schema-1.0.md`: the download contract and how each rule is represented and checked.
- `AGENTS.md`: the map for agents and editors: which files a change touches, which command checks it and where each kind of rule lives.
- `scripts/check.py`: the one check command (validation, lint, tests, private copies, build, browser gate, comparison with a base revision, screenshot sheet); see [AGENTS.md](AGENTS.md). `tests/` and `scripts/check_page.py` hold the tests and the headless-browser gate. Node is required for the JavaScript checks; the browser gate requires Chrome or Chromium.

## For agents and scripts

Generated from the published dataset by `scripts/agent_files.py`, the one home of this text; the same text is the page's "Using the data" block, the `orientation` in `api/v<major>/index.json` and `llms.txt` on the site.

<!-- agents:start -->

### What this is

Which states have an AI Security Institute (AISI), covering all 193 UN member states plus Taiwan and the EU AI Office, with funding, staff and leadership where published. Every record cites its sources. Published by Accelerate Europe; version 1.0.0, data as of 2026-10-23.

### Files and addresses

On the published site, the full dataset is `data/aisi.json`, every record with its sources and check dates; `data/aisi.schema.json` defines every field, and `data/datapackage.json` lists every file, including one CSV per table. For one state, `api/v1/jurisdictions/{id}.json` (ISO 3166-1 alpha-2 code, EU for the European Union) holds its record and its institute's with the sources they cite; for one institute, `api/v1/institutes/{id}.json` holds the record with its sources; `api/v1/jurisdictions.json` lists every jurisdiction with its status, and `api/v1/index.json` describes these files. Paths are relative to the site address.

### Status

`status` is one of established, planned, proposed, exploring or none_identified, defined in `dataset.status_definitions`. `status_since` is the date the current status began, meaning the earliest event in the unbroken run of events at that status. none_identified means our searches found no qualifying government step, not that none exists. Bodies a reader might mistake for an institute, such as AI Act authorities, are listed under each jurisdiction's `related_bodies`.

### Head, reporting line and national partners

Each institute can carry `head`, `reports_to` and `national_partners`: who leads it, which ministry-level body it answers to and which national bodies it works with. Each has a `state`: stated, not_published or not_applicable. not_published means we searched and found nothing published, not that nothing exists. `head.person_id` names an entry of the institute's `people`. A partner has `what` it is, a `relation` (member, works_with or planned, defined in `dataset.vocabularies.partner_relation`) and `how`, one sentence written from its quoted source; `cyber_agency` marks the national cyber security agency, the only body searched for every institute, so a missing partner of any other kind says nothing. The card's Partners line shows at most six partners per institute; its "How they work together" fold, the data and the downloads hold all of them. The CSVs are `institutes.csv` (head and reporting-line columns) and `national_partners.csv`.

### Confidence and missing values

`status_confidence` and `confidence` rate the evidence as high, medium or low; the schema defines each level, and `dataset.method` sets the rules for status confidence. An omitted field or null means not known or not published, never zero or false. Some fields give null, omission or an empty list a specific meaning, such as not applicable; their schema descriptions say which.

### Sources

`citations` gives, for each field, the IDs of the sources whose quoted excerpt supports it. Each source has its address, an archive copy where one exists, and its excerpts, with an English gloss for other languages. `check.last_checked` is the recorded date of a record's last full re-check (Freshness in `dataset.method`); where `check.last_checked_basis` is historical_upper_bound, that date is only an upper bound.

### Licence

Code: MIT (`LICENSE`). Data, page text and documentation: CC BY 4.0 (`LICENSE-DATA`; Creative Commons Attribution 4.0, https://creativecommons.org/licenses/by/4.0/), with attribution. Quotations from sources remain with their rights holders and are reproduced as quotations; the licence covers our own compilation, text and data.

### Cite as

Accelerate Europe (2026). AI Security Institutes tracker, version 1.0.0, data as of 23 October 2026. Compiled by Jason Hoelscher-Obermaier.

<!-- agents:end -->

## Licences

The code is licensed under the [MIT License](LICENSE).

The Inter fonts bundled in `scripts/fonts/` retain their [SIL Open Font License 1.1](scripts/fonts/OFL.txt). Regenerate the social image with `uv run --locked scripts/social_preview.py`.

The data, page text and documentation, including the authored dataset in `data/aisi.json` and its generated data exports, are licensed under [Creative Commons Attribution 4.0 International (CC BY 4.0)](https://creativecommons.org/licenses/by/4.0/). The full legal code is included in [LICENSE-DATA](LICENSE-DATA), also published at the site root. Credit: Accelerate Europe, compiled by Jason Hoelscher-Obermaier. Include the licence link and indicate any changes. The dataset licence is recorded in `dataset.license`, `CITATION.cff` and the generated data package. Quotations from sources remain with their rights holders and are reproduced as quotations; the licence covers our own compilation, text and data.

## Update the data

1. Edit `data/aisi.json`. For any new or changed fact, add or update its entry in `sources` (url, publisher, type primary or secondary, published and accessed dates) and bind a supporting excerpt to its stable claim selector. Broad source lists record research associations; the build derives displayed citations from those bindings.
2. Record check dates as `dataset.method` (Freshness) sets out: `check.last_checked` for a full re-check, `check.partial` for partial work.
3. Starting from public 1.0.0, bump `dataset.version` (major: breaking schema change; minor: new records, status changes or additive schema fields; patch: corrections), set `dataset.as_of`, and add a `changelog` entry with the same version.
4. The build derives EUR conversions from annual amounts and ECB rate inputs. Stage the change and run `uv run scripts/check.py`; it refuses schema errors, unknown or unused source ids, impossible dates, a jurisdiction and its institute disagreeing, or a changed Europe scope, and reports the changed claims beside their excerpts.
5. Check links: `python3 -c "import json;[print(s['url']) for s in json.load(open('data/aisi.json'))['sources']]" > /tmp/urls.txt && lychee --no-progress --insecure --max-retries 1 /tmp/urls.txt` (lychee misreads the CSV, so feed it plain URLs). Government sites that block automated readers get an `access_note` on the source.
6. Publish the build that passed: `uv run scripts/check.py --release --out dist` checks and leaves it in `dist/`.

## Conventions

- Record ids never change across renames; name history lives in `institutes[].names`.
- An omitted field or `null` means not known or not published, never zero or false.
- `status` vocabulary: established, planned, proposed, exploring, none_identified (definitions in `dataset.status_definitions`). AI Act authorities are recorded under `related_bodies` with `counts_as_aisi: false`, not as institutes.
- AI Act authority entries distinguish `designation_state` (`designated` or `proposed`) and, where established, coded `authority_roles`. Their `designation_basis` records the act or decision (precedence rules in `dataset.method`); their role text states sector limits. Missing fields mean unresolved or inapplicable; a blank Commission listing does not establish non-designation.
- `confidence`: high = stated in a primary source; medium = primary but indirect, or several consistent secondary sources; low = single secondary source or estimate. Status confidence, including what a `none_identified` record needs for medium or high, follows `dataset.method` in [data/aisi.json](data/aisi.json).
- `dataset.vocabularies` holds controlled code lists (`testing_capacity`, `model_access` and `staffing_model` for institutes, `membership_body` for `jurisdictions[].memberships`, among others). Adding a code is a minor version; removing or renaming one is a major version. Consumers should tolerate codes they do not recognise.
- Country ids are ISO 3166-1 alpha-2 (GB for the UK, GR for Greece), plus EU for the European Union. Eurostat uses UK and EL for the same two.
- Text follows the Accelerate Europe style: British spelling, no em-dashes.

## Download contract (1.0.0)

- Known activity is generated from the output catalogue. Authored `data/aisi.json` has no `institutes[].operating`; the downloaded `aisi.json` adds an array of `{kind, date, lineage, source_ids}` per institute. `[]` means no qualifying item was found. The qualification policy lives in `dataset.method`; each output has a reviewed `qualifies_as` (`published_work`, `joint_testing` or `none`) and `qualification_note`. Do not edit generated activity or infer qualification from titles. See the [selection and export contract](docs/design/schema-1.0.md#known-activity), [publication functions](scripts/published.py) (`output_activity_kind`, `known_activity`, `publish`) and [hand-worked checks](tests/test_known_activity.py).
- `institutes[].names[].from` is generated, never authored: each name links by `event` to the jurisdiction timeline event that gave it, and `from` is that event's date. `events.csv` has no name rows; the timeline row carries the name change. See the [names contract](docs/design/schema-1.0.md#outputs-names-and-funding).
- `institutes[].announced` is generated, never authored: the date of the first commitment or proposal event in the jurisdiction timeline, null when none is recorded (a designation is never read as an announcement). See the [timeline contract](docs/design/schema-1.0.md#timeline-and-status).
- Intervals between milestones are generated, never authored: the downloaded `aisi.json` adds `institutes[].intervals` with `announced_to_established`, `established_to_first_qualifying_activity` and `announced_to_first_qualifying_activity`, each `{days_min, days_max}` in signed whole days or null, and `institutes.csv` exports them as six `_days_min`/`_days_max` columns. The rule, including why the intervals are not a measure of speed or quality, lives in `dataset.method` (Intervals between milestones); see the [timeline contract](docs/design/schema-1.0.md#timeline-and-status) and [publication functions](scripts/published.py) (`milestones`, `intervals`).
- `known_activity.csv` now exports one selected earliest item per institute and kind: `institute_id`, `kind`, `date`, `lineage`, `source_ids`, `source_urls`. `outputs.csv` adds `lineage`, `attribution`, `qualifies_as` and `qualification_note`. The old operating `value`, `as_of`, `evidence` and `notes` object, `institutes.csv` operating columns, supporting-signal rows and `dataset.operating_rule` are removed. Leadership, staff and websites keep their own fields; unique evidence limits stay in institute notes.
- `agreements` replaces `access_agreements`; `counterparty` replaces `developer`, with `counterparty_type`. The CSV is now `agreements.csv`. Consumers must use the new names; there is no deprecated alias. The agreement CSV also exports `participation`: `institute_party`, `named_implementer` or `recipient`. A pledge uses `recipient` for its named beneficiary, including a documented predecessor.
- Jurisdiction `iso3`, `m49` and `wikidata`, and institute `wikidata`, have `identifier_source_ids`; numeric country codes stay strings. Null means unknown or inapplicable, with distinctions in `identifier_notes`.
- Optional source `snapshot` metadata publishes only `retrieved`, `sha256` and, when the copy has text, the versioned main-text hash `text_sha256` with its `text_scheme` (defined in the schema); `sources.csv` exports these as `snapshot_retrieved`, `snapshot_sha256`, `snapshot_text_sha256` and `snapshot_text_scheme`. Copies and private paths are excluded from every download. Timestamps retain their recorded RFC3339 precision and offset.
- Authored excerpts bind verbatim quotations to stable selectors; downloads resolve them to version-specific RFC 6901 pointers after materialisation. Non-English text has an English gloss. Reordering preserves the claim identity. Binding checks establish links, not semantic proof.
- `outputs.csv`, `network_milestones.csv`, `related_bodies.csv`, `closest_bodies.csv` and `source_excerpts.csv` expose nested records. Optional `closest_body` names one related body for a `none_identified` jurisdiction (selection rules in `dataset.method`); it does not change jurisdiction status. `events.csv` merges dated timeline events, appointments, memberships, agreements, funding decisions and network milestones, with source IDs/URLs and canonical record pointers. No current membership has a sourced joining date, so this release has no membership rows in `events.csv`. Timeline events and network milestones carry a plain `short` of at most 12 words, also exported in their own CSVs and `events.csv`; other event origins leave `short` empty. Undated records are omitted, partial dates are preserved, future dates are flagged, and fiscal periods are never used as decision dates.
- Annual funding stays in its stated currency with `annual_basis_code` (`allocation_current_year`, `period_mean` or `other`) and a documented basis; which amounts get an annual figure is set by `dataset.method` (Annual funding). Amounts are not actual expenditure; future funding is labelled separately.
- `dataset.exchange_rates` and `exchange_rates.csv` retain the cited ECB euro foreign exchange reference rates (calendar-2025 annual averages, units per euro) and their derived inverses, with the source URL and access date in the cited source. The latest complete calendar year supplies one rate per currency, for every currency used in funding; EUR is the base and has no row. `funding.csv` gives each EUR estimate with the rate used (`units_per_eur`) beside the stated-currency amount. [Rate checks](scripts/build.py) and [regression tests](tests/test_exchange_rates.py) enforce its arithmetic, common basis, coverage and citations.
- The catalogues cover retrieved publications and qualifying agreements; inaccessible listings and unpublished information limit completeness. Authority research covers EU and EEA states, with unresolved allocations stated in notes.
- Jurisdictions carry Wikidata identifiers and country/territory records carry ISO3 and M49 codes; country codes are inapplicable to the EU. Unresolved institute identifiers remain null, which does not establish that no item exists.
- Every decisive claim listed in `dataset.method` (Excerpts) has an exact excerpt pointer. Unknown constitutive legal acts are omitted; policy commitments remain in the timeline. Quotations and translations require editorial review as well as pointer checks. Uncertain dates retain their precision limits. Published EUR annual estimates are derived from annual_amount and dataset.exchange_rates; the conversion limitation is stated in dataset.method.
- See [schema 1.0](docs/design/schema-1.0.md), [consistency and pointer checks](scripts/build.py) and [regression tests](tests/test_build.py). Builds are offline; source retrieval and Wikidata verification are editorial work.

The download contract assigns an explicit `kind` to every related body (`ai_act_authority` or `other`), so removing optional designation details cannot bypass the authority checks. The jurisdiction and institute CSVs export `last_checked_basis` alongside `last_checked`; a historical upper bound does not guarantee freshness. Every changelog version also has a short reader summary.

Outputs share one earliest public date per document lineage; `credit.institute_id` and `credit.date` separately record the institution credited and the first version carrying that credit. `credited_from` is generated for downloads when the dates differ. What counts as an output and as a first output is defined in `dataset.method`.

Source preference and title translation follow `dataset.method`. Source `origin` identifies the issuing country or international body; null is unresolved. `build.py --report-dir DIR` writes an informational `source-origins.md` review there (for example `--report-dir work/reports`); without it, validation and builds only print the count and write nothing. It checks each cited record separately; a domestic source on a parent does not cover a nested claim. Statistical, identifier, historical independence and search-query citations are listed separately in the JSON report. Reports are local and never tracked. An unwritable report directory produces a warning without failing otherwise valid data.

Source titles retain their recorded wording in `title`; optional `title_en` is an English translation, not necessarily an official title. The source's `language` describes the document read, not its title. `sources.csv` exports both fields, with an empty `title_en` cell when omitted.

## Cite

The citation, current counts, agents and definition blocks, and `CITATION.cff` (type dataset; Accelerate Europe and the compiler as authors), are generated from the published dataset by `uv run scripts/build.py --update-readme`, the only build or check command that writes a tracked file; every other canonical build and check rejects a stale block.

<!-- citation:start -->

Accelerate Europe (2026). AI Security Institutes tracker, version 1.0.0, data as of 23 October 2026. Compiled by Jason Hoelscher-Obermaier.

<!-- citation:end -->

Corrections: open an issue or pull request on GitHub, or write to <contact@accelerate-europe.org>.

## Consistency checks

See [the consistency guide](docs/consistency.md) for the enforced claim identities, coverage limits, checks and report commands.

## Review rule tables

Run `uv run scripts/rule_tables.py --data data/aisi.json --output work/reports/rule-tables.md` to compare outputs, first outputs, agreements and budget bases for every institute, negative confidence for every `none_identified` jurisdiction, and AI Act authority bases for EU members plus Iceland, Liechtenstein and Norway. Main tables give decisions side by side; appendices retain full claims and citations. First-output comparison uses the explicit `credit.date`. Authority identification uses `kind`, including bodies whose optional designation details remain unresolved.

Optional `--decisions PATH` and `--uk-audit PATH` flags include explicit editorial exclusions. These inputs remain local research evidence under ignored `work/`; without them, exclusions are marked unrecorded rather than inferred from missing catalogue entries. The generated report is also local and gitignored. It records the dataset hash and must be regenerated after data changes; it does not verify source accuracy or replace editorial review.

The report records paths and SHA-256 hashes for the dataset and both optional review inputs, explicitly marking omitted inputs. Main tables show supplied exclusions and reasons; the budget table separates explicitly dated current allocations from period means, future funding and other annual bases, preserving commitment status. Included UK review decisions missing from the supplied catalogue are listed as pending. Run from the repository root so local evidence paths resolve consistently.

## Quote verification

`scripts/verify_excerpts.py` checks literal quotations against cached source text. Run `uv run --group evidence python scripts/verify_excerpts.py --all-excerpts --offline` to audit an existing cache, or omit `--offline` to retrieve source copies. Online retrieval is opt-in: the build does not invoke this tool. The verifier tries each declared live/archive copy and one rendered fallback, reuses cached attempts, and spaces request starts by host. It rejects error, login, challenge, empty-render, empty application, document-viewer-only and recognised cookie-settings-only responses. Reports and source caches stay in ignored `work/`.

Matching normalises Unicode composition, whitespace and quotation marks only, and rejects matches that cut adjacent digits or comma, full-stop, Arabic, full-width, space or apostrophe number separators. Ambiguous adjacent numbers fail this boundary check and need review. HTML can also be checked in a separate view excluding explicitly hidden accessibility text. Both views retain noscript fallback text available without JavaScript; this does not verify its visibility in a JavaScript-enabled browser. PDF text can be checked through independent default, layout and, when the `pdftotext` executable is installed, Poppler extraction views. Independent PDF views are still attempted when the primary parser returns no text or raises an extraction error. A quotation must match within one view. The evidence group includes `fonttools` because pypdf uses it to decode embedded CFF Type1 fonts. A match establishes literal occurrence, not support for a claim. Non-matches need editorial review: readable PDF glyphs, especially Arabic and image-based text, can remain visual evidence with the source page recorded in `access_note`. A rendered publication timestamp must be labelled as metadata rather than article-body wording.

The default checks decisive excerpts; `--all-excerpts` checks every recorded quotation and `--all-sources` also retrieves sources without quotations. Defaults are one second between request starts to the same host, four workers and a 25-second timeout. Cached failures are retained; use a fresh `--cache` directory for an intentional new retrieval attempt. Reports distinguish mismatches, transport failures, uncached sources, rejected responses, retrieved but unextractable documents, extraction failures and local storage failures. A quotation found only outside its declared excerpt URL is flagged as `matched_other_copy` for review. Matches confined to hidden HTML text or to an undeclared rendered copy are flagged separately. Rendered plain text from Jina may contain spoken accessibility expansions that cannot be distinguished automatically; these still need editorial review. Exit status 1 means at least one quotation needs review. Reports record the extraction version and whether Poppler was available. For XLSX workbooks the verifier reads only literal string cells of visible worksheets; the [source snapshot guide](docs/source-snapshots.md) lists the exclusions.

Cache metadata or checksum corruption stops the audit rather than trusting damaged evidence. Some original sources reject the Python trust store while working with platform TLS verification; a fresh cache can therefore fail where a preserved, normally verified platform retrieval succeeded. Any such imported response must retain its actual retrieval method, timestamp and complete-response hash.

## Private source copies

See [the source snapshot guide](docs/source-snapshots.md) for private-copy preservation, public hashes, archive checks and offline commands.

Legacy archive candidates in `data/reference/archive_urls.json` are lookup history, not verified evidence. Apply them with `uv run --group evidence python scripts/archive_sources.py --apply`. Application retrieves uncached captures, including raw Wayback content behind wrappers, into `work/source-cache`; add `--offline` to use cached captures only. Each candidate must pass the shared archive-capture, response-content and document-identity checks before its URL is written. Unavailable responses, rejected pages, non-captures and mirrors are skipped and reported. Existing canonical archive URLs are left unchanged. The command without `--apply` retains its existing network lookup and Save Page Now behaviour.

## Release checklist

Follow the [launch runbook](docs/launch.md) for the public source export, hosting setup and post-launch checks.

- Run excerpt verification and refresh [private source snapshots](docs/source-snapshots.md).
- Review stale full-check dates and the recorded evidence limits.
- Read the check report's Release warnings: sources marked `legal_text` whose `instrument_status_checked` is missing or more than 183 days old (rule `rescinded-instruments` in `dataset.method`; measured at `as_of`, and at the release date under `--release`). Re-verify each instrument's status and record the date, or leave the warning for the owner to accept. The warnings also list work dated after `as_of` (flag searches, source, snapshot and excerpt access dates, instrument status checks); `--release` fails on these, so set `as_of` to the release date first.
- Rerun the full page sweep on the release build (every fold open, all table rows shown, 1440, 1000 and 390 px, light and dark) and fix or log every finding.
- Remove draft wording from the README, canonical citation, schema descriptions, CLAUDE.md and AGENTS.md; regenerate the README citation, current counts, agents and definition blocks with `uv run scripts/build.py --update-readme`.
- Set `PUBLIC_URL` in `scripts/agent_files.py` to the public site address; `--release` fails while it is the placeholder. Regenerate with `--update-readme`, since `CITATION.cff` carries the address.
- Owner or team, at launch: switch on the Zenodo GitHub integration for the public repository, so each release gets a DOI with the same creators as `CITATION.cff`.
- Run `uv run scripts/check.py --release --out dist`; every step must pass, none NOT RUN, and `dist/` is the build to publish.
- Owner, at launch: enable branch protection requiring the `build` job and GitHub secret scanning with push protection, and run a one-off secrets scan of the full repository history.

## Public source export

Export the reviewed release commit with `git archive <release-commit> | tar -x -C <empty-export-directory>`. The `.gitattributes` export rules leave superseded internal notes in `docs/history/` and the maintainers' review protocol `docs/review.md` out of the archive. Initialise the public repository from that exported tree with a fresh history; do not push the private repository history.
