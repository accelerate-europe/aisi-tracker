# Release

The steps for every release, in order. Development is public: work lands on `next` in the public repository `accelerate-europe/aisi-tracker`, and its `main` is what the site deploys. A release moves `main` forward to a reviewed `next` commit. The maintainers' review notes and the public claims list stay in the private repository, read from its checkout, and source copies stay in a private store. One-time setup of the public repository, hosting and Zenodo is in [launch](launch.md).

## Release or deploy without a version

A change to `data/aisi.json` is released with a version: patch for corrections, minor for new records, status changes or additive schema fields, major for breaking schema changes. Adding a code to `dataset.vocabularies` is minor; removing or renaming one is major, except fields and vocabularies listed in `dataset.provisional`, which may change in a minor with a changelog line.

A page, code or documentation change that leaves `data/aisi.json`, `schema/aisi.schema.json` and the download files unchanged can be deployed without a version: run steps 3 and 4 unchanged (`--release` passes when the data is unchanged, and CI on public `main` runs the same release checks), and skip the changelog, GitHub release and Zenodo. A schema or download change needs a version even when the data is unchanged.

## 1. Before the release commit

- Method, definition or naming text: a change ships once the owner has approved its final wording and its rule-impact table (`uv run --locked scripts/rule_tables.py`), with no waiting time. A change to the definition or to `dataset.status_definitions` also re-checks the public claims in the private repository's `docs/claims.csv` before the release, at any version, as the claims bullet below describes.
- Run quotation verification and refresh the private source copies ([source snapshots](source-snapshots.md)).
- Review stale full-check dates (`check.last_checked`) and the recorded evidence limits.
- At a minor or major release, in the private repository checkout: re-check the public claims in its `docs/claims.csv` against the release data and update `verdict`, `our_record` and `checked`. A verdict that flips points at a fix or a mistake.
- Blind sample, at every minor or major release; it measures an error rate, so it is never skipped or narrowed to records thought risky. A patch release keeps the smaller blind re-check: re-derive three to five randomly chosen records from their sources without looking at the stored values first, record the differences, and check more records if it finds many. The sample, in a research pass folder ([research passes](research-passes.md)):
  1. Draw about 10 records at random with a recorded seed, from the institutes (a field any excerpt supports, then one unit of it, so that the outputs do not swamp the draw) and from the jurisdictions without an institute (the whole record).
  2. Re-derive each one blind: the deriver sees the task, the cited sources' addresses and private copies and a fresh search up to the release day, never the stored value.
  3. Compare claim by claim and give each claim one class: correct, wording only, wrong fact, stale (right when checked, overtaken since) or unsupported (not carried by the cited source, even where probably true). A record takes its worst class, in the order wrong fact, unsupported, stale, wording only, correct. Omissions are listed for triage but not counted as errors.
  4. Adjudicate every claim not classed correct against the full private copies, by someone other than the comparer: blind re-derivations erred in about a third of the items of the first sample (14 November 2026), so a difference is not yet an error. The adjudicated classes are final.
  5. Report the error rate (records with a wrong fact, stale or unsupported claim, of those drawn, with a 95% interval), separately for institutes and for jurisdictions without an institute, and name any error that would change what an official concludes. It goes into the release summary the owner reads before the Go, not the public changelog. Fixes go in as reviewed data commits like any other.
- Fresh-eyes audit, the last data review: auditors never see the diff; each takes two or three records sampled by risk (changed institutes, high-visibility records, random controls), checks the bound quotations against the private copies, searches for anything new since the last check and reads the rendered card, table row and API file in the site CI built for the `next` commit the audit covers (`uv run --locked scripts/ci_run.py site <sha> --out <folder>`).

## 2. The release commit

1. Set `dataset.version`, and set `dataset.as_of` to the release day. Leave `dataset.citation`: step 3 writes its version and date.
2. Finish the `changelog` entry for that version, dated the release day. Its `summary` is the release's headline on the page, in `feed.xml` and in the GitHub and Zenodo release notes, and feed readers often show nothing else. It leads with the one or two changes that matter most to readers, in at most two plain sentences of at most 25 words each, with no tool or process names; the summary is not a list, the changes are. Each change goes in `changes`, one line per change. A major release keeps the summary short and links migration notes from `changes`. `--release` refuses an entry that is missing, still labelled as a draft, lists no changes, or has more than two sentences or a sentence over 25 words.
3. Run `uv run --locked scripts/build.py --update-readme`, which sets the version and date in `dataset.citation` from `dataset.version` and `as_of` and regenerates the README blocks, `CITATION.cff` and `.zenodo.json`.
4. Stage everything, run `uv run --locked scripts/check.py`, read the report and commit after an independent review of the staged diff ([AGENTS.md](../AGENTS.md), Done means). The release commit goes onto `next` like any other reviewed commit.

## 3. Release check

