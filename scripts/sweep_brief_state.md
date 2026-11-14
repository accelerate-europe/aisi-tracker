# Pass $pass_id: stale-fact re-check of a few states

You run one research pass for the AI Security Institutes tracker (https://aisi.accelerate-europe.org). The data you check is release $version (as of $as_of). For each state below, re-read its record against its cited sources and search for what has changed, then record screened findings. You do not edit the dataset: a separate session turns findings into data changes after an independent check.

$states

## Ground rules

- You work only in this pass folder. Each state has `packets/<STATE>/packet.json` (the record, the institutes of that state if any, every cited source with its excerpts, and `copy.file`: the text of our saved copy of the source, relative to the packet folder), the state's `topic_queries` and `entity_seeds`. Read nothing else on disk.
- Search with Exa (`web_search_exa`, `web_fetch_exa`). Use the built-in web search only when Exa returns nothing useful, and say so in `found_by`. At most 25 searches per state, and at most four subagents searching at the same time. If Exa fails with a quota or rate error twice in a row, stop searching, and record that in `notes.md`.
- Editorial rules are on the method page; follow the full rule text there, never a summary: https://aisi.accelerate-europe.org/method.html (source preference `#source-preference`, excerpts `#excerpts`, known activity `#known-activity`, AI Act authorities `#ai-act-authorities`, qualifying body `#qualifying-body`, rescinded instruments `#rescinded-instruments`).
- Never invent or guess URLs, dates, names or figures. Quote only text you read, in the original language.
- Personal data stays out: name people only as public office holders or institute staff in their official role, as the source names them. Never record a personal social-media profile, not even as a rejected result.

## Per state

1. **Re-read.** Re-read every field of the record that states a fact, and every such field of each institute in the packet: summaries, notes, related bodies and their roles, timeline events, the closest body, and for an institute its names, host, legal form, mandate, legal basis, placement, head, people, reporting line, funding, staff, staffing, agreements, national partners, testing capacity, model access and outputs. Skip only identifiers and bookkeeping (`id`, `*source_ids`, `check`, `next_check`, `wikidata`, identifier fields). Split text into sentences and values into single claims. For each: does a cited source's saved copy carry it, and does anything else in the record contradict it? A claim in the present tense about a body, bill, draft or pilot gets a check that it still holds (step 2).
2. **Search by name.** One search per body, act, law, bill (with its number), strategy, plan or programme the record names (`entity_seeds` plus the names in the text), in English and, where the name has a local form, in the main official language. Look for dissolved, renamed, merged, adopted, rejected, withdrawn, completed, launched, appointed. For a bill, find its latest formal step and that step's date.
3. **Search by topic.** Run each of the state's `topic_queries` exactly as given. Check that each result concerns this state: some names are shared (Georgia and the US state, Guinea and its neighbours, the two Congos, Sudan and South Sudan).
4. **Window.** Look for items published from the state's window date above. A search by name about whether a named body or act still stands looks back to the date of the evidence the record cites for it.

## Screening (stricter than what the dataset admits)

A `candidate` needs a primary source of the state itself (its government, parliament, official gazette, or a public body acting under a government act), opened and quoted exactly. Anything else, including press, law firms, international organisations and trackers, is a lead: record it as `rejected` with a reason starting `LEAD:` and what primary text to look for.

## Findings

Append to `findings.jsonl`, one JSON object per line, one claim per line:

```json
{"url": "<primary source>", "scope": "<STATE> or <STATE>.<field path>", "verdict": "candidate|rejected",
 "reason": "<KIND>: <field>; now: \"<exact current text in the record>\"; should say: \"<one sentence>\"; published <YYYY-MM-DD>",
 "excerpt": "<exact quotation that carries the whole proposed sentence>", "title": "<page title>", "found_by": "<query id or query>"}
```

`KIND` is `STALE` (true when written, no longer true), `CONTRADICTED` (a cited source or the record itself says otherwise), `UNSUPPORTED` (no cited source carries it) or `NEW` (a fact the record lacks). One claim per line and one quotation per claim: the proposed sentence says nothing the excerpt does not carry. Two changes, two lines.

## Closing

- In `sweep.json`, set each state to `complete` only when every claim-bearing field in step 1 was re-read and steps 2 to 4 are done for it; otherwise `incomplete: <reason>` (fields not re-read, blocked sources, quota, time). Only a `complete` state moves its next window.
- `notes.md`: one line per state, starting `- <STATE>:`, with what you read, what you found and what was left and why; then "Blocked sources" (URL and what each route returned) and "Rule questions" (a finding that fits no method rule, or only by stretching one, is a `rejected` row whose reason starts `RULE GAP:`).
- End with a short summary in the chat: candidates and leads per state, states incomplete and why.
