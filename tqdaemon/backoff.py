"""Retry backoff (YAML ``retry_backoff_sec``): space out re-admissions of a task that keeps failing.

After the n-th consecutive failed run the task is not admitted until
``finished_at + base * 2**(n-1)`` (capped at a day, plus a stable 0-20 % jitter so a batch of tasks that
failed together does not retry in lock step). n counts the failed runs at the end of the run history,
but never more than the failures since the last explicit ``tq retry`` (which resets max_attempts too),
so an operator's retry starts the task immediately. A forced start bypasses it.
"""

from __future__ import annotations

import hashlib
import sqlite3
import time
from datetime import datetime

MAX_DELAY_SEC = 86400.0
_TAIL_RUNS = 16


def backoff_delay(base_sec: float, failures: int, key: str = "") -> float:
    """Seconds to wait after the latest failed run (0 when there is nothing to back off from)."""
    if base_sec <= 0 or failures <= 0:
        return 0.0
    delay = min(MAX_DELAY_SEC, float(base_sec) * 2 ** min(failures - 1, 30))
    jitter = int(hashlib.sha256(f"{key}:{failures}".encode()).hexdigest()[:8], 16) / 0xFFFFFFFF * 0.2
    return delay * (1.0 + jitter)


def _ts(value: object) -> float | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


def backoff_left(conn: sqlite3.Connection, row, *, now: float | None = None) -> tuple[float, int]:
    """(seconds still to wait, consecutive failures) for a queued task row; (0, 0) when not backing off."""
    from tqdaemon.db import failed_attempt_count
    from tqdaemon.rows import row_get

    base = float(row_get(row, "retry_backoff_sec", 0) or 0)
    if base <= 0:
        return 0.0, 0
    project, task_id = str(row["project"]), str(row["task_id"])
    runs = conn.execute(
        "SELECT status, finished_at FROM runs WHERE project = ? AND task_id = ? AND finished_at IS NOT NULL "
        "ORDER BY run_id DESC LIMIT ?",
        (project, task_id, _TAIL_RUNS),
    ).fetchall()
    tail = 0
    for r in runs:
        if r["status"] != "failed":
            break
        tail += 1
    if tail == 0:
        return 0.0, 0
    since_retry = failed_attempt_count(
        conn, project, task_id, baseline=int(row_get(row, "retry_attempt_baseline", 0) or 0)
    )
    failures = min(tail, since_retry)
    finished = _ts(runs[0]["finished_at"])
    if failures == 0 or finished is None:
        return 0.0, 0
    now = time.time() if now is None else now
    return max(0.0, finished + backoff_delay(base, failures, f"{project}/{task_id}") - now), failures


def backoff_left_sec(conn: sqlite3.Connection, row) -> float:
    """Seconds still to wait (FilterContext.backoff_left_sec)."""
    return backoff_left(conn, row)[0]


def backoff_left_bulk(
    conn: sqlite3.Connection,
    rows: list,
    *,
    now: float | None = None,
) -> dict[tuple[str, str], float]:
    """F-247: one prefetch of recent runs for the admit window; map key → seconds left.

    Rows with ``retry_backoff_sec <= 0`` are omitted (caller treats missing as 0).
    """
    from tqdaemon._db_tasks import failed_attempt_counts_bulk
    from tqdaemon.rows import row_get

    now = time.time() if now is None else now
    need: list[tuple[str, str]] = []
    bases: dict[tuple[str, str], float] = {}
    baselines: dict[tuple[str, str], int] = {}
    for row in rows:
        base = float(row_get(row, "retry_backoff_sec", 0) or 0)
        if base <= 0:
            continue
        key = (str(row["project"]), str(row["task_id"]))
        need.append(key)
        bases[key] = base
        baselines[key] = int(row_get(row, "retry_attempt_baseline", 0) or 0)
    if not need:
        return {}

    # Fetch finished runs for the window in one scan, then group in Python.
    flat_args: list[object] = []
    for p, t in need:
        flat_args.extend((p, t))
    # Chunked OR is fine for admit window ≤256.
    clauses = " OR ".join(["(project = ? AND task_id = ?)"] * len(need))
    run_rows = conn.execute(
        f"SELECT project, task_id, status, finished_at, run_id FROM runs "
        f"WHERE finished_at IS NOT NULL AND ({clauses}) "
        f"ORDER BY run_id DESC",
        flat_args,
    ).fetchall()
    tails: dict[tuple[str, str], list] = {k: [] for k in need}
    for r in run_rows:
        key = (str(r["project"]), str(r["task_id"]))
        bucket = tails.get(key)
        if bucket is None or len(bucket) >= _TAIL_RUNS:
            continue
        bucket.append(r)

    since_retry = failed_attempt_counts_bulk(conn, need, baselines=baselines)
    out: dict[tuple[str, str], float] = {}
    for key in need:
        runs = tails.get(key) or []
        tail = 0
        for r in runs:
            if r["status"] != "failed":
                break
            tail += 1
        if tail == 0:
            continue
        failures = min(tail, int(since_retry.get(key, 0)))
        finished = _ts(runs[0]["finished_at"]) if runs else None
        if failures == 0 or finished is None:
            continue
        left = max(
            0.0,
            finished + backoff_delay(bases[key], failures, f"{key[0]}/{key[1]}") - now,
        )
        if left > 0:
            out[key] = left
    return out
