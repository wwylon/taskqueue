"""Built-in undeletable system alerts (prefs + emit + Windows toast).

Keys: any_failure, all_done, task_frozen, tq_stopped.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from taskqueue.dt_utils import iso_utc as _iso, now_utc as _now, parse_utc as _parse_ts, within_window

log = logging.getLogger(__name__)

_dbg = logging.getLogger("taskqueue.alerts")

SYSTEM_ALERT_TITLES = {
    "any_failure": "Task failed",
    "all_done": "Queue idle",
    "task_frozen": "Task frozen",
    "tq_stopped": "Stopped by taskqueue",
    "run_anomaly": "Unusual run",
}

SYSTEM_ALERT_KEYS = ("any_failure", "all_done", "task_frozen", "tq_stopped", "run_anomaly")
ALL_DONE_COOLDOWN_SEC = 3600
ALL_DONE_RECENT_SEC = 3600

_STOP_REASON_LABELS = {
    "ram_hard": "stopped — host RAM hard limit exceeded",
    "vram_hard": "stopped — host VRAM hard limit exceeded",
    "wall_clock_cap": "stopped — wall-clock cap reached",
    "frozen": "stopped — frozen (no stdout/CPU for too long)",
    "frozen_restart": "restarted — frozen (no stdout/CPU); re-queued",
    "evicted": "stopped — evicted for capacity (another task needed resources)",
    "deps_invalidated": "stopped — dependency was invalidated mid-run",
    "host-sleep": "interrupted — host went to sleep",
    "scavenger_preempt": "paused — yielded capacity to a higher-priority task (will resume)",
    "user": "stopped by operator",
}

_ERROR_CODE_LABELS = {
    "tq.run.nonzero": "non-zero exit code",
    "tq.run.preflight": "preflight check failed",
    "tq.run.frozen": "no stdout/CPU (frozen)",
    "tq.run.cap": "wall-clock cap exceeded",
    "tq.stop.ram": "RAM limit exceeded",
    "tq.stop.vram": "VRAM limit exceeded",
    "tq.block.service_down": "required service unavailable",
    "tq.block.service_vram": "insufficient VRAM for required service",
}


def _fmt_elapsed(sec: float | None) -> str:
    if sec is None or sec < 0:
        return ""
    sec = int(sec)
    if sec < 60:
        return f"{sec}s"
    m, s = divmod(sec, 60)
    if m < 60:
        return f"{m}m{s:02d}s"
    h, m = divmod(m, 60)
    return f"{h}h{m:02d}m"











def _alert_log(msg: str) -> None:
    """One line in the daemon log (stdout) for every alert that fires."""
    print(f"[alert] {time.strftime('%H:%M:%S')} {msg}", flush=True)


def error_label(code: str | None) -> str | None:
    """Short label if we have one, else the full registry description, else the raw code."""
    if not code:
        return None
    if code in _ERROR_CODE_LABELS:
        return _ERROR_CODE_LABELS[code]
    from taskqueue.errors import describe

    return describe(code) or code


def _run_context(conn: sqlite3.Connection, project: str, task_id: str, run_id: int | None) -> dict[str, Any]:
    """Best-effort facts about the run/task for an alert detail. Never raises."""
    out: dict[str, Any] = {}
    try:
        t = conn.execute(
            "SELECT block_reason, max_attempts, description FROM tasks WHERE project = ? AND task_id = ?",
            (project, task_id),
        ).fetchone()
        if t is not None:
            if t["block_reason"]:
                out["block_reason"] = str(t["block_reason"])[:300]
            if t["description"]:
                out["description"] = str(t["description"])[:200]
            if int(t["max_attempts"] or 0) > 0:
                out["max_attempts"] = int(t["max_attempts"])
        if run_id is not None:
            r = conn.execute(
                "SELECT started_at, log_path, worker_id FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if r is not None:
                if r["started_at"]:
                    out["started_at"] = r["started_at"]
                if r["worker_id"]:
                    out["worker"] = r["worker_id"]
                if r["log_path"]:
                    out["log_path"] = r["log_path"]
                    out["log_tail"] = _log_tail(r["log_path"])
            n = conn.execute(
                "SELECT COUNT(*) FROM runs WHERE project = ? AND task_id = ? AND run_id <= ?",
                (project, task_id, run_id),
            ).fetchone()[0]
            out["attempt"] = int(n)
    except Exception:
        pass
    return out


def _log_tail(path: str, n: int = 5) -> list[str]:
    try:
        from taskqueue.logio import tail_log_lines

        lines = [ln.strip()[:240] for ln in tail_log_lines(path, 40) if ln.strip()]
        return lines[-n:]
    except Exception:
        return []


def seed_system_alerts(conn: sqlite3.Connection) -> None:
    """INSERT OR IGNORE the four system alert rows."""
    try:
        for key in SYSTEM_ALERT_KEYS:
            conn.execute(
                "INSERT OR IGNORE INTO system_alerts (key, enabled, windows_toast, fire_count) "
                "VALUES (?, 1, 1, 0)",
                (key,),
            )
        conn.commit()
    except sqlite3.OperationalError:
        # DB locked by daemon — rows are already seeded; safe to skip.
        pass


def queue_status_triplet(conn: sqlite3.Connection) -> tuple[int, int, int]:
    """Return (running, failed, queued) task counts."""
    counts = {"running": 0, "failed": 0, "queued": 0}
    try:
        for row in conn.execute(
            "SELECT status, COUNT(*) AS n FROM tasks "
            "WHERE status IN ('running','failed','queued') GROUP BY status"
        ).fetchall():
            st = row["status"]
            if st in counts:
                counts[st] = int(row["n"] or 0)
    except sqlite3.OperationalError:
        pass
    return counts["running"], counts["failed"], counts["queued"]


# Toast colour per alert (taskqueue.health levels): failures red, needs-a-look amber.
SYSTEM_ALERT_LEVELS = {
    "any_failure": "red",
    "tq_stopped": "amber",
    "task_frozen": "amber",
    "run_anomaly": "amber",
    "all_done": "green",
}


def toast_title_from_counts(running: int, failed: int, queued: int) -> str:
    return f"tq {running}/{failed}/{queued}"


_STOP_REASON_TO_ERROR_CODES: dict[str, set[str]] = {
    "frozen": {"tq.run.frozen"},
    "frozen_restart": {"tq.run.frozen"},
    "ram_hard": {"tq.stop.ram"},
    "vram_hard": {"tq.stop.vram"},
    "wall_clock_cap": {"tq.run.cap"},
}


def format_tq_stop_why(
    stop_reason: str | None,
    last_error_code: str | None,
) -> str:
    label = _STOP_REASON_LABELS.get(stop_reason or "", stop_reason) if stop_reason else None
    err = _ERROR_CODE_LABELS.get(last_error_code or "", last_error_code) if last_error_code else None
    # Skip the error label when it's redundant with the stop_reason label.
    redundant = last_error_code in _STOP_REASON_TO_ERROR_CODES.get(stop_reason or "", set())
    if label and err and not redundant:
        return f"{label} ({err})"
    return label or err or "stopped by TaskQueue"


def get_system_alert(conn: sqlite3.Connection, key: str) -> sqlite3.Row | None:
    try:
        return conn.execute("SELECT * FROM system_alerts WHERE key = ?", (key,)).fetchone()
    except sqlite3.OperationalError:
        return None


def list_system_alerts(
    conn: sqlite3.Connection,
    *,
    frozen_run_ids: set[int] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Public list for GET /api/alerts/system."""
    try:
        rows = conn.execute(
            "SELECT key, enabled, windows_toast, last_fired_at, fire_count "
            "FROM system_alerts ORDER BY key"
        ).fetchall()
    except sqlite3.OperationalError:
        return {"alerts": []}
    by_key = {r["key"]: r for r in rows}
    alerts = []
    for key in SYSTEM_ALERT_KEYS:
        row = by_key.get(key)
        if row is None:
            continue
        firing = system_alert_is_hot(
            conn, key, enabled=bool(int(row["enabled"] or 0)),
            frozen_run_ids=frozen_run_ids, now=now,
        )
        alerts.append({
            "key": key,
            "enabled": int(row["enabled"] or 0),
            "windows_toast": int(row["windows_toast"] or 0),
            "last_fired_at": row["last_fired_at"],
            "fire_count": int(row["fire_count"] or 0),
            "firing": firing,
        })
    return {"alerts": alerts}


