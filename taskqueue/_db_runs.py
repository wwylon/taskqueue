"""Run-lifecycle and worker-ops CRUD — extracted from db.py.

All public names are re-exported via ``from taskqueue._db_runs import *``
in ``db.py``, so callers continue to use ``from taskqueue.db import X``.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from typing import Any

from taskqueue._db_tasks import get_task_status
from taskqueue._time import _now  # noqa: F401  # canonical ISO timestamp helper


# --- run records ---

def insert_run(
    conn: sqlite3.Connection,
    project: str,
    task_id: str,
    log_path: str,
    worker_id: str | None = None,
    *,
    commit: bool = True,
) -> int:
    cur = conn.execute(
        "INSERT INTO runs (project, task_id, started_at, status, log_path, worker_id) "
        "VALUES (?, ?, ?, 'running', ?, ?)",
        (project, task_id, _now(), log_path, worker_id),
    )
    run_id = cur.lastrowid or 0
    conn.execute(
        "UPDATE tasks SET last_run_id = ? WHERE project = ? AND task_id = ?",
        (run_id, project, task_id),
    )
    if commit:
        conn.commit()
    return run_id


def set_run_pid(
    conn: sqlite3.Connection,
    run_id: int,
    pid: int,
    *,
    create_time: float | None = None,
) -> None:
    """Record OS pid (+ create_time) for local orphan reaping after a dirty restart."""
    conn.execute(
        "UPDATE runs SET pid = ?, pid_create_time = ? WHERE run_id = ?",
        (pid, create_time, run_id),
    )
    conn.commit()


def update_run_progress(
    conn: sqlite3.Connection,
    run_id: int,
    progress_pct: float | None,
    last_log_line: str | None,
    peak_ram_gib: float | None = None,
    peak_vram_gib: float | None = None,
    peak_cpu_pct: float | None = None,
) -> None:
    conn.execute(
        "UPDATE runs SET progress_pct = ?, last_log_line = ?, peak_ram_gib = ?, peak_vram_gib = ?, peak_cpu_pct = ? WHERE run_id = ?",
        (progress_pct, last_log_line, peak_ram_gib, peak_vram_gib, peak_cpu_pct, run_id),
    )
    conn.commit()


def update_run_metrics(conn: sqlite3.Connection, run_id: int, metrics: dict) -> None:
    """R8: Persist latest TQ_METRIC values for a running task."""
    conn.execute(
        "UPDATE runs SET metrics_json = ? WHERE run_id = ?",
        (json.dumps(metrics, ensure_ascii=False), run_id),
    )
    conn.commit()


def set_run_stop_reason(conn: sqlite3.Connection, run_id: int, reason: str) -> None:
    """R9: Record the human-supplied stop reason for a run."""
    conn.execute("UPDATE runs SET stop_reason = ? WHERE run_id = ?", (reason, run_id))
    conn.commit()


def finish_run(
    conn: sqlite3.Connection,
    run_id: int,
    exit_code: int,
    status: str,
    *,
    last_error_code: str | None = None,
    peak_ram_gib: float | None = None,
    peak_vram_gib: float | None = None,
    peak_cpu_pct: float | None = None,
    only_if_running: bool = False,
    stopped_by: str | None = None,
    stop_reason: str | None = None,
    commit: bool = True,
) -> bool:
    """Finalize a run. If only_if_running, no-op when already terminal. Returns whether updated."""
    from taskqueue.errors import classify_error
    error_category = classify_error(last_error_code, exit_code, stopped_by)
    # G3: compute wall_sec from started_at in-database to avoid Python datetime parsing.
    wall_sec_expr = (
        "CAST(ROUND((julianday(?) - julianday(started_at)) * 86400) AS INTEGER)"
    )
    if peak_ram_gib is not None or peak_vram_gib is not None or peak_cpu_pct is not None:
        sql = (
            f"UPDATE runs SET finished_at = ?, wall_sec = {wall_sec_expr}, "
            "exit_code = ?, status = ?, last_error_code = ?, "
            "lease_expires_at = NULL, peak_ram_gib = COALESCE(?, peak_ram_gib), "
            "peak_vram_gib = COALESCE(?, peak_vram_gib), peak_cpu_pct = COALESCE(?, peak_cpu_pct), "
            "stopped_by = COALESCE(?, stopped_by), stop_reason = COALESCE(stop_reason, ?), "
            "error_category = ? WHERE run_id = ?"
        )
        now = _now()
        params: tuple = (
            now, now, exit_code, status, last_error_code, peak_ram_gib, peak_vram_gib, peak_cpu_pct,
            stopped_by, stop_reason, error_category, run_id,
        )
    else:
        sql = (
            f"UPDATE runs SET finished_at = ?, wall_sec = {wall_sec_expr}, "
            "exit_code = ?, status = ?, last_error_code = ?, "
            "lease_expires_at = NULL, stopped_by = COALESCE(?, stopped_by), "
            "stop_reason = COALESCE(stop_reason, ?), error_category = ? WHERE run_id = ?"
        )
        now = _now()
        params = (
            now, now, exit_code, status, last_error_code, stopped_by, stop_reason,
            error_category, run_id,
        )
    if only_if_running:
        sql += " AND status = 'running'"
    cur = conn.execute(sql, params)
    if commit:
        conn.commit()
    return (cur.rowcount or 0) > 0


def _update_predicted_duration(
    conn: sqlite3.Connection,
    project: str,
    task_id: str,
) -> None:
    """Recompute predicted_duration_sec as the median of the last 10 wall_sec values."""
    rows = conn.execute(
        "SELECT wall_sec FROM runs WHERE project = ? AND task_id = ? "
        "AND status = 'completed' AND wall_sec IS NOT NULL ORDER BY run_id DESC LIMIT 10",
        (project, task_id),
    ).fetchall()
    if not rows:
        return
    vals = sorted(r["wall_sec"] for r in rows)
    p50 = vals[len(vals) // 2]
    conn.execute(
        "UPDATE tasks SET predicted_duration_sec = ? WHERE project = ? AND task_id = ?",
        (float(p50), project, task_id),
    )


def finalize_run_and_task(
    conn: sqlite3.Connection,
    run_id: int,
    project: str,
    task_id: str,
    exit_code: int,
    status: str,
    *,
    last_error_code: str | None = None,
    peak_ram_gib: float | None = None,
    peak_vram_gib: float | None = None,
    peak_cpu_pct: float | None = None,
    stopped_by: str | None = None,
    stop_reason: str | None = None,
    only_if_running: bool = True,
    clear_task_error: bool = False,
) -> bool:
    """Atomically finalize a run and its task row. Returns False if run was not running.

    ``clear_task_error`` (successful runs) also resets last_error_code / block_reason / the
    cap auto-retry counter in the same transaction; otherwise ``last_error_code`` is stamped on the task.
    """
    from taskqueue._db_tasks import _TERMINAL_STATUSES

    if conn.in_transaction:
        conn.rollback()
    conn.execute("BEGIN IMMEDIATE")
    try:
        updated = finish_run(
            conn,
            run_id,
            exit_code,
            status,
            last_error_code=last_error_code,
            peak_ram_gib=peak_ram_gib,
            peak_vram_gib=peak_vram_gib,
            peak_cpu_pct=peak_cpu_pct,
            only_if_running=only_if_running,
            stopped_by=stopped_by,
            stop_reason=stop_reason,
            commit=False,
        )
        if not updated:
            conn.rollback()
            return False
        now = _now()
        if status in _TERMINAL_STATUSES:
            cur = conn.execute(
                "UPDATE tasks SET status = ?, updated_at = ?, finished_at = ? "
                "WHERE project = ? AND task_id = ? AND status = 'running'",
                (status, now, now, project, task_id),
            )
        else:
            cur = conn.execute(
                "UPDATE tasks SET status = ?, updated_at = ? "
                "WHERE project = ? AND task_id = ? AND status = 'running'",
                (status, now, project, task_id),
            )
        if (cur.rowcount or 0) == 0:
            conn.rollback()
            return False
        if clear_task_error:
            conn.execute(
                "UPDATE tasks SET last_error_code = NULL, block_reason = NULL, cap_auto_retry_count = 0 "
                "WHERE project = ? AND task_id = ?",
                (project, task_id),
            )
        elif last_error_code:
            conn.execute(
                "UPDATE tasks SET last_error_code = ? WHERE project = ? AND task_id = ?",
                (last_error_code, project, task_id),
            )
        # G3: update predicted_duration_sec (p50 of last 10 wall_sec for this task).
        if status in _TERMINAL_STATUSES:
            _update_predicted_duration(conn, project, task_id)
        conn.commit()
        return True
    except Exception:
        conn.rollback()
        raise


def lease_expiry_iso(ttl_sec: int | None = None) -> str:
    from datetime import timedelta
    from taskqueue.config import LEASE_TTL_SEC

    sec = LEASE_TTL_SEC if ttl_sec is None else ttl_sec
    return (datetime.now(UTC) + timedelta(seconds=sec)).isoformat()


def renew_run_lease(conn: sqlite3.Connection, run_id: int, ttl_sec: int | None = None) -> None:
    conn.execute(
        "UPDATE runs SET lease_expires_at = ? WHERE run_id = ? AND status = 'running'",
        (lease_expiry_iso(ttl_sec), run_id),
    )
    conn.commit()


def renew_run_lease_for_worker(
    conn: sqlite3.Connection,
    run_id: int,
    worker_id: str,
    ttl_sec: int | None = None,
) -> bool:
    """Renew lease only if the run is still running and owned by worker_id."""
    cur = conn.execute(
        "UPDATE runs SET lease_expires_at = ? "
        "WHERE run_id = ? AND status = 'running' AND worker_id = ?",
        (lease_expiry_iso(ttl_sec), run_id, worker_id),
    )
    conn.commit()
    return (cur.rowcount or 0) > 0


def get_run_owned(
    conn: sqlite3.Connection, run_id: int, worker_id: str
) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM runs WHERE run_id = ? AND worker_id = ?",
        (run_id, worker_id),
    ).fetchone()


def worker_health(last_seen_iso: str | None, *, heartbeat_sec: float = 30, offline_sec: int | None = None) -> str:
    """Three-state liveness classification for display/diagnosis.

    Distinct from the coarser stored `workers.status` (online/offline, set
    by `mark_stale_workers_offline` and used for dispatch gating).
    'unreachable' means a heartbeat or two was missed but the worker isn't
    past the stored-offline threshold yet -- a plausible network blip, not
    a confirmed-dead worker. Not stored anywhere; computed fresh from
    `last_seen` whenever asked.
    """
    from taskqueue.config import WORKER_OFFLINE_SEC

    offline_sec = WORKER_OFFLINE_SEC if offline_sec is None else offline_sec
    if not last_seen_iso:
        return "offline"
    try:
        last_seen = datetime.fromisoformat(last_seen_iso)
    except ValueError:
        return "offline"
    if last_seen.tzinfo is None:
        last_seen = last_seen.replace(tzinfo=UTC)
    age = (datetime.now(UTC) - last_seen).total_seconds()
    if age <= heartbeat_sec * 2:
        return "online"
    if age <= offline_sec:
        return "unreachable"
    return "offline"


def reclaim_expired_leases(conn: sqlite3.Connection, *, heartbeat_sec: float = 30) -> list[tuple[str, str]]:
    """Interrupt runs whose lease expired; apply execution_semantics. Returns (project, task_id).

    Timing/safety is unchanged: a run is still reclaimed purely on lease expiry
    (LEASE_TTL_SEC, independent of worker heartbeat). Labeling: if the worker is
    still online by heartbeat at reclaim time, tag
    ``tq.infra.lease_expired_worker_alive``; otherwise ``tq.infra.worker_lost``.

    R-011: ``idempotent`` / ``checkpointable`` → re-queue; ``non_idempotent`` /
    ``at_most_once`` → ``unknown`` (operator decides; never auto-restart).
    """
    from taskqueue import errors as tqerr
    from taskqueue.fencing import (
        lost_attempt_task_status,
        resolve_execution_semantics,
    )

    now = _now()
    rows = conn.execute(
        "SELECT run_id, project, task_id, worker_id FROM runs "
        "WHERE status = 'running' AND worker_id IS NOT NULL "
        "AND lease_expires_at IS NOT NULL AND lease_expires_at < ?",
        (now,),
    ).fetchall()
    reclaimed: list[tuple[str, str]] = []
    for row in rows:
        worker_row = conn.execute(
            "SELECT last_seen FROM workers WHERE worker_id = ?", (row["worker_id"],)
        ).fetchone()
        last_seen = worker_row["last_seen"] if worker_row else None
        code = (
            tqerr.INFRA_LEASE_EXPIRED_WORKER_ALIVE
            if worker_health(last_seen, heartbeat_sec=heartbeat_sec) == "online"
            else tqerr.INFRA_WORKER_LOST
        )
        cur = conn.execute(
            "UPDATE runs SET status = 'interrupted', finished_at = ?, exit_code = -1, "
            "wall_sec = CAST(ROUND((julianday(?) - julianday(started_at)) * 86400) AS INTEGER), "
            "last_error_code = ?, error_category = ?, lease_expires_at = NULL, "
            "stopped_by = 'lease-timeout' "
            "WHERE run_id = ? AND status = 'running' "
            "AND lease_expires_at IS NOT NULL AND lease_expires_at < ?",
            (now, now, code, tqerr.classify_error(code, -1, "lease-timeout"),
             row["run_id"], now),
        )
        if (cur.rowcount or 0) == 0:
            continue
        task_row = conn.execute(
            "SELECT idempotent, execution_semantics FROM tasks "
            "WHERE project = ? AND task_id = ?",
            (row["project"], row["task_id"]),
        ).fetchone()
        raw_sem = ""
        idem = False
        if task_row is not None:
            keys = task_row.keys()
            if "execution_semantics" in keys:
                raw_sem = task_row["execution_semantics"] or ""
            idem = bool(int(task_row["idempotent"] or 0)) if "idempotent" in keys else False
        semantics = resolve_execution_semantics(raw_sem, idempotent=idem)
        new_status = lost_attempt_task_status(semantics)
        conn.execute(
            # W0.7: also clear stop_requested so a re-queued task doesn't
            # immediately stop again (permanent flap when lease expires under stop).
            # F-144: clear stale block_reason so UI does not show a pre-lease capacity
            # explanation after the task is requeued from lease loss.
            "UPDATE tasks SET status = ?, stop_requested = 0, last_error_code = ?, "
            "block_reason = NULL, updated_at = ? "
            "WHERE project = ? AND task_id = ? AND status = 'running'",
            (new_status, code, now, row["project"], row["task_id"]),
        )
        reclaimed.append((row["project"], row["task_id"]))
    if reclaimed:
        conn.commit()
    return reclaimed


def mark_stale_workers_offline(conn: sqlite3.Connection, offline_sec: int | None = None) -> int:
    from datetime import timedelta
    from taskqueue.config import WORKER_OFFLINE_SEC

    sec = WORKER_OFFLINE_SEC if offline_sec is None else offline_sec
    cutoff = (datetime.now(UTC) - timedelta(seconds=sec)).isoformat()
    # Skip the write lock when no online workers have missed their deadline.
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM workers WHERE status = 'online' AND last_seen < ?",
        (cutoff,),
    ).fetchone()
    if not row or int(row["n"]) == 0:
        return 0
    cur = conn.execute(
        "UPDATE workers SET status = 'offline' "
        "WHERE status = 'online' AND last_seen < ?",
        (cutoff,),
    )
    conn.commit()
    return cur.rowcount or 0


def force_task(conn: sqlite3.Connection, project: str, task_id: str) -> str | None:
    """Mark task to bypass resource limits. Returns error string, or None on success."""
    task = get_task_status(conn, project, task_id)
    if not task:
        return "Task not found."
    status = task["status"] if hasattr(task, "keys") else task[0]
    if status == "running":
        return "Cannot force task because it is currently running. Stop it first."
    # Clear stop_requested so an earlier stop (esp. offline) cannot immediately
    # re-stop the task after force requeues it.
    cur = conn.execute(
        "UPDATE tasks SET status = ?, force_run = 1, enabled = 1, stop_requested = 0 "
        "WHERE project = ? AND task_id = ? AND status != 'running'",
        ("queued", project, task_id),
    )
    conn.commit()
    if (cur.rowcount or 0) == 0:
        return "Cannot force task because it is currently running. Stop it first."
    try:
        from taskqueue.sched_wake import wake_scheduler
        wake_scheduler()
    except Exception:
        pass
    return None


def remove_task(conn: sqlite3.Connection, project: str, task_id: str) -> str | None:
    task = get_task_status(conn, project, task_id)
    if not task:
        return "Task not found."
    # Tolerate Row or plain tuple (cli callers sometimes omit row_factory).
    status = task["status"] if hasattr(task, "keys") else task[0]
    if status == "running":
        return "Cannot remove task because it is currently running. Stop it first."
    # Guarded delete: a task claimed between the check above and here is kept.
    cur = conn.execute(
        "DELETE FROM tasks WHERE project = ? AND task_id = ? AND status != 'running'",
        (project, task_id),
    )
    if cur.rowcount == 0:
        conn.rollback()
        return "Cannot remove task because it is currently running. Stop it first."
    conn.execute(
        "DELETE FROM runs WHERE project = ? AND task_id = ?",
        (project, task_id),
    )
    conn.commit()
    return None


def _runs_filter_sql(
    *,
    project: str | None = None,
    search: str = "",
    exclude_projects: list[str] | None = None,
    started_by: str | None = None,
) -> tuple[str, list[Any]]:
    """Build WHERE clause + params for recent-runs queries (project + text search)."""
    where: list[str] = []
    params: list[Any] = []
    if project:
        where.append("project = ?")
        params.append(project)
    if exclude_projects:
        where.append(f"project NOT IN ({','.join('?' * len(exclude_projects))})")
        params.extend(exclude_projects)
    if started_by:
        where.append(
            "EXISTS (SELECT 1 FROM tasks _sb WHERE _sb.project = runs.project "
            "AND _sb.task_id = runs.task_id AND _sb.started_by = ?)"
        )
        params.append(started_by)
    raw = (search or "").strip()
    exact_run = raw.startswith("#")
    q = raw[1:].strip() if exact_run else raw
    if q:
        if exact_run or q.isdigit():
            where.append("CAST(run_id AS TEXT) = ?")
            params.append(q)
        else:
            pat = f"%{q}%"
            where.append(
                "(task_id LIKE ? OR project LIKE ? OR CAST(run_id AS TEXT) LIKE ?"
                " OR IFNULL(error_category, '') LIKE ? OR IFNULL(worker_id, '') LIKE ?)"
            )
            params.extend([pat, pat, pat, pat, pat])
    if not where:
        return "", []
    return "WHERE " + " AND ".join(where), params


def get_recent_runs(
    conn: sqlite3.Connection,
    limit: int = 50,
    project: str | None = None,
    offset: int = 0,
    search: str = "",
    exclude_projects: list[str] | None = None,
    started_by: str | None = None,
) -> list[sqlite3.Row]:
    # W0.3: filter by project in SQL before LIMIT so low-traffic projects aren't
    # starved by the global 100-row cap (previously filtered in Python after LIMIT).
    where_sql, params = _runs_filter_sql(
        project=project, search=search, exclude_projects=exclude_projects, started_by=started_by
    )
    return conn.execute(
        f"SELECT * FROM runs {where_sql} ORDER BY run_id DESC LIMIT ? OFFSET ?",
        (*params, limit, offset),
    ).fetchall()


def count_recent_runs(
    conn: sqlite3.Connection,
    *,
    project: str | None = None,
    search: str = "",
    exclude_projects: list[str] | None = None,
    started_by: str | None = None,
) -> int:
    where_sql, params = _runs_filter_sql(
        project=project, search=search, exclude_projects=exclude_projects, started_by=started_by
    )
    row = conn.execute(f"SELECT COUNT(*) FROM runs {where_sql}", params).fetchone()
    return int(row[0]) if row else 0


def get_run(conn: sqlite3.Connection, run_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()


def get_runs_for_task(conn: sqlite3.Connection, project: str, task_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM runs WHERE project = ? AND task_id = ? ORDER BY run_id DESC",
        (project, task_id),
    ).fetchall()


def gpu_hours_today(conn: sqlite3.Connection, project: str) -> float:
    """GPU-task wall hours the project has used since UTC midnight (running runs count up to now).

    A run counts when its task declares ``gpu_vram_gib`` > 0; runs spanning midnight are
    clipped to today.
    """
    midnight = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    row = conn.execute(
        "SELECT SUM((julianday(COALESCE(r.finished_at, 'now')) "
        "          - MAX(julianday(r.started_at), julianday(?))) * 24.0) AS h "
        "FROM runs r JOIN tasks t ON t.project = r.project AND t.task_id = r.task_id "
        "WHERE r.project = ? AND COALESCE(r.finished_at, '9999') >= ? "
        "AND COALESCE(json_extract(t.resources, '$.gpu_vram_gib'), 0) > 0",
        (midnight, project, midnight),
    ).fetchone()
    return round(float(row["h"] or 0.0), 3)


def get_last_run_for_task(conn: sqlite3.Connection, project: str, task_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM runs WHERE project = ? AND task_id = ? ORDER BY run_id DESC LIMIT 1",
        (project, task_id),
    ).fetchone()


def get_last_runs_for_tasks(
    conn: sqlite3.Connection, keys: "list[tuple[str, str]]",
) -> "dict[tuple[str, str], sqlite3.Row]":
    """Latest run row per (project, task_id); keys with no run are absent.

    Batched equivalent of :func:`get_last_run_for_task` for the admit loop.
    """
    out: dict[tuple[str, str], sqlite3.Row] = {}
    uniq = list(dict.fromkeys(keys))
    for i in range(0, len(uniq), 300):
        chunk = uniq[i:i + 300]
        marks = ",".join("(?, ?)" for _ in chunk)
        params = [x for k in chunk for x in k]
        rows = conn.execute(
            "SELECT r.* FROM runs r WHERE r.run_id IN ("
            f"SELECT MAX(run_id) FROM runs WHERE (project, task_id) IN (VALUES {marks}) "
            "GROUP BY project, task_id)",
            params,
        ).fetchall()
        for r in rows:
            out[(r["project"], r["task_id"])] = r
    return out
