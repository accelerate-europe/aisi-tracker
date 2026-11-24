# Release

The steps for every release, in order. Development is public: work lands on `next` in the public repository `accelerate-europe/aisi-tracker`, and its `main` is what the site deploys. A release is one reviewed commit on top of `next`; once CI has passed on it, `next` and `main` both move to that commit. The maintainers' review notes and the public claims list stay in the private repository, read from its checkout, and source copies stay in a private store. One-time setup of the public repository, hosting and Zenodo is in [launch](launch.md).

A release checks what changed since the last release tag. The whole dataset is audited once a week, apart from any release ([weekly audit](#weekly-audit)), and the release takes in that audit's findings.

## Versions during development

A change to `data/aisi.json` is released with a version: patch for corrections, minor for new records, status changes or additive schema fields, major for breaking schema changes. Adding a code to `dataset.vocabularies` is minor; removing or renaming one is major, except fields and vocabularies listed in `dataset.provisional`, which may change in a minor with a changelog line.

A page, code or documentation fix that must go live is a patch release through all the steps below. Deployments without a version are retired.

Between releases, `next` carries the planned next version with `-dev`, normally the next patch (for example, `1.1.1-dev` after `1.1.0`). Only `next` changes that line; work and data branches inherit it. A branch planning a minor or major release changes the planned number when it merges into `next`. There are no development entries in the changelog.

The authored `dataset.as_of` stays at the last release date. Development builds publish the latest date of recorded checking or source work, including re-reads, never earlier than that baseline. Raw downloads from GitHub retain the authored baseline. The citation, README citation block, `CITATION.cff` and `.zenodo.json` still describe the last release; README counts and definitions stay current. `--update-readme` works during development when those blocks need refreshing.

On refs other than `main`, CI refuses a stable version whose `v<version>` tag points at another commit, even if the data is unchanged. An untagged release candidate or a tag on the same commit passes. `--release`, the tag helper and the Zenodo step refuse `-dev`.

## 1. Before the release commit

- Start from a `next` whose latest page sweep has been read. Every push to `next` that changes the data, the page or the scripts starts the sweep (`.github/workflows/sweep.yml`); if `next`'s head has none, start one at once with `gh workflow run sweep.yml --ref next` and do the rest of this step while it runs (about 10 minutes). Reading it is described in [reading a sweep](#reading-a-sweep). Fixes go into `next` as reviewed commits before the release commit.
- Take in the latest [weekly audit](#weekly-audit): its error rate goes into the release summary for the owner, and its open findings are fixed or listed there as open.
- Method, definition or naming text: a change ships once the owner has approved its final wording and its rule-impact table (`uv run --locked scripts/rule_tables.py`), with no waiting time. A change to the definition or to `dataset.status_definitions` also re-checks the public claims in the private repository's `docs/claims.csv` before the release, at any version, as the claims bullet below describes.
- The release's scope: after `git fetch --tags`, `uv run --locked scripts/release_scope.py --seed <seed> --out <folder>` compares `next` with the last `v*` tag and writes, by ID only, the records, claims, excerpts and sources that changed, the dataset text that changed (method, definitions, vocabularies: the owner's wording approval above, not a record check), the sources whose quotations need verifying and a seeded random draw of changed records for the blind re-check. `scope.json` and `scope.md` carry no stored values or quotations and may be given to derivers and auditors; `detail.json` holds the before and after values for the comparer only. An empty list means nothing of that kind changed. Record the seed in the pass folder.
- Quotation verification of the scope's sources, and a refresh of their private copies ([source snapshots](source-snapshots.md)): `scope.md` prints the `scripts/verify_excerpts.py` command with one `--source` per source.
- At a minor or major release, in the private repository checkout: re-check the public claims in its `docs/claims.csv` against the release data and update `verdict`, `our_record` and `checked`. A verdict that flips points at a fix or a mistake.
- Blind re-check of changed records, at every release, in a research pass folder ([research passes](research-passes.md)): the scope's draw of changed records, at least three (`--draw`, default 4; about 10 at a minor or major release; all of them when fewer changed), re-derived blind, compared and adjudicated as in steps 2 to 4 of the [blind sample](#blind-sample). Record the differences and check more changed records if it finds many. The result goes into the release summary the owner reads before the Go, not the public changelog. Fixes go in as reviewed data commits like any other.
- Fresh-eyes read, the last data review: auditors never see the diff; each takes two or three of the scope's changed records, chosen by risk (changed institutes, high-visibility records first), checks the bound quotations against the private copies, searches for anything new since the last check and reads the rendered card, table row and API file in the site CI built for the `next` commit the read covers (`uv run --locked scripts/ci_run.py site <sha> --out <folder>`).

## 2. The release commit

On a branch `release-X.Y.Z` from `next`'s head, so that `next` can later fast-forward to it:

1. Remove `-dev` from `dataset.version`, and set `dataset.as_of` to the release day. Leave `dataset.citation`: step 3 writes its version and date.
2. Finish the `changelog` entry for that version, dated the release day. Its `summary` is the release's headline on the page, in `feed.xml` and in the GitHub and Zenodo release notes, and feed readers often show nothing else. It leads with the one or two changes that matter most to readers, in at most two plain sentences of at most 25 words each, with no tool or process names; the summary is not a list, the changes are. Each change goes in `changes`, one line per change. A major release keeps the summary short and links migration notes from `changes`. `--release` refuses an entry that is missing, still labelled as a draft, lists no changes, or has more than two sentences or a sentence over 25 words.
3. Run `uv run --locked scripts/build.py --update-readme`, which sets the version and date in `dataset.citation` from `dataset.version` and `as_of` and regenerates the README blocks, `CITATION.cff` and `.zenodo.json`.
4. Stage everything and commit after an independent review of the staged diff ([AGENTS.md](../AGENTS.md), Done means). The commit does not go onto `next` yet: `next` moves in step 4, together with `main`.

## 3. Release check

1. The previous release's tag (`v` plus its version) must be in your clone (`git fetch --tags`): `--release` compares with the last `v*` tag reachable from `HEAD`, and without one it refuses to run.
2. With the private store configured (`AISI_SNAPSHOTS`) and nothing staged beyond the release commit, run `uv run --locked scripts/check.py --release --only local`. It runs validation with the release rules (including work dated after `as_of`, which CI applies only on `main`), lint and the export scan, which CI repeats, and the steps that need the private store and never run on CI: copies, passes and the comparison with the last tag. Every step must pass and none may be NOT RUN.
3. Push the release branch and start its CI run and its sweep by hand: `gh workflow run pages.yml --ref release-X.Y.Z -f base=origin/next` and `gh workflow run sweep.yml --ref release-X.Y.Z`. Wait with `uv run --locked scripts/ci_run.py wait <release-sha>`. This run is the release's one CI run before `main` moves: the unit tests, the build, the content checks, the browser gate in four shards and the layout walk, on the exact commit that will be deployed. No contact sheet is taken; the sweeps of `next` and of the release commit show the whole page instead.
4. Read the sweep of the release commit ([reading a sweep](#reading-a-sweep)). A fault it finds is fixed on the release branch as a reviewed commit, and the release check runs again from step 3.2 on the new head.
5. Read the report of step 2, including its Release warnings: legal instruments whose status check is missing or more than 183 days old (rule `rescinded-instruments` in `dataset.method`). Re-verify each one and record the date, or leave the warning for the owner to accept. They also list the accepted exceptions to rule `status-evidence`: timeline events that move a status, or launch dates, for which no primary text could be retrieved (`PRIMARY_SOURCE_EXCEPTIONS` in `scripts/method_checks.py`). Any other such event whose date or act no primary source carries fails validation, as does a listed exception that is no longer flagged or whose cited source has no access note. A count per kind follows of facts stated where no excerpt is required and none is bound: related bodies with no source outside Europe (a Europe-scope one fails validation), roles, stages and designation states with no excerpt, and notes, summaries and descriptions with no excerpt (`unquoted_claims`, same file; the full list is `unquoted-claims.md` beside the warnings). `--release` also fails on work dated after `as_of`, a placeholder `PUBLIC_URL`, an unfinished changelog entry, draft wording in the citation, changelog, method, schema descriptions, README, CLAUDE.md, AGENTS.md and `docs/` (except `docs/history/`), and internal process wording in data prose, schema descriptions and the changelog. Every CI run already applies all of these except the rule on work dated after `as_of` (`--release-wording`), so wording is refused by the next CI run (a pull request, a run by hand, or a push to `next`), not first at the release.

### Reading a sweep

Download the run's six artifacts with `gh run download <run-id> -p 'sweep-*' -D <absolute-dir>` (the run from `gh run list --workflow sweep.yml --branch <branch>`; gh 2.45 refuses `-D .` as path traversal) and, in that directory, `for dir in sweep-*/; do tar -xf "${dir}sweep.tar" -C "$dir"; done` to unpack them into their own directories (`sweep-<width>-<theme>/sweep/`). The sweep opens every fold and shows all table rows at 1440, 1000 and 390 px, light and dark. On a host with memory for a browser and Noto fonts for every script in the data, `uv run --locked scripts/sweep_page.py --dist dist --out DIR` sweeps a local build the same way; on a shared host use CI. The output holds 1600 px slices `<width>-<theme>-NN.png`, extra Functions row and grid images, Funding record images, and `sweep.json` (controls opened, page height, overflow, folds left closed, table rows shown, console errors, Functions and Funding capture counts and failures); the sweep fails on console errors, horizontal overflow, a collapsed button outside a single-open group, a table still filtered or a failed Functions row, grid or Funding record capture. Read every slice and fix or log every finding, by the page review checklist in the maintainers' review notes (`docs/review.md` in the private repository checkout).

## 4. Move `next` and `main`, and deploy

The owner's Go is needed for this step. CI must have passed on the release commit on its branch (step 3), and `next`'s head must still be an ancestor of it, so that `next` fast-forwards; if `next` has moved, rebase the release branch onto it, run the checks of step 1 on what `next` gained (`release_scope.py --base <old next head>`), and run the release check again from step 3.2.

1. Push the release commit to `next`, then to `main`, as two separate commands, checking that each one succeeded before the next: `git push origin <release-sha>:next`, then `git push origin <release-sha>:main`. Never chain them with `&&` behind other commands, and never force: a push that is not a fast-forward is refused, and the release starts again from an up-to-date `next`. The pre-push hook (`.githooks/pre-push`) runs gitleaks and the internal-names scan on the commits being pushed, and refuses the push when the private terms file is missing; CI runs both again.
2. The push to `main` starts the `pages` workflow there, which runs the release checks and the build again, publishes with `deploy` and then checks the live site (`smoke`). Wait for it with `uv run --locked scripts/ci_run.py wait <release-sha> --branch main`. The push to `next` starts its own run of the same commit; nothing waits for it.

A fix that must go live after this point is another patch release, starting at step 1.

## 5. After `main` has moved

The GitHub release and the Zenodo step need the owner's Go, like moving `main`; one Go may cover step 4 and this step when the owner says so. In a clone checked out at the release commit (detached is fine):

```sh
uv run --locked scripts/release_after.py         # prints what it will do and checks what it can without writing
uv run --locked scripts/release_after.py --act --work <folder>   # does it; the notes and the development worktree go in <folder>
```

It checks first that `HEAD` carries a released version, equals `origin/main` and is on `origin/next`, and that CI passed on `main` for it. Then, one step at a time, stopping at the first that fails and never running a later one:

1. The live site serves the new version (`scripts/smoke_site.py --version`).
2. The GitHub release: notes from the version's changelog entry (summary, then one line per change), tag from `scripts/release_tag.py`, which refuses `-dev`, a different version in `.zenodo.json` at `HEAD`, another version tag on `HEAD`, or the tag already existing elsewhere, locally or on the remote. Never type the tag by hand.
3. Zenodo archives the GitHub release and mints the version DOI; `scripts/zenodo_metadata.py` then adds what `.zenodo.json` cannot carry (Accelerate Europe as organisational creator, so the record's citation matches ours, and the MIT licence for the code): diff, `--apply`, diff again, which must report nothing to do. The script waits for Zenodo to hold the new version and for its API, which can serve the old record for about a minute after `--apply`. The Zenodo token is read only inside the script, from the maintainers' token file it names, and is never printed or put on a command line.
4. `git fetch --tags`; the next release check compares with the new tag.
5. The next development version: on a new branch `dev-X.Y.(Z+1)` from `origin/next`, `dataset.version` becomes `X.Y.(Z+1)-dev` and `scripts/build.py --update-readme` refreshes the README; `as_of`, the citation and the changelog stay at the release. It stages the change and stops: review, commit, push, run CI by hand and merge into `next` as usual. A planned minor or major release can change that number when its branch merges into `next`.

Run again after a stop, it skips the steps already done. Within three days of the release, reopen the news and publications pages of every institute, record a `check.partial` entry for each and add anything new.

## Weekly audit

The whole dataset is audited once a week, on its own schedule and not tied to a release. It runs as research passes in the private store ([research passes](research-passes.md)) and produces, for each week, a pass folder with the draw and seed, the derivations, the comparisons, the adjudications, the error rate and a findings list. Findings go into `next` as reviewed data commits; the next release reports the latest error rate to the owner and lists any finding still open.

- Quotation verification of all sources (`uv run --locked scripts/verify_excerpts.py`) and a refresh of the private copies ([source snapshots](source-snapshots.md)).
- Stale full-check dates (`check.last_checked`) and the recorded evidence limits.
- The blind sample below.
- Fresh-eyes audit: auditors never see recent diffs; each takes two or three records sampled by risk (changed institutes, high-visibility records, random controls), checks the bound quotations against the private copies, searches for anything new since the last check and reads the rendered card, table row and API file in the site CI built for the `next` commit the audit covers (`uv run --locked scripts/ci_run.py site <sha> --out <folder>`).

### Blind sample

It measures an error rate, so it is never skipped or narrowed to records thought risky:

1. Draw about 10 records at random with a recorded seed, from the institutes (a field any excerpt supports, then one unit of it, so that the outputs do not swamp the draw) and from the jurisdictions without an institute (the whole record).
2. Re-derive each one blind: the deriver sees the task, the cited sources' addresses and private copies and a fresh search up to the day of the audit, never the stored value.
3. Compare claim by claim and give each claim one class: correct, wording only, wrong fact, stale (right when checked, overtaken since) or unsupported (not carried by the cited source, even where probably true). A record takes its worst class, in the order wrong fact, unsupported, stale, wording only, correct. Omissions are listed for triage but not counted as errors.
4. Adjudicate every claim not classed correct against the full private copies, by someone other than the comparer: blind re-derivations erred in about a third of the items of the first sample (14 November 2026), so a difference is not yet an error. The adjudicated classes are final.
5. Report the error rate (records with a wrong fact, stale or unsupported claim, of those drawn, with a 95% interval), separately for institutes and for jurisdictions without an institute, and name any error that would change what an official concludes. It goes into the next release summary the owner reads before the Go, not the public changelog.
