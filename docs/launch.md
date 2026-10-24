# Launch

Use this runbook for the first public release. Repository creation, publication, DNS changes and preview removal are owner actions. Record the release commit, check reports, public import commit, workflow run and DOI.

## Prepare the release

1. Resolve the [release checklist](../README.md#release-checklist), including draft wording, evidence limits and licence scope. Keep the documentation licence stated in the README unless the owner changes it.
2. Regenerate the tracked metadata with `uv run scripts/build.py --update-readme`. Stage the intended changes, including README, `CITATION.cff` and `.zenodo.json`.
3. Configure `AISI_SNAPSHOTS` as described in the [source snapshot guide](source-snapshots.md). Run `uv run scripts/check.py --release --out dist`. Require a successful exit and every step PASS, including private copies and quotation verification. A `dist/` directory can exist after validation fails and is not proof of a successful check.
4. Read the report and complete the independent review of the staged tree and the visual pass (`AGENTS.md`, Done means). Commit exactly the reviewed staged tree and record its full SHA. Do not export a working tree or an unreviewed branch tip.

## Create the public source tree

1. Choose an empty export directory outside the maintainer checkout. From the maintainer repository, set `release_sha` to the reviewed full SHA and `export_dir` to that empty directory. Export the commit, honouring `.gitattributes`.

   ```sh
   set -o pipefail
   git archive "$release_sha" | tar -x -C "$export_dir"
   ```

2. Inspect the exported files and scan for secrets, private paths, internal notes and research job labels. Confirm that export-ignored files (`docs/history/`, `docs/review.md`) are absent and `LICENSE`, `LICENSE-DATA`, `CITATION.cff` and `.zenodo.json` are present. Source snapshots and local reports must not be included.
3. In the export directory, run `git init -b main`, stage the import with `git add .`, and independently review that staged tree. Run the public checks before committing it.

   ```sh
   uv run scripts/check.py --release --only fast,build,gate --out dist
   uv run scripts/slide_map.py dist/maps/aisi-europe
   ```

4. Require both commands to exit successfully and read the check report. Commit the reviewed import with its `Reviewed-by:` trailer. Require `git status --porcelain --untracked-files=all` to be empty afterwards. This repository has fresh history; never push the maintainer repository's history.

## Configure hosting and release services

1. At the authorised launch, create the empty public repository under `accelerate-europe`. Enable GitHub Actions and select **Settings → Pages → Source → GitHub Actions**. The included `pages` workflow checks every branch and deploys `main`; ensure the `github-pages` environment permits `main`.
2. Verify ownership of `aisi.accelerate-europe.org` in the organisation's Pages settings using the TXT record GitHub supplies. Retain that record. In the repository's Pages settings, set the custom domain to `aisi.accelerate-europe.org` before adding the DNS record. See [domain verification](https://docs.github.com/en/pages/configuring-a-custom-domain-for-your-github-pages-site/verifying-your-custom-domain-for-github-pages).
3. Add the DNS CNAME for `aisi.accelerate-europe.org` pointing to `accelerate-europe.github.io`, without a repository path. With Actions-based Pages, configure the custom domain in repository settings; a `CNAME` file is unnecessary. Once DNS and the certificate are ready, enable **Enforce HTTPS**. Follow [GitHub's custom-domain instructions](https://docs.github.com/en/pages/configuring-a-custom-domain-for-your-github-pages-site/managing-a-custom-domain-for-your-github-pages-site). The error page's root-relative recovery and favicon links assume the custom domain root; every other page (the tracker, method and API pages) links to the favicon relatively.
4. Enable secret scanning and push protection. With Gitleaks installed, run `gitleaks git --redact --log-opts="--all" .` from the fresh public repository to scan its complete history before pushing it. Resolve findings before publication. See [GitHub secret scanning](https://docs.github.com/en/code-security/concepts/secret-security/secret-scanning).
5. Push the reviewed import and monitor both `build` and `deploy`. Once the `build` check is available, protect `main` with required pull requests and that status check; block force pushes and deletion. Verify the rule applies to maintainers. Before merging data changes, contributors run `uv run scripts/check.py --release --only fast,build,gate --out dist`; ordinary pull-request checks do not run the release wording guard. The maintainer also runs the full release check with the private source store as described above. See [protected branches](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-protected-branches/about-protected-branches).
6. Connect Zenodo to GitHub, grant access to the public repository and enable that repository in Zenodo **before** creating the first GitHub release. Check the creators, title, version and licence against `.zenodo.json`, which Zenodo reads in place of `CITATION.cff`. Create the version tag and GitHub release from the reviewed public commit, after pushing it, with `tag=$(uv run scripts/release_tag.py) && gh release create "$tag" --target "$(git rev-parse HEAD)"`, never typing the tag: the script fails unless the tag is `v` plus `dataset.version`, `.zenodo.json` at HEAD carries the same version no other version tag sits on HEAD and the tag does not already exist, locally or on `origin`, on another commit. Confirm Zenodo's archive and DOI were created and inspect their metadata; enabling the integration alone does not mint a DOI. See [Zenodo's repository setup](https://help.zenodo.org/docs/github/enable-repository/).

## Verify publication

- Check `https://aisi.accelerate-europe.org/` over HTTPS, with no authentication or certificate warning. Confirm the expected version, dates and canonical address, and that HTTP redirects to HTTPS.
- Open the page at 1440, 1000 and 390 px in light and dark themes. Check cards, filters, table, funding, disclosures and keyboard navigation against the reviewed build.
- Download JSON, CSVs, schema and data package; open the API index and representative jurisdiction and institute records. Compare the published dataset hash with the reviewed build. Open both map formats under `maps/aisi-europe` and the favicon. Download `LICENSE` and `LICENSE-DATA` and compare their SHA-256 with the packaged files.
- Check `robots.txt`, `sitemap.xml`, JSON-LD and the social preview image and metadata. Confirm indexing is allowed for the tracker and the error page is absent from the sitemap.
- Request an invented nested path and confirm HTTP 404, the custom error page, its favicon and a working return link. A successful response showing the tracker at a missing path is not a correct 404.
- Verify the protected branch, secret-scanning settings, release tag, Zenodo archive and DOI link. Retain the public workflow run and verification results.
- After these checks pass, remove the temporary preview deployment and its obsolete DNS and access configuration. Verify that the preview is no longer available and the public site still works.
