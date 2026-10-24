"""Print the release tag (v<dataset.version>) after checking it against .zenodo.json, HEAD and the remote.

  tag=$(uv run scripts/release_tag.py) && gh release create "$tag" --target "$(git rev-parse HEAD)" ...

Run in the public repository on the commit to be released, after pushing it. Exits 1, printing nothing
on stdout, if .zenodo.json at HEAD carries another version, HEAD has another v* tag, or the tag already
exists, locally or on the remote, on anything but HEAD.
"""

import argparse
import json
import subprocess
import sys
from pathlib import Path

try:
    from .release_checks import check_release_tag, release_tag
except ImportError:
    from release_checks import check_release_tag, release_tag

ROOT = Path(__file__).resolve().parents[1]


def git(root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, check=check)


def local_tag_target(root: Path, tag: str) -> str | None:
    """The commit a local tag points to, None if absent; a tag on a non-commit counts as occupied."""
    if git(root, "rev-parse", "--verify", "--quiet", f"refs/tags/{tag}", check=False).returncode != 0:
        return None
    peeled = git(root, "rev-parse", "--verify", "--quiet", f"refs/tags/{tag}^{{commit}}", check=False)
    return peeled.stdout.strip() if peeled.returncode == 0 else "a non-commit"


def remote_tag_target(root: Path, remote: str, tag: str) -> str | None:
    """The object a remote tag points to (peeled for annotated tags), None if absent."""
    lines = git(root, "ls-remote", "--tags", remote, f"refs/tags/{tag}", f"refs/tags/{tag}^{{}}").stdout.split("\n")
    refs = dict(reversed(line.split("\t")) for line in lines if line)
    return refs.get(f"refs/tags/{tag}^{{}}") or refs.get(f"refs/tags/{tag}")


def main(argv: list[str] | None = None, root: Path = ROOT) -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--remote", default="origin", help="remote the release is created on (default: origin)")
    args = ap.parse_args(argv)
    data = json.loads(git(root, "show", "HEAD:data/aisi.json").stdout)
    zenodo = git(root, "show", "HEAD:.zenodo.json").stdout
    tag = release_tag(data)
    head = git(root, "rev-parse", "HEAD").stdout.strip()
    head_tags = git(root, "tag", "--points-at", "HEAD").stdout.split()
    errors = check_release_tag(data, zenodo, head_tags, local_tag_target(root, tag), head)
    remote = remote_tag_target(root, args.remote, tag)
    if remote is not None and remote != head:
        errors.append(f"tag {tag} already exists on {args.remote} at {remote[:12]}, not on HEAD {head[:12]}")
    if errors:
        print("\n".join(errors), file=sys.stderr)
        sys.exit(1)
    print(tag)


if __name__ == "__main__":
    main()
