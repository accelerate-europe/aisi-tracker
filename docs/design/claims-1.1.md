# Quoted, coded claims (1.1.0)

Design for 1.1.0's shared structure: functions, body roles and lead developments as one kind of record, generated summaries, and freshness. Status: proposal, 14 November 2026; nothing here is built until the owner has approved the items in [For your approval](#for-your-approval). It extends the [download contract](schema-1.0.md) and adds no breaking change. Cites to today's code are to the `next` branch this document was committed on.

## The model in five lines

1. Every fact a reader sees has one home: a structured field, or a **claim**: one sentence, one bound quote, and a code where a code fits.
2. A function of an institute, the role of a related body and a state's lead development are all claims, in one `claims[]` list per record.
3. Summaries, the closest body's explanation, related-body role text and "How we checked" are generated; authors write claims, codes and `check.log`, never these texts.
4. Negative findings come only from `check`, worded with their date; no claim may state one.
5. Each quote carries the date it was last found in its source; a claim whose source fails long enough leaves the summary.

What a record shows is one rule for every state (section 3). How often a source is re-read is internal and never changes what a record shows.

## 1. Data shapes

### Authored

A claim sits in `jurisdictions[].claims[]` or `institutes[].claims[]`:

| Field | Kinds | Meaning |
| --- | --- | --- |
| `id` | all | `claims-` plus a new UUID hex; permanent, never reused. `claims` joins `ID_COLLECTIONS` (`scripts/references.py:20-31`), so excerpts bind it by identity. |
| `kind` | all | `function`, `development` or `fact` ([kinds](#claim-kinds)). |
| `code` | function, development | A code from `dataset.vocabularies.function` or `.development`; none for `fact`. |
| `basis` | function | `mandate` (a founding, legal, plan or announcement text assigns it) or `activity` (a text shows it done). Evidence of both is two claims. |
| `body_id` | function on a jurisdiction | The `related_bodies` id the claim is about. A function claim on an institute is about the institute and has no `body_id`. |
| `text` | all | One sentence saying no more than its quote. For a development, the clause that completes the one generated sentence of pattern 3 (lower-case start, no date). |
| `act`, `date` | development | Who did what to which instrument ("the government adopted the national AI strategy to 2030") and the act's date. |
| `source_ids` | all | The sources whose excerpts bind the claim. |

Binding is as for any field today: an excerpt's `supports` names the leaf by stable selector, for example `["jurisdictions", {"id": "SI"}, "claims", {"id": "claims-…"}, "text"]` (authored form, `scripts/references.py:193-198`), published as an RFC 6901 pointer (`schema/aisi.schema.json:2354-2363`). Required leaves: `text` (exactly one excerpt), and for a development `act` and `date`.

Other authored additions:
- `check.log`: `[{date, text}]`, our search and reading notes ("Searched in Slovenian and English.", "The national gazette blocks automated readers."). It is the only home for the search log; it states nothing about the world.
- `check.functions` (institutes only): `{date, codes}`, the function codes the institute was coded against and when. This is the record of the search behind every "not found".
- `closest_body.body_id`: the `related_bodies` id of the closest body, so its function claims attach; `closest_body.gap` gets a required excerpt (the act it concerns for `remit`, the body's own text for `designation`).
- `dataset.vocabularies.function` and `.development`: code → `{label, definition}`, function codes also `group` (as decided on 14 November) and `applies_to` (`institute`, `body` or both), plus `deprecated: true` and `replaced_by: [codes]` on a retired code.
- `dataset.provisional`: the fields and vocabularies that may still change in a minor version (below).

**A migrated record** is a jurisdiction whose authored input has no `summary`. In it `summary`, `notes`, `related_bodies[].role` and `closest_body.note` are generated and may not be authored; every remaining free-text leaf of the record and its institute (`mandate`, field `notes` and `description`) carries a bound excerpt. A record with an authored `summary` is legacy and builds as today. After 1.2.0 the authored schema refuses `summary` (item 21).

### Published

All new fields are additive; every existing field keeps its name, type and CSV column.

- `summary`, `notes`, `related_bodies[].role`, `closest_body.note`: same names and types (`schema/aisi.schema.json:757-760, 780-782, 932-934`), values generated for migrated records. `summary` stays required (`schema/aisi.schema.json:660`); the CSV columns stay (`scripts/build.py:650-651, 921`). They join the derived fields in `authored_schema` (`scripts/references.py:205-215`).
- `claims[]` as authored, plus generated `reread` `{state, last_found, since}` (the worst of its required leaves, section 4) and `expired` (true when the claim has left the summary).
- `sources[].excerpts[].reread` `{state, last_found, since}`: per quote.
- `institutes[].functions[]`: one row per current code that applies to institutes, in vocabulary order: `{code, value, claim_ids}`, `value` one of `mandate`, `activity`, `both`, `not_found`, `not_coded`.
- New tables `functions.csv` (one row per institute and code) and `claims.csv` (one row per claim, with `reread_state` and `last_found`; without it a CSV user would see body roles and developments only as generated prose). A new code adds rows, never columns.

**Not found and not coded.** For an institute and a current code: a claim gives `mandate`, `activity` or `both`; no claim and the code in `check.functions.codes` gives `not_found`; the code not in that list gives `not_coded` (for example every institute, the day a code is added). Related bodies are not coded against the whole list, so for them only claims are published and an absent code means nothing. A cell the author and reviewer dispute gets no claim: `not_found`, and a question in that institute's confirmation request (decision of 14 November).

**Not yet operating.** The functions display marks an institute whose status is not `established` as "not yet operating", never with the status word, so the 2.0.0 merge of `proposed` and `planned` changes no functions wording (item 13).

**Provisional and retired codes.** `dataset.provisional` lists `institutes[].functions`, `vocabularies.function` (which holds the role codes too) and `vocabularies.development`; the `claims[]` shape itself is not provisional. While listed, their codes may be added, split, renamed or removed in a minor, with a changelog line and `replaced_by`. A retired code stays in the vocabulary with `deprecated: true` and `replaced_by`, is never reused, may not be used by a claim, and gets no `functions[]` row. README.md:48, docs/release.md:7, AGENTS.md:40 and `scripts/agent_files.py:173` each gain "except fields and vocabularies listed in `dataset.provisional`" (item 12). The list leaves provisional when the owner signs it off.

### Worked examples

Real records; new ids are fresh UUIDs; excerpt texts are verbatim from the cited private copies.

**A function: UK AISI tests models.** Authored, in `institutes[gb-aisi]`:

```json
"claims": [{"id": "claims-3f271759be5d4ae9bef2ea6015a0d1ec", "kind": "function", "code": "model_testing",
            "basis": "activity", "text": "With the US institute, it ran a joint pre-deployment evaluation of Anthropic's latest model.",
            "source_ids": ["us-claude35-2024"]}],
"check": {"…": "…", "functions": {"date": "2026-11-14", "codes": ["model_testing", "…all 16…"]}}
```

The existing `us-claude35-2024` excerpt "The U.S. Artificial Intelligence Safety Institute (US AISI) and the UK Artificial Intelligence Safety Institute (UK AISI) conducted a joint pre-deployment evaluation of Anthropic’s latest model" gains the selector `["institutes", {"id": "gb-aisi"}, "claims", {"id": "claims-3f27…"}, "text"]`. Published `functions[]` includes `{"code": "model_testing", "value": "activity", "claim_ids": ["claims-3f27…"]}` and, with no foresight claim (round 4: not found), `{"code": "foresight", "value": "not_found", "claim_ids": []}`. A 17th code added later reads `not_coded` until the UK is coded against it. By hand: one sentence; one excerpt on `text`; code current; basis allowed for a function; no `body_id` on an institute; meets the test "ran a test of an identifiable AI model" (the quote names the UK institute as doing it and identifies the model by developer and release; the same source's title names it), so activity. The institute's own "we rigorously test leading AI systems" would not do: it names no model.

**A body role with a gap: Poland's NASK centre.** Authored, in `jurisdictions[PL]`:

```json
"claims": [
  {"id": "claims-e0228007a6d742798ba085b873fc1e98", "kind": "function", "code": "research", "basis": "activity",
   "body_id": "related_bodies-8e2bf69316145732aea04a2f9bbd82eb",
   "text": "It researches the security, explainability and interpretability of foundation models.",
   "source_ids": ["pl-r8-research-teams"]},
  {"id": "claims-9af9576c5e7c45198d8ea7fba5891114", "kind": "function", "code": "evaluation_methods", "basis": "activity",
   "body_id": "related_bodies-8e2bf69316145732aea04a2f9bbd82eb",
   "text": "It develops methods for analysing and auditing models.", "source_ids": ["pl-r8-research-teams"]}],
"closest_body": {"name": "NASK AI Safety Research Centre", "stage": "exists", "gap": "remit",
                 "body_id": "related_bodies-8e2bf69316145732aea04a2f9bbd82eb", "source_ids": ["pl-nask", "pl-r8-resolution130", "pl-r8-research-teams"]}
```

Bindings: "research on the security, explainability, and interpretability of fundamental models" → first `text`; "We develop methods for generating, analyzing and auditing models" → second `text`; Resolution 130's "zbudowanie wokół Ośrodka Badań nad Bezpieczeństwem Sztucznej Inteligencji organizacji" (building an organisation around the centre) → `closest_body/gap`, the government act that concerns the body without assigning it evaluation; "w NASK powstał Ośrodek Badań nad Bezpieczeństwem Sztucznej Inteligencji." → `name` and `stage`, as today. Generated: the body's `role` is the two texts; `closest_body.note` is "It researches … foundation models. It develops methods for analysing and auditing models. Exists; its government role does not qualify." (stage and gap words as the page has them, `site/index.html:1620-1621`); the summary is pattern 2 (section 3). Today's hand-written note, with its several facts and our negatives, is gone: the OpenAI agreement and the AITAS lab become further claims if kept, and "not a government assignment" is the gap code. The excerpts that bind today's `summary` and `closest_body/note` are rebound to the claims, and in a migrated record the required closest-body leaves become `name`, `stage` and `gap` instead of `note` (`scripts/method_checks.py:39-42`). By hand: both claims name an existing body id; codes are function codes allowed for bodies; each `text` has one excerpt; `gap` has its excerpt; no `text` states an absence.

**A lead development: Slovenia's strategy.** Authored, in `jurisdictions[SI]`:

```json
"claims": [{"id": "claims-e5ef5cd2a96443e88c374befa55b7c68", "kind": "development", "code": "testing_duty",
            "date": "2026-03-05", "act": "the government adopted the national AI strategy to 2030",
            "text": "plans national safety-test procedures for algorithms",
            "source_ids": ["si-gov-novice-2026-03-05", "si-nsai2030"]}]
```

New excerpts (neither source has one today): on `si-gov-novice-2026-03-05` (published 2026-03-05), "Vlada Republike Slovenije je na današnji seji sprejela Nacionalno strategijo za umetno inteligenco do leta 2030 (NsUI 2030)." with gloss "At today's session the Government adopted the National AI Strategy to 2030." → `act` and `date` (a primary source published on the act's date carries that date, rule of 14 November); on `si-nsai2030`, "Introduction of national procedures and safety tests for the review, testing and updating of algorithms, along with response mechanisms for cases of violations, bias or misuse of AI systems." → `text`. Generated pattern 3: "On 5 March 2026 the government adopted the national AI strategy to 2030, which plans national safety-test procedures for algorithms." By hand against the level 2 bar: domestic primary sources (origin SI, government); an adopted act; dated within three years of `check.last_checked` 2026-10-16; plans testing of AI systems; one development in the record; no status.

## 2. Code lists (proposals)

Each code has a one-line test a reviewer applies to the bound quote. Owner wording; all provisional.

### Claim kinds

| Kind | Where | Test |
| --- | --- | --- |
| `function` | institutes; related bodies via `body_id` | The quote shows the body doing, or being assigned, what the code's test names. |
| `development` | jurisdictions without a status, at most one | The quote is the act itself and the act passes the level 2 bar (section 3). |
| `fact` | jurisdictions with a status, and institutes | A quoted fact with no field and no code (Germany's "first phase", Spain's first team by the end of 2026). Shown in the record's list, never in the summary. |

### Functions (the 16 slugs, round 4 wording)

General rules (round 4 brief R1-R7, abridged): being hosted inside a ministry, a committee seat or one staff member's role never meets a test; conditional wording ("may include", "will explore") is not a mandate; work counts only where the text names the body as doing, leading, hosting, chairing or coordinating it; residual research last.

| Slug | Label | Test |
| --- | --- | --- |
| `model_testing` | Model testing | It ran or commissioned a test of an identifiable AI model, or a text assigns it testing AI models or systems. |
| `evaluation_methods` | Evaluation methods and tools | It develops or publishes test methods, benchmarks, protocols or software; running existing benchmarks is model testing. |
| `research` | Research | It conducts or funds research on a named AI-risk topic or programme that meets no other test. |
| `standards_guidance` | Standards and guidelines | It publishes, or is tasked to develop, standards, codes or guidance addressed to AI developers, providers or deployers; no addressee or facilitating others' text does not count. |
| `regulatory_enforcement` | Enforces AI rules | It enforces AI rules itself: investigates, sanctions or holds legal powers over providers. |
| `regulatory_support` | Supports regulators | A text gives it a role supporting regulators or the implementation of AI regulation. |
| `risk_monitoring` | Capability and risk monitoring | It reports regularly on what AI can do or what risks emerge (a periodic or numbered series, or a monitoring mandate); one paper does not count. |
| `foresight` | Foresight | It publishes, or is tasked with, explicit forecasts, scenarios or early-warning indicators about future developments; threat models of current misuse do not count. |
| `policy_advice` | Policy advice | It publishes or is tasked with policy research, recommendations or options for government on AI. |
| `incident_response` | National incident role | It holds a standing national role, without an end date, in collecting, reporting or responding to AI incidents. |
| `international_cooperation` | International cooperation | It takes part for its jurisdiction in international networks, joint testing or agreements with foreign counterparts. |
| `convening` | Convening | It hosts, chairs or coordinates a national consortium or standing partnership, or is designated the national hub or contact point. |
| `capacity_building` | Capacity building | A named programme or mandate clause builds named kinds of domestic organisations' ability to understand, evaluate or safely use advanced AI. |
| `talent` | Talent | It trains or funds people outside its own staff. |
| `adoption` | Adoption and competitiveness | A text gives it a role in promoting AI uptake, commercialisation or the domestic industry. |
| `public_access_continuity` | Access and continuity for public bodies | It secures access to frontier models or compute for other public bodies, or assesses the jurisdiction's dependence on frontier AI. |

Not in the list, pending the AE team's replies (by 20 November): evaluator qualification and accreditation. If added, it is one more slug; every institute reads `not_coded` until coded.

### Role codes for related bodies

The role code of a body is a function code (one list, one set of tests, and the closest body's functions can sit beside the institutes' in a Compare table), plus two codes that apply only to bodies, because the qualifying rule names them as not enough on their own:

| Slug | Label | Test |
| --- | --- | --- |
| `certification` | Certification and conformity | It certifies, or assesses the conformity of, AI products or systems. |
| `sandbox` | Regulatory sandbox | It runs, or is tasked to set up, an AI regulatory sandbox. |

An AI Act authority's role claim is usually `regulatory_enforcement`, with the sector limit in its `text`; its AI Act roles stay in `authority_roles`.

### Gap codes (closest body)

The two of today, unchanged (`schema/aisi.schema.json:1170-1174`; rule `closest-candidate`); no new code is needed, because the body's function codes already say what it does instead:

| Code | Page words | Test |
| --- | --- | --- |
| `remit` | its government role does not qualify | A quoted government act creates, funds, plans, proposes or recognises the body, and no quote assigns it model-level evaluation. |
| `designation` | no government designation | The quotes show qualifying work, self-described or documented, and no government act concerning the body is cited. |

### Development codes

| Slug | Label | Test |
| --- | --- | --- |
| `public_body` | Named public body or programme | The quoted act creates, funds or assigns a named public body or national programme for AI. |
| `testing_duty` | Testing duty | The quoted act requires or plans testing, assessment or certification of AI systems. |

## 3. What a record shows, and the six patterns

### The rule, the same for every state

A state without a status shows, in this order: the finding; the closest body, if the existing `closest-candidate` rule gives one; AI Act authorities; other related bodies that have at least one role claim; and at most one lead development. Testable form:

1. A jurisdiction without a status has at most one `development` claim and no `fact` claim.
2. A related body is published only with `source_ids` (an error once the Europe-scope ones are sourced, decision of 14 November) and, in a migrated record, with at least one role claim or an AI Act `kind`.
3. A development passes the level 2 bar: a quote from a domestic primary source (government, parliament, gazette, or a public body acting under a government act) for an act in force or an operating body, dated within three years before `check.last_checked`, that creates, funds or assigns a named public body or national programme for AI, or requires or plans testing, assessment or certification of AI systems. Drafts, bills, announcements and strategies naming neither fail. The build enforces the source type and origin and the three-year window; the reviewer judges the rest.
4. No region, priority or re-read cadence enters this rule.

### The patterns

Words in braces are fields; nothing else varies. A state without a status gets pattern 1, then the first of 2, 3, 4 that applies. A state with a status gets 5, and 6 when it has an institute with a `mandate_short` (NL and SE have none).

| # | Pattern | Fields |
| --- | --- | --- |
| 1 | No qualifying institute identified as of {month}. | `check.last_checked` |
| 2 | Closest body: {name} ({function labels}), which {stage}, but {gap}. | `closest_body`, its role claims |
| 3 | On {date} {act}, which {text}. | the development claim |
| 4 | {body} is its AI Act {roles}. / {body} is proposed as its AI Act {roles}. / {body} is listed as its AI Act {roles}, but no national designation is confirmed. Then "; {n} other AI Act bodies are listed below" when n > 1, "; 1 other AI Act body is listed below" when n = 1. | the AI Act authority holding the single point of contact, else the most roles, ties by name; `designation_state`, `authority_roles` |
| 5 | {short name} was set up on {established} as {legal form} ({host} for a unit). / {Status} since {status_since}: {event short}. | institute, status event `short` (`scripts/build.py:805`) |
| 6 | It {mandate_short, first letter lower-case} | `mandate_short` |

Fixed in the patterns, from the first run on real data: the host in brackets avoids "within Cabinet Office" without an article; authority names come first; the event `short` loses its own full stop. Pattern 2 says what the closest body does through its function labels, which is the officials' near-miss explanation in one line (item 17); the full explanation, its role sentences plus stage and gap, is the generated `closest_body.note`. A pattern drops out when the quotes behind its fields have expired (section 4) and the next applies; patterns 1 and 5 never drop.

"How we checked" (`site/index.html:1533-1550`) is built from `check.searches` and `check.log` instead of `notes`; the page's finding regex (`site/index.html:1316`) learns the "as of {month}" opener.

### The 32 Europe-scope lines

Generated by a throwaway script from the fields on `next` when this was written, pattern 3 and the bracketed function labels written by hand where the claims do not exist yet. All checks are dated October 2026 today; the month moves with each re-check.

| | Line | Note |
| --- | --- | --- |
| GB | UK AISI was set up on 2 November 2023 as a unit within an existing body (Cabinet Office). It tests frontier AI models for security and safety risks. | |
| FR | INESIA was set up on 31 January 2025 as a network of existing bodies. It builds public expertise in evaluating and securing advanced AI, and supports AI Act implementation. | |
| DE | AISI Deutschland was set up on 31 August 2026 as a network of existing bodies. It analyses leading AI models, especially their cyber capabilities and risks. | "first phase" becomes a `fact` claim |
| ES | Planned since 21 September 2026: IA360 plan commits to an AI Security Institute within AESIA. It will evaluate the most advanced models before and after deployment. | first team by end of 2026: `fact` |
| NO | Proposed since 7 October 2026: Budget proposes NOK 30m and an AI safety mandate for KI Norge. It would add AI safety and model evaluation to KI Norge's remit. | contact point: `convening` claim |
| NL | Exploring since 1 June 2026: Cabinet confirms an interdepartmental exploration of an independent AI safety institute. | parliament's motion: `fact` |
| SE | Exploring since 26 November 2024: Advisory recommendation for a Swedish AI safety institute. | SEK 30m and location: `fact` |
| CH | No qualifying institute identified as of October 2026. | convention timetable fails level 2 (an announcement) |
| IT | No qualifying institute identified as of October 2026. National Cybersecurity Agency (ACN) is its AI Act single point of contact and market-surveillance authority; 5 other AI Act bodies are listed below. | |
| PL | No qualifying institute identified as of October 2026. Closest body: NASK AI Safety Research Centre (research, evaluation methods and tools), which exists, but its government role does not qualify. | worked example |
| IE | No qualifying institute identified as of October 2026. AI Office of Ireland is its AI Act single point of contact; 10 other AI Act bodies are listed below. | |
| AT | No qualifying institute identified as of October 2026. Data Protection Authority is listed as its AI Act market-surveillance authority, but no national designation is confirmed. | |
| FI | No qualifying institute identified as of October 2026. Traficom is its AI Act single point of contact, market-surveillance authority and notifying authority; 9 other AI Act bodies are listed below. | |
| DK | No qualifying institute identified as of October 2026. Danish Agency for Digital Government is its AI Act single point of contact, market-surveillance authority and notifying authority; 2 other AI Act bodies are listed below. | |
| BE | No qualifying institute identified as of October 2026. Belgian Institute for Postal Services and Telecommunications (IBPT/BIPT) is proposed as its AI Act market-surveillance authority and notifying authority. | strategy proposal fails level 2 |
| CZ | No qualifying institute identified as of October 2026. Czech Telecommunication Office (ČTÚ) is proposed as its AI Act single point of contact and market-surveillance authority; 1 other AI Act body is listed below. | |
| EE | No qualifying institute identified as of October 2026. Consumer Protection and Technical Regulatory Authority (TTJA) is proposed as its AI Act single point of contact and market-surveillance authority. | near miss only if the data pass gives a closest body |
| PT | No qualifying institute identified as of October 2026. ANACOM is proposed as its AI Act single point of contact and market-surveillance authority. | |
| IS | No qualifying institute identified as of October 2026. On 2 July 2025 the government presented its first AI Action Plan, which creates a new centre for AI and language technology. | act date and verb to confirm against the plan |
| LI | No qualifying institute identified as of October 2026. | administration strategy names no body: fails |
| LU | No qualifying institute identified as of October 2026. National Commission for Data Protection (CNPD) is proposed as its AI Act single point of contact and market-surveillance authority; 8 other AI Act bodies are listed below. | |
| GR | No qualifying institute identified as of October 2026. On 20 July 2026 Law 5321/2026 was published in the gazette, which sets up a Centre for Coordination and Expertise for AI within the telecoms regulator EETT. | quote on the centre to bind |
| HR | No qualifying institute identified as of October 2026. | plan presented, not adopted: fails |
| SI | No qualifying institute identified as of October 2026. On 5 March 2026 the government adopted the national AI strategy to 2030, which plans national safety-test procedures for algorithms. | worked example |
| SK | No qualifying institute identified as of October 2026. Closest body: AI CyberLab ({labels}), which is planned, but its government role does not qualify. | labels from its claims, likely model testing |
| HU | No qualifying institute identified as of October 2026. Minister of Science and Technology (minister responsible for enterprise development in AI matters) is its AI Act single point of contact and market-surveillance authority; 1 other AI Act body is listed below. | long name: a data fix |
| RO | No qualifying institute identified as of October 2026. ANCOM is proposed as its AI Act single point of contact and market-surveillance authority; 4 other AI Act bodies are listed below. | Senate bill B597 fails (a bill) |
| BG | No qualifying institute identified as of October 2026. Communications Regulation Commission is proposed as its AI Act single point of contact; 11 other AI Act bodies are listed below. | stale: the bill was rejected on 4 November; one code to fix in the data pass |
| LT | No qualifying institute identified as of October 2026. Communications Regulatory Authority (RRT) is its AI Act single point of contact and market-surveillance authority; 1 other AI Act body is listed below. | |
| LV | No qualifying institute identified as of October 2026. Consumer Rights Protection Centre (PTAC) is its AI Act single point of contact and market-surveillance authority; 2 other AI Act bodies are listed below. | the AI Centre may be the closest body: data pass |
| CY | No qualifying institute identified as of October 2026. Closest body: European AI Certification Lab ({labels}), which is proposed, but its government role does not qualify. | stage stale: the strategy was adopted on 10 November |
| MT | No qualifying institute identified as of October 2026. Malta Digital Innovation Authority is its AI Act single point of contact, market-surveillance authority and notifying authority; 1 other AI Act body is listed below. | |

## 4. Freshness and expiry

**What the monitor records.** It already logs, per source and run, each bound quote's text hash and whether the cited and the checked copy carry it (`scripts/monitor_sources.py:248-258, 539-551`, in the private `checks/checks.jsonl`, `docs/source-snapshots.md:63`), and the run's class per source (`scripts/monitor_sources.py:1-26, 80`). The hash is the excerpt identity the dataset uses (`scripts/references.py:34-38`). Nothing more is needed per quote. New: an export, `monitor_sources.py --rereads`, writes the tracked file `data/rereads.json` (keyed by source id, then quote hash, because the identity is source-scoped and 31 quote texts recur across sources → `state`, `last_found`, `since`, `last_attempt`); it is the only writer, and the file travels with DATA commits like the README blocks. The build joins it into `excerpts[].reread` and `claims[].reread`; CI needs no private store. A quote with no monitor row takes its source's `snapshot.retrieved` as `last_found`.

**States**, per quote. A failure state starts at the first attempt that shows it (`since`) and holds until an attempt finds the quote again; a later blocked, failed or missing attempt never resets or clears it. A confirmed loss (`quote_gone`, `source_gone`) replaces an access failure (`blocked_or_failed`) and keeps its own start date and shorter deadline through later access failures. `last_found` only moves when the quote is found.

| State | Set by | Page says | Deadline (dates against `dataset.as_of`) |
| --- | --- | --- | --- |
| `confirmed` | the quote is in the checked copy (`in_checked_copy`, any live route) | Source last re-read {last_found}. | none |
| `quote_gone` | the cited copy carried the quote and a readable new copy does not (`substantive`, `quote_lost`) | Not found in the source on {since}; last found {last_found}; under review. | triage within 7 days; leaves the summary after one month |
| `source_gone` | `gone` (404, 410, host gone) | Source offline since {since}; last found {last_found}. | as `quote_gone` |
| `blocked_or_failed` | `blocked`, `failed`, or `archived` (a Wayback copy is not a re-read) | Source could not be re-read since {since}; last found {last_found}. | other routes tried; stays, labelled, up to six months, then leaves the summary |
| `not_attempted` | `confirmed` so far, but no attempt (or only `skipped`) for 14 days | Source last re-read {last_found}. | never expires; a maintenance alert |

A leaf's state is the best of its excerpts' states; a claim's is the worst of its required leaves (`text`, and for a development `act` and `date`), and the same holds for the leaves a pattern uses. "Verified" is never said. An expired claim leaves the summary but stays in the record's list with its label, and returns at the next confirmed re-read. A development also leaves when its date is more than three years before `as_of`.

**Maintenance alerts** (release warnings, `scripts/build.py:999, 1094`, and the weekly digest): a `quote_gone` or `source_gone` older than 7 days; a `blocked_or_failed` older than five months; a quote with no attempt for 14 days, whatever its state; `data/rereads.json` older than 8 days at a release; each claim that expired since the last release.

**Cadence** stays internal (item 19): the monitor re-reads every source weekly; status states keep the news re-check within three days after each release (`docs/release.md:74`); the "often" group (Europe scope, a closest candidate, a live lead development, or a documented use) is swept quarterly, the rest yearly. Nothing on the page depends on the group. A source that stays online with old text (Maldives) is caught only by the sweep, which is decided but not running and waits on the sources-study decisions (item 22).

## 5. Checks

**Structural (build errors):**
- A claim's `text` has exactly one excerpt; a development's `act` and `date` have one each (added to `required_excerpt_pointers`, `scripts/method_checks.py:23-103`, reported at `scripts/method_checks.py:388-389`). A fact without a quote cannot be written.
- `text` is one sentence (a development's generated sentence is).
- Codes exist and are not retired; `kind`, `code`, `basis` and `body_id` combine as section 1 says; a function code's `applies_to` matches the claim's subject (body-only codes need a `body_id`); `fact` and `development` sit only where section 3 allows; `check.functions` codes are current and apply to institutes.
- A development cites a primary source whose origin is the jurisdiction and is dated within three years before `check.last_checked`.
- A migrated record authors none of the generated texts; its remaining free-text leaves are bound, so the `unquoted_claims` kinds (`scripts/method_checks.py:295-335`) are errors for it, warnings elsewhere until 1.2.0.
- Patterns: a unit test per pattern and per fixed defect; the Europe lines as fixtures.

**Review (other model family, each new or changed claim with its quote):** the quote carries the sentence and no more; the sentence states no absence (a "nothing found" belongs only to the generated finding from `check`; this is the mixed "quoted fact plus our negative" sentence behind three of the eight sample errors, and a word list cannot catch it reliably, so the build only lists claims with negative words to help the reviewer); the code is not stronger than the quote (activity versus mandate, enforcement versus support, the function test met); a development is an act in force, not a draft or the body's own description; the gap's act concerns the body; `check.log` states nothing about the world. A disputed claim is removed and becomes a question.

**Measured** by the blind sample at each minor release (`docs/release.md:17`), scored per claim: correct, stronger than its quote, wrong code, unsupported, stale. The release summary gives the error rate per claim and per record, with its interval.

## 6. Build plan

Each step is one branch, merged to `next` on its own with CI green and review, and releasable alone.

| Step | Content | Needs | Page after it |
| --- | --- | --- | --- |
| A. Shapes | schema and authored schema, `claims` identity, vocabularies with `dataset.provisional`, `check.log` and `check.functions`, structural checks, `functions[]`, `claims.csv`, `functions.csv`, the provisional clause in four places, API and `llms.txt` lines | approval of section 2 | unchanged |
| B. Generator | the six patterns, notes from `check.log`, role and closest note from claims, migrated-record rule, "How we checked" from `check`, finding regex | A, approval of section 3 | unchanged (nothing migrated) |
| C. Freshness | `--rereads` export, `data/rereads.json`, states, expiry, alerts | A | "Source last re-read {date}" and the state labels in every source list |
| D. Functions data | the 15 institutes coded with quotes, cross-model review, disputed cells to the confirmation list | A, the signed-off list | downloads and API only |
| E. Functions table | presentation A on Compare | D, the AE team's reply on presentation | the table, with the "not yet operating" mark |
| F. Europe migration | the 32 records and the six European institutes (49 fields, 121 sentences): R deleted, L to `check.log`, Q to claims, N researched or dropped, unsourced bodies deleted, closest-body ids and gap quotes, at most one development each (SI, GR, IS), `fact` claims for status records and their list on the card | B, C; the 1.0.10 data fixes (BG, CY, LT) first | 32 generated lines, claims lists, labels |

A goes first; B, C and D run in parallel after it; E and F follow. 1.1.0 ships after F. Then 1.2.0: the other 10 status records and 153 states, the authored schema refuses `summary`, and the unquoted-claims check becomes an error everywhere.

**Effort, honestly:** agent-days, each with its review and CI rounds: A 1.5, B 2, C 1.5 to 2, D 1.5, E 1 to 1.5, F 1.5 to 2, so 9 to 10.5 in all; with three streams in parallel about two calendar weeks. Earlier estimates (one session for the code; 1.5 to 2 days for functions) left out the freshness step, the review rounds and the page. Owner time: about one hour for the approvals below, 20 minutes each for the visual passes of C, E and F, and one read of the 32 lines at F.

## Traceability (decided items 12 to 22)

| Item | Where |
| --- | --- |
| 12 functions shipped so the list can change | §1 published, provisional and retired codes; §2 |
| 13 no status word in the functions display | §1 not yet operating; §6 E |
| 14 one home per fact, one shared structure | model; §1 authored |
| 15 generated summaries, negatives from `check`, `check.log` | §1 migrated record; §3 patterns; §5 review of absences |
| 16 one rule, level 2 | §3 the rule |
| 17 near misses from role plus gap | §1 PL example; §2 gap codes; §3 pattern 2 |
| 18 freshness, expiry by reason | §4 |
| 19 cadence internal | §4 cadence |
| 20 approval once, then review and sample | §5; For your approval |
| 21 migration, additive, provisional vocabularies | §1; §6 |
| 22 known limit | §4 cadence |

## For your approval

Each item: yes, or tell me what to change.

1. **Uncoded facts.** For countries with an institute, a quoted fact that fits no field (Germany's "first phase") becomes a claim without a code, listed below the summary, never in it. Otherwise it would be dropped.
2. **One code list for institutes and bodies.** A body's role uses the 16 function codes, plus two only bodies get: "Certification and conformity" and "Regulatory sandbox". The alternative is a separate role list.
3. **The 16 functions**, with the tests in section 2 (round 4 wording). This is the sign-off you reserved after round 3. Evaluator accreditation waits for the AE team.
4. **Two development codes**: "Named public body or programme" and "Testing duty".
5. **Gap codes stay as today**: "its government role does not qualify" and "no government designation", kept stable in the schema rather than moved into a provisional list. Your decision of 14 November put gap codes in a provisional list on the assumption that new ones were needed; none are, because the body's function codes say what it does instead.
6. **The six summary lines:**
   - "No qualifying institute identified as of October 2026."
   - "Closest body: NASK AI Safety Research Centre (research, evaluation methods and tools), which exists, but its government role does not qualify."
   - "On 5 March 2026 the government adopted the national AI strategy to 2030, which plans national safety-test procedures for algorithms."
   - "Traficom is its AI Act single point of contact, market-surveillance authority and notifying authority; 9 other AI Act bodies are listed below."
   - "UK AISI was set up on 2 November 2023 as a unit within an existing body (Cabinet Office)." or "Exploring since 1 June 2026: Cabinet confirms …"
   - "It tests frontier AI models for security and safety risks."
7. **Freshness wording**: "Source last re-read 10 November 2026." / "Not found in the source on {date}; last found {date}; under review." / "Source offline since {date}; last found {date}." / "Source could not be re-read since {date}; last found {date}."

The 32 lines in section 3 are a draft to look at if you like; your one read of them is at the migration step, as decided.
