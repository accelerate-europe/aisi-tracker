# Release

The steps for every release, in order. Development is public: work lands on `next` in the public repository `accelerate-europe/aisi-tracker`, and its `main` is what the site deploys. A release moves `main` forward to a reviewed `next` commit. The maintainers' review notes and the public claims list stay in the private repository, read from its checkout, and source copies stay in a private store. One-time setup of the public repository, hosting and Zenodo is in [launch](launch.md).

## Release or deploy without a version

A change to `data/aisi.json` is released with a version: patch for corrections, minor for new records, status changes or additive schema fields, major for breaking schema changes. Adding a code to `dataset.vocabularies` is minor; removing or renaming one is major.

A page, code or documentation change that leaves `data/aisi.json`, `schema/aisi.schema.json` and the download files unchanged can be deployed without a version: run steps 3 and 4 unchanged (`--release` passes when the data is unchanged, and CI on public `main` runs the same release checks), and skip the changelog, GitHub release and Zenodo. A schema or download change needs a version even when the data is unchanged.

## 1. Before the release commit

- Method, definition or naming text: a change ships once the owner has approved its final wording and its rule-impact table (`uv run scripts/rule_tables.py`), with no waiting time. A change to the definition or to `dataset.status_definitions` also re-checks the public claims in the private repository's `docs/claims.csv` before the release, at any version, as the claims bullet below describes.
- Run quotation verification and refresh the private source copies ([source snapshots](source-snapshots.md)).
- Review stale full-check dates (`check.last_checked`) and the recorded evidence limits.
- At a minor or major release, in the private repository checkout: re-check the public claims in its `docs/claims.csv` against the release data and update `verdict`, `our_record` and `checked`. A verdict that flips points at a fix or a mistake.
- Blind re-check: re-derive three to five randomly chosen records from their sources without looking at the stored values first; record the differences, and check more records if it finds many.
- Fresh-eyes audit, the last data review: auditors never see the diff; each takes two or three records sampled by risk (changed institutes, high-visibility records, random controls), checks the bound quotations against the private copies, searches for anything new since the last check and reads the rendered card, table row and API file.

## 2. The release commit

1. Set `dataset.version`, and set `dataset.as_of` to the release day.
2. Finish the `changelog` entry for that version, dated the release day. Its `summary` is the release's headline on the page, in `feed.xml` and in the GitHub and Zenodo release notes, and feed readers often show nothing else: at most two plain sentences and 60 words about what changed for readers, by place or topic, with no tool or process names. Each change goes in `changes`, one line per change. A major release keeps the summary short and links migration notes from `changes`. `--release` refuses an entry that is missing, still labelled as a draft, lists no changes or has a longer summary.
3. Run `uv run scripts/build.py --update-readme`, which regenerates the README blocks, `CITATION.cff` and `.zenodo.json`.
4. Stage everything, run `uv run scripts/check.py`, read the report and commit after an independent review of the staged diff ([AGENTS.md](../AGENTS.md), Done means). The release commit goes onto `next` like any other reviewed commit.

## 3. Release check

1. The previous release's tag (`v` plus its version) must be in your clone (`git fetch --tags`): `--release` compares with the last `v*` tag reachable from `HEAD`, and without one it refuses to run.
2. With the private store configured (`AISI_SNAPSHOTS`), run `uv run scripts/check.py --release --out dist` on the committed release. Every step must pass and none may be NOT RUN; `dist/` is the build to sweep. A `dist/` left behind by a failed run is not a passed build.
3. Full page sweep on that `dist/`: every fold open, all table rows shown, 1440, 1000 and 390 px, light and dark. Fix or log every finding, by the page review checklist in the maintainers' review notes (`docs/review.md` in the private repository checkout).
4. Read the report, including its Release warnings: legal instruments whose status check is missing or more than 183 days old (rule `rescinded-instruments` in `dataset.method`). Re-verify each one and record the date, or leave the warning for the owner to accept. `--release` also fails on work dated after `as_of`, a placeholder `PUBLIC_URL`, an unfinished changelog entry, draft wording in the citation, changelog, method, schema descriptions, README, CLAUDE.md, AGENTS.md and `docs/` (except `docs/history/`), and internal process wording in data prose, schema descriptions and the changelog.

## 4. Move `main` and deploy

The owner's Go is needed for this step. CI must have passed on the release commit on `next`, including the browser gate.

1. Fast-forward public `main` to that commit and push it: `git push origin <release-sha>:main`. Never force: a push that is not a fast-forward is refused, and the release starts again from an up-to-date `next`. The pre-push hook (`.githooks/pre-push`) runs gitleaks and the internal-names scan on the commits being pushed, and refuses the push when the private terms file is missing; CI runs both again.
2. The `pages` workflow runs the checks, the build and the browser gate in four shards; `build` is the required check, and `deploy` publishes that build. When both have passed, open the site and confirm the version and data date.

A fix found after this point is a reviewed commit on `next`, and the release starts again from step 3.

## 5. GitHub release

In the public clone, at the pushed commit, with the release notes taken from the changelog:

```sh
jq -r '.dataset.version as $v | .changelog[] | select(.version == $v) | .summary, "", (.changes[] | "- " + .)' data/aisi.json > <notes-file>
tag=$(uv run scripts/release_tag.py) && gh release create "$tag" --target "$(git rev-parse HEAD)" --title "$tag" --notes-file <notes-file>
```

`release_tag.py` prints `v` plus `dataset.version` and refuses when `.zenodo.json` at `HEAD` carries another version, `HEAD` has another version tag, or the tag already exists elsewhere, locally or on the remote. Never type the tag by hand.

## 6. Zenodo

Zenodo archives the GitHub release and mints the version DOI. Then add what `.zenodo.json` cannot carry (Accelerate Europe as organisational creator, so the record's citation matches ours, and the MIT licence for the code):

```sh
uv run scripts/zenodo_metadata.py           # diff against the new Zenodo version
uv run scripts/zenodo_metadata.py --apply   # needs ZENODO_TOKEN
uv run scripts/zenodo_metadata.py           # must report nothing to do
```

Zenodo's API can serve the old record for about a minute after `--apply`. The script rereads it for up to 2 minutes; if the final diff still shows the old values right after "published; record ... now matches", wait a minute and run it again.

## 7. After the release

- Fetch the new tag (`git fetch --tags`); the next release check compares with it.
- Within three days, reopen the news and publications pages of every institute, record a `check.partial` entry for each and add anything new.
