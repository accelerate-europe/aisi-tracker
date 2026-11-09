# Release

The steps for every release, in order. The maintainers work in the private repository, which also holds the review notes, and keep source copies in a private store outside it; the public repository `accelerate-europe/aisi-tracker` gets each release as one exported commit, and its `main` is what the site deploys. One-time setup of the public repository, hosting and Zenodo is in [launch](launch.md).

## Release or deploy without a version

A change to `data/aisi.json` is released with a version: patch for corrections, minor for new records, status changes or additive schema fields, major for breaking schema changes. Adding a code to `dataset.vocabularies` is minor; removing or renaming one is major.

A page, code or documentation change that leaves `data/aisi.json`, `schema/aisi.schema.json` and the download files unchanged can be deployed without a version: run steps 3 to 6 unchanged (`--release` passes when the data is unchanged, and CI on public `main` runs the same release checks), and skip the changelog, GitHub release, Zenodo and private tag. A schema or download change needs a version even when the data is unchanged.

## 1. Before the release commit

- Freeze: changes to method, definition or naming text made less than 48 hours before the release go to the next release.
- Run quotation verification and refresh the private source copies ([source snapshots](source-snapshots.md)).
- Review stale full-check dates (`check.last_checked`) and the recorded evidence limits.
- At a minor or major release, in the private repository: re-check the public claims in `docs/claims.csv` against the release data and update `verdict`, `our_record` and `checked`. A verdict that flips points at a fix or a mistake.
- Blind re-check: re-derive three to five randomly chosen records from their sources without looking at the stored values first; record the differences, and check more records if it finds many.
- Fresh-eyes audit, the last data review: auditors never see the diff; each takes two or three records sampled by risk (changed institutes, high-visibility records, random controls), checks the bound quotations against the private copies, searches for anything new since the last check and reads the rendered card, table row and API file.

## 2. The release commit

1. Set `dataset.version`, and set `dataset.as_of` to the release day.
2. Finish the `changelog` entry for that version, dated the release day. Its `summary` is the release's headline on the page, in `feed.xml` and in the GitHub and Zenodo release notes, and feed readers often show nothing else: at most two plain sentences and 60 words about what changed for readers, by place or topic, with no tool or process names. Each change goes in `changes`, one line per change. A major release keeps the summary short and links migration notes from `changes`. `--release` refuses an entry that is missing, still labelled as a draft, lists no changes or has a longer summary.
3. Run `uv run scripts/build.py --update-readme`, which regenerates the README blocks, `CITATION.cff` and `.zenodo.json`.
4. Stage everything, run `uv run scripts/check.py`, read the report and commit after an independent review of the staged diff ([AGENTS.md](../AGENTS.md), Done means).

## 3. Release check

1. The previous release's tag (`v` plus its version) must be on the private repository: `--release` compares with the last `v*` tag reachable from `HEAD`, and without one it refuses to run.
2. With the private store configured (`AISI_SNAPSHOTS`), run `uv run scripts/check.py --release --out dist` on the committed release. Every step must pass and none may be NOT RUN; `dist/` is the build to publish. A `dist/` left behind by a failed run is not a passed build.
3. Full page sweep on that `dist/`: every fold open, all table rows shown, 1440, 1000 and 390 px, light and dark. Fix or log every finding, by the page review checklist in the maintainers' review notes.
4. Read the report, including its Release warnings: legal instruments whose status check is missing or more than 183 days old (rule `rescinded-instruments` in `dataset.method`). Re-verify each one and record the date, or leave the warning for the owner to accept. `--release` also fails on work dated after `as_of`, a placeholder `PUBLIC_URL`, an unfinished changelog entry, draft wording in the citation, changelog, method, schema descriptions, README, CLAUDE.md, AGENTS.md and `docs/` (except `docs/history/`), and internal process wording in data prose, schema descriptions and the changelog.

## 4. Private main

Fast-forward the private `main` to the release commit and push it. A fix found after this point (for example by the publication review) is a reviewed commit on the private `main`, and the export starts again from there.

## 5. Public export (1.x)

The 1.x releases reach the public repository as one exported commit each. From 2.0 development is public-first, and this step goes.

1. Export the release commit into an empty directory outside any checkout; `git archive` leaves out the files `.gitattributes` marks `export-ignore` (internal history, review protocol, decision log, claims list and `data/internal/`):

   ```sh
   set -o pipefail
   git -C <private-repository> archive <release-sha> | tar -x -C <export-dir>
   ```

2. Scan that directory: `gitleaks dir --redact --no-banner <export-dir>` must find nothing, and `uv run scripts/export_scan.py <export-dir>`, run from the private checkout, must report it clean: no personal home paths, dated notes citing commit IDs or any term from the private terms file (`export-terms.txt` beside the source store, or `AISI_EXPORT_TERMS`). The full check's `export` step runs the same scan earlier. Scan the export, not the public clone, whose ignored folders (`.venv`, `dist`) hold local paths.
3. In a clone of the public repository, on an up-to-date `main`, replace the tree with the same export: `git rm -rq . && git -C <private-repository> archive <release-sha> | tar -x -C . && git add -A`. `LICENSE`, `LICENSE-DATA`, `CITATION.cff` and `.zenodo.json` must be present. `uv run scripts/check.py --only fast` must pass. Build with `uv run scripts/build.py --out <build-dir>`, `<build-dir>` outside the clone, and compare it with the release build: `diff -r <build-dir> <private-repository>/dist` must show no differences.
4. Publication review: an independent reviewer reads the staged public diff for anything that should not be public (credentials, personal data, internal host or repository names, personal paths, internal notes). Commit with its `Reviewed-by:` trailer only after checking that the staged diff is the one reviewed; `git status --porcelain --untracked-files=all` must then be empty.

## 6. Push and deploy

Push the public `main`. The `pages` workflow runs the checks, the build and the browser gate in four shards; `build` is the required check, and `deploy` publishes that build. When both have passed, open the site and confirm the version and data date.

## 7. GitHub release

In the public clone, at the pushed commit, with the release notes taken from the changelog:

```sh
jq -r '.dataset.version as $v | .changelog[] | select(.version == $v) | .summary, "", (.changes[] | "- " + .)' data/aisi.json > <notes-file>
tag=$(uv run scripts/release_tag.py) && gh release create "$tag" --target "$(git rev-parse HEAD)" --title "$tag" --notes-file <notes-file>
```

`release_tag.py` prints `v` plus `dataset.version` and refuses when `.zenodo.json` at `HEAD` carries another version, `HEAD` has another version tag, or the tag already exists elsewhere, locally or on the remote. Never type the tag by hand.

## 8. Zenodo

Zenodo archives the GitHub release and mints the version DOI. Then add what `.zenodo.json` cannot carry (Accelerate Europe as organisational creator, so the record's citation matches ours, and the MIT licence for the code):

```sh
uv run scripts/zenodo_metadata.py           # diff against the new Zenodo version
uv run scripts/zenodo_metadata.py --apply   # needs ZENODO_TOKEN
uv run scripts/zenodo_metadata.py           # must report nothing to do
```

Zenodo's API can serve the old record for about a minute after `--apply`. The script rereads it for up to 2 minutes; if the final diff still shows the old values right after "published; record ... now matches", wait a minute and run it again.

## 9. After the release

- Tag the private commit you exported with the same tag and push the tag; the next release check compares with it.
- Within three days, reopen the news and publications pages of every institute, record a `check.partial` entry for each and add anything new.
