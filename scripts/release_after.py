"""Finish a release after the owner's Go has moved main, stopping at the first failure.

    uv run --locked scripts/release_after.py [--act --work DIR --token-file FILE]

The default prints the plan and probes the release without changing it. --act writes
notes, creates the GitHub release, updates Zenodo and stages the next development
version in a separate worktree. It never commits or pushes. See docs/release.md.
--act requires --work so development files have an explicit destination.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import datetime
from pathlib import Path

try:
    from .versioning import parse_version, require_release
except ImportError:
    from versioning import parse_version, require_release

ROOT = Path(__file__).resolve().parents[1]
STEPS = ("Live site", "Release notes", "GitHub release", "Zenodo", "Tags",
         "Next development version", "Summary")
UV = ["uv", "run", "--locked"]
NEWS = ("Within three days, reopen the news and publications pages of every institute, "
        "record a check.partial entry for each and add anything new (docs/release.md step 7).")


class Refusal(Exception):
    """A failed check which must prevent every later step."""


def release_notes(data: dict, version: str) -> str:
    entries = [entry for entry in data["changelog"] if entry["version"] == version]
    if len(entries) != 1:
        raise Refusal(f"expected one changelog entry for {version}, found {len(entries)}")
    entry, = entries
    summary, changes = entry.get("summary"), entry.get("changes")
    if not isinstance(summary, str) or not summary.strip():
        raise Refusal(f"changelog {version} has an empty summary")
    if not isinstance(changes, list) or not changes or not all(isinstance(c, str) and c.strip() for c in changes):
        raise Refusal(f"changelog {version} needs non-empty changes")
    return "\n".join([summary, "", *("- " + change for change in changes)]) + "\n"


def development_bytes(raw: bytes, release: str, version: str) -> bytes:
    """Replace one value, preserving all other bytes and checking the parsed difference."""
    before = json.loads(raw)
    if before["dataset"]["version"] != release:
        raise Refusal(f"origin/next carries {before['dataset']['version']}, expected {release}")
    expected = json.loads(raw)
    expected["dataset"]["version"] = version
    candidates = []
    # The changelog also has version lines; accept only the edit of dataset.version.
    for match in re.finditer(rb'(?m)^[ \t]*"version"[ \t]*:[ \t]*"([^"\r\n]*)"', raw):
        if match[1] != release.encode():
            continue
        changed = raw[:match.start(1)] + version.encode() + raw[match.end(1):]
        if json.loads(changed) == expected:
            candidates.append(changed)
    if len(candidates) != 1:
        raise Refusal("cannot identify one dataset.version line without changing other JSON values")
    changed, = candidates
    return changed


def edit_development_version(path: Path, release: str, version: str) -> None:
    path.write_bytes(development_bytes(path.read_bytes(), release, version))


def read_token(path: Path) -> str:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        raise Refusal(f"cannot read Zenodo token file {path}") from None
    values = []
    for line in lines:
        match = re.match(r"^\s*(?:export\s+)?ZENODO_TOKEN\s*=(.*)$", line)
        if match:
            try:
                parts = shlex.split(match[1], comments=True)
            except ValueError:
                raise Refusal(f"invalid ZENODO_TOKEN in {path}") from None
            if len(parts) != 1 or not parts[0]:
                raise Refusal(f"empty or invalid ZENODO_TOKEN in {path}")
            values.extend(parts)
    if len(values) != 1:
        raise Refusal(f"expected one ZENODO_TOKEN key in {path}")
    token, = values
    return token


class ReleaseAfter:
    def __init__(self, *, root, act, work, runner, token_file, sleep, clock):
        self.root, self.act, self.work = root, act, work
        self.runner, self.token_file = runner, token_file
        self.sleep, self.clock = sleep, clock
        self.step = 0
        self.precondition = 1
        self.outcomes = {}
        # Even an inherited token must not reach read-only helpers or git.
        self.env = dict(os.environ)
        inherited = self.env.pop("ZENODO_TOKEN", "")
        self.secrets = [inherited] if inherited else []

    def scrub(self, value: str) -> str:
        for secret in self.secrets:
            value = value.replace(secret, "[redacted]")
        return value

    def say(self, value: str, *, error=False):
        print(self.scrub(value), file=sys.stderr if error else sys.stdout, flush=True)

    def command(self, argv, *, cwd=None):
        suffix = f" (in {cwd})" if cwd is not None else ""
        self.say(f"  $ {shlex.join(argv)}{suffix}")

    def call(self, argv, *, allowed=(0,), cwd=None, token=None, echo=True):
        self.command(argv, cwd=cwd)
        env = self.env.copy()
        if token is not None:
            env["ZENODO_TOKEN"] = token
        try:
            result = self.runner(argv, cwd=cwd or self.root, env=env,
                                 capture_output=True, text=True, check=False)
        except OSError as exc:
            raise Refusal(f"{shlex.join(argv)} could not start: {exc}") from None
        # Scrub before retaining output as well as before printing it.
        result = subprocess.CompletedProcess(argv, result.returncode,
                                            self.scrub(result.stdout or ""), self.scrub(result.stderr or ""))
        if echo or result.returncode not in allowed:
            if result.stdout.strip():
                self.say(result.stdout.rstrip())
            if result.stderr.strip():
                self.say(result.stderr.rstrip(), error=True)
        if result.returncode not in allowed:
            self.failed(argv, result)
        return result

    def failed(self, argv, result, reason=""):
        suffix = f" ({reason})" if reason else ""
        raise Refusal(f"{shlex.join(argv)} exited {result.returncode}{suffix}")

    def begin(self, step):
        self.step = step
        self.say(f"{step}. {STEPS[step - 1]}")

    def outcome(self, text):
        self.outcomes[self.step] = text
        self.say(f"  {text}")

    def preconditions(self):
        self.say("Precondition 1: HEAD carries a release version")
        data = json.loads(self.call(["git", "show", "HEAD:data/aisi.json"], echo=False).stdout)
        version = data["dataset"]["version"]
        require_release(version)
        self.precondition = 2
        self.say("Precondition 2: HEAD is origin/main and an ancestor of origin/next")
        self.call(["git", "fetch", "origin", "main", "next", "--tags"])
        sha = self.call(["git", "rev-parse", "HEAD"], echo=False).stdout.strip()
        main = self.call(["git", "rev-parse", "origin/main"], echo=False).stdout.strip()
        if sha != main:
            raise Refusal(f"HEAD {sha} does not equal origin/main {main}")
        self.call(["git", "merge-base", "--is-ancestor", sha, "origin/next"])
        self.precondition = 3
        self.say("Precondition 3: CI passed on main for HEAD")
        command = UV + ["scripts/ci_run.py", "wait", sha, "--branch", "main"]
        if not self.act:
            command += ["--timeout", "5", "--interval", "1"]
        self.call(command)
        return data, version, sha

    def github_release(self, tag, sha, notes):
        view = ["gh", "release", "view", tag, "--json", "tagName,publishedAt,isDraft"]
        result = self.call(view, allowed=(0, 1), echo=False)
        if result.returncode == 1:
            # Authentication and network failures must never become permission to create.
            if (result.stderr.strip() or result.stdout.strip()).lower() != "release not found":
                self.failed(view, result, "could not establish whether the release exists")
            create = ["gh", "release", "create", tag, "--target", sha,
                      "--title", tag, "--notes-file", str(notes)]
            if not self.act:
                self.command(create)
                self.outcome("would run (create GitHub release)")
                return None
            self.call(create)
            result = self.call(view, echo=False)
            outcome = "done (created GitHub release)"
        else:
            outcome = "already done (GitHub release tag points at HEAD)"
        release = json.loads(result.stdout)
        if release["tagName"] != tag or release["isDraft"] or not release["publishedAt"]:
            raise Refusal(f"{tag} is not the expected published GitHub release")
        refs = self.call(["git", "ls-remote", "--tags", "origin", f"refs/tags/{tag}", f"refs/tags/{tag}^{{}}"], echo=False)
        targets = dict(line.split()[::-1] for line in refs.stdout.splitlines() if line)
        target = targets.get(f"refs/tags/{tag}^{{}}") or targets.get(f"refs/tags/{tag}")
        if target != sha:
            raise Refusal(f"release {tag} points at {target or 'no remote tag'}, not HEAD {sha}")
        self.outcome(outcome)
        return datetime.fromisoformat(release["publishedAt"].replace("Z", "+00:00")).timestamp()

    def zenodo(self, version, published):
        token = None
        if self.act:
            token = read_token(self.token_file)
            self.secrets.append(token)
        elif self.token_file is None:
            self.say("  Token file: not given (--act needs --token-file)")
        else:
            self.say(f"  Token file {self.token_file}: {'exists' if self.token_file.is_file() else 'missing'} (not read)")
        diff = UV + ["scripts/zenodo_metadata.py"]
        apply = diff + ["--apply"]
        if not self.act and published is None:
            self.command(diff)
            self.command(apply)
            self.command(diff)
            self.outcome("would run (after the GitHub release: wait for Zenodo to archive it, diff, --apply, verify)")
            return
        result = self.call(diff, allowed=(0, 1, 2))
        missing = re.compile(r"refused: 0 Zenodo versions of concept \d+ carry version "
                             + re.escape(repr(version)) + r"; expected one\s*\Z")
        deadline = self.clock() + 600
        while result.returncode == 2:
            fresh = published is not None and 0 <= self.clock() - published < 600
            last_line = next((line.strip() for line in reversed(result.stderr.splitlines()) if line.strip()), "")
            if not missing.fullmatch(last_line) or not fresh or self.clock() + 30 > deadline:
                self.failed(diff, result, "Zenodo refused or its version did not appear within ten minutes")
            self.say("  Zenodo has not archived the new release; waiting 30 s")
            self.sleep(30)
            result = self.call(diff, allowed=(0, 1, 2))
        if result.returncode == 0:
            self.outcome("already done (Zenodo metadata matches)")
            return
        if not self.act:
            self.command(apply)
            self.command(diff)
            if self.token_file is None or not self.token_file.is_file():
                raise Refusal(f"cannot apply without a Zenodo token file (--token-file; given: {self.token_file or 'none'})")
            self.outcome("would run (--apply, then verify; token key checked only with --act)")
            return
        applied = self.call(apply, token=token)
        verified = self.call(diff, allowed=(0, 1))
        if verified.returncode == 1 and "published; record " in applied.stdout and " now matches" in applied.stdout:
            self.say("  Zenodo still differs immediately after publishing; waiting 60 s once")
            self.sleep(60)
            verified = self.call(diff, allowed=(0, 1))
        if verified.returncode:
            self.failed(diff, verified, "Zenodo metadata still differs after applying")
        self.outcome("done (Zenodo applied and verified)")

    def development(self, version):
        next_version = parse_version(version).next_patch
        branch = f"dev-{next_version.removesuffix('-dev')}"
        dev = self.work / "dev"
        local = self.call(["git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"], allowed=(0, 1), echo=False)
        remote = self.call(["git", "ls-remote", "--heads", "origin", f"refs/heads/{branch}"], echo=False)
        if local.returncode == 0 or remote.stdout.strip():
            raise Refusal(f"{branch}: already done or in progress (branch exists locally or on origin)")
        if dev.exists() or dev.is_symlink():
            raise Refusal(f"{dev}: already done or in progress (worktree path exists)")
        raw = self.call(["git", "show", "origin/next:data/aisi.json"], echo=False).stdout.encode()
        development_bytes(raw, version, next_version)
        commands = [
            (["git", "worktree", "add", "-b", branch, str(dev), "origin/next"], None),
            (UV + ["scripts/build.py", "--update-readme"], dev),
            (["git", "add", "-A"], dev),
            (["git", "diff", "--cached", "--stat"], dev),
        ]
        for index, (command, cwd) in enumerate(commands):
            if self.act:
                self.call(command, cwd=cwd)
            else:
                self.command(command, cwd=cwd)
            if index == 0:
                self.say(f"  Python: replace only dataset.version with {next_version} in {dev / 'data/aisi.json'}; verify the JSON difference")
                if self.act:
                    edit_development_version(dev / "data/aisi.json", version, next_version)
        self.outcome("done (development changes staged for review)" if self.act else "would run (prepare development worktree and stage changes)")
        self.say(f"  Next in {dev}: independent review of git diff --cached; commit with a Reviewed-by trailer; push {branch}; run CI by hand; merge into next after CI passes.")
        self.command(["gh", "workflow", "run", "pages.yml", "--ref", branch, "-f", "base=origin/next"], cwd=dev)
        self.say("  Wait for that commit's CI with scripts/ci_run.py before merging (docs/release.md).")

    def summary(self):
        self.say("7. Summary")
        for number, name in enumerate(STEPS[:-1], 1):
            self.say(f"  {number}. {name}: {self.outcomes.get(number, 'not reached')}")
        self.say(f"  Manual follow-up: {NEWS}")

    def run(self):
        try:
            data, version, sha = self.preconditions()
            self.say(f"{'Act' if self.act else 'Dry run'}; work directory: {self.work}" + ("" if self.act else " (created only with --act)"))
            self.begin(1)
            self.call(UV + ["scripts/smoke_site.py", "--version", version])
            self.outcome("already done (live site passed)")
            self.begin(2)
            notes = self.work / f"notes-{version}.md"
            body = release_notes(data, version)
            self.say(f"  Python: write changelog summary, blank line and '- ' changes to {notes}")
            if notes.is_symlink():
                raise Refusal(f"notes path is a symbolic link: {notes}")
            if notes.exists():
                if notes.read_bytes() != body.encode():
                    raise Refusal(f"existing notes differ from HEAD: {notes}")
                self.outcome("already done (notes match HEAD changelog)")
            elif self.act:
                self.work.mkdir(parents=True, exist_ok=True)
                with notes.open("x", encoding="utf-8", newline="\n") as stream:
                    stream.write(body)
                self.outcome("done (release notes written)")
            else:
                self.outcome("would run (write release notes)")
            self.begin(3)
            tag = self.call(UV + ["scripts/release_tag.py"], echo=False).stdout.strip()
            if not tag or len(tag.splitlines()) != 1:
                raise Refusal("release_tag.py did not print one tag")
            published = self.github_release(tag, sha, notes)
            self.begin(4)
            self.zenodo(version, published)
            self.begin(5)
            fetch = ["git", "fetch", "origin", "--tags"]
            if self.act:
                self.call(fetch)
                self.outcome("already done (tags fetched)")
            else:
                self.command(fetch)
                self.outcome("would run (fetch tags)")
            self.begin(6)
            self.development(version)
        except (Refusal, OSError, ValueError, KeyError, TypeError) as exc:
            if self.step:
                self.outcomes[self.step] = f"{'stopped' if self.act else 'would refuse'} ({exc})"
                self.say(f"STOPPED at step {self.step} ({STEPS[self.step - 1]}): {exc}", error=True)
                self.summary()
            else:
                self.say(f"{'REFUSED' if self.act else 'would refuse'} at precondition {self.precondition}: {exc}", error=True)
            return 1
        self.summary()
        return 0


def main(argv=None, *, root=ROOT, runner=subprocess.run, sleep=time.sleep, clock=time.time):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--act", action="store_true", help="perform the plan, leaving development changes staged")
    parser.add_argument("--work", type=Path, help="notes and development worktree directory (required with --act; dry-run default: a new temporary path)")
    # The owner names the token file on each run, so the repository carries no host's private path.
    parser.add_argument("--token-file", type=Path, help="file holding ZENODO_TOKEN=... (required with --act; read only for the Zenodo apply)")
    args = parser.parse_args(argv)
    if args.act and args.work is None:
        parser.error("--act requires --work")
    if args.act and args.token_file is None:
        parser.error("--act requires --token-file")
    work = args.work or Path(tempfile.gettempdir()) / f"aisi-release-{uuid.uuid4().hex}"
    return ReleaseAfter(root=Path(root), act=args.act, work=work.absolute(), runner=runner,
                        token_file=args.token_file, sleep=sleep, clock=clock).run()


if __name__ == "__main__":
    sys.exit(main())
