# Sweeps

Two kinds of research pass keep records current between releases. `scripts/sweep.py` prepares them; they run and close like any other pass ([research passes](research-passes.md)).

- **Per-state re-check** (pass type `targeted`): a few states a week. For each, an agent re-reads every sentence of the record against the saved copies of its cited sources, searches by the name of every body, act and bill the record names, runs the state's fixed topic queries (one in English for each of the 195 jurisdictions, one more in the main official language for 142 where that is not English, and a third for Belgium), and records what is stale, contradicted, unsupported or new. It finds facts that have gone stale. It does not reliably find new facts that the record does not name.
- **Global topic sweep** (pass type `news-sweep`): once a month, 15 fixed cross-country queries (three topics in five languages) over the 60 days to the month's last day (both ends included), triaged against the published data. It finds new facts in any state, most within a month, and misses quiet steps and items published only in other languages.

Neither edits the data. The data session takes a pass's `candidate` findings only after an agent of the other model family has checked each against its excerpt, and names the pass in its commit (`Research-pass:`).

## Cadence

Per-state re-checks follow the internal re-read cadence: the states with a status are re-checked after each release by the news re-check ([release runbook](release.md)); an "often" group of states without a status (Europe scope, a closest body, or a lead development) every quarter; the rest every year. That is about six states a week. The global sweep runs monthly as a three-month trial (decided 14 November 2026); after the third run the owner sees what it found and decides whether it continues, changes its queries or stops.

## Preparing a pass

```sh
uv run --locked scripts/sweep.py state 2026-11-23-sweep-w48 --who "<name>" --states MT,LU,EE,AL,AM,AZ
uv run --locked scripts/sweep.py global 2026-12-01-global-sweep --who "<name>" --month 2026-11
```

`state` writes the pass folder with `pass.json` (open), `brief.md` from `scripts/sweep_brief_state.md`, an empty `findings.jsonl`, `sweep.json` (the window and a status per state) and per state `packets/<STATE>/packet.json` with `sources/<id>.txt`: the record, the institutes of that state, every cited source with its excerpts and the text of its newest private copy (the quotation verifier's literal view), the state's topic queries from `data/reference/sweep_queries.csv` and the names the record states. `global` writes `pass.json`, `brief.md` from `scripts/sweep_brief_global.md` and `sweep.json`. Neither overwrites an existing folder; without a pass store the exit status is 3 (NOT RUN).

Run per-state passes with the Claude Code harness: the agent reads its packets from the pass folder, and the Codex pass harness has no tool that reads local files.

## Topic queries

`data/reference/sweep_queries.csv` holds one row per query: `id` (a jurisdiction ID of the dataset), `query_id` (`topic-en`; `topic-local` in the main official language when that is not English; `topic-local-2` for a second, as in Belgium), `lang` (a BCP 47 tag, so `zh-Hans`, `prs` and `cnr` keep their distinctions), `query` and `translation_verified` (`no` for a translation no speaker has checked). The Bhutan and Maldives local queries are the ones the record's own research passes ran. The queries are written as Boolean strings, but the search engine treats quotation marks and `OR` as hints and may relax them, so a result is checked for the state it concerns: Georgia, Guinea, Congo and Sudan share names with other places.

## Search window

A state's window starts at `--since` if given; else at the start of the newest closed sweep pass whose `sweep.json` marks that state `complete`; else 24 months back. A first sweep looks 24 months back because a state's last full check is not assumed to be complete: in the calibration, four of nine known facts dated from before the last check. A state marked `incomplete: <reason>` moves nothing. A search about whether a named body or act still stands looks back to the date of the evidence the record cites for it.

## Findings

Pass findings format ([research passes](research-passes.md#findings)), one claim per line and one exact quotation per claim: `reason` gives the kind (`STALE`, `CONTRADICTED`, `UNSUPPORTED`, `NEW`), the field, the current text and the proposed sentence; `excerpt` carries the whole proposed sentence. A candidate needs a primary source of the state itself; anything else is recorded as `rejected` with a reason starting `LEAD:`. This screening threshold is stricter than what the dataset admits (`dataset.method`, source preference).

## Known limits

- Country names: each topic query uses one form of the state's name (for example "People's Republic of China" rather than "China", "Ivory Coast" rather than "Côte d'Ivoire", "Turkey" rather than "Türkiye"), so items that use only another form can be missed. The searches by the names the record states and the agent's follow-up queries cover part of this; the table is not extended with aliases.
- Eritrea has no query in Tigrinya: English is among its government's working languages, so its topic query is in English only.

## Calibration (14 November 2026)

Twelve states with known answers, six drawn at random, release 1.0.9. The per-state re-check found 4 of 5 known stale or contradicted facts and 1 of 4 known new facts; a repair (queries limited to government domains) found none of the missed ones. The global sweep, replayed monthly over 23 months, surfaced 18 of 32 status events and lead developments within 60 days (13 from a primary source; median 9 days), with about 100 on-topic items a month to triage. Search cost: $0.007 a query (Exa's own figure), about $0.14 a state.
