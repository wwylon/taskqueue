"""Deep diagnose bundle: master task/run + optional worker (slave) context."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from taskqueue import config as tqconfig
from taskqueue._db_tasks import get_task
from taskqueue.errors import describe


def _tail_log(path: str | None, n: int = 80) -> list[str]:
    from taskqueue.logio import tail_log_lines

    return tail_log_lines(path, n)


def build_diagnose(
    conn: sqlite3.Connection,
    project: str,
    task_id: str,
    *,
    home: Path | None = None,
    log_tail_lines: int = 80,
) -> dict[str, Any]:
    home = home or tqconfig.state_home()
    task = get_task(conn, project, task_id)
    runs = conn.execute(
        "SELECT * FROM runs WHERE project = ? AND task_id = ? ORDER BY run_id DESC LIMIT 5",
        (project, task_id),
    ).fetchall()
    last = runs[0] if runs else None
    worker = None
    if last and last["worker_id"]:
        w = conn.execute(
            "SELECT * FROM workers WHERE worker_id = ?",
            (last["worker_id"],),
        ).fetchone()
        if w:
            from taskqueue.db import get_config, worker_health

            heartbeat_sec = float(get_config(conn).get("heartbeat_sec", 30))
            worker = dict(w)
            # Finer-grained than the stored `status` (online/offline, used
            # for dispatch gating): "unreachable" means missed heartbeats
            # but not yet past the offline threshold -- a plausible network
            # blip, distinct from a confirmed-dead worker.
            worker["health"] = worker_health(w["last_seen"], heartbeat_sec=heartbeat_sec)
        else:
            worker = {"worker_id": last["worker_id"], "missing": True}

    ctrl_path = tqconfig.control_path(home, project, task_id)
    control = None
    if ctrl_path.is_file():
        try:
            control = ctrl_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            control = None

    log_path = last["log_path"] if last else None
    err = dict(task)["last_error_code"] if task else None
    metrics = None
    try:
        from taskqueue.metrics_snapshot import build_run_metrics
        metrics = build_run_metrics(conn, project, task_id, home=home, samples=60)
    except Exception:
        metrics = None
    return {
        "project": project,
        "task_id": task_id,
        "task": dict(task) if task else None,
        "error_description": describe(err),
        "runs": [dict(r) for r in runs],
        "last_run": dict(last) if last else None,
        "worker": worker,
        "control_file": str(ctrl_path),
        "control": control,
        "log_path": log_path,
        "log_tail": _tail_log(log_path, log_tail_lines),
        "home": str(home),
        "metrics": metrics,
    }
