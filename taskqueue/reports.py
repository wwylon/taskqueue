"""Aggregate queue reports for CLI / HTTP / MCP."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

from taskqueue.dt_utils import iso_utc


def build_report(
    conn: sqlite3.Connection,
    *,
    project: str | None = None,
    days: int = 7,
) -> dict[str, Any]:
    days = max(1, min(int(days), 365))
    since = iso_utc(datetime.now(timezone.utc) - timedelta(days=days))  # same format as stored started_at

    params: list[Any] = [since]
    proj_sql = ""
    if project:
        proj_sql = " AND project = ?"
        params.append(project)

    status_rows = conn.execute(
        f"SELECT status, COUNT(*) AS n FROM runs "
        f"WHERE started_at >= ?{proj_sql} GROUP BY status",
        params,
    ).fetchall()
    by_status = {r["status"] or "unknown": int(r["n"]) for r in status_rows}

    err_params: list[Any] = []
    err_sql = (
        "SELECT last_error_code, COUNT(*) AS n FROM tasks "
        "WHERE last_error_code IS NOT NULL"
    )
    if project:
        err_sql += " AND project = ?"
        err_params.append(project)
    err_sql += " GROUP BY last_error_code ORDER BY n DESC LIMIT 20"
    by_error = {
        r["last_error_code"]: int(r["n"])
        for r in conn.execute(err_sql, err_params).fetchall()
    }

    dur_row = conn.execute(
        f"SELECT AVG("
        f"  CASE WHEN finished_at IS NOT NULL AND started_at IS NOT NULL "
        f"  THEN (julianday(replace(finished_at,'Z','')) - julianday(replace(started_at,'Z',''))) * 86400.0 "
        f"  END"
        f") AS avg_sec, COUNT(*) AS n FROM runs "
        f"WHERE started_at >= ? AND status = 'completed'{proj_sql}",
        params,
    ).fetchone()
    avg_completed_sec = None
    if dur_row and dur_row["avg_sec"] is not None:
        avg_completed_sec = round(float(dur_row["avg_sec"]), 1)

    recent_fail = conn.execute(
        f"SELECT project, task_id, status, exit_code, started_at, finished_at "
        f"FROM runs WHERE started_at >= ? AND status IN ('failed','interrupted'){proj_sql} "
        f"ORDER BY run_id DESC LIMIT 25",
        params,
    ).fetchall()

    task_counts = conn.execute(
        "SELECT status, COUNT(*) AS n FROM tasks WHERE 1=1"
        + (" AND project = ?" if project else "")
        + " GROUP BY status",
        ([project] if project else []),
    ).fetchall()

    return {
        "since": since,
        "days": days,
        "project": project,
        "runs_by_status": by_status,
        "tasks_by_status": {r["status"]: int(r["n"]) for r in task_counts},
        "error_codes": by_error,
        "avg_completed_sec": avg_completed_sec,
        "recent_failures": [dict(r) for r in recent_fail],
    }