def system_alert_is_hot(
    conn: sqlite3.Connection,
    key: str,
    *,
    enabled: bool,
    frozen_run_ids: set[int] | None = None,
    now: datetime | None = None,
) -> bool:
    """Whether the sidebar should treat this enabled alert as currently hot."""
    if not enabled:
        return False
    if key == "all_done":
        return _is_queue_idle(conn)
    if key == "task_frozen":
        if frozen_run_ids is not None:
            return bool(frozen_run_ids)
        return _count_running_with_frozen_block(conn) > 0
    return False


def count_system_alert_firing(
    conn: sqlite3.Connection,
    *,
    frozen_run_ids: set[int] | None = None,
    now: datetime | None = None,
) -> int:
    try:
        rows = conn.execute(
            "SELECT key, enabled FROM system_alerts WHERE enabled = 1"
        ).fetchall()
    except sqlite3.OperationalError:
        return 0
    n = 0
    for row in rows:
        if system_alert_is_hot(
            conn, row["key"], enabled=True,
            frozen_run_ids=frozen_run_ids, now=now,
        ):
            n += 1
    return n


def update_system_alert(
    conn: sqlite3.Connection,
    key: str,
    data: dict[str, Any],
) -> dict[str, Any] | None:
    if key not in SYSTEM_ALERT_KEYS:
        return None
    row = get_system_alert(conn, key)
    if row is None:
        return None
    fields: dict[str, Any] = {}
    if "enabled" in data:
        fields["enabled"] = 0 if str(data.get("enabled")).strip().lower() in ("0", "false", "no") else 1
    if "windows_toast" in data:
        fields["windows_toast"] = (
            0 if str(data.get("windows_toast")).strip().lower() in ("0", "false", "no") else 1
        )
    if fields:
        sets = ", ".join(f"{k} = ?" for k in fields)
        conn.execute(
            f"UPDATE system_alerts SET {sets} WHERE key = ?",
            (*fields.values(), key),
        )
        conn.commit()
    row = get_system_alert(conn, key)
    assert row is not None
    return {
        "key": row["key"],
        "enabled": int(row["enabled"] or 0),
        "windows_toast": int(row["windows_toast"] or 0),
        "last_fired_at": row["last_fired_at"],
        "fire_count": int(row["fire_count"] or 0),
        "firing": False,
    }


