"""Terminate orphaned local task process trees (dirty daemon restart)."""

from __future__ import annotations

import time
from typing import Literal

import psutil

ReapResult = Literal["killed", "gone", "skipped_reuse", "denied"]


def kill_process_tree(
    pid: int,
    *,
    create_time: float | None = None,
    graceful_sec: float = 5.0,
) -> ReapResult:
    """SIGTERM (then SIGKILL) ``pid`` and descendants.

    When ``create_time`` is set, refuse to kill if the live process's
    ``create_time`` differs (PID reuse guard).
    """
    try:
        parent = psutil.Process(pid)
    except psutil.NoSuchProcess:
        return "gone"
    except psutil.AccessDenied:
        return "denied"

    if create_time is not None:
        try:
            live_ct = parent.create_time()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return "gone"
        # psutil create_time is float seconds; allow tiny float slack.
        if abs(live_ct - create_time) > 0.05:
            return "skipped_reuse"

    try:
        children = parent.children(recursive=True)
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        children = []
    procs = [*children, parent]

    for p in procs:
        try:
            p.terminate()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass

    deadline = time.monotonic() + max(0.0, graceful_sec)
    alive: list[psutil.Process] = []
    for p in procs:
        try:
            remaining = deadline - time.monotonic()
            if remaining > 0:
                p.wait(timeout=remaining)
            elif p.is_running():
                alive.append(p)
        except psutil.TimeoutExpired:
            alive.append(p)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass

    for p in alive:
        try:
            p.kill()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
        try:
            p.wait(timeout=2)
        except (psutil.TimeoutExpired, psutil.NoSuchProcess, psutil.AccessDenied):
            pass

    return "killed"


def terminate_tree(proc, *, graceful_sec: float = 10.0, background: bool = False) -> None:
    """Stop ``proc`` and its descendants (SIGTERM, then SIGKILL after ``graceful_sec``).

    ``Popen.terminate()`` only signals the direct child and leaves grandchildren
    running. With ``background=True`` the escalation runs on a daemon thread so
    callers on a heartbeat/watchdog loop do not block.
    """
    import threading

    pid = getattr(proc, "pid", None)
    if not pid:
        return

    def _run() -> None:
        try:
            kill_process_tree(pid, graceful_sec=graceful_sec)
        except Exception:
            try:
                proc.terminate()
            except Exception:
                pass

    if background:
        threading.Thread(target=_run, name="tq-terminate-tree", daemon=True).start()
    else:
        _run()
