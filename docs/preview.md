# Branch previews

The `preview` job publishes a marked copy of the existing site artifact after every push to `next`, and after a run by hand of another branch (or of `next`) only when that run asks for it: `gh workflow run pages.yml --ref <branch> -f base=origin/next -f preview=true`. Without `-f preview=true` a run by hand logs `preview not requested for this run; nothing published`; work branches run CI by hand often and hold unfinished records, so their previews exist only on request. It does not wait for the live gate; the index links the run and reads its current status from the public Actions API. A run that builds no site publishes nothing. A failed site build records the attempt and keeps the previous preview. Pull requests and `main` never publish previews. The live build and deploy are unchanged.

Previews live at <https://accelerate-europe.github.io/aisi-tracker-preview/>. They are public, unpublished work. Every HTML page says so and has `noindex, nofollow`. JSON and CSV downloads cannot carry noindex on GitHub Pages. The project-path `robots.txt` is not a crawler control: crawlers read that file only at the host root.

## Repository layout

The public `accelerate-europe/aisi-tracker-preview` repository serves the root of `gh-pages`:

- `next/` and one folder per other branch, lower-case with characters outside `a-z0-9._-` replaced by `-`. Reserved names and collisions are refused.
- `b/<sha10>/`: the first successfully published build of that commit. A rerun or another branch at the same commit reuses it. The index records its original branch, run and build time.
- `manifest.json`: current builds, last attempts, run ordering and frozen provenance.
- `keep.txt`: one ten-character commit per line, with optional `#` comments. Edit it in the preview repository to keep a frozen build.
- Generated `index.html`, `404.html`, `robots.txt` and `.nojekyll`. A removed frozen URL names its commit and links to the index and `next/`.

Each publish removes folders for deleted branches, except `next`. At most the 10 newest unkept frozen builds stay (newest by first publish), each for 21 days at most, and fewer, oldest first, if the tree would exceed 900 MB (900,000,000 bytes). Kept builds and the current builds of live folders are protected and do not count towards the 10. The limits are the constants `FROZEN_COUNT`, `FROZEN_DAYS` and `BUDGET` in `scripts/preview.py`. If those copies exceed the budget, publishing fails with a message to reduce the keep list or live folders. Keeping a commit does not restore an already removed build.

Every publish creates a single parentless commit. Generated history belongs in the source repository; the preview repository keeps one snapshot. A force-with-lease protects concurrent publishes and web edits of `keep.txt`. A rejected lease starts from a fresh clone and recomputes everything, at most three tries. Older run IDs and attempts cannot replace newer ones.

## One-time owner setup

1. Create the public repository `accelerate-europe/aisi-tracker-preview` with a README. No workflow is needed there. The first publish creates `gh-pages` and `keep.txt`.
2. Generate a dedicated ed25519 SSH key. Add its public half as a deploy key on the preview repository with write access. Store its private half in the source repository's Actions secret `PREVIEW_DEPLOY_KEY`.
3. Optionally set the source repository variable `PREVIEW_FEEDBACK_MAIL`. Otherwise the banner uses `dataset.contact` from the built data. No address is stored in the workflow.
4. After the first publish, in the preview repository's Pages settings choose deployment from a branch, `gh-pages`, `/ (root)`.

Without that secret the job succeeds and publishes nothing. GitHub's branch-based Pages limit of ten builds an hour applies.

## Local checks

`uv run --locked scripts/preview.py transform DIST OUT --branch next --sha FULL_SHA --run-url RUN_URL --built ISO_TIMESTAMP` writes a new directory and leaves `DIST` untouched. It removes DOI and citation claims, keeps semver fields and regenerates the page's cache marks with the build's functions. When the shipped schema forbids `dataset.preview`, the dataset and page data carry the notice in their citation and description instead. API metadata and the data package carry preview provenance.

Run `uv run --locked scripts/preview.py transform` with those arguments, then `uv run --locked scripts/check_preview.py --dist OUT --shots SHOTS`. The browser checker uses the existing Playwright version and host browser lock. Its lock is a copy of `check_page.py.lock` with the same header requirements. It checks the marked build in branch and frozen folders under a fake project-site prefix, records requests and assumed-root references, and captures the banners at 1280 and 390 pixels in both themes, before and after hydration and with page data blocked. The live browser gate stays unchanged.

`uv run --locked pytest tests/test_preview.py -q` checks the transform, schema compatibility, path rules, frozen identity, retention and publish races without starting a browser. `uv run --locked scripts/preview.py publish TREE --transformed OUT` applies to an existing tree; omit `--transformed` to record a failed build. Its other required arguments are listed by `uv run --locked scripts/preview.py publish --help`. The `push` subcommand adds the clone and lease loop used by CI.