def _is_queue_idle(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM tasks WHERE status IN ('queued','running')"
    ).fetchone()
    return int(row["n"] or 0) == 0


def _has_recent_terminal(
    conn: sqlite3.Connection,
    *,
    now: datetime | None = None,
    window_sec: int = ALL_DONE_RECENT_SEC,
) -> bool:
    moment = _now(now)
    cutoff = moment - timedelta(seconds=max(window_sec, 0))
    row = conn.execute(
        "SELECT 1 FROM runs WHERE finished_at >= ? "
        "AND status IN ('completed','failed','interrupted','suspended','stopped') LIMIT 1",
        (_iso(cutoff),),
    ).fetchone()
    return row is not None


def _count_running_with_frozen_block(conn: sqlite3.Connection) -> int:
    """Best-effort hot check without daemon in-memory frozen set."""
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM tasks "
            "WHERE status = 'running' AND last_error_code = 'tq.run.frozen'"
        ).fetchone()
        return int(row["n"] or 0)
    except sqlite3.OperationalError:
        return 0


def _mark_fired(conn: sqlite3.Connection, key: str, now: datetime) -> None:
    conn.execute(
        "UPDATE system_alerts SET fire_count = fire_count + 1, last_fired_at = ? WHERE key = ?",
        (_iso(now), key),
    )
    conn.commit()