1. The previous release's tag (`v` plus its version) must be in your clone (`git fetch --tags`): `--release` compares with the last `v*` tag reachable from `HEAD`, and without one it refuses to run.
2. With the private store configured (`AISI_SNAPSHOTS`), run `uv run --locked scripts/check.py --release --out dist` on the committed release. Every step must pass and none may be NOT RUN; `dist/` is the build to sweep. A `dist/` left behind by a failed run is not a passed build.
3. Full page sweep of the release commit: every fold open, all table rows shown, 1440, 1000 and 390 px, light and dark. On CI, with the release commit pushed to `next`: `gh workflow run sweep.yml --ref next`, then `gh run download <run-id> -n sweep -D <absolute-dir>` (the run from `gh run list --workflow sweep.yml`; gh 2.45 refuses `-D .` as path traversal) and `tar -xf sweep.tar` there. On a host with memory for a browser and Noto fonts for every script in the data, `uv run --locked scripts/sweep_page.py --dist dist --out DIR` sweeps the local `dist/` the same way; on a shared host use CI. The output holds 1600 px slices `<width>-<theme>-NN.png` and `sweep.json` (controls opened, page height, overflow, folds left closed, table rows shown, console errors); the sweep fails on console errors, horizontal overflow, a collapsed button or a table still filtered. Read every slice and fix or log every finding, by the page review checklist in the maintainers' review notes (`docs/review.md` in the private repository checkout).
4. Read the report, including its Release warnings: legal instruments whose status check is missing or more than 183 days old (rule `rescinded-instruments` in `dataset.method`). Re-verify each one and record the date, or leave the warning for the owner to accept. They also list the accepted exceptions to rule `status-evidence`: timeline events that move a status, or launch dates, for which no primary text could be retrieved (`PRIMARY_SOURCE_EXCEPTIONS` in `scripts/method_checks.py`). Any other such event whose date or act no primary source carries fails validation, as does a listed exception that is no longer flagged or whose cited source has no access note. A count per kind follows of facts stated where no excerpt is required and none is bound: related bodies with no source outside Europe (a Europe-scope one fails validation), roles, stages and designation states with no excerpt, and notes, summaries and descriptions with no excerpt (`unquoted_claims`, same file; the full list is `unquoted-claims.md` beside the warnings). `--release` also fails on work dated after `as_of`, a placeholder `PUBLIC_URL`, an unfinished changelog entry, draft wording in the citation, changelog, method, schema descriptions, README, CLAUDE.md, AGENTS.md and `docs/` (except `docs/history/`), and internal process wording in data prose, schema descriptions and the changelog. Every CI run already applies all of these except the rule on work dated after `as_of` (`--release-wording`), so wording is refused by the next CI run (a pull request, a run by hand, or a push to `next`), not first at the release.

## 4. Move `main` and deploy

The owner's Go is needed for this step. CI must have passed on the release commit on `next`, including the browser gate; every push to `next` and to `main` gets the full run, whatever changed (`uv run --locked scripts/ci_run.py wait <sha> --branch next` waits for the run and names any failed job).

1. Fast-forward public `main` to that commit and push it: `git push origin <release-sha>:main`. Never force: a push that is not a fast-forward is refused, and the release starts again from an up-to-date `next`. The pre-push hook (`.githooks/pre-push`) runs gitleaks and the internal-names scan on the commits being pushed, and refuses the push when the private terms file is missing; CI runs both again.
2. The `pages` workflow runs the checks, the build and the browser gate in four shards; `build` is the required check, and `deploy` publishes that build. When both have passed (`uv run --locked scripts/ci_run.py wait <release-sha> --branch main`), open the site and confirm the version and data date.

A fix found after this point is a reviewed commit on `next`, and the release starts again from step 3.

## 5. GitHub release

In the public clone, at the pushed commit, with the release notes taken from the changelog:

```sh
jq -r '.dataset.version as $v | .changelog[] | select(.version == $v) | .summary, "", (.changes[] | "- " + .)' data/aisi.json > <notes-file>
tag=$(uv run --locked scripts/release_tag.py) && gh release create "$tag" --target "$(git rev-parse HEAD)" --title "$tag" --notes-file <notes-file>
```

`release_tag.py` prints `v` plus `dataset.version` and refuses when `.zenodo.json` at `HEAD` carries another version, `HEAD` has another version tag, or the tag already exists elsewhere, locally or on the remote. Never type the tag by hand.

## 6. Zenodo

Zenodo archives the GitHub release and mints the version DOI. Then add what `.zenodo.json` cannot carry (Accelerate Europe as organisational creator, so the record's citation matches ours, and the MIT licence for the code):

```sh
uv run --locked scripts/zenodo_metadata.py           # diff against the new Zenodo version
uv run --locked scripts/zenodo_metadata.py --apply   # needs ZENODO_TOKEN
uv run --locked scripts/zenodo_metadata.py           # must report nothing to do
```

Zenodo's API can serve the old record for about a minute after `--apply`. The script rereads it for up to 2 minutes; if the final diff still shows the old values right after "published; record ... now matches", wait a minute and run it again.

## 7. After the release

- Fetch the new tag (`git fetch --tags`); the next release check compares with it.
- Within three days, reopen the news and publications pages of every institute, record a `check.partial` entry for each and add anything new.
