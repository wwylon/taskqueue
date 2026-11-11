"""Owner-facing stop / hold summaries from the latest run + error codes."""

from __future__ import annotations

import sqlite3
from typing import Any

from tqdaemon.errors import describe
from tqdaemon.rows import row_get

# Machine stop_reason → short operator text (control-file / daemon codes).
_STOP_REASON_LABELS: dict[str, str] = {
    "ram_hard": "host RAM hard limit",
    "vram_hard": "GPU VRAM hard limit",
    "wall_clock_cap": "wall-clock cap",
    "frozen": "task frozen (no progress)",
    "frozen_restart": "frozen — auto-restart",
    "deps_invalidated": "dependency invalidated while running",
    "scavenger_preempt": "scavenger preempted for other work",
    "evicted": "evicted (limit decrease)",
    "daemon_shutdown": "daemon shutdown",
    "host-sleep": "host sleep / clock jump",
    "stop_requested": "stop requested",
    "orphan": "orphan after daemon restart",
    "clock-jump": "host clock jump",
}

_STOPPED_BY_LABELS: dict[str, str] = {
    "tq": "TqDaemon",
    "user": "operator",
    "host-sleep": "host sleep",
    "lease-timeout": "worker lease timeout",
    "wall-cap": "wall-clock cap",
}


def label_stop_reason(reason: str | None) -> str | None:
    if not reason:
        return None
    r = str(reason).strip()
    if not r:
        return None
    if r in _STOP_REASON_LABELS:
        return _STOP_REASON_LABELS[r]
    if r.startswith("clock-jump"):
        return _STOP_REASON_LABELS.get("clock-jump", r)
    if r.startswith("alert:"):
        return f"system alert ({r[6:]})"
    return r


def label_stopped_by(who: str | None) -> str | None:
    if not who:
        return None
    w = str(who).strip()
    return _STOPPED_BY_LABELS.get(w, w)


def latest_run_stop_fields(conn: sqlite3.Connection, project: str, task_id: str) -> dict[str, Any]:
    """Return stop_reason / stopped_by / last_error_code from the newest run (if any)."""
    row = conn.execute(
        "SELECT stop_reason, stopped_by, last_error_code, exit_code FROM runs "
        "WHERE project = ? AND task_id = ? ORDER BY run_id DESC LIMIT 1",
        (project, task_id),
    ).fetchone()
    if row is None:
        return {}
    return {
        "stop_reason": row["stop_reason"],
        "stopped_by": row["stopped_by"],
        "run_last_error_code": row["last_error_code"],
        "exit_code": row["exit_code"],
    }


def format_stop_summary(
    *,
    status: str | None = None,
    stop_reason: str | None = None,
    stopped_by: str | None = None,
    last_error_code: str | None = None,
) -> str | None:
    """One-line summary for agents / why / task_result. None if nothing useful."""
    reason_l = label_stop_reason(stop_reason)
    by_l = label_stopped_by(stopped_by)
    err = describe(last_error_code) if last_error_code else None
    if by_l == "TqDaemon" or stopped_by == "tq":
        detail = reason_l or err or stop_reason or last_error_code or "automated stop"
        code = f" ({last_error_code})" if last_error_code else ""
        return f"stopped by TqDaemon: {detail}{code}"
    if reason_l or stop_reason:
        who = by_l or "operator"
        return f"stopped by {who}: {reason_l or stop_reason}"
    if last_error_code and status in ("stopped", "interrupted", "suspended", "failed"):
        return f"{status or 'stopped'}: {err or last_error_code}"
    return None


def attach_stop_fields(conn: sqlite3.Connection, project: str, task_id: str, task_row: Any = None) -> dict[str, Any]:
    """Fields to merge into task_result / why / API detail."""
    fields = latest_run_stop_fields(conn, project, task_id)
    code = None
    status = None
    if task_row is not None:
        code = row_get(task_row, "last_error_code")
        status = row_get(task_row, "status")
    code = code or fields.get("run_last_error_code")
    summary = format_stop_summary(
        status=str(status) if status else None,
        stop_reason=fields.get("stop_reason"),
        stopped_by=fields.get("stopped_by"),
        last_error_code=code,
    )
    out: dict[str, Any] = {
        "stop_reason": fields.get("stop_reason"),
        "stopped_by": fields.get("stopped_by"),
    }
    if summary:
        out["stop_summary"] = summary
    return out