def maybe_emit_system_alert(
    conn: sqlite3.Connection,
    home: Path | None,
    key: str,
    body: str,
    *,
    now: datetime | None = None,
    cooldown_sec: int = 0,
    send_toast: bool = True,
    title: str | None = None,
    detail: dict[str, Any] | None = None,
) -> bool:
    """Fire one system alert if enabled. Returns True if emitted.

    When ``send_toast`` is True and the row has windows_toast=1, show a Windows
    toast titled ``tq running/failed/queued``.
    """
    if key not in SYSTEM_ALERT_KEYS:
        return False
    row = get_system_alert(conn, key)
    if row is None or not int(row["enabled"] or 0):
        _dbg.debug("alert %s suppressed (rule disabled): %s", key, body)
        return False
    moment = _now(now)
    if within_window(row["last_fired_at"], cooldown_sec, moment):
        _dbg.debug("alert %s suppressed (cooldown %ss): %s", key, cooldown_sec, body)
        return False
    _mark_fired(conn, key, moment)
    ev_title = title or SYSTEM_ALERT_TITLES.get(key, key)
    detail = dict(detail or {})
    detail.setdefault("title", ev_title)
    extra = " ".join(
        f"{k}={detail[k]}" for k in ("run_id", "exit_code", "error_code", "stop_reason") if detail.get(k) is not None
    )
    _alert_log(f"{key}: {body}" + (f" [{extra}]" if extra else ""))
    if home is not None:
        try:
            from taskqueue.notify import append_notify_event, build_notify_payload

            append_notify_event(
                Path(home),
                build_notify_payload(
                    project="alert",
                    task_id=key,
                    status=f"alert:{key}",
                    run_id=None,
                    exit_code=None,
                    last_error_code=None,
                    started_by="alert",
                    notify_url="",
                    description=body,
                    ts=_iso(moment),
                    detail=detail,
                ),
            )
        except Exception:
            pass
    if send_toast and int(row["windows_toast"] or 0) and home is not None:
        r, f, q = queue_status_triplet(conn)
        from taskqueue.notify import _send_toast

        level = SYSTEM_ALERT_LEVELS.get(key)
        if title is None:
            _send_toast(toast_title_from_counts(r, f, q), body, level=level)
        else:
            _send_toast(ev_title, f"{body}\n{r} running · {f} failed · {q} queued", level=level)
    return True


def on_run_terminal(
    conn: sqlite3.Connection,
    home: Path | None,
    *,
    project: str,
    task_id: str,
    status: str,
    run_id: int | None = None,
    last_error_code: str | None = None,
    stopped_by: str | None = None,
    stop_reason: str | None = None,
    exit_code: int | None = None,
    elapsed_sec: float | None = None,
) -> None:
    """Emit any_failure / tq_stopped from a finalize path. Never raises."""
    try:
        label = f"{project}/{task_id}"
        elapsed_str = f" after {_fmt_elapsed(elapsed_sec)}" if elapsed_sec is not None else ""
        base = {
            "project": project,
            "task_id": task_id,
            "run_id": run_id,
            "status": status,
            "exit_code": exit_code,
            "error_code": last_error_code,
            "error_label": error_label(last_error_code),
            "elapsed_sec": round(elapsed_sec, 1) if elapsed_sec is not None else None,
        }
        base = {k: v for k, v in base.items() if v is not None}
        if status == "failed":
            err_label = _ERROR_CODE_LABELS.get(last_error_code or "", last_error_code) if last_error_code else None
            exit_str = f" (exit {exit_code})" if exit_code is not None and exit_code != 0 else ""
            if err_label:
                body = f"{label} failed{elapsed_str}: {err_label}{exit_str}"
            elif exit_code is not None and exit_code != 0:
                body = f"{label} failed{elapsed_str} with exit code {exit_code}"
            else:
                body = f"{label} failed{elapsed_str}"
            detail = {**base, **_run_context(conn, project, task_id, run_id)}
            tail = detail.get("log_tail") or []
            if tail:
                body += f"\nlast output: {tail[-1][:160]}"
            maybe_emit_system_alert(conn, home, "any_failure", body, title=f"Task failed: {label}", detail=detail)
        if stopped_by == "tq":
            why = format_tq_stop_why(stop_reason, last_error_code)
            detail = {**base, **_run_context(conn, project, task_id, run_id)}
            if stop_reason:
                detail["stop_reason"] = stop_reason
            maybe_emit_system_alert(
                conn, home, "tq_stopped", f"{label}{elapsed_str}: {why}",
                title=f"Stopped by taskqueue: {label}", detail=detail,
            )
    except Exception:
        log.warning("system alert evaluation failed", exc_info=True)
        return
    if status == "completed" and run_id is not None:
        _emit_run_anomaly(conn, home, project, task_id, run_id)


