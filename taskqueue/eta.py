"""Task ETA: time left for a running task, and how much was left when a run was stopped.

Operators mostly stop tasks because they are too slow, so the estimate at the moment of the
stop is kept with the run (it is derived from data the run already persists, so nothing extra
is written): progress %, wall time and the recent progress rate in ``metrics_json._progress``.
Agents get the same ``eta`` block from ``wait_for_task``, ``task_result``, ``stop_task`` and
``progress_snapshot``.

Basis, best first:

- ``rate``: recent progress rate (EWMA of %/s, :func:`taskqueue.log_issues.note_progress`).
- ``average``: run time so far / progress %, when no rate is known yet (one progress line seen).
- ``history``: median wall time of completed runs (``tasks.predicted_duration_sec``) minus time
  spent, for tasks that print no progress.
"""

from __future__ import annotations

import json
import sqlite3
import time
from datetime import UTC, datetime
from typing import Any

from taskqueue._fmt import fmt_duration
from taskqueue._time import parse_iso


def _num(v: Any) -> float | None:
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def _timing(metrics_json: Any) -> dict | None:
    if isinstance(metrics_json, dict):
        mj: Any = metrics_json
    elif metrics_json:
        try:
            mj = json.loads(metrics_json)
        except (TypeError, ValueError):
            return None
    else:
        return None
    t = mj.get("_progress") if isinstance(mj, dict) else None
    return t if isinstance(t, dict) else None


def remaining(
    *, progress_pct: Any, timing: dict | None, elapsed_sec: Any, predicted_sec: Any
) -> tuple[float, str] | None:
    """(seconds left, basis) or None when there is nothing to estimate from."""
    pct, elapsed, predicted = _num(progress_pct), _num(elapsed_sec), _num(predicted_sec)
    rate = _num((timing or {}).get("rate"))
    if pct is not None and 0 <= pct < 100:
        if rate and rate > 0:
            return (100.0 - pct) / rate, "rate"
        if pct > 0 and elapsed and elapsed > 0:
            return elapsed / pct * (100.0 - pct), "average"
    if pct is None and predicted and predicted > 0 and elapsed is not None:
        return max(0.0, predicted - elapsed), "history"
    return None


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_eta(
    *,
    task_status: str | None,
    run_status: str | None,
    progress_pct: Any,
    metrics_json: Any,
    started_at: str | None,
    finished_at: str | None,
    wall_sec: Any,
    predicted_sec: Any,
    now: float | None = None,
) -> dict[str, Any] | None:
    """The ``eta`` block for a task from its latest run; None when there is nothing useful to say.

    Running: ``remaining_sec`` and ``finish_at``. A run that ended before finishing (stopped,
    interrupted, suspended, failed): the frozen estimate with ``at_stop: true`` and ``stopped_at``.
    A task that never ran (or whose last run completed): ``typical_sec`` only, when known.
    """
    now = time.time() if now is None else now
    predicted = _num(predicted_sec)
    out: dict[str, Any] = {}
    if predicted and predicted > 0:
        out["typical_sec"] = round(predicted)
    live = run_status == "running" and task_status == "running"
    ended_early = run_status not in (None, "running", "completed") and finished_at is not None
    if live:
        elapsed = None
        if started_at:
            try:
                elapsed = max(0.0, now - parse_iso(started_at).timestamp())
            except (TypeError, ValueError):
                elapsed = None
        est = remaining(
            progress_pct=progress_pct, timing=_timing(metrics_json), elapsed_sec=elapsed, predicted_sec=predicted
        )
        if est:
            left, basis = est
            out.update(remaining_sec=round(left), basis=basis, finish_at=_iso(now + left))
            if progress_pct is not None:
                out["progress_pct"] = _num(progress_pct)
            out["text"] = f"~{fmt_duration(left)} left ({basis})"
    elif ended_early:
        est = remaining(
            progress_pct=progress_pct, timing=_timing(metrics_json), elapsed_sec=wall_sec, predicted_sec=predicted
        )
        if est:
            left, basis = est
            out.update(remaining_sec=round(left), basis=basis, at_stop=True, stopped_at=finished_at)
            at = f" at {_num(progress_pct):g}%" if progress_pct is not None else ""
            out["progress_pct"] = _num(progress_pct)
            out["text"] = f"was ~{fmt_duration(left)} from done{at} when the run ended ({run_status}, {basis})"
    if not out:
        return None
    if "text" not in out and "typical_sec" in out:
        out["text"] = f"usually takes {fmt_duration(out['typical_sec'])}"
    return out


_SELECT = (
    "SELECT t.status AS task_status, t.predicted_duration_sec, r.status AS run_status, r.progress_pct, "
    "json_extract(r.metrics_json, '$._progress') AS progress_timing, r.started_at, r.finished_at, r.wall_sec "
    "FROM tasks t LEFT JOIN runs r ON r.run_id = ("
    "SELECT MAX(run_id) FROM runs WHERE project = t.project AND task_id = t.task_id) "
    "WHERE t.project = ? AND t.task_id = ?"
)


def task_eta(conn: sqlite3.Connection, project: str, task_id: str, *, now: float | None = None) -> dict | None:
    """:func:`build_eta` for one task, read from the DB (latest run). Only the ``_progress`` part of
    ``metrics_json`` is loaded."""
    try:
        r = conn.execute(_SELECT, (project, task_id)).fetchone()
    except sqlite3.OperationalError:  # SQLite built without JSON1
        return None
    if r is None:
        return None
    timing = None
    if r["progress_timing"]:
        try:
            timing = json.loads(r["progress_timing"])
        except (TypeError, ValueError):
            timing = None
    return build_eta(
        task_status=r["task_status"],
        run_status=r["run_status"],
        progress_pct=r["progress_pct"],
        metrics_json={"_progress": timing} if isinstance(timing, dict) else None,
        started_at=r["started_at"],
        finished_at=r["finished_at"],
        wall_sec=r["wall_sec"],
        predicted_sec=r["predicted_duration_sec"],
        now=now,
    )
