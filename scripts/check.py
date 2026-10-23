# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""One check command for agents, the pre-commit hook and CI.

  uv run scripts/check.py                       # every step on the staged snapshot, compared with HEAD
  uv run scripts/check.py --base global-expansion
  uv run scripts/check.py --only fast           # validate,lint,tests: the pre-commit hook
  uv run scripts/check.py --preflight           # can this agent run every step?

The staged index (not the working tree) is exported to a temporary directory and every step
runs there with the locked dependencies, so results describe exactly what would be committed
and nothing is written into the checkout. The report (Markdown, logs, screenshots) goes to
--report-dir (which must be empty) or a new temporary directory. A step that cannot run is
NOT RUN, which fails unless --allow-skip is given (refused in CI). This coordinator uses the
standard library only, so starting it never syncs the checkout's environment; the pre-commit
hook runs the staged copy of this file.
"""

import argparse
import hashlib
import io
import json
import os
import shutil
import re
import subprocess
import sys
import tarfile
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urljoin, urlsplit

sys.dont_write_bytecode = True


def toplevel() -> Path:
    """The repository in the current directory; the hook runs this file from stdin."""
    found = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True)
    return Path(found.stdout.strip()) if found.returncode == 0 else Path(__file__).resolve().parents[1]


ROOT = toplevel()
STEPS = ("validate", "lint", "tests", "copies", "build", "gate", "compare", "sheet")
# The pre-commit hook runs "fast"; CI runs "fast,build,gate". One definition for both.
GROUPS = {"fast": ("validate", "lint", "tests")}
NOT_RUN = 3  # check_page.py's exit code when Chrome is missing
EXCERPT_CHARS = 240
MAX_ROWS = 300
SERVE_URL = "http://127.0.0.1:8767/"
# This coordinator and snapshot_sources.py both read AISI_SNAPSHOTS.
_snapshot_directory = os.environ.get("AISI_SNAPSHOTS")
SNAPSHOTS = Path(_snapshot_directory).expanduser() if _snapshot_directory else None


@dataclass
class Result:
    step: str
    status: str  # PASS, FAIL or NOT RUN
    seconds: float = 0.0
    detail: str = ""
    log: Path | None = None


@dataclass
class Context:
    repo: Path
    snapshot: Path
    work: Path
    report: Path
    base: str | None
    release: bool
    dist: Path
    cache: Path
    env: dict = field(default_factory=dict)
    built: bool = False  # set only by a passing build step in this run
    sections: dict[str, str] = field(default_factory=dict)  # report sections by step
    serve_url: str = SERVE_URL  # where the owner serves the staged build for the Where to look links
    snapshots: Path | None = None  # private source snapshots that seed quote verification


UV_KEPT = ("UV_CACHE_DIR", "UV_PYTHON_INSTALL_DIR", "UV_PYTHON_DOWNLOADS", "UV_INDEX", "UV_DEFAULT_INDEX",
           "UV_EXTRA_INDEX_URL", "UV_NATIVE_TLS", "UV_OFFLINE", "UV_HTTP_TIMEOUT", "UV_EXCLUDE_NEWER")


def clean_env() -> dict:
    """Children run in the snapshot: no hook index, no outer virtualenv, no bytecode files."""
    # Any other uv setting could redirect the project, environment or sync and bypass the lock,
    # so only cache, interpreter and index settings and the resolution cutoff (which uv.lock records) pass through.
    env = {
        k: v for k, v in os.environ.items()
        if not k.startswith("GIT_") and k != "VIRTUAL_ENV" and (not k.startswith("UV_") or k.startswith(UV_KEPT))
    }
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def git(repo: Path, *args: str, check: bool = True) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=check).stdout


def run(ctx: Context, step: str, cmd: list[str], cwd: Path | None = None) -> tuple[int, float, Path]:
    log = ctx.report / "logs" / f"{step}.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    start = time.monotonic()
    with log.open("a", encoding="utf-8") as out:
        out.write("$ " + " ".join(cmd) + "\n")
        out.flush()
        code = subprocess.run(cmd, cwd=cwd or ctx.snapshot, env=ctx.env, stdout=out, stderr=subprocess.STDOUT).returncode
    return code, time.monotonic() - start, log


def tail(log: Path, lines: int = 12) -> str:
    return "\n".join(log.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:])


def command_step(ctx: Context, step: str, cmd: list[str]) -> Result:
    code, seconds, log = run(ctx, step, cmd)
    if code == 0:
        return Result(step, "PASS", seconds, log=log)
    return Result(step, "FAIL", seconds, tail(log), log)


def throwaway_index(tree: Path) -> None:
    """Some checks list tracked files with git, so an exported tree gets its own throwaway index."""
    clean = clean_env()
    subprocess.run(["git", "init", "-q", str(tree)], check=True, env=clean)
    subprocess.run(["git", "-C", str(tree), "add", "-A"], check=True, env=clean)


def make_snapshot(repo: Path, dest: Path) -> str:
    """Export a frozen copy of the staged index; the id hashes every path, mode and blob in it."""
    live = Path(os.environ.get("GIT_INDEX_FILE") or git(repo, "rev-parse", "--git-path", "index").strip())
    if not live.is_absolute():
        live = repo / live
    frozen = dest.parent / f"{dest.name}.index"
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(live, frozen)
    env = {**os.environ, "GIT_INDEX_FILE": str(frozen)}
    subprocess.run(["git", "-C", str(repo), "checkout-index", "-a", f"--prefix={dest}/"], check=True, env=env)
    listing = subprocess.run(["git", "-C", str(repo), "ls-files", "-s", "-z"], capture_output=True, check=True, env=env).stdout
    throwaway_index(dest)
    return hashlib.sha256(listing).hexdigest()[:16]


# Steps -------------------------------------------------------------------------------------------


def step_validate(ctx: Context) -> Result:
    cmd = ["uv", "run", "--locked", "-q", "python", "scripts/build.py", "--check", "--require-roster",
           "--report-dir", str(ctx.report / "source-origins")]
    result = command_step(ctx, "validate", cmd + (["--release"] if ctx.release else []))
    # build.py writes its release warnings beside the source-origin report; they never fail the step.
    warnings = ctx.report / "source-origins" / "release-warnings.md"
    if result.status == "PASS" and warnings.is_file():
        ctx.sections["warnings"] = warnings.read_text(encoding="utf-8")
        count = sum(line.startswith("- ") for line in ctx.sections["warnings"].splitlines())
        result.detail = f"{count} release warning{'' if count == 1 else 's'} (see Release warnings)" if count else ""
    return result


def step_lint(ctx: Context) -> Result:
    return command_step(ctx, "lint", ["uv", "run", "--locked", "-q", "ruff", "check", "--no-cache", "."])


def step_tests(ctx: Context) -> Result:
    return command_step(ctx, "tests", ["uv", "run", "--locked", "-q", "pytest", "-q", "-p", "no:cacheprovider"])


def snapshot_location(store: Path | None) -> str:
    return (f"not found at {store} (set AISI_SNAPSHOTS or pass --snapshots DIR)" if store is not None
            else "not configured (set AISI_SNAPSHOTS or pass --snapshots DIR)")


def step_copies(ctx: Context) -> Result:
    """Every recorded snapshot hash names a file held in the private store, checksum-verified.

    Mirrors snapshot_sources.stored_copy; repeated here because this coordinator is stdlib-only.
    """
    start = time.monotonic()
    store = ctx.snapshots
    if store is None or not store.is_dir():
        return Result("copies", "NOT RUN", 0.0, f"private store {snapshot_location(store)}; "
                      "a maintainer with the private store must run this step")
    data = json.loads((ctx.snapshot / "data/aisi.json").read_text(encoding="utf-8"))
    wanted = {s["id"]: s["snapshot"]["sha256"] for s in data["sources"] if s.get("snapshot")}
    missing = []
    for sid, digest in sorted(wanted.items()):
        manifest = store / sid / "manifest.json"
        rows = json.loads(manifest.read_text(encoding="utf-8")) if manifest.is_file() else []
        held = False
        for row in rows:
            path = (store / row["path"]).resolve()
            if (row.get("sha256") == digest and path.is_relative_to(store.resolve()) and path.is_file()
                    and hashlib.sha256(path.read_bytes()).hexdigest() == digest):
                held = True
                break
        if not held:
            missing.append(sid)
    seconds = time.monotonic() - start
    if missing:
        return Result("copies", "FAIL", seconds, f"{len(missing)} recorded snapshots have no matching file in {store} "
                      f"(save source copies there, see AGENTS.md): {', '.join(missing)}")
    return Result("copies", "PASS", seconds, f"{len(wanted)} recorded snapshots held in {store}")


def step_build(ctx: Context) -> Result:
    result = command_step(ctx, "build", ["uv", "run", "--locked", "-q", "python", "scripts/build.py", "--out", str(ctx.dist)])
    ctx.built = result.status == "PASS"
    return result


def gate_result(step: str, code: int, seconds: float, log: Path) -> Result:
    if code == 0:
        return Result(step, "PASS", seconds, log=log)
    if code == NOT_RUN:
        return Result(step, "NOT RUN", seconds, "Chrome/Chromium is not installed", log)
    return Result(step, "FAIL", seconds, tail(log), log)


def step_gate(ctx: Context) -> Result:
    if not ctx.built:
        return Result("gate", "NOT RUN", detail="no successful build of this snapshot in this run")
    code, seconds, log = run(ctx, "gate", ["uv", "run", "--locked", "-q", "scripts/check_page.py", "--dist", str(ctx.dist)])
    return gate_result("gate", code, seconds, log)


RENDERING_PATHS = ("site/", "scripts/build.py", "scripts/published.py", "scripts/display.py", "scripts/references.py", "scripts/row_checks.py")


def extremes(data, pointer: str = "", out: dict | None = None) -> dict:
    """Longest string and longest list per field pattern (array indices replaced by *)."""
    out = {} if out is None else out
    pattern = re.sub(r"/\d+(?=/|$)", "/*", pointer)
    if isinstance(data, dict):
        for key, value in data.items():
            extremes(value, f"{pointer}/{key}", out)
    elif isinstance(data, list):
        out[pattern + "[]"] = max(out.get(pattern + "[]", 0), len(data))
        for index, value in enumerate(data):
            extremes(value, f"{pointer}/{index}", out)
    elif isinstance(data, str):
        out[pattern] = max(out.get(pattern, 0), len(data))
    return out


def rendering_impact(ctx: Context) -> list[str]:
    """Why rendering can differ from the base: page or renderer changes, or longer extreme content."""
    env = {**os.environ, "GIT_INDEX_FILE": str(ctx.work / "snapshot.index")}
    changed = subprocess.run(["git", "-C", str(ctx.repo), "diff", "--cached", "--name-only", ctx.base],
                             capture_output=True, text=True, check=True, env=env).stdout.split()
    reasons = [f"{path} changed" for path in changed if path.startswith(RENDERING_PATHS)]
    if "data/aisi.json" in changed:
        shown = git(ctx.repo, "show", f"{ctx.base}:data/aisi.json", check=False)
        old = extremes(json.loads(shown)) if shown else {}
        new = extremes(json.loads((ctx.snapshot / "data/aisi.json").read_text(encoding="utf-8")))
        grown = sorted(k for k, v in new.items() if v > old.get(k, 0))
        if grown:
            reasons.append(f"longest content grew in {len(grown)} fields, e.g. {', '.join(grown[:3])}")
    return reasons


def build_base(ctx: Context) -> Path | None:
    """Export and build the base revision with its own build script; None if that fails."""
    base_dir = ctx.work / "base"
    base_dir.mkdir()
    archive = subprocess.run(["git", "-C", str(ctx.repo), "archive", "--format=tar", ctx.base], capture_output=True, check=True).stdout
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(base_dir, filter="data")
    throwaway_index(base_dir)
    # No --out: older revisions only build into their own dist/, here inside the temporary export.
    code, _, _ = run(ctx, "sheet", ["uv", "run", "--locked", "-q", "python", "scripts/build.py"], cwd=base_dir)
    return base_dir / "dist" if code == 0 and (base_dir / "dist/index.html").is_file() else None


def capture(ctx: Context, dist: Path, out: Path) -> tuple[int, float, Path]:
    # The staged check_page.py captures both builds, so before and after use the same states.
    return run(ctx, "sheet", ["uv", "run", "--locked", "-q", "scripts/check_page.py", "--dist", str(dist), "--capture", str(out)])


def crop_links(shots: Path, crop: str) -> str:
    """Links to the before and after capture of one crop; a side without a capture says so."""
    sides = []
    for side in ("before", "after"):
        path = shots / side / f"{crop}.png"
        sides.append(f"[{side}]({path})" if path.is_file() else f"{side}: absent")
    return ", ".join(sides)


def where_to_look(ctx: Context, changes: list[dict]) -> str:
    """The owner's list: one line per logical change, with where it shows and a link into the
    locally served staged build (copied into the report so it outlives this run)."""
    site = ctx.report / "site"
    shutil.copytree(ctx.dist, site)
    port = urlsplit(ctx.serve_url).port or 80
    lines = ["## Where to look", "",
             f"Serve the staged build with `python3 -m http.server {port} --bind 127.0.0.1 -d {site}`, then open each link and compare with the live site. Widths are window widths; the page opens light, and the theme toggle at the top right switches to dark.", ""]
    if not changes:
        lines.append("No visible change in the captured states.")
    for change in changes:
        link = urljoin(ctx.serve_url, f"#{change['anchor']}")
        lines.append(f"{change['number']}. {change['label']}: {change['what']}; at {change['states']}. [Open]({link})")
    return "\n".join(lines + [""])


def step_sheet(ctx: Context) -> Result:
    """Capture fixed page states; with rendering impact against the base, lead with before/after pairs."""
    if not ctx.built:
        return Result("sheet", "NOT RUN", detail="no successful build of this snapshot in this run")
    start = time.monotonic()
    shots, sheets = ctx.report / "screenshots", ctx.report / "sheets"
    code, _, log = capture(ctx, ctx.dist, shots / "after")
    if code != 0:
        return gate_result("sheet", code, time.monotonic() - start, log)
    reasons = rendering_impact(ctx) if ctx.base else []
    before = None
    if reasons:
        base_dist = build_base(ctx)
        if base_dist is None:
            return Result("sheet", "NOT RUN", time.monotonic() - start, f"rendering can change ({'; '.join(reasons)}) but the base {ctx.base[:12]} did not build", log)
        code, _, log = capture(ctx, base_dist, shots / "before")
        if code != 0:
            return gate_result("sheet", code, time.monotonic() - start, log)
        before = shots / "before"
    cmd = ["uv", "run", "--locked", "-q", "scripts/contact_sheet.py", "--after", str(shots / "after"), "--out", str(sheets)]
    code, _, log = run(ctx, "sheet", cmd + (["--before", str(before)] if before else []))
    if code != 0:
        return Result("sheet", "FAIL", time.monotonic() - start, tail(log), log)
    index = json.loads((sheets / "index.json").read_text(encoding="utf-8"))
    lines = ["## Visual pass", ""]
    if not reasons:
        lines.append(f"No rendering impact against {ctx.base[:12] if ctx.base else 'the base'}: no change to {' or '.join(RENDERING_PATHS)} and no longer extreme content. Visual pass not required; full sheets below.")
    else:
        changes = index["changes"]
        ctx.sections["where"] = where_to_look(ctx, changes)
        lines += [f"Rendering can change: {'; '.join(reasons)}.", "",
                  f"{len(changes)} change{'' if len(changes) == 1 else 's'} in {index['compared']} crops. Open each Where to look link on the live page at 1440, 1000 and 390 px, light and dark, and judge it against the design principles in site/index.html; the images below are optional.", ""]
        for change in changes:
            lines.append(f"- {change['number']}. [{change['image']}]({sheets / change['image']}): {change['label']} ({change['states']})")
            if change["left_out"]:
                lines.append(f"  - {len(change['left_out'])} smaller regions not in the image; all regions in {sheets / 'index.json'}")
        crops = sorted({r.split(" ")[0] for c in changes for r in c["regions"]})
        if crops:
            lines += ["", "Whole crops, full resolution:", ""]
            lines += [f"- {c}: {crop_links(shots, c)}" for c in crops]
    lines += ["", "### Full sheets (light beside dark)", ""]
    for width, pages in index["sheets"].items():
        lines.append(f"- {width} px: " + ", ".join(f"[{name}]({sheets / name})" for name, _ in pages))
    lines += ["", f"Full-resolution crops: {shots / 'after'}", ""]
    ctx.sections["sheet"] = "\n".join(lines)
    detail = f"{len(index['changes'])} changes" if reasons else "no rendering impact"
    return Result("sheet", "PASS", time.monotonic() - start, detail, log)


# Base comparison ---------------------------------------------------------------------------------


REMOVED = " (removed)"


def child(pointer: str, key) -> str:
    return f"{pointer}/{str(key).replace('~', '~0').replace('/', '~1')}"


def diff(old, new, pointer: str = "", base: str | None = None) -> list[tuple[str, object, object]]:
    """Changed leaves; records match permanent IDs or documented natural keys.

    A change or addition is named by its pointer into the staged data; a removal by its pointer
    into the base data plus " (removed)", because that is where its evidence is looked up.
    """
    base = pointer if base is None else base
    if isinstance(old, dict) and isinstance(new, dict):
        changes = []
        for key in list(new) + [k for k in old if k not in new]:
            if key not in old:
                changes.append((child(pointer, key), None, new[key]))
            elif key not in new:
                changes.append((child(base, key) + REMOVED, old[key], None))
            else:
                changes += diff(old[key], new[key], child(pointer, key), child(base, key))
        return changes
    if isinstance(old, list) and isinstance(new, list):
        # Keep the staged coordinator self-contained: the pre-commit hook executes
        # it from stdin, before the snapshot exists, without checkout imports.
        collection = pointer.rsplit("/", 1)[-1]
        identity_key = {"outputs": "lineage", "operating": "kind", "exchange_rates": "currency", "memberships": "body", "modes": "code"}.get(collection, "id")
        keyed = all(isinstance(x, dict) and identity_key in x for x in old + new)
        if keyed and any(len({x[identity_key] for x in rows}) != len(rows) for rows in (old, new)):
            raise ValueError(f"{pointer}: duplicate stable record identity")
        if keyed:
            before = {x[identity_key]: (index, x) for index, x in enumerate(old)}
            changes = []
            for index, item in enumerate(new):
                if item[identity_key] in before:
                    old_index, old_item = before.pop(item[identity_key])
                    changes += diff(old_item, item, child(pointer, index), child(base, old_index))
                else:
                    changes.append((child(pointer, index), None, item))
            return changes + [(child(base, index) + REMOVED, item, None) for index, item in before.values()]
        changes = []
        for index in range(max(len(old), len(new))):
            if index >= len(new):
                changes.append((child(base, index) + REMOVED, old[index], None))
            elif index >= len(old):
                changes.append((child(pointer, index), None, new[index]))
            else:
                changes += diff(old[index], new[index], child(pointer, index), child(base, index))
        return changes
    return [] if old == new else [(pointer, old, new)]


def short(value, limit: int = 160) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    text = " ".join(text.split()).replace("|", "\\|")
    return text if len(text) <= limit else text[: limit - 1] + "…"


def supporting(data: dict, pointer: str) -> list[tuple[str, dict]]:
    pointer = pointer.removesuffix(REMOVED)
    found = []
    for source in data.get("sources", []):
        for excerpt in source.get("excerpts", []):
            if any(s == pointer or s.startswith(pointer + "/") or pointer.startswith(s + "/") for s in excerpt["supports"]):
                found.append((source["id"], excerpt))
    return found


def excerpt_keys(data: dict) -> set[tuple[str, str]]:
    return {(s["id"], e["text"]) for s in data.get("sources", []) for e in s.get("excerpts", [])}


def binding_changes(old: dict, new: dict) -> list[dict]:
    """Report bindings and gloss edits even when quotation text is unchanged."""
    def bindings(data):
        result = {}
        for source in data.get("sources", []):
            for excerpt in source.get("excerpts", []):
                identity = excerpt.get("id") or "sha256:" + hashlib.sha256(excerpt["text"].encode("utf-8")).hexdigest()
                key = (source["id"], identity, excerpt["text"], excerpt.get("url", source.get("url", "")))
                group = result.setdefault(key, {"supports": set(), "glosses": set()})
                group["supports"].update(
                    json.dumps(ref, ensure_ascii=False, sort_keys=True) for ref in excerpt["supports"]
                )
                if "gloss" in excerpt:
                    group["glosses"].add(excerpt["gloss"])
        return result

    before, after = bindings(old), bindings(new)
    rows = []
    for key in sorted(before.keys() | after.keys()):
        previous = before.get(key, {"supports": set(), "glosses": set()})
        current = after.get(key, {"supports": set(), "glosses": set()})
        added = current["supports"] - previous["supports"]
        removed = previous["supports"] - current["supports"]
        before_gloss, after_gloss = sorted(previous["glosses"]), sorted(current["glosses"])
        if added or removed or before_gloss != after_gloss:
            row = {"source_id": key[0], "excerpt_id": key[1], "text": key[2], "url": key[3],
                   "added": [json.loads(ref) for ref in sorted(added)],
                   "removed": [json.loads(ref) for ref in sorted(removed)]}
            if before_gloss != after_gloss:
                row.update(gloss_before=before_gloss, gloss_after=after_gloss)
            rows.append(row)
    return rows


def verify_quotes(ctx: Context, data: dict, changed: list[tuple[str, str]]) -> tuple[str, list[str]]:
    """Offline literal quote check of new excerpt texts against the local source cache."""
    if not changed:
        return "PASS", ["No new or edited excerpt texts."]
    seeds = [*(["--seed-cache", str(ctx.cache)] if ctx.cache.is_dir() else []),
             *(["--seed-snapshots", str(ctx.snapshots)] if ctx.snapshots and ctx.snapshots.is_dir() else [])]
    if not seeds:
        return "NOT RUN", [f"{len(changed)} new or edited excerpt texts; no source cache at {ctx.cache} (pass --cache DIR) "
                           f"and private snapshots {snapshot_location(ctx.snapshots)}."]
    report = ctx.report / "excerpt-verification.json"
    # The real cache and the snapshots are only read: entries are copied into a scratch cache outside
    # the checkout, which absorbs re-extraction. A fresh clone without a cache is seeded from the snapshots.
    cmd = ["uv", "run", "--locked", "-q", "python", "scripts/verify_excerpts.py", "--offline", "--all-excerpts",
           "--cache", str(ctx.work / "source-cache"), *seeds, "--report", str(report)]
    for source_id in sorted({s for s, _ in changed}):
        cmd += ["--source", source_id]
    # Exit 1 only means some quotation needs review; the rows below say which.
    code, _, log = run(ctx, "compare", cmd)
    if code not in (0, 1) or not report.is_file():
        return "FAIL", [f"verify_excerpts.py failed; see {log}", tail(log)]
    wanted = set(changed)
    verified = json.loads(report.read_text(encoding="utf-8"))
    rows = [r for s in verified["sources"] for r in s["excerpts"] if (r["source_id"], r["text"]) in wanted]
    missing = wanted - {(r["source_id"], r["text"]) for r in rows}
    if verified.get("partial") or missing:
        return "FAIL", [f"verify_excerpts.py did not finish or skipped {len(missing)} excerpts; see {log}", tail(log)]
    lines = ["| Source | Excerpt ID | Status | Excerpt |", "| --- | --- | --- | --- |"]
    for row in rows:
        identity = row.get("excerpt_id") or "sha256:" + hashlib.sha256(row["text"].encode("utf-8")).hexdigest()
        lines.append(f"| {row['source_id']} | `{identity}` | {row['status']} | {short(row['text'], EXCERPT_CHARS)} |")
    mismatched = [r for r in rows if r["status"] == "mismatch"]
    unverified = [r for r in rows if not r["status"].startswith("matched") and r["status"] != "mismatch"]
    lines.append("")
    lines.append(f"{len(rows) - len(mismatched) - len(unverified)} matched, {len(mismatched)} not found in the cached document (fails), {len(unverified)} unverified (no usable cached copy; NOT RUN).")
    return ("FAIL" if mismatched else "NOT RUN" if unverified else "PASS"), lines


def resolved_report_data(ctx: Context, data: dict) -> dict:
    """Use the staged resolver once per document, never the live checkout's code."""
    script = "import json,sys; sys.path.insert(0,'scripts'); from references import materialise_references; print(json.dumps(materialise_references(json.load(sys.stdin))))"
    result = subprocess.run([sys.executable, "-c", script], cwd=ctx.snapshot, env=ctx.env,
                            input=json.dumps(data), text=True, capture_output=True)
    if result.returncode:
        raise ValueError(f"staged reference resolver failed: {result.stderr.strip()}")
    return json.loads(result.stdout)


