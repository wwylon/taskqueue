"""Aggregation over ``api_usage`` (interaction counters) and task provenance + outcomes."""
from __future__ import annotations

import json
import sqlite3
from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import Any

from taskqueue.operator_features import percentile

USAGE_DIMS = {
    "channel": "channel",
    "client": "client",
    "operator": "operator",
    "project": "project",
    "op": "op",
    "error_code": "error_code",
    "day": "substr(bucket_ts, 1, 10)",
    "hour": "bucket_ts",
}
TASK_DIMS = ("via", "by", "client", "project", "day")


def _dims(raw: Any, allowed: Any, default: list[str]) -> list[str]:
    if isinstance(raw, str):
        raw = [p.strip() for p in raw.split(",")]
    out = [d for d in (raw or default) if d]
    bad = [d for d in out if d not in allowed]
    if bad:
        raise ValueError(f"unknown group_by {bad}; allowed: {sorted(allowed)}")
    return out


def usage_by(
    conn: sqlite3.Connection,
    *,
    days: int = 7,
    by: Any = None,
    project: str | None = None,
    channel: str | None = None,
    operator: str | None = None,
    limit: int = 200,
) -> list[dict[str, Any]]:
    """Interaction counters grouped by ``by`` (whitelisted dimensions)."""
    dims = _dims(by, USAGE_DIMS, ["channel", "client"])
    cutoff = (datetime.now(UTC) - timedelta(days=max(1, int(days)))).strftime("%Y-%m-%dT%H:00:00Z")
    where, params = ["bucket_ts >= ?"], [cutoff]
    for col, val in (("project", project), ("channel", channel), ("operator", operator)):
        if val:
            where.append(f"{col} = ?")
            params.append(val)
    select = ", ".join(f"{USAGE_DIMS[d]} AS {d}" for d in dims)
    group = ", ".join(USAGE_DIMS[d] for d in dims)
    rows = conn.execute(
        f"SELECT {select}, SUM(calls) AS calls, "
        # ``wait.*`` codes (timeout, task failed) are normal wait outcomes, not errors,
        # and must not inflate avg_ms / max_ms (notice latency).
        "SUM(CASE WHEN error_code != '' AND error_code NOT LIKE 'wait.%' THEN calls ELSE 0 END) AS errors, "
        "SUM(CASE WHEN IFNULL(error_code, '') NOT LIKE 'wait.%' THEN total_ms ELSE 0 END) AS work_ms, "
        "SUM(CASE WHEN IFNULL(error_code, '') NOT LIKE 'wait.%' THEN calls ELSE 0 END) AS work_calls, "
        "MAX(CASE WHEN IFNULL(error_code, '') NOT LIKE 'wait.%' THEN max_ms ELSE NULL END) AS max_ms "
        f"FROM api_usage WHERE {' AND '.join(where)} GROUP BY {group} "
        "ORDER BY calls DESC LIMIT ?",
        (*params, max(1, min(int(limit), 2000))),
    ).fetchall()
    out = []
    for r in rows:
        d = {k: r[k] for k in dims}
        calls = int(r["calls"] or 0)
        work_calls = int(r["work_calls"] or 0)
        work_ms = float(r["work_ms"] or 0)
        d.update(
            calls=calls,
            errors=int(r["errors"] or 0),
            avg_ms=round(work_ms / work_calls, 1) if work_calls else 0.0,
            max_ms=int(r["max_ms"] or 0),
        )
        out.append(d)
    return out


