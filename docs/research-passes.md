# Research passes

Research for the dataset runs as dedicated passes. A pass is one research session, or a session and its subagents, with its own folder in the private store and a pass file that states who ran it, with which harness and models, when, what kind of pass it is and its scope. During a pass the agent searches, reads and writes findings; it does not edit the repository. Data changes follow in a separate session whose commits name the pass.

`scripts/research_pass.py` extracts a pass's log and validates pass folders. `scripts/research_log.py` is the transcript extractor it uses; its module docstring defines the log format. `scripts/sweep.py` prepares the two recurring kinds of pass, the per-state re-check and the monthly global topic sweep ([sweeps](sweep.md)).

## Where passes live

Passes live in the private store, beside the source copies:

```
<private-store>/
  sources/                 # AISI_SNAPSHOTS
  passes/                  # AISI_PASSES (default: passes/ beside AISI_SNAPSHOTS)
    .homes/claude/         # Claude Code home for passes (CLAUDE_CONFIG_DIR): settings, login, sessions
    .homes/codex/          # Codex home for passes (CODEX_HOME): config, login, sessions
    2026-11-07-news-recheck/
      pass.json            # the pass file
      brief.md             # the brief the session started from
      findings.jsonl       # screened results, one per line
      notes.md             # optional free text: leftovers, access problems, open questions
      transcripts/         # copies of the session transcripts, subagents included
      log.jsonl            # extracted log (research_pass.py extract)
      log.diagnostics.json # extractor diagnostics
```

Transcripts are copied because Claude Code prunes old session files; the log can always be extracted again from the copies (`extract PASS --transcripts PASS/transcripts`). `extract` never overwrites a copy with a file that does not extend it, so running it again after a resumed session is safe. Nothing in a pass folder is committed to the repository or published.

Personal data must never enter a pass folder. Passes run from their own harness homes, so nothing from the user's own Claude Code or Codex setup loads: no settings, memory, hooks, skills, plugins or MCP servers. Instruction files (`CLAUDE.md`, `CLAUDE.local.md`, `AGENTS.md`, `.claude/rules/`) in the pass folder or any folder above it are excluded too, and so are project settings, hooks and MCP servers configured there (tested with marker files and a hook in the pass folder and its parents). The session has Exa, web search and fetch, and writes only in the pass folder; it has no shell and reads no local files outside the pass folder, so mail, messages, calendar, notes and other files are out of reach. What the brief and the person running the pass type into the session is not checked, and Claude Code still shows the session the account's email address.

## The pass file

`pass.json`; the schema is `PASS_SCHEMA` in `scripts/research_pass.py`.

```json
{
  "pass_id": "2026-11-07-news-recheck",
  "who": "Jason Hoelscher-Obermaier",
  "harness": {"name": "claude_code", "version": "2.1.318"},
  "models": ["claude-opus-5-5"],
  "sessions": [],
  "started": "2026-11-07",
  "ended": null,
  "type": "news-sweep",
  "scope": {"states": [], "institutes": ["all"], "fields": []},
  "search_tools": [
    {"tool": "mcp__exa__web_search_exa", "engine": "exa"},
    {"tool": "WebSearch", "engine": null, "options": "standard"}
  ],
  "stopping_reason": null,
  "status": "open"
}
```

- `pass_id` is the start date and a short slug, and names the folder.
- `type` is one of `full-recheck`, `news-sweep`, `access-check`, `field-fill`, `targeted`.
- `scope` lists states (ISO codes), institute IDs and fields (key paths of jurisdiction or institute records, dotted for nested keys, such as `national_partners.entries.relation`: any path the schema allows there, so a field no record has yet, such as `check.log`, can be scoped) from `data/aisi.json` and `schema/aisi.schema.json`; `"all"` stands alone in its list. Scope is set per pass, not per search: every search in a pass counts as effort for the pass's scope, off-topic ones included. A search is tagged with a narrower scope only when a pass covers several scopes and the search belongs to just one of them. (This moves to `dataset.method` with the research summary in 1.1.0, when effort counts are published.)
- `search_tools` lists every search tool the pass used. At least one must have a known engine (Exa). `engine` is set only for tools in `KNOWN_ENGINES` and stays `null` for the rest; it is never inferred.
- `sessions`, `models`, `harness` and `search_tools` must cover every execution in the log; `extract` prints the values to copy in.
- A closed pass has `ended`, `stopping_reason` (why the search stopped: scope exhausted, time box, blocked sources), its sessions, `log.jsonl` and `findings.jsonl`.

## Findings