def step_compare(ctx: Context) -> Result:
    start = time.monotonic()
    new = json.loads((ctx.snapshot / "data/aisi.json").read_text(encoding="utf-8"))
    shown = git(ctx.repo, "show", f"{ctx.base}:data/aisi.json", check=False)
    old = json.loads(shown) if shown else {}
    changes = diff({k: v for k, v in old.items() if k != "sources"}, {k: v for k, v in new.items() if k != "sources"})
    lines = [f"## Changes against {ctx.base}", "", f"### Changed claims ({len(changes)})", ""]
    if changes:
        old_resolved = resolved_report_data(ctx, old)
        new_resolved = resolved_report_data(ctx, new)
        lines += ["Pointers refer to the staged data; excerpts are those whose `supports` pointer is the claim, inside it or above it.", "",
                  "| Pointer | Before | After | Supporting excerpts |", "| --- | --- | --- | --- |"]
        for pointer, before, after in changes[:MAX_ROWS]:
            excerpts = supporting(old_resolved if pointer.endswith(REMOVED) else new_resolved, pointer)
            cited = "<br>".join(f"{sid}: \"{short(e['text'], EXCERPT_CHARS)}\"" for sid, e in excerpts[:4])
            if len(excerpts) > 4:
                cited += f"<br>and {len(excerpts) - 4} more"
            lines.append(f"| `{pointer}` | {short(before) if before is not None else ''} | {short(after) if after is not None else ''} | {cited or 'none'} |")
        if len(changes) > MAX_ROWS:
            lines.append(f"\n{len(changes) - MAX_ROWS} more changes are in changes.json.")
        (ctx.report / "changes.json").write_text(json.dumps([{"pointer": p, "before": b, "after": a} for p, b, a in changes], ensure_ascii=False, indent=1), encoding="utf-8")
    bindings = binding_changes(old, new)
    lines += ["", f"### Changed evidence bindings and glosses ({len(bindings)})", ""]
    if bindings:
        lines += ["References retain their authored representation (stable selectors or legacy pointers). Binding changes require semantic review; the quote check below rechecks only new or edited text, not unchanged quotations whose bindings changed.", "",
                  "| Source | Excerpt ID | Excerpt | Added references | Removed references | Gloss before / after |", "| --- | --- | --- | --- | --- | --- |"]
        lines += [f"| {row['source_id']} | `{row['excerpt_id']}` | {short(row['text'], EXCERPT_CHARS)} | {short(row['added'])} | {short(row['removed'])} | {short([row['gloss_before'], row['gloss_after']]) if 'gloss_before' in row else ''} |" for row in bindings[:MAX_ROWS]]
        lines.append("\nFull quotations, source URLs and references are in evidence-bindings.json.")
        (ctx.report / "evidence-bindings.json").write_text(json.dumps(bindings, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    changed_excerpts = sorted(excerpt_keys(new) - excerpt_keys(old))
    status, quote_lines = verify_quotes(ctx, new, changed_excerpts)
    lines += ["", f"### Quote verification of new or edited excerpts ({len(changed_excerpts)})", "", *quote_lines, "",
              "Not yet covered here: changed check dates with their basis and changed page copy beside its facts."]
    ctx.sections["compare"] = "\n".join(lines) + "\n"
    detail = f"{len(changes)} changed claims, {len(bindings)} changed evidence entries, {len(changed_excerpts)} new or edited excerpts"
    if status != "PASS":
        detail += "; quote verification " + ("found a quotation missing from its cached source" if status == "FAIL" else "NOT RUN")
    return Result("compare", status, time.monotonic() - start, detail)


# Preflight ---------------------------------------------------------------------------------------


def preflight(cache: Path, snapshots: Path | None) -> int:
    rows = []
    try:
        with tempfile.TemporaryDirectory(prefix="aisi-check-") as tmp:
            (Path(tmp) / "probe").write_text("ok")
        rows.append(("temporary output", True, tempfile.gettempdir()))
    except OSError as exc:
        rows.append(("temporary output", False, str(exc)))
    for tool in ("git", "uv", "node"):
        rows.append((tool, bool(shutil.which(tool)), shutil.which(tool) or "not on PATH"))
    try:  # check_page.py's own locked script environment, never the checkout's .venv
        chrome = subprocess.run(["uv", "run", "--locked", "-q", "scripts/check_page.py", "--find-chrome"], cwd=ROOT, env=clean_env(),
                                capture_output=True, text=True, timeout=300).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        chrome = ""
    rows.append(("Chrome/Chromium (build, gate, sheet)", bool(chrome), chrome or "missing: the build fails (pre-rendering needs Chrome), so gate and sheet cannot run"))
    print("| Capability | OK | Detail |\n| --- | --- | --- |")
    for name, ok, detail in rows:
        print(f"| {name} | {'yes' if ok else 'NO'} | {detail} |")
    print(f"| source cache (quote verification) | {'yes' if cache.is_dir() else 'no (optional)'} | {cache} |")
    store_detail = str(snapshots) if snapshots is not None else snapshot_location(None)
    rows.append(("private store (copies step; seeds quote verification)", bool(snapshots and snapshots.is_dir()), store_detail))
    print(f"| {rows[-1][0]} | {'yes' if rows[-1][1] else 'NO'} | {store_detail} |")
    return 0 if all(ok for _, ok, _ in rows) else 1


# The owner's list of where to look leads, then release warnings, the agent's visual pass and the claims.
REPORT_ORDER = ("where", "warnings", "sheet", "compare")


def ordered_sections(sections: dict[str, str]) -> list[str]:
    return [sections[step] for step in REPORT_ORDER if step in sections]


def blocking(results: list[Result], allow_skip: bool) -> list[Result]:
    """FAIL always blocks; NOT RUN blocks unless skips were explicitly allowed."""
    return [r for r in results if r.status == "FAIL" or (r.status == "NOT RUN" and not allow_skip)]


RUNNERS = {
    "validate": step_validate, "lint": step_lint, "tests": step_tests, "copies": step_copies, "build": step_build,
    "gate": step_gate, "compare": step_compare, "sheet": step_sheet,
}


# Main --------------------------------------------------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base", default="HEAD", help="revision to compare the staged snapshot with (default: HEAD)")
    ap.add_argument("--only", help=f"comma-separated steps from {','.join(STEPS)}, or fast (default: all)")
    ap.add_argument("--out", type=Path, help="build into this directory instead of a temporary one (CI publishes it)")
    ap.add_argument("--allow-skip", action="store_true", help="let NOT RUN steps pass (local only; refused in CI)")
    ap.add_argument("--skip-browser", action="store_true", help="record gate and sheet as NOT RUN when this agent cannot run Chrome; requires --allow-skip")
    ap.add_argument("--release", action="store_true", help="validate with build.py --release")
    ap.add_argument("--report-dir", type=Path, help="where to write report.md, logs and screenshots (default: a new temporary directory)")
    ap.add_argument("--cache", type=Path, default=ROOT / "work/source-cache", help="source cache for offline quote verification")
    ap.add_argument("--snapshots", type=Path, default=SNAPSHOTS, help="private store of source copies (default: AISI_SNAPSHOTS, otherwise unconfigured); checks copies and seeds quote verification")
    ap.add_argument("--serve-url", default=SERVE_URL, help=f"where the staged build is served for the Where to look links (default: {SERVE_URL})")
    ap.add_argument("--preflight", action="store_true", help="only check that this agent can run every step")
    args = ap.parse_args()
    if args.snapshots is not None:
        args.snapshots = args.snapshots.expanduser().resolve()
    if args.preflight:
        return preflight(args.cache, args.snapshots)
    steps = STEPS if not args.only else tuple(x for s in args.only.split(",") for x in GROUPS.get(s.strip(), (s.strip(),)))
    if unknown := set(steps) - set(STEPS):
        ap.error(f"unknown steps: {', '.join(sorted(unknown))}")
    if args.allow_skip and os.environ.get("CI"):
        ap.error("--allow-skip is for local runs only")
    if args.skip_browser and not args.allow_skip:
        ap.error("--skip-browser requires --allow-skip")
    base = git(ROOT, "rev-parse", "--verify", f"{args.base}^{{commit}}").strip() if {"compare", "sheet"} & set(steps) else None
    report = (args.report_dir or Path(tempfile.mkdtemp(prefix="aisi-check-report-"))).resolve()
    if report.exists() and any(report.iterdir()):
        ap.error(f"--report-dir {report} is not empty; every report starts from an empty directory")
    report.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="aisi-check-") as tmp:
        work = Path(tmp)
        snapshot_id = make_snapshot(ROOT, work / "snapshot")
        dist = args.out.resolve() if args.out else work / "dist"
        ctx = Context(ROOT, work / "snapshot", work, report, base, args.release, dist, args.cache.resolve(), clean_env(), serve_url=args.serve_url,
                      snapshots=args.snapshots)
        results = []
        for step in STEPS:
            if step in steps:
                try:
                    result = (Result(step, "NOT RUN", detail="browser unavailable to this agent; owner must run full check")
                              if args.skip_browser and step in {"gate", "sheet"} else RUNNERS[step](ctx))
                except Exception as exc:  # noqa: BLE001 - a crashed step is a failed step, and the report still gets written
                    result = Result(step, "FAIL", detail=f"{type(exc).__name__}: {exc}")
                print(f"{result.status:8} {step} ({result.seconds:.1f} s)", file=sys.stderr, flush=True)
                results.append(result)
    unstaged = git(ROOT, "diff", "--name-only").split()
    untracked = git(ROOT, "ls-files", "--others", "--exclude-standard").split()
    failed = blocking(results, args.allow_skip)
    lines = ["# Check report", "", f"Staged snapshot {snapshot_id}" + (f", compared with {args.base} ({base[:12]})" if base else "") + ".", ""]
    if unstaged or untracked:
        lines += [f"Not checked (unstaged or untracked): {', '.join(unstaged + untracked)}.", ""]
    lines += ["| Step | Result | Seconds | Detail |", "| --- | --- | --- | --- |"]
    for r in results:
        lines.append(f"| {r.step} | {r.status} | {r.seconds:.1f} | {short(r.detail.splitlines()[-1] if r.detail else '', 200)}{f' ([log]({r.log}))' if r.log else ''} |")
    lines += ["", f"Overall: {'FAIL' if failed else 'PASS'}" + (" (NOT RUN steps allowed by --allow-skip)" if args.allow_skip and any(r.status == 'NOT RUN' for r in results) else ""), ""]
    for r in results:
        if r.status == "FAIL" and r.detail:
            lines += [f"### {r.step} output (last lines)", "", "```", r.detail, "```", ""]
    (report / "report.md").write_text("\n".join(lines + ordered_sections(ctx.sections)), encoding="utf-8")
    print(f"{'FAIL' if failed else 'PASS'}: report at {report / 'report.md'}", file=sys.stderr)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
