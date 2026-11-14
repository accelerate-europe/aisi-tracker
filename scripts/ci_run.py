"""Wait for CI on a commit, or download the site CI built for it. Uses the GitHub CLI (gh), signed in.

  uv run --locked scripts/ci_run.py wait [SHA]                 # until the pages run for SHA (default HEAD) ends
  uv run --locked scripts/ci_run.py wait SHA --branch main     # the run of the push to main (a release, runbook step 4)
  uv run --locked scripts/ci_run.py site [SHA] --out DIR       # the `site` artifact of that run, extracted into DIR

Runs are those of push events (to main or next) and of runs by hand (gh workflow run pages.yml --ref <branch>), not a
pull request's merge build, on --branch when given; the newest counts. On main and next only the push run counts
(--event overrides). A run by hand whose changes needed no build
(pages.yml's plan) has no site to download.

wait exits 0 when the run succeeded and 1 otherwise, naming the failed jobs and the command for their logs. site
refuses an archive member that would land outside DIR (an absolute path, `..`, or a link), which `gh run download`
cannot be trusted to do for this artifact: the site is a tar inside the artifact zip.
"""
from __future__ import annotations

import argparse
import io
import json
import subprocess
import sys
import tarfile
import time
import zipfile
from pathlib import Path, PurePosixPath
from typing import Callable

WORKFLOW = "pages.yml"
ARTIFACT = "site"
Gh = Callable[[list[str]], str]


class CiError(Exception):
    pass


def gh(args: list[str]) -> str:
    result = subprocess.run(["gh", *args], capture_output=True, text=True)
    if result.returncode:
        raise CiError(f"gh {' '.join(args)}: {result.stderr.strip() or result.stdout.strip()}")
    return result.stdout


def gh_bytes(args: list[str]) -> bytes:
    result = subprocess.run(["gh", *args], capture_output=True)
    if result.returncode:
        raise CiError(f"gh {' '.join(args)}: {result.stderr.decode(errors='replace').strip()}")
    return result.stdout


def full_sha(rev: str) -> str:
    return subprocess.run(["git", "rev-parse", "--verify", f"{rev}^{{commit}}"], capture_output=True, text=True, check=True).stdout.strip()


# Runs that check exactly the commit: a push to main or next, or a run by hand on a branch (pages.yml starts no run on
# a push to other branches). A pull request's run builds its merge ref, another tree, so it never counts.
EVENTS = ("push", "workflow_dispatch")
# On main and next only the push run counts: it always runs every check (and on main publishes), while a run by hand
# with a base may skip the build, so a newer one must not stand in for it.
PUSH_ONLY_BRANCHES = ("main", "next")


def run_events(branch: str | None, event: str | None = None) -> tuple[str, ...]:
    return (event,) if event else ("push",) if branch in PUSH_ONLY_BRANCHES else EVENTS


def latest_run(run_gh: Gh, repo: str, sha: str, workflow: str, branch: str | None = None, event: str | None = None) -> dict | None:
    """The newest run of the workflow for this commit, on branch when given, or None: of the given event, else a
    push run on main or next and a push or by-hand run elsewhere. A re-run keeps its run and shows the current attempt."""
    events = run_events(branch, event)
    runs = json.loads(run_gh(["run", "list", "--repo", repo, "--commit", sha, "--workflow", workflow,
                              *(["--branch", branch] if branch else []),
                              "--json", "databaseId,status,conclusion,createdAt,url,event", "--limit", "50"]))
    runs = [r for r in runs if r["event"] in events]
    return max(runs, key=lambda r: (r["createdAt"], r["databaseId"])) if runs else None


def wait(run_gh: Gh, repo: str, sha: str, workflow: str = WORKFLOW, timeout: float = 3600, interval: float = 30,
         sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic, branch: str | None = None,
         event: str | None = None) -> dict:
    """Poll until the commit's newest run (on branch, when given) has completed; return it. CiError when none appears
    or it outlasts timeout."""
    start = clock()
    while True:
        run = latest_run(run_gh, repo, sha, workflow, branch, event)
        if run and run["status"] == "completed":
            return run
        if clock() - start > timeout:
            raise CiError(f"no completed {workflow} run for {sha[:12]} after {timeout:.0f} s"
                          + (f" (run {run['databaseId']} is {run['status']})" if run else " (no run started)"))
        sleep(interval)


def failed_jobs(run_gh: Gh, repo: str, run_id: int) -> list[str]:
    jobs = json.loads(run_gh(["run", "view", str(run_id), "--repo", repo, "--json", "jobs"]))["jobs"]
    return [f"{job['name']}: {job['conclusion']}" for job in jobs if job["conclusion"] not in ("success", "skipped")]


