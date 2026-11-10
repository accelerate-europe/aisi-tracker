"""Build the source monitor's short digest and send it with the maintainers' notification command.

The command is configured on the host, never in the repository: AISI_DIGEST_COMMAND holds it (split like a
shell command line), and the digest's title and body are added as its last two arguments.
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess
from datetime import date

CLASSES = (
    "substantive", "gone", "trivial", "unchanged", "archived", "blocked", "failed", "skipped",
)
ROUTES = ("headless", "jina", "wayback")
BODY_LIMIT = 1500
# A notification command may name its endpoint in an error; the digest never repeats a URL.
URL = re.compile(r"[a-z][a-z0-9+.-]*://\S+", re.IGNORECASE)


def _safe(text: str) -> str:
    return URL.sub("<url>", text.strip())


def _source_lines(heading: str, rows: list[dict], shown: int) -> list[str]:
    if not rows:
        return []
    lines = [heading + ":"]
    lines.extend(
        f"{row['source_id']} ({row['reason']}, {row['route']})" for row in rows[:shown]
    )
    if len(rows) > shown:
        lines.append(f"and {len(rows) - shown} more")
    return lines


def build(summary: dict) -> tuple[str, str]:
    """Return a plain digest, reserving room for totals, errors and the report path."""
    run_id = summary["run_id"]
    run_date = date.fromisoformat(run_id.split("T", 1)[0]).isoformat()
    title = f"AISI monitor {run_date}"
    error = summary["error"]
    if summary["interrupted"]:
        title = "INTERRUPTED: " + title
    elif error is not None:
        title = "FAILED: " + title

    revision = summary["dataset_revision"]
    commit = (revision["commit"] or "unknown")[:7]
    lines = [
        f"Run {run_id[:24]}",
        f"Dataset {revision['version']} ({commit}); {summary['seconds'] / 60:.1f} minutes",
    ]
    counts = ", ".join(
        f"{name} {summary['counts'][name]}"
        for name in CLASSES if summary["counts"].get(name)
    )
    if counts:
        lines.append(counts)
    baselines = ", ".join(f"{label} {count}" for label, count in summary["baselines"])
    comparison = (baselines + "; " if baselines else "") + f"target under {summary['target']}"
    lines.append(f"Live-unchecked {summary['live_unchecked']} ({comparison})")
    routes = ", ".join(
        f"{route} {summary['by_route'][route]}"
        for route in ROUTES if summary["by_route"].get(route)
    )
    if routes:
        lines.append("Decided by fallback: " + routes)

    # Source ID order, with reason and route as tie-breaks, makes truncation stable.
    groups = [
        (name.capitalize(), sorted(summary[name], key=lambda r: (
            r["source_id"], r["reason"], r["route"],
        )))
        for name in ("substantive", "gone", "unconfirmed")
    ]
    shown = [min(8, len(rows)) for _, rows in groups]
    tail = []
    due = summary["next_checks_due"]
    if due:
        # Keep the caller's due-date order; the monitor breaks ties by jurisdiction ID.
        tail.append(f"next_check due: {len(due)} ({', '.join(due[:5])})")
    if not summary["complete"]:
        tail.append(f"Not complete: resume with --resume {run_id}")
    if error is not None:
        detail = " ".join(error.split())
        if len(detail) > 240:
            detail = detail[:237] + "..."
        tail.append("Error: " + detail)
    tail.append(summary["report"])

    while True:
        sections = [
            _source_lines(heading, rows, count)
            for (heading, rows), count in zip(groups, shown, strict=True)
        ]
        body = "\n".join(lines + [line for section in sections for line in section] + tail)
        if len(body) <= BODY_LIMIT:
            return title, body
        if not any(shown):
            # Only fixed lines are left: cut them, keeping the report path last.
            report = tail[-1][: BODY_LIMIT // 2]
            return title, body[: BODY_LIMIT - len(report) - 5].rstrip() + "\n...\n" + report
        # Shorten the largest section first, retaining an explicit omitted count.
        largest = max(
            (i for i, count in enumerate(shown) if count),
            key=lambda i: len("\n".join(sections[i])),
        )
        shown[largest] -= 1


COMMAND = "AISI_DIGEST_COMMAND"


def send(title: str, body: str) -> None:
    """Send once, raising on failure so the run can retry notification on resume."""
    try:
        command = shlex.split(os.environ.get(COMMAND, ""))
    except ValueError as exc:
        raise RuntimeError(f"{COMMAND} is not a valid command line: {exc}") from None
    if not command or not command[0]:
        raise RuntimeError(f"no notification command: set {COMMAND}")
    name = os.path.basename(command[0])
    try:
        result = subprocess.run(
            [*command, title, body], capture_output=True, text=True, timeout=60, check=False,
        )
    except FileNotFoundError:
        raise RuntimeError(f"{name} not found") from None
    except subprocess.TimeoutExpired as exc:
        stderr = exc.stderr or ""
        if isinstance(stderr, bytes):
            stderr = stderr.decode("utf-8", errors="replace")
        detail = _safe(stderr)
        raise RuntimeError(
            f"{name} timed out after 60 seconds" + (f": {detail}" if detail else "")
        ) from None  # the timeout's own message repeats the command
    if result.returncode:
        detail = _safe(result.stderr)
        raise RuntimeError(f"{name} exited {result.returncode}" + (f": {detail}" if detail else ""))
