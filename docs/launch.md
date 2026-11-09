# Launch

One-time setup for the first public release; every release, the first included, then follows the [release runbook](release.md). Repository creation, publication, DNS changes and preview removal are owner actions.

## Before the first release

- Set `PUBLIC_URL` in `scripts/agent_files.py` to the public site address; `--release` fails while it is the placeholder. Run `uv run scripts/build.py --update-readme`, since `CITATION.cff` and `.zenodo.json` carry the address.
- The first public commit was the export of the reviewed 1.0.0 release commit (the 1.x export step, retired when development became public-first), initialised with `git init -b main` in an empty directory: fresh history, never the private repository's.

## Repository and hosting

1. Create the empty public repository under `accelerate-europe`. Enable GitHub Actions and select **Settings → Pages → Source → GitHub Actions**. The `pages` workflow checks every branch and deploys `main`; the `github-pages` environment must allow `main`.
2. Verify ownership of `aisi.accelerate-europe.org` in the organisation's Pages settings with the TXT record GitHub supplies, and keep that record. Set the repository's custom domain to `aisi.accelerate-europe.org` before adding the DNS record ([domain verification](https://docs.github.com/en/pages/configuring-a-custom-domain-for-your-github-pages-site/verifying-your-custom-domain-for-github-pages)).
3. Add the DNS CNAME for `aisi.accelerate-europe.org` pointing to `accelerate-europe.github.io`, without a repository path; with Actions-based Pages no `CNAME` file is needed. Once DNS and the certificate are ready, enable **Enforce HTTPS** ([custom domains](https://docs.github.com/en/pages/configuring-a-custom-domain-for-your-github-pages-site/managing-a-custom-domain-for-your-github-pages-site)). The error page's recovery and favicon links are root-relative and assume the custom domain; every other page links to the favicon relatively.
4. Enable secret scanning with push protection. Before the first push, scan the fresh repository's whole history: `gitleaks git --redact --log-opts="--all" .` ([secret scanning](https://docs.github.com/en/code-security/concepts/secret-security/secret-scanning)).
5. After the first push, once the `build` check exists, protect `main`: required pull requests and the `build` check, no force pushes or deletion, applying to maintainers too ([protected branches](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-protected-branches/about-protected-branches)). Contributors run `uv run scripts/check.py --release --only fast,build,gate --out dist` before merging data changes; ordinary pull-request checks do not run the release wording check.
6. Connect Zenodo to GitHub and enable the public repository in Zenodo **before** the first GitHub release; enabling it does not mint a DOI by itself. Zenodo reads `.zenodo.json` in place of `CITATION.cff`: check its creators, title, version and licence ([Zenodo's repository setup](https://help.zenodo.org/docs/github/enable-repository/)).

## Verify publication

- `https://aisi.accelerate-europe.org/` loads over HTTPS without authentication or certificate warnings, HTTP redirects to HTTPS, and the version, dates and canonical address are as expected.
- The page at 1440, 1000 and 390 px in light and dark themes matches the reviewed build: cards, filters, table, funding, disclosures and keyboard navigation.
- JSON, CSVs, schema, data package, the API index and sample jurisdiction and institute records download; the published dataset hash matches the reviewed build; both map formats under `maps/aisi-europe` and the favicon open; `LICENSE` and `LICENSE-DATA` match the packaged files by SHA-256.
- `robots.txt`, `sitemap.xml`, JSON-LD and the social preview image and metadata are right; the tracker may be indexed and the error page is not in the sitemap.
- An invented nested path returns HTTP 404 with the custom error page, its favicon and a working return link; the tracker served at a missing path is not a correct 404.
- The protected branch, secret scanning, release tag, Zenodo archive and DOI link are in place.
- Then remove the temporary preview deployment and its DNS and access configuration, and check that the preview is gone and the public site still works.
