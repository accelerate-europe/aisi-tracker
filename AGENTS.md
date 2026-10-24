# Agent guide

The map for agents and editors working in this repository. It routes; it does not restate rules. `CLAUDE.md` points here.

The staged `check.py` command and shared pre-commit/CI steps run the automated checks. Every commit also gets an independent review of exactly what is staged, named in a `Reviewed-by:` trailer.

## Where things live

- `data/aisi.json`: the authored dataset, the single source of truth. Editorial rules are in its `dataset.method`; status definitions in `dataset.status_definitions`.
- `schema/aisi.schema.json`: the published download contract. `references.authored_schema` derives the authored-input contract from it; generated fields are forbidden there.
- `scripts/published.py`: the shared primary-facts, stable-reference and field-citation pipeline, used by every consumer. `scripts/build.py` with `method_checks.py`, `shared_claims.py`, `release_checks.py` and `reader_text.py`: the invariants, enforced at validation. `tests/`: their regression tests.
- `scripts/row_checks.py`: the Head, Reports to and Partners rows: cross-field checks, required evidence pointers and the generated card selection ([design](docs/design/schema-1.0.md#head-reporting-line-and-national-partners)). Relation words and the cyber tail come only from `dataset.vocabularies`.
- `scripts/flag_checks.py`: testing capacity and model access (downloads, API and the page's Testing and access groups) and staffing model (downloads and API only): checks beyond the schema, required evidence pointers, the attribution guard and the derived model access value ([design](docs/design/schema-1.0.md#testing-capacity-and-model-access), [staffing model](docs/design/schema-1.0.md#staffing-model)).
- `scripts/agent_files.py`: files for agents and scripts that `build.py` generates from the published data: the static per-record API under `api/v<major>/`, the orientation text (page "Using the data", README "For agents and scripts", the API index, `llms.txt`), the page-head JSON-LD, `sitemap.xml`, `robots.txt`, the tracked `CITATION.cff` and `.zenodo.json` and the `PUBLIC_URL` site address. `tests/test_api.py` checks the API against `aisi.json`.
- `site/index.html`: the page. The build saves it pre-rendered (`scripts/prerender.py`); the comment at the top of its script says how the script takes over. Its design principles are the comment at the top of its `<style>`.
- `scripts/check.py`: the one check command. `scripts/check_page.py`: the browser gate it runs. `scripts/contact_sheet.py`: the screenshot sheet.
- `docs/design/schema-1.0.md`: the download contract and how rules are represented. `docs/consistency.md`: what the consistency checks cover. `docs/source-snapshots.md`: private source copies.
- `docs/history/` and `docs/review.md`, when present in a development checkout: superseded notes, not current rules (do not act on them), and the maintainers' review protocol. Public source exports exclude both.
- `work/` (gitignored): reports, caches, screenshots and scratch files. Never commit them.

## Change type, files, done

| Change | Edit | Also |
| --- | --- | --- |
| A fact | the record in `data/aisi.json`, its `sources` entry and an excerpt whose `supports` points at the claim | advance `check.last_checked` only after a full re-check; version, `as_of` and changelog per README "Update the data" |
| A status | the jurisdiction `timeline` event; the build derives status and dates ([timeline rules](docs/design/schema-1.0.md#timeline-and-status)) | excerpt on the event |
| Funding amount or exchange rates | primary amounts or ECB rate inputs in `data/aisi.json`; EUR results are generated | |
| A field | schema, `build.py` export and tables, tests | from public 1.0.0: minor for additive fields, major for breaking changes |
| An editorial rule | `dataset.method` first | `uv run scripts/rule_tables.py` and its impact table before editing records; reread affected reader summaries, then `uv run scripts/method_copy.py refresh <summary-id>` |
| Page copy | `site/index.html` and the same string in `scripts/check_page.py` (the gate checks copy independently) | visual pass |
| Page layout or style | `site/index.html`, its principles comment if a rule changes, and a gate check for a new rule | visual pass |
| A release | README "Release checklist" | `check.py --release` |

Done means: the change is staged, `uv run scripts/check.py` passes (add `--base <branch>` when handing back a branch), you have read its report, and the commit has had an independent review of the staged diff. A visual pass means opening each Where to look link in the report on the served staged build at 1440, 1000 and 390 px, light and dark, and judging it against the design principles at the top of `site/index.html`.

## Commands

- `uv run scripts/check.py`: checks exactly the staged snapshot and writes nothing into the checkout. It prints the report path.
- `uv run scripts/check.py --only fast`: validation, lint and tests (about 25 s; tests run in four pytest-xdist workers). This is also the pre-commit hook; install it once per clone with `git config core.hooksPath .githooks`.
- The browser gate (`scripts/check_page.py`, about 1.5 min) runs its checks as independent jobs in parallel processes: `--jobs N`, by default the CPUs up to 8. Errors are reported in the sequential order, so the report does not depend on N; `--jobs 1` runs the jobs one after another, which helps when debugging one check. CI builds the site once, splits the browser jobs over four machines (`check.py --gate-shard K/4`) and reports them from the shard results in the `build` job (`--gate-merge`), which is the required check.
- `uv run scripts/check.py --preflight`: can this agent run every step (Chrome, Node, the private store)? If a step would be NOT RUN, say so in the handback and name who must run it; never report it as passed.
- `uv run scripts/build.py --out DIR` builds the site. Chrome or Chromium is a build requirement: `scripts/prerender.py` saves the page as rendered in headless Chrome, and the build fails without it (there is no NOT RUN path for the build); `uv run scripts/build.py --update-readme` regenerates the README citation, current counts, agents and definition blocks, `CITATION.cff` and `.zenodo.json`, the only build or check command that writes a tracked file.
- New earliest credited work needs a separately reviewed CODE update to the hand-worked source expectations in `scripts/check_page.py`; an unknown institute/lineage pair fails the gate with its key. Keep that update separate from the DATA commit.
- Regenerated README citation/count/agents/definition blocks, `CITATION.cff` and `.zenodo.json` travel with the DATA change that changes their inputs; prose or generator changes remain CODE.
- Stable selectors use permanent record IDs, or the documented natural keys for output lineage, currency and membership body. Correcting a natural key requires updating its bindings; see the [download contract](docs/design/schema-1.0.md#source-excerpts).
- The dataset is several MB: inspect single records with `jq` or a short Python snippet rather than reading the file.

## Standing rules

- Never invent or guess URLs, dates, names or figures. Every new fact needs a source you retrieved, chosen as `dataset.method` (Source preference) sets out. For a JavaScript-only page try `https://r.jina.ai/<URL>` once, then record an `access_note`.
- Configure the private store with `AISI_SNAPSHOTS` ([source snapshots](docs/source-snapshots.md)). Source copies go there through `scripts/snapshot_sources.py`, never only into a clone's `work/`; `check.py`'s copies step fails when a recorded snapshot hash has no file there.
- Before editing a quotation whose source is not cached, fetch it or save a snapshot first; otherwise the compare step reports NOT RUN.
- British spelling, no em-dashes, plain words, in data text and docs (`reader_text.py` enforces part of this).
- One home per kind of rule: editorial rules in `dataset.method`, field meaning in schema descriptions, invariants in code and tests, design rules in the page's principles comment. Link to the home; do not restate it.
- Select data by key or by an explicit, documented order with a tie-break; `[0]`/`slice(0, n)` on data is allowed only where a check enforces the order or uniqueness.
- Fix the class, not the instance: when you fix a defect, look for the same defect elsewhere.
- Questions: one question per decision, recommended option first.
- Agents follow the full rules in `dataset.method` (rendered at `method.html#<rule-id>`), never the reader summaries, which are owner copy.