def safe_member_path(name: str) -> PurePosixPath:
    """The member's path inside the target folder ("./" is the folder itself); CiError for anything that leaves it."""
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts or "\\" in name or (path.parts and ":" in path.parts[0]):
        raise CiError(f"archive member {name!r} would land outside the target folder")
    return path


def extract_tar(data: bytes, out: Path) -> int:
    """Extract a tar into out, refusing any member outside it, any link and any special file; returns the file count."""
    count = 0
    with tarfile.open(fileobj=io.BytesIO(data)) as archive:
        members = archive.getmembers()
        for member in members:
            safe_member_path(member.name)
            if not (member.isfile() or member.isdir()):
                raise CiError(f"archive member {member.name!r} is a link or special file")
        for member in members:
            target = out / safe_member_path(member.name)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(archive.extractfile(member).read())
            count += 1
    return count


def site_tar(zipped: bytes) -> bytes:
    """The site.tar inside the artifact zip (the workflow uploads exactly one file)."""
    with zipfile.ZipFile(io.BytesIO(zipped)) as archive:
        names = [n for n in archive.namelist() if not n.endswith("/")]
        if [safe_member_path(n).as_posix() for n in names] != ["site.tar"]:
            raise CiError(f"expected one site.tar in the artifact, found {names}")
        return archive.read(names[0])


def download_site(run_gh: Gh, get_bytes: Callable[[list[str]], bytes], repo: str, run: dict, out: Path) -> int:
    if run["conclusion"] != "success":
        raise CiError(f"run {run['databaseId']} ended {run['conclusion']}; its site is not a passed build ({run['url']})")
    artifacts = json.loads(run_gh(["api", f"repos/{repo}/actions/runs/{run['databaseId']}/artifacts"]))["artifacts"]
    found = [a for a in artifacts if a["name"] == ARTIFACT and not a.get("expired")]
    if len(found) != 1:
        raise CiError(f"run {run['databaseId']} has {len(found)} unexpired {ARTIFACT!r} artifacts")
    if out.exists() and any(out.iterdir()):
        raise CiError(f"{out} is not empty")
    out.mkdir(parents=True, exist_ok=True)
    return extract_tar(site_tar(get_bytes(["api", f"repos/{repo}/actions/artifacts/{found[0]['id']}/zip"])), out)


def default_repo(run_gh: Gh) -> str:
    return run_gh(["repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner"]).strip()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("command", choices=("wait", "site"))
    parser.add_argument("rev", nargs="?", default="HEAD", help="commit (default HEAD)")
    parser.add_argument("--repo", help="OWNER/NAME (default: the checkout's GitHub repository)")
    parser.add_argument("--workflow", default=WORKFLOW)
    parser.add_argument("--branch", help="only runs on this branch (e.g. main for a release); on main and next only push runs count")
    parser.add_argument("--event", choices=EVENTS, help="only runs of this event (default: push on main and next, else either)")
    parser.add_argument("--timeout", type=float, default=3600, help="wait: seconds before giving up (default 3600)")
    parser.add_argument("--interval", type=float, default=30, help="wait: seconds between polls (default 30)")
    parser.add_argument("--out", type=Path, help="site: an empty or new folder for the extracted site")
    args = parser.parse_args(argv)
    if args.command == "site" and not args.out:
        parser.error("site needs --out DIR")
    try:
        sha, repo = full_sha(args.rev), args.repo or default_repo(gh)
        if args.command == "wait":
            run = wait(gh, repo, sha, args.workflow, args.timeout, args.interval, branch=args.branch, event=args.event)
            print(f"run {run['databaseId']} for {sha[:12]}: {run['conclusion']} ({run['url']})")
            if run["conclusion"] == "success":
                return 0
            if run["conclusion"] == "cancelled":
                print("cancelled, most likely superseded by a newer push to the branch: check that commit's run")
                return 1
            for line in failed_jobs(gh, repo, run["databaseId"]):
                print(f"- {line}")
            print(f"logs: gh run view {run['databaseId']} --repo {repo} --log-failed")
            return 1
        run = latest_run(gh, repo, sha, args.workflow, args.branch, args.event)
        if run is None or run["status"] != "completed":
            raise CiError(f"no completed {args.workflow} run for {sha[:12]}; wait for it first")
        count = download_site(gh, gh_bytes, repo, run, args.out)
        print(f"{count} files of run {run['databaseId']}'s site in {args.out}")
        return 0
    except (CiError, subprocess.CalledProcessError, OSError, ValueError, KeyError, tarfile.TarError, zipfile.BadZipFile) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
