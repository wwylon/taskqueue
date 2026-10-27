"""Tasks stats, timeline forecast, schedules, alert rules, operator audit, API tokens.

Read models come from tasks/runs. Timeline projects a forward multi-lane Gantt.
Alert evaluation runs at the end of a scheduler tick. Tokens are SHA-256 hashes;
the legacy config auth_token stays a separate admin credential for workers and
the master lease.
"""

from __future__ import annotations
from taskqueue._time import parse_iso as _parse_iso

import hashlib
import hmac
import json
import math
import re
import secrets
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from taskqueue.auth import configured_token, token_required
from taskqueue.cron import cron_matches
from taskqueue._db_tasks import get_task
from taskqueue.dt_utils import iso_utc as _iso, now_utc as _now, parse_utc as _parse_ts, within_window
from taskqueue.rows import row_get

_TERMINAL = ("completed", "failed", "interrupted", "suspended", "stopped")
ALERT_KINDS = ("run_duration", "failed_count", "queued_depth", "vram_pct")
ALERT_ACTIONS = ("notify", "notify_stop")
TOKEN_SCOPES = ("read", "write", "admin")
_TOKEN_PREFIX = "tq_live_"

_WRITE_PREFIXES = (
    "/api/stop/",
    "/api/retry/",
    "/api/force/",
    "/api/enable/",
    "/api/priority/",
    "/api/mark-processed/",
    "/api/project/freeze/",
    "/api/project/unfreeze/",
    "/api/project/stop-all/",
)











def percentile(sorted_vals: list[float], p: float) -> float | None:
    """Nearest-rank percentile. ``p`` is 0–100. Empty input is None."""
    n = len(sorted_vals)
    if n == 0:
        return None
    if n == 1:
        return float(sorted_vals[0])
    idx = max(0, math.ceil((p / 100.0) * n) - 1)
    return float(sorted_vals[min(idx, n - 1)])


def task_stats(
    conn: sqlite3.Connection,
    *,
    project: str | None = None,
    task_id: str | None = None,
) -> list[dict[str, Any]]:
    """One row per (project, task_id) from terminal runs with a duration.

    ``mean_sec`` / ``p50_sec`` / ``p95_sec`` use **completed** runs only.
    ``count`` and ``success_rate`` cover all terminal statuses.
    """
    sql = (
        "SELECT project, task_id, started_at, finished_at, status FROM runs "
        "WHERE finished_at IS NOT NULL AND started_at IS NOT NULL "
        "AND status IN ('completed','failed','interrupted','suspended','stopped')"
    )
    args: list[Any] = []
    if project:
        sql += " AND project = ?"
        args.append(project)
    if task_id:
        sql += " AND task_id = ?"
        args.append(task_id)
    grouped: dict[tuple[str, str], list[tuple[float, str, str]]] = {}
    for row in conn.execute(sql, tuple(args)).fetchall():
        started = _parse_ts(row["started_at"])
        finished = _parse_ts(row["finished_at"])
        if started is None or finished is None:
            continue
        dur = (finished - started).total_seconds()
        if dur < 0:
            continue
        key = (row["project"] or "", row["task_id"] or "")
        grouped.setdefault(key, []).append((dur, row["finished_at"], row["status"] or ""))
    out: list[dict[str, Any]] = []
    for (proj, tid), samples in grouped.items():
        last_finish = max(samples, key=lambda s: s[1])[1]
        completed_n = sum(1 for s in samples if s[2] == "completed")
        n = len(samples)
        completed_durs = sorted(s[0] for s in samples if s[2] == "completed")
        if completed_durs:
            mean_sec: float | None = round(sum(completed_durs) / len(completed_durs), 1)
            p50_sec: float | None = round(percentile(completed_durs, 50) or 0, 1)
            p95_sec: float | None = round(percentile(completed_durs, 95) or 0, 1)
        else:
            mean_sec = p50_sec = p95_sec = None
        out.append({
            "project": proj,
            "task_id": tid,
            "last_finish": last_finish,
            "mean_sec": mean_sec,
            "p50_sec": p50_sec,
            "p95_sec": p95_sec,
            "count": n,
            "success_rate": round(completed_n / n, 3) if n else 0,
        })
    out.sort(key=lambda r: (r["project"], r["task_id"]))
    return out


