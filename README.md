# AI Security Institutes tracker

[![DOI](https://zenodo.org/badge/DOI/10.5281/zenodo.23600431.svg)](https://doi.org/10.5281/zenodo.23600431) · Browse the tracker at [aisi.accelerate-europe.org](https://aisi.accelerate-europe.org)

This tracker shows which states have an AI Security Institute (AISI) and, where published, its funding, staff and leadership. Every jurisdiction and AISI cites its sources and the date of its last recorded full check, marked where that date is only an upper bound. Funding, staff and leadership figures also carry a confidence rating. Published by Accelerate Europe under CC BY 4.0, DOI [10.5281/zenodo.23600431](https://doi.org/10.5281/zenodo.23600431).

<!-- definition:start -->

We count a body as an AI Security Institute (AISI) if a national government or an international organisation, such as the EU, has tasked it with testing general-purpose or frontier AI models for their capabilities and risks, or has named it its AI safety institute or its AI security institute. Being a regulator, certification body or cyber security agency is not enough on its own. [What counts](https://aisi.accelerate-europe.org/method.html#qualifying-body)

<!-- definition:end -->

<!-- counts:start -->

Version 1.0.6, data as of 9 November 2026: 195 jurisdictions, 15 AISIs, 134 outputs, 56 agreements, 9 network milestones and 1,193 sources. For the 178 jurisdictions where our searches found no qualifying government step towards an AISI, our confidence in that finding is low for 34, medium for 144 and high for none.

<!-- counts:end -->

## What's in the data

| Entry | What it is |
| --- | --- |
| Jurisdiction | One of the 193 UN member states, Taiwan or the EU, with its AISI status: established, planned, proposed, exploring or none identified ([definitions](https://aisi.accelerate-europe.org/method.html)). |
| AISI | A body that meets the definition above, or a named body planned or formally proposed for that role. |
| Related body | A body that could be mistaken for an AISI, such as an AI Act authority. Its record says why it does not qualify. Recorded for its jurisdiction, not counted. |
| Output | A report, joint test, guidance, evaluation, tool or dataset credited to an AISI or a predecessor. Joint work appears under each of the involved AISIs. |
| Agreement | An agreement that names an AISI as a party or implementer, with an AI developer, a government, another AISI or another organisation. A company's pledge to give an AISI model access is listed too. Each is signed, announced or pledged. |
| Network milestone | An event of the international network of AISIs, such as its founding or a joint testing exercise. It does not by itself show which members took part. |
| Source | A document we cite, usually with the passages we quote from it. Each quoted passage names the facts it supports. |

"None identified" and "not published" mean our searches found nothing, not that nothing exists.

## Using the data

All paths are relative to [aisi.accelerate-europe.org](https://aisi.accelerate-europe.org).

| You want | File |
| --- | --- |
| Everything in one file | `data/aisi.json`, with field meanings in `data/aisi.schema.json` |
| Spreadsheets | one CSV per table, listed in `data/datapackage.json` |
| The list of jurisdictions, with their IDs, status and AISI ID | `api/v1/jurisdictions.json` |
| One jurisdiction | `api/v1/jurisdictions/GB.json`: the two-letter country code (ISO 3166-1, so GB for the UK and GR for Greece), or EU |
| One AISI | `api/v1/institutes/gb-aisi.json`: the AISI ID from the jurisdictions list |
| Guidance for AI agents | `llms.txt` |

Before using a funding figure, check what it covers: some figures are another body's money, part of a budget, or proposed rather than adopted (the `coverage` and `commitment` fields say which). There are two kinds of confidence rating: one for each jurisdiction's status (`status_confidence`), and one for each funding, staff or leadership figure (`confidence`).

Jurisdiction and AISI IDs never change. New codes in `dataset.vocabularies` can appear in a minor version; removing or renaming one is a major version. Field meanings are in `data/aisi.schema.json`, the download contract is [schema 1.0](docs/design/schema-1.0.md), and the editorial rules are on the [method page](https://aisi.accelerate-europe.org/method.html).

## Cite

<!-- citation:start -->

Accelerate Europe (2026). AI Security Institutes tracker, version 1.0.6, data as of 9 November 2026. Compiled by Jason Hoelscher-Obermaier. https://doi.org/10.5281/zenodo.23600431

<!-- citation:end -->

To report an error, use the "Report a correction" issue form on GitHub (incomplete reports are welcome), open a pull request, or write to <contact@accelerate-europe.org>.

## Licences

Code: [MIT](LICENSE). The Inter fonts in `scripts/fonts/` keep their [SIL Open Font License 1.1](scripts/fonts/OFL.txt).

Data, page text and documentation, including `data/aisi.json` and its exports: [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/), legal code in [LICENSE-DATA](LICENSE-DATA). Credit Accelerate Europe, compiled by Jason Hoelscher-Obermaier, link the licence and say what you changed. Quotations from sources remain with their rights holders and are reproduced as quotations; the licence covers our own compilation, text and data.

## Working on the tracker

Install Git, [uv](https://docs.astral.sh/uv/getting-started/installation/), Python 3.11 or newer and Node.js, then, from the repository root:

```sh
uv sync --locked --group dev
uv run --locked scripts/check_page.py --install-chrome   # the locked Playwright's Chromium; the build needs it
uv run scripts/check.py --preflight                      # which check steps can run here
uv run scripts/build.py --out dist                       # build the site
python3 -m http.server 8000 --bind 127.0.0.1 --directory dist
```

On Linux, Chromium needs system libraries: `python -m playwright install-deps chromium` (as root) installs them. Then open `http://127.0.0.1:8000/`.

The build needs no network access; fetching sources and checking Wikidata identifiers are separate editorial steps. `uv run scripts/check.py` checks what is staged in Git, not the working tree, so stage your edits first. Some steps need the maintainers' [private source store](docs/source-snapshots.md) and report NOT RUN without it, which fails the check unless you pass `--allow-skip` (local runs only). Public CI runs the fast checks, the build and the browser gate.

To correct or add a fact:

1. Edit the jurisdiction or AISI in `data/aisi.json`. Add its source to `sources` (url, publisher, primary or secondary, published and accessed dates) with an excerpt that quotes the passage and points at the fact it supports (`supports`); the build links each fact to its sources from these excerpts.
2. Check the links with [lychee](https://lychee.cli.rs/). It needs a plain list of URLs, which this command extracts from the dataset: `mkdir -p work && python3 -c "import json;[print(s['url']) for s in json.load(open('data/aisi.json'))['sources']]" > work/urls.txt && lychee --no-progress --insecure --max-retries 1 work/urls.txt`. A government site that blocks automated readers gets an `access_note` on its source.
3. Stage the change and run `uv run scripts/check.py`. It refuses schema errors, unknown or unused sources, impossible dates and disagreements between a jurisdiction and its AISI, and lists the changed facts next to their excerpts.

Versions, the changelog and publishing are in the [release runbook](docs/release.md).

More for maintainers:

- [AGENTS.md](AGENTS.md): which files a change touches, which command checks it and where each kind of rule lives.
- [Release runbook](docs/release.md) and [launch](docs/launch.md).
- [Consistency checks](docs/consistency.md): repeated claims, output credit, the publication pipeline and the rule tables.
- [Source snapshots](docs/source-snapshots.md): private source copies, quotation checks, archive links and the change monitor.
- [Research passes](docs/research-passes.md): how research sessions are run and recorded.
