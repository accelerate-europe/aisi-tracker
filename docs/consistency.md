# Consistency checks

Run checks from the repository root:

```sh
uv run python scripts/build.py --check --require-roster
uv run --group dev pytest -q
```

To build the downloads and coverage resource rather than validate only (default output `dist/`; neither command writes a tracked file):

```sh
uv run python scripts/build.py --require-roster --out dist
```

Every output citation must have a reviewed `output_role`, including in small synthetic fragments. Removing all role metadata cannot disable this requirement. A `document` identifies one research document, software artefact or direct substantive release and declares its `output_lineage`. A `context` source is an index, general briefing, homepage or notice covering separate publications. Every output has a document anchor. A changed output label cannot hide disagreement with its document, and aliases of the same document URL cannot declare different lineages. All output rows in one lineage retain one publication date. Source `published` is its own bibliographic version or page date, not automatically the earliest lineage date.

The producer generates each `first_output` timeline event from the earliest `credit.date` in its institute catalogue and binds it to that output’s `lineage`. Its citations come from the selected output’s `credit.institute_id` and `credit.date` excerpt bindings. Strict authoring rejects a stored first-output event and requires explicit output credit; there is no publication-date fallback at that boundary. Publication, institutional credit, document version and legal commencement remain distinct facts.

`dataset.shared_claims` links explicitly reviewed repeated events, mirrored instruments and quantitative assertions. Targets use stable record identities rather than array positions. Claim identity includes subject, metric, unit, period and scope; values remain in their original records. Registered dates and quantities must agree. Mirrored agreements must agree on date and state. Missing, ambiguous or duplicate targets fail checks. Six designation/launch pairs describe one government act and therefore share a date constraint.

A one-time editorial audit inventoried structured dates and quantities, including jurisdiction population, during the pre-release review. That historical research remains in ignored `work/`; a normal build does not regenerate its per-leaf or pairwise decisions. Staff targets, current headcounts, different institutes, funding totals and fiscal allocations are not treated as equal merely because they cite one source. The producer calculates component totals, period means and current-profile annual figures from their primary inputs; the [funding contract](design/schema-1.0.md#generated-funding-values) lists the generated fields and invalid-input cases.

For registered numeric claims, the subject must be the owning institute. Funding units must match the currency, except `eur_per_year_est`, whose unit is EUR. An annual funding record uses its recorded period; a current allocation within multi-year funding uses `annual_period`, and an annual-profile amount uses its own start/end interval. Staff units are people and periods are observation dates. Missing context or unsupported numeric fields fail rather than bypassing identity checks. The metric and scope descriptions remain editorial judgements, not machine interpretations of prose. Date claims may describe network publications shared by multiple institutes; they do not inherit a numeric ownership rule. A source's bibliographic publication date may be a target only when that source itself is among the claim's cited evidence.

Normal builds write `dist/data/shared-claim-coverage.json`. The path is relative to the repository root. This reports output/lineage and first-output counts, registered agreement/date/numeric counts and a pointer list of numeric institute leaves outside a repeated-fact constraint. Its `unchecked_numeric_leaves` are RFC 6901 locations in the same build's published `aisi.json`, not stable record identities; they must be resolved against that file and can change after reordering. The report records this in `pointer_basis`. It does not inventory every date, include jurisdiction numeric leaves or reproduce the historical editorial audit. Such leaves are not silently counted as verified repetitions. Semantic identity is curated: the checks enforce registered repeated assertions, not a claim that arbitrary text has been understood. Free-prose numbers are not independently parsed, and equality does not verify the underlying source. New records must receive document roles and first-output bindings where applicable; editors must review whether new dates or quantities repeat existing facts before extending the registry.

## Known activity

The [1.0 activity contract](design/schema-1.0.md#known-activity) has no manually stored evidence selection. `known_activity` in `scripts/published.py` derives one earliest item per kind from reviewed outputs using explicit institutional credit dates. Strict authoring rejects any supplied activity array, even one that agrees with the generated result. The broad `first_output` milestone remains separate. [Regression tests](../tests/test_known_activity.py) check dates, citations, ties, partial precision, exclusions, empty exports and changes to primary inputs against hand-worked expectations. The [page checker](../scripts/check_page.py) independently selects from outputs and checks each displayed clause against reviewed credit-source expectations; [Node and mutation tests](../tests/test_operating_display.py) cover the renderer and oracle. These checks do not prove the editorial qualification or source accuracy. The hand-worked oracle rejects unknown institute/lineage pairs with a diagnostic naming the missing key; adding new earliest credited work requires a separately reviewed CODE update to its expected sources.

## Publication boundary

`publish` in `scripts/published.py` is the common producer for JSON, CSV, events, the page and the slide map. It derives facts, resolves stable evidence selectors to pointers in that published JSON, then generates field citations and display choices. The build validates the result against the download schema. The [authored contract](design/schema-1.0.md#authored-and-published-contracts) forbids generated fields instead of maintaining parallel copies with equality checks.

Funding derivation calculates both total amounts from `sum_of` components, annual means from the stated total and years, and annual amount and period from the unique current profile row where applicable. Exchange-rate inverses and EUR annual estimates then use those values. Field citations follow the primary evidence, including component, profile and exchange-rate inputs; broad `source_ids` associations do not supply missing field evidence. Tests change primary inputs, reject forbidden authored results and exercise reordered records with shared sources.

Stable CSV identities, quote hashes and report locations are defined by the [export contract](design/schema-1.0.md#exports-and-checks).

The page reads the producer’s choices, including the annual-estimate flag, funding and staff IDs, advisory and small-state flags, and development groups. [Render-only tests](../tests/test_render_only.py) supply conflicting raw facts and generated choices to ensure the renderer follows the latter, and mutate citation fallbacks to demonstrate failure. The browser gate and screenshot comparison remain required before integration.