`findings.jsonl` holds one JSON object per screened result: `url`, `scope` (the state, institute or field it bears on), `verdict` (`candidate` for a result to use in the data step, `rejected` for one screened out), `reason`, and optionally `title`, `excerpt` (an exact quotation) and `found_by` (the query or page that led to it). A pass that screened nothing has an empty file. A result that is a person's own profile page (a LinkedIn `/in/` or `/pub/` address) is left out altogether, not recorded as rejected; `validate` refuses one.

## Running a pass

The commands below use `$AISI_PASSES`; set it to the pass store (for example `export AISI_PASSES=<private-store>/passes`), or use the paths `homes` prints. Once per machine, write the two homes from the configuration in `scripts/` and log in to each:

```sh
uv run --locked scripts/research_pass.py homes
CLAUDE_CONFIG_DIR="$AISI_PASSES/.homes/claude" claude auth login
CODEX_HOME="$AISI_PASSES/.homes/codex" codex login
```

Run `homes` again after a change to `scripts/research_pass_claude.json`, `research_pass_mcp.json` or `research_pass_codex.toml`; it replaces the configuration and keeps logins and sessions. Exa needs `EXA_API_KEY` in the environment.

1. Create the folder and write `pass.json` with `"status": "open"`, and `brief.md` with the task: the scope, what to look for, and that findings go to `findings.jsonl` in the format above. The session reads no repository files: rules come from the published method page (`https://aisi.accelerate-europe.org/method.html#<rule-id>`) and current values from the published API (`api/v1/`), or the brief quotes what it needs.
2. Start the session from the pass folder, with either harness:

   ```sh
   cd "$AISI_PASSES/2026-11-07-news-recheck"
   CLAUDE_CONFIG_DIR="$AISI_PASSES/.homes/claude" claude --setting-sources user --strict-mcp-config --mcp-config="$AISI_PASSES/.homes/claude/mcp.json" -- "$(cat brief.md)"
   CODEX_HOME="$AISI_PASSES/.homes/codex" codex "$(cat brief.md)"
   ```

   `--mcp-config` takes several values, so it is written with `=` and followed by `--`; written as `--mcp-config PATH "$(cat brief.md)"`, it reads the brief as a second config file and the session starts without its task. A new pass folder first shows Claude Code's folder-trust prompt, whose default answer exits; choose "Yes, I trust this folder". Add `-p` (Claude) or use `codex exec --skip-git-repo-check` for an unattended run (a pass folder is not a Git repository, and `codex exec` refuses to start outside one without the flag). In Claude Code everything outside the allow list in the settings is denied without asking, `--setting-sources user` loads only the home's settings, never project settings (which could add hooks), and `--strict-mcp-config` ignores MCP servers configured anywhere else. In Codex the shell, image viewer and image generation are off, so no tool reads local files (the sandbox bounds writes, not reads: the boundary for reads is the tool set), the sandbox allows writes only in the pass folder, and Exa is the only MCP server; its login is kept in the home, not the shared keychain.
3. Close the pass:

   ```sh
   uv run --locked scripts/research_pass.py extract 2026-11-07-news-recheck
   ```

   This finds the sessions started in the pass folder in both homes (or takes `--transcripts PATH...`), copies them into `transcripts/`, writes `log.jsonl` and prints the sessions, models, harness and search tools to record. Fill those in, set `ended`, `stopping_reason` and `"status": "closed"`, then:

   ```sh
   uv run --locked scripts/research_pass.py validate 2026-11-07-news-recheck
   ```

   A closed pass must have logged at least one search with a known-engine tool.

## Naming the pass in a data commit

A commit that adds, removes or changes a jurisdiction, institute or source record in `data/aisi.json` names the pass its facts came from in a trailer:

```
Research-pass: 2026-11-07-news-recheck
```

A commit may name several passes. A data commit that comes from no pass (a typo, a rule refactor, regenerated values) says why instead: `Research-pass: none (<reason>)`. Merge commits inherit from their parents and need no trailer; record changes made while resolving a merge conflict are not checked. Commits authored before 7 November 2026, when passes began, are not checked.

Two places check the trailers:

- The `commit-msg` hook (`.githooks/commit-msg`, installed with the pre-commit hook) refuses a new commit whose staged data changes records without a trailer, and checks each named pass in the store when one is configured. It does not check amends, which the `passes` step catches on the branch, or merges.
- The `passes` step of `check.py` checks every commit in `--base..HEAD`: each named pass must be in the store, valid and closed. A commit with `none` is listed in the report with its reason. Without a pass store the step is NOT RUN when a commit names a pass; a missing trailer fails either way.

`uv run --locked scripts/research_pass.py commits --base <rev>` runs the same check directly.
