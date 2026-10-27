"""Shared "wait until selected tasks finish" logic for ``tq wait`` and MCP ``wait_for_task``.

One definition of terminal statuses, one selector resolver, one polling loop.
Reads go straight to SQLite (WAL) like the other read paths; no writes happen here.
"""
from __future__ import annotations
from taskqueue.jsonutil import json_list

import sqlite3
import time
from collections.abc import Callable, Iterable
from typing import Any

# Finished: the task will not change again without an explicit retry.
from taskqueue._db_tasks import get_task_status
from taskqueue.statuses import TERMINAL_STATUSES  # single source of truth
# Will not run until someone enables it; waiting on it would hang forever.
STALLED_STATUSES = frozenset({"disabled"})
SETTLED_STATUSES = TERMINAL_STATUSES | STALLED_STATUSES

# Exit codes for ``tq wait`` (documented in docs/errors.md).
EXIT_OK = 0
EXIT_FAILED = 1
EXIT_TIMEOUT = 2
EXIT_NOT_FOUND = 3

_SELECT = (
    "SELECT t.project, t.task_id, t.status, t.enabled, t.last_error_code, t.block_reason, "
    "r.exit_code, r.wall_sec, r.stop_reason, r.progress_pct, r.last_log_line "
    "FROM tasks t LEFT JOIN runs r ON r.run_id = t.last_run_id "
)


def split_key(key: str) -> tuple[str, str]:
    project, sep, task_id = key.partition("/")
    if not sep or not project or not task_id:
        raise ValueError(f"expected 'project/task_id', got: {key!r}")
    return project, task_id


def resolve_selectors(
    conn: sqlite3.Connection,
    *,
    keys: Iterable[str] = (),
    project: str | None = None,
    tags: Iterable[str] = (),
    suite: str | None = None,
    group: str | None = None,
    started_by: str | None = None,
    downstream_of: str | None = None,
    include_finished: bool = False,
) -> list[str]:
    """Return ``project/task_id`` keys to wait on.

    Explicit ``keys`` are always kept (even if missing, so they can be reported
    as not found). Filter selectors only add tasks that have not settled yet,
    unless ``include_finished`` is set -- otherwise old completed rows would
    satisfy ``--any`` immediately.
    """
    out: list[str] = []
    seen: set[str] = set()

    def add(k: str) -> None:
        if k not in seen:
            seen.add(k)
            out.append(k)

    for k in keys:
        split_key(k)
        add(k)

    clauses: list[str] = []
    params: list[Any] = []
    if project:
        clauses.append("project = ?")
        params.append(project)
    if suite:
        clauses.append("suite = ?")
        params.append(suite)
    if group:
        clauses.append("grp = ?")
        params.append(group)
    if started_by:
        clauses.append("started_by = ?")
        params.append(started_by)
    tags = [t for t in tags if t]
    if clauses or tags:
        rows = conn.execute(
            "SELECT project, task_id, status, tags FROM tasks"
            + (" WHERE " + " AND ".join(clauses) if clauses else "")
            + " ORDER BY project, task_id",
            params,
        ).fetchall()

        for r in rows:
            if tags:
                have = set(json_list(r["tags"]))
                if not all(t in have for t in tags):
                    continue
            if not include_finished and r["status"] in SETTLED_STATUSES:
                continue
            add(f"{r['project']}/{r['task_id']}")

    if downstream_of:
        from taskqueue._db_tasks import collect_downstream_task_ids

        proj, tid = split_key(downstream_of)
        for did in collect_downstream_task_ids(conn, proj, tid):
            k = f"{proj}/{did}"
            if include_finished:
                add(k)
                continue
            row = get_task_status(conn, proj, did)
            if row is not None and row["status"] not in SETTLED_STATUSES:
                add(k)
    return out


def next_actions(status: str | None, error_code: str | None = None) -> list[str]:
    """Suggested follow-up tool/CLI calls for a settled task (hints, not commands)."""
    if status == "completed":
        return ["task_result"]
    if status in ("failed", "interrupted", "stopped", "unknown"):
        return ["diagnose", "log_tail", "retry_task (after fixing the cause)"]
    if status == "suspended":
        return ["retry_task (resumes from checkpoint)"]
    if status == "disabled":
        return ["patch_task enabled=true (task will not run while disabled)"]
    return []


HINT_TIMEOUT_SEC = 3600
_HINT_MAX_KEYS = 4


def wait_command(keys: Iterable[str] = (), *, project: str | None = None) -> str:
    """``tq wait …`` line that blocks until ``keys`` (or ``project``'s pending tasks) settle."""
    keys = list(keys)
    if len(keys) > _HINT_MAX_KEYS and project:
        target = f"--project {project}"
    elif keys:
        target = " ".join(keys)
    elif project:
        target = f"--project {project}"
    else:
        target = "<project>/<task_id>"
    return f"tq wait {target} --timeout {HINT_TIMEOUT_SEC}"