_ANOMALY_LABELS = {"wall": "wall time", "ram": "peak RAM", "vram": "peak VRAM"}


def _emit_run_anomaly(conn, home, project: str, task_id: str, run_id: int) -> None:
    """Fire run_anomaly when a completed run is far above the task's own history. Never raises."""
    try:
        from taskqueue.operator_features import run_detail

        found = ((run_detail(conn, run_id) or {}).get("anomaly") or {}).get("flags") or []
        if not found:
            return
        parts = []
        for a in found:
            unit = "s" if a["metric"] == "wall" else " GiB"
            parts.append(
                f"{_ANOMALY_LABELS[a['metric']]} {a['value']:.4g}{unit} "
                f"(p95 of earlier runs {a['baseline_p95']:.4g}{unit}, {a['ratio']:g}x)"
            )
        label = f"{project}/{task_id}"
        maybe_emit_system_alert(
            conn, home, "run_anomaly", f"{label}: " + "; ".join(parts),
            title=f"Unusual run: {label}",
            detail={"project": project, "task_id": task_id, "run_id": run_id, "anomalies": found},
        )
    except Exception:
        log.warning("system alert evaluation failed", exc_info=True)
        return


def emit_task_frozen(
    conn: sqlite3.Connection,
    home: Path | None,
    *,
    project: str,
    task_id: str,
    elapsed_sec: float | None = None,
    run_id: int | None = None,
) -> bool:
    """Emit task_frozen (caller enforces once-per-run_id)."""
    try:
        elapsed_str = f" (running for {_fmt_elapsed(elapsed_sec)})" if elapsed_sec is not None else ""
        detail = {"project": project, "task_id": task_id, "run_id": run_id,
                  "elapsed_sec": round(elapsed_sec, 1) if elapsed_sec is not None else None}
        detail = {k: v for k, v in detail.items() if v is not None}
        detail.update(_run_context(conn, project, task_id, run_id))
        tail = detail.get("log_tail") or []
        body = f"{project}/{task_id}: no stdout or CPU activity detected{elapsed_str} — task may be hung"
        if tail:
            body += f"\nlast output: {tail[-1][:160]}"
        return maybe_emit_system_alert(
            conn, home, "task_frozen", body,
            title=f"Task frozen: {project}/{task_id}", detail=detail,
        )
    except Exception:
        log.warning("system alert evaluation failed", exc_info=True)
        return False


def evaluate_all_done(
    conn: sqlite3.Connection,
    home: Path | None,
    *,
    now: datetime | None = None,
) -> bool:
    """Tick evaluator for all_done."""
    try:
        if not _is_queue_idle(conn):
            return False
        if not _has_recent_terminal(conn, now=now):
            return False
        r, f, q = queue_status_triplet(conn)
        fail_str = f" ({f} failed)" if f > 0 else ""
        moment = _now(now)
        since = _iso(moment - timedelta(seconds=ALL_DONE_RECENT_SEC))
        detail: dict[str, Any] = {"failed_total": f}
        try:
            counts = {
                row["status"]: int(row["n"])
                for row in conn.execute(
                    "SELECT status, COUNT(*) AS n FROM runs WHERE finished_at >= ? GROUP BY status", (since,)
                ).fetchall()
            }
            detail["last_hour_runs"] = counts
            detail["failed_tasks"] = [
                f"{row['project']}/{row['task_id']}"
                for row in conn.execute(
                    "SELECT project, task_id FROM tasks WHERE status = 'failed' "
                    "ORDER BY updated_at DESC LIMIT 5"
                ).fetchall()
            ]
        except Exception:
            pass
        done = sum((detail.get("last_hour_runs") or {}).values())
        body = f"Queue is idle — nothing queued or running{fail_str}"
        if done:
            body += f"\n{done} run(s) finished in the last hour"
        if detail.get("failed_tasks"):
            body += "\nfailed: " + ", ".join(detail["failed_tasks"])
        return maybe_emit_system_alert(
            conn, home, "all_done", body,
            now=now,
            cooldown_sec=ALL_DONE_COOLDOWN_SEC,
            title="Queue idle" + (f" — {f} failed" if f else ""),
            detail=detail,
        )
    except Exception:
        log.warning("system alert evaluation failed", exc_info=True)
        return False