def timeline(
    conn: sqlite3.Connection,
    *,
    hours: int = 24,
    project: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Forward forecast Gantt: queued/running (+ cron) packed under concurrency limits."""
    from taskqueue.timeline_forecast import forecast_timeline

    return forecast_timeline(conn, hours=hours, project=project, now=now)


_CRON_FIRE_CACHE: dict[tuple[str, int], tuple[datetime, datetime]] = {}
_CRON_FIRE_CACHE_MAX = 512


def next_cron_fire(expr: str, now: datetime | None = None, *, hours: int = 48) -> datetime | None:
    """First minute in the next ``hours`` that matches ``expr``. None if none.

    A found fire time stays valid for every later ``now`` up to that minute (nothing
    matched in between), so repeated polls reuse it instead of re-scanning minutes.
    """
    if not expr or not str(expr).strip():
        return None
    cursor_base = _now(now)
    cursor = (cursor_base + timedelta(minutes=1)).replace(second=0, microsecond=0)
    key = (str(expr), hours)
    hit = _CRON_FIRE_CACHE.get(key)
    if hit is not None:
        scanned_from, fire = hit
        if scanned_from <= cursor <= fire:
            return fire
    end = cursor_base + timedelta(hours=hours)
    steps = 0
    limit = hours * 60 + 2
    scan = cursor
    while scan <= end and steps < limit:
        try:
            if cron_matches(expr, scan):
                if len(_CRON_FIRE_CACHE) >= _CRON_FIRE_CACHE_MAX:
                    _CRON_FIRE_CACHE.clear()
                _CRON_FIRE_CACHE[key] = (cursor, scan)
                return scan
        except Exception:
            return None
        scan += timedelta(minutes=1)
        steps += 1
    return None


def schedules(conn: sqlite3.Connection, *, project: str | None = None, now: datetime | None = None) -> list[dict[str, Any]]:
    sql = (
        "SELECT project, task_id, status, enabled, schedule FROM tasks "
        "WHERE schedule IS NOT NULL AND schedule != '' AND schedule != 'null'"
    )
    args: tuple[Any, ...] = ()
    if project:
        sql += " AND project = ?"
        args = (project,)
    out: list[dict[str, Any]] = []
    for row in conn.execute(sql, args).fetchall():
        try:
            sched = json.loads(row["schedule"] or "null")
        except Exception:
            continue
        if not isinstance(sched, dict):
            continue
        cron_expr = sched.get("cron")
        if not cron_expr:
            continue
        nxt = next_cron_fire(str(cron_expr), now)
        out.append({
            "project": row["project"],
            "task_id": row["task_id"],
            "status": row["status"],
            "enabled": int(row["enabled"] or 0),
            "cron": str(cron_expr),
            "next_fire": _iso(nxt) if nxt else None,
        })
    out.sort(key=lambda r: (r["next_fire"] or "z", r["project"], r["task_id"]))
    return out


def vram_percent(hardware: dict[str, Any] | None) -> float | None:
    if not hardware:
        return None
    if hardware.get("vram_pct") is not None:
        try:
            return float(hardware["vram_pct"])
        except (TypeError, ValueError):
            pass
    gpus = hardware.get("gpus") or []
    used = 0.0
    total = 0.0
    for g in gpus:
        if not isinstance(g, dict):
            continue
        total += float(g.get("vram_gib") or g.get("vram_total_gib") or 0)
        used += float(g.get("vram_used_gib") or 0)
    if total <= 0:
        return None
    return 100.0 * used / total


def _rule_public(row: sqlite3.Row, *, firing: bool) -> dict[str, Any]:
    return {
        "id": row["id"],
        "name": row["name"],
        "kind": row["kind"],
        "threshold": row["threshold"],
        "window_sec": row["window_sec"],
        "project": row["project"],
        "action": row["action"],
        "enabled": int(row["enabled"] or 0),
        "fire_count": int(row["fire_count"] or 0),
        "last_fired_at": row["last_fired_at"],
        "firing": bool(firing),
    }


def rule_is_firing(
    conn: sqlite3.Connection,
    rule: sqlite3.Row | dict[str, Any],
    *,
    now: datetime | None = None,
    hardware: dict[str, Any] | None = None,
) -> tuple[bool, list[dict[str, Any]]]:
    """Return (condition, offending runs). Offending runs only for run_duration."""
    if not int(rule["enabled"] or 0):
        return False, []
    kind = rule["kind"]
    threshold = float(rule["threshold"])
    window = int(rule["window_sec"] or 0)
    project = (rule["project"] or "").strip() or None
    moment = _now(now)
    if kind == "run_duration":
        sql = (
            "SELECT run_id, project, task_id, started_at FROM runs "
            "WHERE status = 'running' AND finished_at IS NULL AND started_at IS NOT NULL"
        )
        args: list[Any] = []
        if project:
            sql += " AND project = ?"
            args.append(project)
        offenders = []
        for row in conn.execute(sql, args).fetchall():
            started = _parse_ts(row["started_at"])
            if started is None:
                continue
            elapsed = (moment - started).total_seconds()
            if elapsed > threshold:
                offenders.append({
                    "run_id": row["run_id"],
                    "project": row["project"],
                    "task_id": row["task_id"],
                    "elapsed_sec": round(elapsed, 1),
                })
        return bool(offenders), offenders
    if kind == "failed_count":
        cutoff = moment - timedelta(seconds=max(window, 0))
        sql = "SELECT finished_at FROM runs WHERE status = 'failed' AND finished_at IS NOT NULL"
        args = []
        if project:
            sql += " AND project = ?"
            args.append(project)
        n = 0
        for row in conn.execute(sql, args).fetchall():
            fin = _parse_ts(row["finished_at"])
            if fin is not None and fin >= cutoff:
                n += 1
        return n > threshold, []
    if kind == "queued_depth":
        sql = "SELECT COUNT(*) AS n FROM tasks WHERE status = 'queued'"
        args = []
        if project:
            sql += " AND project = ?"
            args.append(project)
        n = int(conn.execute(sql, args).fetchone()["n"])
        return n > threshold, []
    if kind == "vram_pct":
        pct = vram_percent(hardware)
        if pct is None:
            return False, []
        return pct > threshold, []
    return False, []


def list_alerts(
    conn: sqlite3.Connection,
    *,
    now: datetime | None = None,
    hardware: dict[str, Any] | None = None,
) -> dict[str, Any]:
    rows = conn.execute("SELECT * FROM alert_rules ORDER BY id").fetchall()
    rules = []
    firing = 0
    for row in rows:
        active, _ = rule_is_firing(conn, row, now=now, hardware=hardware)
        if active:
            firing += 1
        rules.append(_rule_public(row, firing=active))
    return {"rules": rules, "firing_count": firing}


def count_firing_rules(
    conn: sqlite3.Connection,
    hardware: dict[str, Any] | None = None,
    *,
    now: datetime | None = None,
) -> int:
    try:
        return int(list_alerts(conn, now=now, hardware=hardware)["firing_count"])
    except sqlite3.OperationalError:
        return 0


def create_alert(conn: sqlite3.Connection, data: dict[str, Any]) -> dict[str, Any]:
    name = str(data.get("name") or "").strip()
    kind = str(data.get("kind") or "").strip()
    action = str(data.get("action") or "notify").strip()
    if not name:
        raise ValueError("name is required")
    if kind not in ALERT_KINDS:
        raise ValueError(f"kind must be one of {', '.join(ALERT_KINDS)}")
    if action not in ALERT_ACTIONS:
        raise ValueError(f"action must be one of {', '.join(ALERT_ACTIONS)}")
    try:
        threshold = float(data.get("threshold"))
    except (TypeError, ValueError):
        raise ValueError("threshold must be a number")
    try:
        window_sec = int(data.get("window_sec") if data.get("window_sec") is not None else 3600)
    except (TypeError, ValueError):
        raise ValueError("window_sec must be an integer")
    if window_sec < 0:
        raise ValueError("window_sec must be >= 0")
    project = str(data.get("project") or "").strip() or None
    enabled = 0 if str(data.get("enabled", 1)).strip() in ("0", "false", "no") else 1
    cur = conn.execute(
        "INSERT INTO alert_rules (name, kind, threshold, window_sec, project, action, enabled) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (name, kind, threshold, window_sec, project, action, enabled),
    )
    conn.commit()
    row = conn.execute("SELECT * FROM alert_rules WHERE id = ?", (cur.lastrowid,)).fetchone()
    return _rule_public(row, firing=False)


def update_alert(conn: sqlite3.Connection, rule_id: int, data: dict[str, Any]) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM alert_rules WHERE id = ?", (rule_id,)).fetchone()
    if row is None:
        return None
    fields: dict[str, Any] = {}
    if "name" in data:
        name = str(data.get("name") or "").strip()
        if not name:
            raise ValueError("name is required")
        fields["name"] = name
    if "kind" in data:
        kind = str(data.get("kind") or "").strip()
        if kind not in ALERT_KINDS:
            raise ValueError(f"kind must be one of {', '.join(ALERT_KINDS)}")
        fields["kind"] = kind
    if "action" in data:
        action = str(data.get("action") or "").strip()
        if action not in ALERT_ACTIONS:
            raise ValueError(f"action must be one of {', '.join(ALERT_ACTIONS)}")
        fields["action"] = action
    if "threshold" in data:
        fields["threshold"] = float(data["threshold"])
    if "window_sec" in data:
        window_sec = int(data["window_sec"])
        if window_sec < 0:
            raise ValueError("window_sec must be >= 0")
        fields["window_sec"] = window_sec
    if "project" in data:
        fields["project"] = str(data.get("project") or "").strip() or None
    if "enabled" in data:
        fields["enabled"] = 0 if str(data.get("enabled")).strip().lower() in ("0", "false", "no") else 1
    if fields:
        sets = ", ".join(f"{k} = ?" for k in fields)
        conn.execute(
            f"UPDATE alert_rules SET {sets} WHERE id = ?",
            (*fields.values(), rule_id),
        )
        conn.commit()
    row = conn.execute("SELECT * FROM alert_rules WHERE id = ?", (rule_id,)).fetchone()
    return _rule_public(row, firing=False)


def delete_alert(conn: sqlite3.Connection, rule_id: int) -> bool:
    cur = conn.execute("DELETE FROM alert_rules WHERE id = ?", (rule_id,))
    conn.commit()
    return cur.rowcount > 0


def evaluate_alerts(
    conn: sqlite3.Connection,
    home: Path | None,
    hardware: dict[str, Any] | None,
    cfg: dict[str, Any] | None = None,
    *,
    now: datetime | None = None,
) -> list[int]:
    """Fire enabled rules whose condition is true and whose cooldown has elapsed.

    Returns the ids that fired on this call. ``notify_stop`` stops the offending
    run only for the per-task kind (``run_duration``).
    """
    moment = _now(now)
    try:
        rows = conn.execute("SELECT * FROM alert_rules WHERE enabled = 1").fetchall()
    except sqlite3.OperationalError:
        return []
    fired: list[int] = []
    for rule in rows:
        if within_window(rule["last_fired_at"], int(rule["window_sec"] or 0), moment):
            continue
        active, offenders = rule_is_firing(conn, rule, now=moment, hardware=hardware)
        if not active:
            continue
        conn.execute(
            "UPDATE alert_rules SET fire_count = fire_count + 1, last_fired_at = ? WHERE id = ?",
            (_iso(moment), rule["id"]),
        )
        conn.commit()
        fired.append(int(rule["id"]))
        _emit_alert(home, cfg, rule, offenders)
        if rule["action"] == "notify_stop" and rule["kind"] == "run_duration" and home is not None:
            from taskqueue._db_tasks import request_stop_with_control

            for off in offenders:
                try:
                    request_stop_with_control(conn, Path(home), off["project"], off["task_id"])
                except Exception:
                    pass
    return fired


def _emit_alert(home: Path | None, cfg: dict[str, Any] | None, rule: sqlite3.Row, offenders: list[dict[str, Any]]) -> None:
    if home is None:
        return
    try:
        from taskqueue.notify import emit_task_notify

        project = (rule["project"] or "") or (offenders[0]["project"] if offenders else "")
        task_id = offenders[0]["task_id"] if offenders else str(rule["name"])
        emit_task_notify(
            Path(home),
            project=project or "alert",
            task_id=task_id,
            status="failed",
            run_id=offenders[0]["run_id"] if offenders else None,
            exit_code=None,
            last_error_code="tq.alert." + str(rule["kind"]),
            notify_url=None,
            started_by="alert",
            cfg=cfg or {"toast_notifications": False},
            description=f"{rule['name']}: {rule['kind']} > {rule['threshold']}",
            stop_reason=f"alert:{rule['kind']}",
        )
    except Exception:
        return


def _redact_detail(detail: str | None) -> str | None:
    if not detail:
        return detail
    return re.sub(r"tq_live_[A-Za-z0-9_\-]+", "tq_live_[redacted]", detail)


def write_audit(
    conn: sqlite3.Connection,
    actor: str,
    action: str,
    project: str | None = None,
    target: str | None = None,
    detail: str | None = None,
) -> None:
    try:
        conn.execute(
            "INSERT INTO operator_audit (ts, actor, action, project, target, detail) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                _iso(_now()),
                (actor or "web").strip() or "web",
                action,
                project or None,
                target or None,
                _redact_detail(detail),
            ),
        )
        conn.commit()
    except sqlite3.OperationalError:
        return


def list_audit(
    conn: sqlite3.Connection,
    *,
    limit: int = 200,
    actor: str | None = None,
    action: str | None = None,
    project: str | None = None,
    since: str | None = None,
    until: str | None = None,
    before_id: int | None = None,
) -> list[dict[str, Any]]:
    """Newest-first audit events. ``since``/``until`` are ISO timestamps (``ts`` is ISO text)."""
    limit = max(1, min(int(limit), 2000))
    where: list[str] = []
    params: list[Any] = []
    for col, val in (("actor", actor), ("action", action), ("project", project)):
        if val:
            where.append(f"{col} = ?")
            params.append(val)
    if since:
        where.append("ts >= ?")
        params.append(since)
    if until:
        where.append("ts <= ?")
        params.append(until)
    if before_id is not None:
        where.append("id < ?")
        params.append(int(before_id))
    sql = "SELECT id, ts, actor, action, project, target, detail FROM operator_audit"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY id DESC LIMIT ?"
    rows = conn.execute(sql, (*params, limit)).fetchall()
    return [dict(r) for r in rows]


AUDIT_COLUMNS = ("id", "ts", "actor", "action", "project", "target", "detail")


def audit_to_csv(events: list[dict[str, Any]]) -> str:
    import csv
    import io

    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(AUDIT_COLUMNS)
    for e in events:
        w.writerow(["" if e.get(c) is None else e.get(c) for c in AUDIT_COLUMNS])
    return buf.getvalue()


def _hash_token(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def _clean_projects(projects: Any) -> list[str]:
    if projects is None or projects == "":
        return []
    if isinstance(projects, str):
        projects = [x for x in re.split(r"[,\s]+", projects) if x]
    if not isinstance(projects, (list, tuple)):
        raise ValueError("projects must be a list of project names")
    out: list[str] = []
    for x in projects:
        x = str(x).strip()
        if x and x not in out:
            out.append(x)
    return out


def create_token(
    conn: sqlite3.Connection, name: str, scope: str, projects: Any = None
) -> dict[str, Any]:
    name = (name or "").strip()
    allowed = _clean_projects(projects)
    if allowed and scope.strip() == "admin":
        raise ValueError("admin tokens cannot be limited to projects")
    scope = (scope or "").strip()
    if not name:
        raise ValueError("name is required")
    if scope not in TOKEN_SCOPES:
        raise ValueError(f"scope must be one of {', '.join(TOKEN_SCOPES)}")
    secret = _TOKEN_PREFIX + secrets.token_urlsafe(24)
    prefix = secret[:16]
    digest = _hash_token(secret)
    now = _iso(_now())
    cur = conn.execute(
        "INSERT INTO api_tokens (name, token_hash, prefix, scope, created_at, projects) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (name, digest, prefix, scope, now, json.dumps(allowed) if allowed else None),
    )
    conn.commit()
    return {
        "id": cur.lastrowid,
        "name": name,
        "prefix": prefix,
        "scope": scope,
        "projects": allowed,
        "created_at": now,
        "token": secret,
    }


def list_tokens(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT id, name, prefix, scope, created_at, last_used_at, revoked_at, projects "
        "FROM api_tokens ORDER BY id"
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["projects"] = _loads_projects(d.get("projects"))
        out.append(d)
    return out


def _loads_projects(raw: Any) -> list[str]:
    try:
        v = json.loads(raw) if raw else []
    except ValueError:
        return []
    return [str(x) for x in v] if isinstance(v, list) else []


def resolve_token_projects(conn: sqlite3.Connection, provided: str | None) -> list[str] | None:
    """Project allowlist of a presented API token; ``None`` means unrestricted.

    A token row whose list cannot be parsed fails closed (empty list = nothing allowed).
    """
    if not provided:
        return None
    try:
        row = conn.execute(
            "SELECT projects FROM api_tokens WHERE token_hash = ? AND revoked_at IS NULL",
            (_hash_token(provided),),
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    if row is None or not row["projects"]:
        return None
    try:
        v = json.loads(row["projects"])
    except ValueError:
        return []
    return [str(x) for x in v] if isinstance(v, list) and v else []


# Default-deny: a project-scoped token may only call these, and only with ?project= naming
# allowed projects. Every other route (including unscoped lists) is refused.
_PROJECT_SCOPED_GET = ("/api/queue", "/api/history", "/api/audit")


def project_scope_allows(projects: list[str], method: str, path: str, query: dict[str, list[str]]) -> bool:
    if method == "POST" and path.rstrip("/") == "/api/sse-ticket":
        return True
    if method not in ("GET", "HEAD"):
        return False
    if (path.rstrip("/") or "/") not in _PROJECT_SCOPED_GET:
        return False
    wanted = [v for v in query.get("project", [])]
    if not wanted or any(v not in projects for v in wanted):
        return False
    return True


def revoke_token(conn: sqlite3.Connection, token_id: int) -> bool:
    row = conn.execute("SELECT id, revoked_at FROM api_tokens WHERE id = ?", (token_id,)).fetchone()
    if row is None:
        return False
    if row["revoked_at"]:
        return True
    conn.execute(
        "UPDATE api_tokens SET revoked_at = ? WHERE id = ?",
        (_iso(_now()), token_id),
    )
    conn.commit()
    return True


def resolve_access(
    cfg: dict[str, Any],
    conn: sqlite3.Connection,
    provided: str | None,
) -> tuple[str | None, str | None]:
    """Return (scope, via) or (None, None) when the credential is rejected.

    ``via`` is ``legacy`` for config.auth_token (or auth disabled) and ``token``
    for a non-revoked api_tokens row. Workers must keep using the legacy token.
    """
    if not token_required(cfg):
        return "admin", "legacy"
    expected = configured_token(cfg)
    if provided and hmac.compare_digest(provided, expected):
        return "admin", "legacy"
    if not provided:
        return None, None
    digest = _hash_token(provided)
    try:
        row = conn.execute(
            "SELECT id, scope, revoked_at, last_used_at FROM api_tokens WHERE token_hash = ?",
            (digest,),
        ).fetchone()
    except sqlite3.OperationalError:
        return None, None
    if row is None or row["revoked_at"]:
        return None, None
    # Wave C3: throttle last_used_at writes (~1/min) to cut SQLite contention on GETs.
    now = _now()
    try:
        prev = row_get(row, "last_used_at")
    except Exception:
        prev = None
    should_touch = True
    if prev:
        try:
            from datetime import datetime, timezone
            prev_dt = _parse_iso(str(prev))
            if prev_dt.tzinfo is None:
                prev_dt = prev_dt.replace(tzinfo=timezone.utc)
            should_touch = (now - prev_dt).total_seconds() >= 60.0
        except Exception:
            should_touch = True
    if should_touch:
        conn.execute(
            "UPDATE api_tokens SET last_used_at = ? WHERE id = ?",
            (_iso(now), row["id"]),
        )
        conn.commit()
    return str(row["scope"]), "token"


def is_worker_path(path: str) -> bool:
    path = path.rstrip("/") or "/"
    return (
        path.startswith("/api/worker")
        or path == "/api/yield-master"
        or path.startswith("/api/transfer/")
    )


def scope_allows(scope: str | None, method: str, path: str) -> bool:
    if scope == "admin":
        return True
    path = path.rstrip("/") or "/"
    if method == "GET" or method == "HEAD":
        # F-117: raw YAML (may contain env:) is admin-only for scoped tokens.
        if path.startswith("/api/tasks/") and path.endswith("/yaml"):
            return False
        return scope in ("read", "write")
    if path == "/api/sse-ticket" and method == "POST":
        return scope in ("read", "write")
    if scope != "write":
        return False
    if any(path.startswith(prefix.rstrip("/")) and (
        path == prefix.rstrip("/") or path.startswith(prefix)
    ) for prefix in _WRITE_PREFIXES):
        return True
    if path.startswith("/api/groups/") and (path.endswith("/stop") or path.endswith("/retry")):
        return True
    if path.startswith("/api/tasks/") and path.endswith(("/retry", "/patch", "/priority")):
        return True
    return False


def pause_task(conn: sqlite3.Connection, project: str, task_id: str) -> tuple[bool, str | None]:
    """Disable a task (schedule pause). Running tasks are refused."""
    row = get_task(conn, project, task_id)
    if row is None:
        return False, "task not found"
    if row["status"] == "running":
        return False, "cannot pause a running task"
    yaml_path = row["yaml_source"] or ""
    if yaml_path:
        from taskqueue.yaml_patch import patch_task_yaml

        try:
            patch_task_yaml(Path(yaml_path), {"enabled": False}, task_id=task_id)
        except (OSError, ValueError):
            pass  # DB state still changes; the next YAML sync may re-enable it
    from taskqueue.db import _now as _db_now

    conn.execute(
        "UPDATE tasks SET enabled = 0, status = 'disabled', updated_at = ? "
        "WHERE project = ? AND task_id = ?",
        (_db_now(), project, task_id),
    )
    conn.commit()
    return True, None


_RUN_DIFF_FIELDS = (
    "status", "exit_code", "wall_sec", "worker_id", "package_hash", "last_error_code",
    "error_category", "peak_ram_gib", "peak_vram_gib", "peak_cpu_pct", "spec_command",
)


def _loads(text: str | None, default: Any) -> Any:
    try:
        return json.loads(text) if text else default
    except ValueError:
        return default


def run_detail(conn: sqlite3.Connection, run_id: int) -> dict[str, Any] | None:
    """One run with its spec snapshot and parsed metrics; ``None`` if unknown."""
    row = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
    if row is None:
        return None
    d = {k: row[k] for k in row.keys() if k not in ("finalize_token", "log_path")}
    d["args"] = _loads(d.pop("args_json", None), None)
    d["env_keys"] = _loads(d.pop("env_keys_json", None), None)
    d["metrics"] = _loads(d.pop("metrics_json", None), {})
    d["anomaly"] = run_anomaly(conn, d)
    return d


ANOMALY_MIN_SAMPLES = 5
ANOMALY_RATIO = 1.5
_ANOMALY_METRICS = (("peak_ram_gib", "ram"), ("peak_vram_gib", "vram"), ("wall_sec", "wall"))


def run_anomaly(conn: sqlite3.Connection, run: dict[str, Any]) -> dict[str, Any]:
    """Flag peaks/wall time well above what earlier completed runs of the task used.

    The baseline is the p95 of the last 20 completed runs before this one; with fewer than
    ``ANOMALY_MIN_SAMPLES`` of them nothing is flagged. A metric is flagged when it exceeds
    ``ANOMALY_RATIO`` x baseline.
    """
    from taskqueue.estimate import _p95

    rows = conn.execute(
        "SELECT peak_ram_gib, peak_vram_gib, wall_sec FROM runs "
        "WHERE project = ? AND task_id = ? AND run_id < ? AND status = 'completed' "
        "ORDER BY run_id DESC LIMIT 20",
        (run.get("project"), run.get("task_id"), run.get("run_id")),
    ).fetchall()
    out: dict[str, Any] = {"samples": len(rows), "flags": []}
    if len(rows) < ANOMALY_MIN_SAMPLES:
        return out
    for col, label in _ANOMALY_METRICS:
        value = run.get(col)
        base = _p95([float(r[col]) for r in rows if r[col] is not None])
        if value is None or base <= 0:
            continue
        ratio = float(value) / base
        if ratio > ANOMALY_RATIO:
            out["flags"].append(
                {"metric": label, "value": value, "baseline_p95": base, "ratio": round(ratio, 2)}
            )
    return out


def diff_runs(conn: sqlite3.Connection, a: int, b: int) -> dict[str, Any] | None:
    """Field-by-field differences between two runs (``None`` if either is unknown)."""
    ra, rb = run_detail(conn, a), run_detail(conn, b)
    if ra is None or rb is None:
        return None
    changes: dict[str, Any] = {}
    for f in _RUN_DIFF_FIELDS:
        if ra.get(f) != rb.get(f):
            changes[f] = {"a": ra.get(f), "b": rb.get(f)}
    if ra["args"] != rb["args"]:
        changes["args"] = {"a": ra["args"], "b": rb["args"]}
    ea, eb = set(ra["env_keys"] or []), set(rb["env_keys"] or [])
    if ea != eb:
        changes["env_keys"] = {"only_a": sorted(ea - eb), "only_b": sorted(eb - ea)}
    ma = ra["metrics"] if isinstance(ra["metrics"], dict) else {}
    mb = rb["metrics"] if isinstance(rb["metrics"], dict) else {}
    # `_series` (sparkline samples) always differs between runs; compare the values, not the curves.
    mchg = {k: {"a": ma.get(k), "b": mb.get(k)} for k in sorted((set(ma) | set(mb)) - {"_series", "_progress"})
            if ma.get(k) != mb.get(k)}
    if mchg:
        changes["metrics"] = mchg
    return {"a": a, "b": b, "same_task": (ra["project"], ra["task_id"]) == (rb["project"], rb["task_id"]),
            "identical": not changes, "changes": changes,
            "runs": {"a": {k: ra[k] for k in ("run_id", "project", "task_id", "started_at", "finished_at")},
                     "b": {k: rb[k] for k in ("run_id", "project", "task_id", "started_at", "finished_at")}}}