def wait_hint(keys: Iterable[str] = (), *, project: str | None = None) -> str:
    """One-line next step printed after a task is created or re-queued."""
    return (
        f"Next: {wait_command(keys, project=project)}  "
        "(agents: run it in the background; it wakes you when the task(s) settle)"
    )


def print_wait_hint(keys: Iterable[str] = (), *, project: str | None = None) -> None:
    """Print :func:`wait_hint` to stderr (keeps stdout parseable); ``TQ_NO_HINTS=1`` silences it."""
    import os
    import sys

    if os.environ.get("TQ_NO_HINTS", "").strip() in ("", "0"):
        print(wait_hint(keys, project=project), file=sys.stderr)


def snapshot_one(conn: sqlite3.Connection, key: str) -> dict[str, Any] | None:
    project, task_id = split_key(key)
    r = conn.execute(_SELECT + "WHERE t.project = ? AND t.task_id = ?", (project, task_id)).fetchone()
    if r is None:
        return None
    status = r["status"]
    last = r["last_log_line"]
    from taskqueue._db_tasks import run_summary
    return {
        **run_summary(conn, project, task_id),
        "key": key,
        "project": project,
        "task_id": task_id,
        "status": status,
        "finished": status in TERMINAL_STATUSES,
        "settled": status in SETTLED_STATUSES,
        "exit_code": r["exit_code"],
        "last_error_code": r["last_error_code"],
        "block_reason": r["block_reason"],
        "stop_reason": r["stop_reason"],
        "wall_sec": r["wall_sec"],
        "progress_pct": r["progress_pct"],
        "last_lines": [last] if last else [],
        "next_actions": next_actions(status, r["last_error_code"]),
    }


def wait_for(
    open_conn: Callable[[], sqlite3.Connection],
    keys: list[str],
    *,
    mode: str = "all",
    timeout_sec: float | None = None,
    poll_sec: float = 5.0,
    on_tick: Callable[[dict[str, Any]], None] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    """Block until ``all``/``any`` of ``keys`` settle, or ``timeout_sec`` elapses.

    ``timeout_sec`` None or <= 0 means wait forever. Polling starts at 1 s and
    backs off to ``poll_sec``. Returns a plain dict (JSON-serialisable).
    """
    if mode not in ("all", "any"):
        raise ValueError("mode must be 'all' or 'any'")
    started = clock()
    deadline = started + timeout_sec if timeout_sec and timeout_sec > 0 else None
    interval = min(1.0, poll_sec) if poll_sec > 0 else 1.0
    cap = max(poll_sec, interval)
    conn = open_conn()
    try:
        while True:
            tasks: list[dict[str, Any]] = []
            not_found: list[str] = []
            for k in keys:
                snap = snapshot_one(conn, k)
                if snap is None:
                    not_found.append(k)
                else:
                    tasks.append(snap)
            settled = [t for t in tasks if t["settled"]]
            pending = [t["key"] for t in tasks if not t["settled"]]
            if not keys:
                done = True
            elif not_found and not tasks:
                done = True
            elif mode == "any":
                done = bool(settled)
            else:
                done = not pending
            now = clock()
            timed_out = False
            if not done and deadline is not None and now >= deadline:
                timed_out = True
            if done or timed_out:
                result = {
                    "ok": not not_found and not timed_out,
                    "done": done,
                    "timed_out": timed_out,
                    "mode": mode,
                    "waited_sec": round(now - started, 1),
                    "tasks": tasks,
                    "pending": pending,
                    "not_found": not_found,
                }
                if timed_out:
                    result["hint"] = "still running; call wait again"
                return result
            if on_tick is not None:
                on_tick({"pending": pending, "waited_sec": round(now - started, 1)})
            delay = interval
            if deadline is not None:
                delay = min(delay, max(0.05, deadline - now))
            sleep(delay)
            interval = min(cap, interval * 1.5)
    finally:
        conn.close()


def exit_code_for(result: dict[str, Any]) -> int:
    """Map a ``wait_for`` result to a ``tq wait`` exit code."""
    if result["not_found"] and not result["tasks"]:
        return EXIT_NOT_FOUND
    if result["timed_out"]:
        return EXIT_TIMEOUT
    if result["not_found"]:
        return EXIT_NOT_FOUND
    settled = [t for t in result["tasks"] if t["settled"]]
    if any(t["status"] != "completed" for t in settled):
        return EXIT_FAILED
    return EXIT_OK