def tasks_by(
    conn: sqlite3.Connection,
    *,
    days: int = 7,
    by: Any = None,
    project: str | None = None,
    limit: int = 200,
) -> list[dict[str, Any]]:
    """Task submissions + outcomes grouped by provenance (via / by / client / project / day)."""
    dims = _dims(by, TASK_DIMS, ["via", "by"])
    cutoff = (datetime.now(UTC) - timedelta(days=max(1, int(days)))).isoformat()
    where, params = ["t.enqueued_at >= ?"], [cutoff]
    if project:
        where.append("t.project = ?")
        params.append(project)
    rows = conn.execute(
        "SELECT t.project, t.status, t.last_error_code, t.submitted_via, t.submitted_by, "
        "t.client_info, t.enqueued_at, r.wall_sec, "
        "(SELECT COUNT(*) FROM runs x WHERE x.project = t.project AND x.task_id = t.task_id) AS n_runs "
        "FROM tasks t LEFT JOIN runs r ON r.run_id = t.last_run_id "
        f"WHERE {' AND '.join(where)}",
        params,
    ).fetchall()
    groups: dict[tuple, dict[str, Any]] = {}
    for r in rows:
        vals = {
            "via": r["submitted_via"],
            "by": r["submitted_by"],
            "client": r["client_info"],
            "project": r["project"],
            "day": (r["enqueued_at"] or "")[:10],
        }
        key = tuple(vals[d] or "(unknown)" for d in dims)
        g = groups.setdefault(key, {"statuses": Counter(), "errors": Counter(), "walls": [], "runs": 0})
        g["statuses"][r["status"]] += 1
        g["runs"] += int(r["n_runs"] or 0)
        if r["last_error_code"] and r["status"] in ("failed", "interrupted", "stopped"):
            g["errors"][r["last_error_code"]] += 1
        if r["wall_sec"] is not None and r["status"] == "completed":
            g["walls"].append(float(r["wall_sec"]))
    out = []
    for key, g in groups.items():
        walls = sorted(g["walls"])
        n = sum(g["statuses"].values())
        d = dict(zip(dims, key))
        d.update(
            tasks=n,
            completed=g["statuses"].get("completed", 0),
            failed=g["statuses"].get("failed", 0),
            interrupted_or_stopped=g["statuses"].get("interrupted", 0) + g["statuses"].get("stopped", 0),
            active=sum(g["statuses"].get(s, 0) for s in ("queued", "running")),
            retries=max(0, g["runs"] - n),
            wall_sec_p50=percentile(walls, 50),
            wall_sec_p95=percentile(walls, 95),
            top_errors=[{"code": c, "count": k} for c, k in g["errors"].most_common(3)],
        )
        out.append(d)
    out.sort(key=lambda x: -x["tasks"])
    return out[: max(1, min(int(limit), 2000))]


def agent_usage(
    conn: sqlite3.Connection,
    *,
    days: int = 7,
    by: Any = None,
    task_by: Any = None,
    project: str | None = None,
    channel: str | None = None,
    operator: str | None = None,
    limit: int = 200,
) -> dict[str, Any]:
    """One call for both views: how agents interact, and what they submit / how it ends."""
    try:
        from taskqueue.usage_track import RECORDER

        RECORDER.flush(conn)  # include this process's not-yet-flushed counters
    except Exception:
        pass
    return {
        "days": int(days),
        "usage": usage_by(conn, days=days, by=by, project=project, channel=channel,
                          operator=operator, limit=limit),
        "tasks": tasks_by(conn, days=days, by=task_by, project=project, limit=limit),
        "tracking_enabled": _tracking_enabled(conn),
    }


def _tracking_enabled(conn: sqlite3.Connection) -> bool:
    try:
        from taskqueue.db import get_config

        return int(get_config(conn).get("usage_tracking", 1)) != 0
    except Exception:
        return True


def format_table(rows: list[dict[str, Any]], title: str) -> str:
    if not rows:
        return f"{title}: (no data)"
    cols = list(rows[0].keys())
    cells = [
        [json.dumps(r[c]) if isinstance(r[c], (list, dict)) else ("" if r[c] is None else str(r[c])) for c in cols]
        for r in rows
    ]
    widths = [max(len(c), *(len(row[i]) for row in cells)) for i, c in enumerate(cols)]
    lines = [title, "  ".join(c.ljust(w) for c, w in zip(cols, widths))]
    lines += ["  ".join(v.ljust(w) for v, w in zip(row, widths)) for row in cells]
    return "\n".join(lines)
