"""One browser slot per host, shared by the gate, pre-render and source monitor.

Only children receive the ownership marker; taking a slot never changes os.environ.
run_in_slot is used from the monitor's main thread, not from its fetch workers.
"""

from __future__ import annotations

import ctypes
import fcntl
import os
import signal
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


def lock_path() -> Path:
    """The configured host lock, with its parent directory ready for use."""
    if path := os.environ.get("AISI_BROWSER_LOCK"):
        result = Path(path).expanduser()
    elif runtime := os.environ.get("XDG_RUNTIME_DIR"):
        result = Path(runtime) / "aisi-browser.lock"
    else:
        result = Path.home() / ".cache/aisi-tracker/browser.lock"
    result = result.resolve()
    result.parent.mkdir(parents=True, exist_ok=True)
    return result


@contextmanager
def browser_slot(holder: str, wait: float | None = None) -> Iterator[bool]:
    """Hold the host lock, or yield False after wait seconds (None waits forever).

An ancestor's resolved-path marker permits nested use. Callers pass that marker
explicitly in their children's environment; this context never sets it globally.
"""
    path = lock_path()
    if os.environ.get("AISI_BROWSER_LOCK_HELD") == str(path):
        yield True
        return
    deadline = None if wait is None else time.monotonic() + max(0, wait)
    announced = False
    with path.open("a+", encoding="utf-8") as handle:
        while True:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if deadline is not None and time.monotonic() >= deadline:
                    yield False
                    return
                if deadline is None and not announced:
                    handle.seek(0)
                    owner = handle.read().strip() or "an unrecorded holder"
                    print(f"Waiting for browser slot held by {owner}", file=sys.stderr, flush=True)
                    announced = True
                time.sleep(0.05 if deadline is None else min(0.05, max(0, deadline - time.monotonic())))
        try:
            handle.seek(0)
            handle.truncate()
            handle.write(f"{holder} (PID {os.getpid()})\n")
            handle.flush()
            yield True
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


@contextmanager
def _reap_descendants() -> Iterator[None]:
    """On Linux, adopt orphaned grandchildren, including detached browser processes.

Other hosts reap orphans themselves. Restore the process setting afterwards;
run_in_slot is a main-thread operation and reaps only the descendants it owns.
"""
    if sys.platform != "linux":
        yield
        return
    libc = ctypes.CDLL(None, use_errno=True)
    previous = ctypes.c_int()
    # Linux prctl: PR_GET_CHILD_SUBREAPER = 37, PR_SET_CHILD_SUBREAPER = 36.
    if libc.prctl(37, ctypes.byref(previous), 0, 0, 0) or libc.prctl(36, 1, 0, 0, 0):
        raise OSError(ctypes.get_errno(), "could not arrange browser child reaping")
    try:
        yield
    finally:
        if libc.prctl(36, previous.value, 0, 0, 0):
            raise OSError(ctypes.get_errno(), "could not restore child reaping")


def _descendants(excluded: set[int] | None = None) -> set[int]:
    """Linux descendants of this process, without the excluded children or their trees."""
    if sys.platform != "linux":
        return set()
    excluded = excluded or set()
    parents = {}
    for path in Path("/proc").glob("[0-9]*/stat"):
        try:
            # The command name can contain spaces and parentheses; ppid follows its closing bracket and state.
            fields = path.read_text().rsplit(")", 1)[1].split()
            parents[int(path.parent.name)] = int(fields[1])
        except (FileNotFoundError, ProcessLookupError, PermissionError):
            continue  # a process exited, or belongs to another user on a host with restricted /proc
    family, frontier = set(), {os.getpid()}
    while frontier:
        frontier = {pid for pid, parent in parents.items() if parent in frontier and pid not in excluded}
        family.update(frontier)
    return family


def _owned_descendants(process: subprocess.Popen | None, before: set[int]) -> set[int]:
    if process is not None:
        process.poll()  # reap the helper first, preserving its return code
    for pid in _descendants(before):
        if process is not None and pid == process.pid:
            continue  # only Popen reaps its own child
        try:
            os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            continue  # not orphaned yet, so its parent still owns it
    # Reaping parents can expose newly adopted descendants that the first scan missed.
    return _descendants(before)


def _group_alive(process: subprocess.Popen) -> bool:
    try:
        os.killpg(process.pid, 0)
    except ProcessLookupError:
        return False
    return True


