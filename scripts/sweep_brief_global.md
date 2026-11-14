# Pass $pass_id: global topic sweep, $since to $until

You run one research pass for the AI Security Institutes tracker (https://aisi.accelerate-europe.org). The current data is release $version (as of $as_of). Your job: run a fixed set of cross-country searches over one window, triage what they return against the published data, and record screened findings. You do not edit the dataset: a separate session turns findings into data changes after an independent check.

## Ground rules

- You work only in this pass folder. Search with Exa (`web_search_exa`); fetch with `web_fetch_exa`. At most four subagents searching at the same time.
- Editorial rules are on the method page; follow the full rule text there: https://aisi.accelerate-europe.org/method.html. Current values: `https://aisi.accelerate-europe.org/api/v1/jurisdictions/<ISO code>.json` and `.../api/v1/institutes/<id>.json`.
- Never invent or guess URLs, dates, names or figures. Quote only text you read, in the original language.
- Personal data stays out: people only in their official role; never record a personal social-media profile, not even as a rejected result.

## Searches

Run each query once, limited to items published from $since to $until, 10 results each:

$queries

Do not add, drop or reword queries: the sweep is measured on this fixed set. Record in `notes.md` the number of results per query.

## Triage

1. Drop what is plainly off topic (explainers, sub-national acts, private institutes, roundups). Count them.
2. For each remaining item: which state does it concern, and is it a government act or body on AI (an institute set up, an authority designated, a strategy or law adopted or rejected, a body dissolved or renamed, a head appointed)? If not, drop it and count it.
3. For each such item, read the state's published record (API above) and, when the record names an institute (`institute_id`), that institute's record too: heads, agreements, funding and outputs live there. Already in either record: drop it and count it. Not in the record, or the record says otherwise: find and open the primary text of the state itself (government, parliament, official gazette, the body acting under a government act).
4. Record it as a `candidate` with the primary source, or as `rejected` with a reason starting `LEAD:` when you could not find or open the primary text.

## Findings

Append to `findings.jsonl`, one JSON object per line, one claim per line. `KIND` is `STALE` (true when written, no longer true), `CONTRADICTED` (the record says otherwise), `UNSUPPORTED` (no source the record cites carries it) or `NEW` (a fact the record lacks). The proposed sentence says nothing the excerpt does not carry; two changes, two lines.

```json
{"url": "<primary source>", "scope": "<STATE> or <STATE>.<field path>", "verdict": "candidate|rejected",
 "reason": "<KIND>: <field>; now: \"<exact current text, or none>\"; should say: \"<one sentence>\"; published <YYYY-MM-DD>",
 "excerpt": "<exact quotation that carries the whole proposed sentence>", "title": "<page title>", "found_by": "<topic:lang>"}
```

## Closing

`notes.md`: results per query; the triage counts (off topic, not a government act on AI, already in the record, candidates, leads); blocked sources; rule questions. End with a short summary in the chat with the same counts.