def _signal_group(process: subprocess.Popen, signum: int) -> None:
    try:
        os.killpg(process.pid, signum)
    except ProcessLookupError:
        pass


def _finish_children(process: subprocess.Popen | None, before: set[int]) -> bool:
    """Stop and reap owned descendants before release, even if Popen raised before returning.

    On Linux the subreaper keeps detached descendants under this process. Other
    platforms retain group-only cleanup. No unrelated child may be launched during
    run_in_slot; existing children and their trees are excluded by the initial scan.
    """
    until = time.monotonic() + 10
    killed, group_signal, sent = False, None, {}
    while True:
        owned = _owned_descendants(process, before)
        group_alive = process is not None and _group_alive(process)
        if not owned and not group_alive:
            break
        killed = True
        signum = signal.SIGTERM if time.monotonic() < until else signal.SIGKILL
        if group_alive and group_signal != signum:
            _signal_group(process, signum)
            group_signal = signum
        for pid in owned:
            if sent.get(pid) != signum:
                try:
                    os.kill(pid, signum)
                except ProcessLookupError:
                    pass
                sent[pid] = signum
        time.sleep(0.05)
    if process is not None:
        process.wait()  # already exited: collects its status
    return killed


@contextmanager
def _deferred_signals() -> Iterator[list[int]]:
    """Record SIGINT and SIGTERM instead of acting on them, from before a child exists until every one is gone.

    Python runs handlers on the main thread whichever thread a signal reaches, so recording handlers (not a
    thread's signal mask) cover sibling threads. A signal while the handlers are being installed arrives before
    any child exists and acts at once. On exit the previous handlers return and the first deferred signal goes
    to the handler that was in place.
    """
    pending: list[int] = []
    previous = {}
    try:
        for sig in (signal.SIGINT, signal.SIGTERM):
            previous[sig] = signal.getsignal(sig)  # saved first, so an interrupted installation is undone too
            signal.signal(sig, lambda signum, frame: pending.append(signum))
        yield pending
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
        if pending:
            # Delivered even while another exception unwinds: a stop request is never lost.
            _deliver(pending[0], previous[pending[0]])


def _deliver(signum: int, handler) -> None:
    """Hand a signal deferred during cleanup to the handler that was in place."""
    if callable(handler):
        handler(signum, None)
    elif handler == signal.SIG_DFL:
        if signum == signal.SIGINT:
            raise KeyboardInterrupt
        raise SystemExit(128 + signum)


@contextmanager
def _termination_handler() -> Iterator[None]:
    """Default SIGTERM must unwind through cleanup; retain a caller's own handler."""
    previous = signal.getsignal(signal.SIGTERM)

    def terminate(signum, frame):
        raise SystemExit(128 + signum)

    try:
        if previous == signal.SIG_DFL:
            signal.signal(signal.SIGTERM, terminate)
        yield
    finally:
        if previous == signal.SIG_DFL:
            signal.signal(signal.SIGTERM, previous)


def run_in_slot(
    cmd: list[str], holder: str, wait: float | None,
    deadline: float | None = None, env: dict | None = None,
) -> tuple[bool, int | None]:
    """Run a process group in the slot, cleaning up every child before release.

deadline is an absolute monotonic time. A forced shutdown returns (True, None);
exceptions, including KeyboardInterrupt, propagate after cleanup. Call this only
from the main thread, without concurrent unrelated child launches: child adoption
and cleanup signal handlers are process-wide. Detached descendants are contained
on Linux; other platforms clean up the helper's process group only.
"""
    with _termination_handler(), browser_slot(holder, wait) as acquired:
        if not acquired:
            return False, None
        child_env = dict(os.environ if env is None else env)
        child_env["AISI_BROWSER_LOCK_HELD"] = str(lock_path())
        with _reap_descendants(), _deferred_signals() as pending:
            before = _descendants()
            process = None
            killed = False
            try:
                process = subprocess.Popen(cmd, env=child_env, start_new_session=True)
                while True:
                    try:
                        process.wait(timeout=0.05)
                        break
                    except subprocess.TimeoutExpired:
                        pass
                    if pending or (deadline is not None and time.monotonic() >= deadline):
                        killed = True
                        break
            finally:
                killed = _finish_children(process, before) or killed
        return True, None if killed else process.returncode
