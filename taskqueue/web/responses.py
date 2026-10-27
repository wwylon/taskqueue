"""Pure response/helpers used by web routes (no Handler)."""
from __future__ import annotations
from taskqueue.jsonutil import json_list

import hashlib
import hmac
import json
import os
import re
import sqlite3
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from http.server import HTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, unquote, urlparse

from taskqueue.auth import check_token, extract_request_token
from taskqueue.capacity import build_capacity
from taskqueue.config import load_projects, validate_config
from taskqueue.db import (
    SCHEMA_VERSION,
    _PROJECT_LIMIT_FIELDS,
    blocked_projects,
    get_all_project_limits,
    get_config,
    holds_master_lease,
    open_db,
    set_project_frozen,
    set_config,
    set_project_limit,
)
from taskqueue._db_tasks import (
    bulk_prior_run_seconds,
    check_ownership,
    enable_task,
    get_queued_tasks,
    count_queued_tasks,
    get_running_tasks,
    get_task,
    lock_contention_summary,
    lock_occupancy,
    request_stop_with_control,
    retry_task,
    set_task_priority,
    set_task_status,
)
from taskqueue._db_runs import force_task, get_recent_runs
from taskqueue.diagnose import build_diagnose
from taskqueue.notify import notify_log_path
from taskqueue.queries import list_attention
from taskqueue.reports import build_report
from taskqueue.resources import ram_used_gib, task_requires_gpu
from taskqueue.win_console import hide_console_check_output_kwargs
from taskqueue.rows import row_get

def _read_git_sha() -> str:
    try:
        import subprocess as _sp
        return _sp.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            stderr=_sp.DEVNULL,
            cwd=str(Path(__file__).resolve().parent.parent),
            **hide_console_check_output_kwargs(),
        ).decode().strip()
    except Exception:
        return "unknown"

_GIT_SHA: str = _read_git_sha()

_STATIC = Path(__file__).resolve().parent.parent / "static"

# F-146: cache "all projects ever seen" per DB path — refreshed at most once per 10s.
_projects_cache_by_db: dict[str, tuple[float, list[str]]] = {}
_PROJECTS_CACHE_TTL = 10.0
_projects_lock = threading.Lock()
# Back-compat aliases for handler/deps that still reference the old names.
_projects_cache: list[str] = []
_projects_cache_ts: float = 0.0


_wheel_sha_cache: dict[str, Any] = {"mtime": None, "sha256": None}
_auth_cfg_cache: dict[str, Any] = {"ts": 0.0, "cfg": None, "db": None}
_AUTH_CFG_TTL = 5.0
_auth_cfg_lock = threading.Lock()

def _get_auth_config(conn: sqlite3.Connection, db_path: Path | str | None = None) -> dict[str, Any]:
    """Cached get_config for auth hot path (5s TTL), keyed by DB path."""
    import time as _time
    now = _time.monotonic()
    db_key = str(db_path) if db_path is not None else ""
    with _auth_cfg_lock:
        cached = _auth_cfg_cache["cfg"]
        if (
            cached is not None
            and _auth_cfg_cache.get("db") == db_key
            and now - float(_auth_cfg_cache["ts"]) < _AUTH_CFG_TTL
        ):
            return cached
    cfg = get_config(conn)
    with _auth_cfg_lock:
        _auth_cfg_cache["cfg"] = cfg
        _auth_cfg_cache["ts"] = _time.monotonic()
        _auth_cfg_cache["db"] = db_key
    return cfg


_TASK_SECRET_FIELDS = ("env", "args_override", "yaml_source")


def _task_dict(row: sqlite3.Row, *, strip_secrets: bool = False) -> dict[str, Any]:
    d = dict(row)
    for field in (
        "args", "resume_args", "requires_paths", "resources", "depends_on",
        "depends_on_any", "requires", "tags", "requires_services",
    ):
        raw = d.get(field)
        if isinstance(raw, str):
            try:
                d[field] = json.loads(raw or ("{}" if field in ("resources", "requires") else "[]"))
            except Exception:
                if field in ("depends_on", "depends_on_any"):
                    d[field] = []
        elif field in ("depends_on", "depends_on_any") and raw is None:
            d[field] = []
    ys = d.get("yaml_source") or ""
    m = re.match(r"^(\d+)", Path(ys).name) if ys else None
    d["yaml_num"] = m.group(1) if m else ""
    d["requires_gpu"] = task_requires_gpu(d.get("resources"))
    if strip_secrets:
        # F-015: env may contain API keys; yaml_source leaks paths; args_override leaks args
        for f in _TASK_SECRET_FIELDS:
            d.pop(f, None)
    return d


def _attach_progress_health(d: dict[str, Any], *, run=None, metrics_json: Any = None) -> None:
    """``d['progress_health']`` = {age_sec, stalled, eta_sec} from the live run or the stored ``_progress``
    (remote runs). Omitted when there is no progress timing yet."""
    import time as _time

    from taskqueue.log_issues import progress_health

    timing = getattr(run, "_progress_timing", None) if run is not None else None
    if timing is None and metrics_json:
        try:
            mj = metrics_json if isinstance(metrics_json, dict) else json.loads(metrics_json)
            timing = mj.get("_progress") if isinstance(mj, dict) else None
        except (ValueError, TypeError):
            timing = None
    health = progress_health(timing, d.get("progress_pct"), _time.time())
    if health:
        d["progress_health"] = health


def _attach_log_field(d: dict[str, Any], *, run=None, metrics_json: Any = None) -> None:
    """Set ``d['log']`` from a live ActiveRun or persisted metrics_json (zeros omitted)."""
    from taskqueue.log_issues import log_from_metrics_json, nonzero_log

    if run is not None:
        lg = nonzero_log(getattr(run, "log_counts", None))
        if lg:
            d["log"] = lg
        return
    lg = nonzero_log(log_from_metrics_json(metrics_json))
    if lg:
        d["log"] = lg


def _slim_resources(res: Any) -> dict[str, Any] | None:
    if not res or not isinstance(res, dict):
        return None
    out: dict[str, Any] = {}
    for k in ("cpu_cores", "ram_gib", "gpu_vram_gib"):
        if res.get(k) is not None:
            out[k] = res[k]
    locks = res.get("locks")
    if isinstance(locks, list) and locks:
        out["locks"] = [str(x) for x in locks]
    return out or None


def _attach_lock_waiting_on(tasks: list[dict[str, Any]], occupancy: dict[str, dict]) -> None:
    """Mutate queue task dicts with lock_waiting_on when blocked on tq.block.mutex."""
    for d in tasks:
        if d.get("last_error_code") != "tq.block.mutex":
            continue
        res = d.get("resources") or {}
        locks = res.get("locks") if isinstance(res, dict) else None
        if not isinstance(locks, list) or not locks:
            continue
        waiting: list[dict[str, Any]] = []
        for lk in locks:
            info = occupancy.get(str(lk)) or {}
            waiting.append({
                "lock": str(lk),
                "holder": info.get("holder"),
                "holder_started_at": info.get("holder_started_at"),
            })
        if waiting:
            d["lock_waiting_on"] = waiting


def _queue_row_dict(d: dict[str, Any]) -> dict[str, Any]:
    """Slim task projection for web UI list views."""
    keys = (
        "project", "task_id", "status", "priority", "yaml_num", "description",
        "tags", "depends_on", "depends_on_any", "spawned_by", "grp", "block_reason",
        "last_error_code",
        "enqueued_at", "started_at", "finished_at", "run_id", "progress_pct",
        "elapsed_sec", "prior_elapsed_sec", "idle_sec", "active_idle_sec", "peak_ram_gib", "peak_vram_gib",
        "peak_cpu_pct", "last_worker_id", "log", "processed_at", "requires_gpu",
        "last_log_line", "lock_waiting_on", "progress_health", "dep_status", "missing_deps",
    )
    out = {k: d[k] for k in keys if k in d and d[k] is not None}
    slim = _slim_resources(d.get("resources"))
    if slim:
        out["resources"] = slim
    return out


def _live_run_dict(d: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "project", "task_id", "run_id", "status", "progress_pct", "elapsed_sec",
        "prior_elapsed_sec", "idle_sec", "active_idle_sec", "last_log_line", "peak_ram_gib", "peak_vram_gib",
        "peak_cpu_pct", "current_ram_gib", "current_vram_gib", "current_cpu_pct",
        "current_gpu_util_pct", "avg_gpu_util_pct", "worker_id", "worker_last_seen_age_sec",
        "started_at", "log", "progress_health",
    )
    return {k: d[k] for k in keys if k in d and d[k] is not None}


def _history_row_dict(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    d = dict(row)
    keys = (
        "run_id", "project", "task_id", "started_at", "finished_at", "status",
        "exit_code", "worker_id", "peak_ram_gib", "peak_vram_gib", "stopped_by", "error_category",
        "anomalies",  # F-113: Unusual badges through dashboard tab=history
    )
    return {k: d[k] for k in keys if k in d}


def _slim_status_payload(status: dict[str, Any]) -> dict[str, Any]:
    out = dict(status)
    out["running"] = [_live_run_dict(t) for t in (status.get("running") or [])]
    return out


def _safe_resolve(project_root: Path, rel_path: str) -> Path | None:
    """Resolve rel_path under project_root; return None on escape / invalid path."""
    try:
        target = (project_root / rel_path).resolve()
        target.relative_to(project_root.resolve())
        return target
    except ValueError:
        return None


def relativize_requires_paths(paths: list[Any], project_root: Path) -> list[str]:
    """Rewrite absolute paths under project_root to relative (for remote workers)."""
    root = project_root.resolve()
    out: list[str] = []
    for p in paths or []:
        path = Path(str(p))
        try:
            rel = path.resolve().relative_to(root)
            out.append(rel.as_posix())
        except (ValueError, OSError):
            out.append(str(p).replace("\\", "/"))
    return out


def _redact_config(cfg: dict[str, Any]) -> dict[str, Any]:
    out = dict(cfg)
    if out.get("auth_token"):
        out["auth_token"] = "***"
    if out.get("notify_url"):
        out["notify_url"] = _redact_url(str(out["notify_url"]))
    return out


def _redact_url(url: str) -> str:
    """Keep scheme + host only: webhook URLs carry their secret in userinfo, path or query."""
    from urllib.parse import urlsplit

    try:
        parts = urlsplit(url)
        host = parts.hostname
        port = parts.port
    except ValueError:
        return "***"
    if not parts.scheme or not host:
        return "***"
    return f"{parts.scheme}://{host}{':%d' % port if port else ''}/***"


_QUEUE_SORT_COLS = {
    "status": "t.status",
    "task_id": "t.task_id",
    "project": "t.project",
    "priority": "t.priority",
    "enqueued_at": "t.enqueued_at",
    "started_at": "lr.started_at",
    "finished_at": "lr.finished_at",
    "description": "t.description",
    "deps": "t.status",  # special-cased in _get_queue_response
    "order": "t.status",  # special-cased — Timeline forecast order
}

_UNGROUPED = "__ungrouped__"
_INCOMPLETE_FOR_DEPS = frozenset({
    "queued", "running", "failed", "interrupted", "suspended", "stopped", "unknown",
    "cancelled",
})

_STATUS_RANK = (
    "CASE t.status"
    " WHEN 'running' THEN 0"
    " WHEN 'interrupted' THEN 1"
    " WHEN 'failed' THEN 2"
    " WHEN 'unknown' THEN 2"
    " WHEN 'cancelled' THEN 2"
    " WHEN 'queued' THEN 3"
    " WHEN 'stopped' THEN 4"
    " WHEN 'completed' THEN 5"
    " WHEN 'disabled' THEN 6"
    " ELSE 7 END"
)

def _queue_order_by(sort: str, sort_dir: str) -> str:
    col = _QUEUE_SORT_COLS.get(sort, "t.status")
    direction = "ASC" if sort_dir == "asc" else "DESC"
    if sort == "status":
        return f"{_STATUS_RANK} {direction}, t.updated_at DESC"
    if sort == "queue_pos":
        return f"t.priority DESC, t.enqueued_at {direction}"
    if sort == "yaml_num":
        return f"t.yaml_num {direction}, t.yaml_source {direction}"
    return f"{col} {direction}"


def _dep_ref_key(owner_project: str, dep: str) -> str:
    """Normalize depends_on entry to project/task_id."""
    if "/" in dep:
        return dep
    return f"{owner_project}/{dep}"


def _blocker_scores(rows: list[Any]) -> dict[str, int]:
    """Count how many incomplete tasks list each task as a dependency."""
    incomplete: set[str] = set()
    for row in rows:
        if row["status"] in _INCOMPLETE_FOR_DEPS:
            incomplete.add(f"{row['project']}/{row['task_id']}")
    scores: dict[str, int] = {}
    for row in rows:
        if row["status"] not in _INCOMPLETE_FOR_DEPS:
            continue
        try:
            deps = json_list(row["depends_on"])
        except Exception:
            deps = []
        for dep in deps:
            key = _dep_ref_key(row["project"], str(dep))
            if key in incomplete:
                scores[key] = scores.get(key, 0) + 1
        try:
            deps_any = (
                json_list(row["depends_on_any"])
                if "depends_on_any" in row.keys()
                else []
            )
        except Exception:
            deps_any = []
        for dep in deps_any if isinstance(deps_any, list) else []:
            key = _dep_ref_key(row["project"], str(dep))
            if key in incomplete:
                scores[key] = scores.get(key, 0) + 1
    return scores


_STATUS_RANK_MAP = {
    "running": 0,
    "interrupted": 1,
    "failed": 2,
    "unknown": 2,
    "queued": 3,
    "stopped": 4,
    "completed": 5,
    "disabled": 6,
}


def _get_queue_response(
    conn: sqlite3.Connection,
    *,
    project: str | None = None,
    status: str | None = None,
    search: str = "",
    worker: str | None = None,
    tag: str | None = None,
    group: str | None = None,
    started_by: str | None = None,
    hide_blocked: bool = False,
    new_failed: bool = False,
    ack_at: str | None = None,
    limit: int = 200,
    offset: int = 0,
    sort: str = "status",
    sort_dir: str = "asc",
    active_runs: dict | None = None,
    timings: dict[str, float] | None = None,
) -> dict[str, Any]:
    """W4.1: core queue query — extracted for testability.

    Returns {"tasks": [...], "total": int, "limit": int, "offset": int}.
    active_runs dict is optional; live elapsed/progress fields omitted when absent.
    ``group=__ungrouped__`` filters tasks with empty grp.
    ``hide_blocked`` drops every task of a frozen or paused project —
    the UI's "active" view. Queued waits (``tq.block.*``) stay visible.
    ``new_failed`` keeps only failed/unknown tasks that ended after ``ack_at``
    (the health badge's "new failed" set; ``ack_at=None`` keeps every failure).
    ``sort=deps`` ranks by how often incomplete peers depend on the task.
    ``sort=order`` ranks by Timeline forecast start order (advisory).
    ``started_by`` scopes to tasks attributed to that operator (R8 mine view).
    When ``timings`` is provided, fills ``queue_sql`` / ``queue_enrich`` ms.
    """
    _t_sql0 = time.perf_counter()
    where: list[str] = []
    params: list[Any] = []
    if project:
        where.append("t.project = ?"); params.append(project)
    if started_by:
        where.append("t.started_by = ?"); params.append(started_by)
    if status:
        where.append("t.status = ?"); params.append(status)
    if hide_blocked:
        blocked = blocked_projects(conn)
        if blocked:
            where.append(f"t.project NOT IN ({','.join('?' * len(blocked))})")
            params.extend(blocked)
    if new_failed and ack_at is not None:
        # Same rule as health.compute_health: a run's finish time, falling back to updated_at.
        where.append(
            "NOT (t.status IN ('failed', 'unknown') AND "
            "COALESCE((SELECT r.finished_at FROM runs r WHERE r.run_id = t.last_run_id), "
            "t.finished_at, t.updated_at, '') "
            "<= ?)"
        )
        params.append(ack_at)
    if search:
        pat = f"%{search}%"
        where.append("(t.task_id LIKE ? OR t.description LIKE ?)"); params.extend([pat, pat])
    if worker:
        where.append("(lr.worker_id = ?)"); params.append(worker)
    if tag:
        where.append("(t.tags LIKE ?)"); params.append(f'%"{tag}"%')
    if group is not None and group != "":
        if group == _UNGROUPED:
            where.append("(t.grp = '' OR t.grp IS NULL)")
        else:
            where.append("t.grp = ?"); params.append(group)
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""

    # Prefer tasks.last_run_id (maintained on run create/finalize) over a correlated MAX(run_id).
    _lr_join = "LEFT JOIN runs lr ON lr.run_id = t.last_run_id"
    _lr_cols = (
        "lr.run_id AS lr_run_id, lr.started_at AS lr_started_at, "
        "lr.peak_ram_gib AS lr_peak_ram_gib, lr.peak_vram_gib AS lr_peak_vram_gib, "
        "lr.peak_cpu_pct AS lr_peak_cpu_pct, lr.worker_id AS lr_worker_id, "
        "lr.metrics_json AS lr_metrics_json, lr.progress_pct AS lr_progress_pct"
    )
    # Count does not need last-run columns unless filtering by worker (lr.worker_id).
    if worker:
        count_sql = (
            f"SELECT COUNT(*) FROM tasks t {_lr_join} {where_sql}"
        )
    else:
        count_sql = f"SELECT COUNT(*) FROM tasks t {where_sql}"
    total = conn.execute(count_sql, params).fetchone()[0]

    use_deps_sort = sort == "deps"
    use_order_sort = sort == "order"
    # Forecast map is expensive; only compute it when actually needed for sort=order.
    forecast_map: dict[tuple[str, str], dict[str, float | int]] = {}
    if use_order_sort:
        try:
            from taskqueue.timeline_forecast import forecast_order_map
            forecast_map = forecast_order_map(conn, hours=24, project=project)
        except Exception:
            forecast_map = {}

    data_sql = (
        f"SELECT t.*, {_lr_cols} "
        f"FROM tasks t {_lr_join} "
        f"{where_sql} "
    )
    if use_deps_sort:
        # Sort on slim columns (no last-run join, no t.*), then load full rows for the page only.
        # The worker filter references lr.*, so that case keeps the full-row path.
        slim = not worker
        if slim:
            rows = list(conn.execute(
                "SELECT t.project, t.task_id, t.status, t.priority, t.enqueued_at, "
                f"t.depends_on, t.depends_on_any FROM tasks t {where_sql}",
                params,
            ).fetchall())
        else:
            rows = list(conn.execute(data_sql, params).fetchall())
        scores = _blocker_scores(rows)
        dir_mul = 1 if sort_dir == "asc" else -1

        def _deps_key(row: Any) -> tuple:
            key = f"{row['project']}/{row['task_id']}"
            # Higher blocker score first regardless of sort_dir on "deps"
            # (asc/desc flips secondary keys only via dir_mul on status rank).
            score = scores.get(key, 0)
            rank = _STATUS_RANK_MAP.get(row["status"], 7)
            prio = int(row["priority"] or 0)
            enq = row["enqueued_at"] or ""
            return (-score, rank * dir_mul, -prio, enq)

        rows.sort(key=_deps_key)
        rows = rows[offset: offset + limit]
        if slim and rows:
            keys = [(r["project"], r["task_id"]) for r in rows]
            full = conn.execute(
                f"SELECT t.*, {_lr_cols} "
                f"FROM tasks t {_lr_join} "
                "WHERE " + " OR ".join("(t.project = ? AND t.task_id = ?)" for _ in keys),
                [v for k in keys for v in k],
            ).fetchall()
            by_key = {(r["project"], r["task_id"]): r for r in full}
            rows = [by_key[k] for k in keys if k in by_key]
    elif use_order_sort:
        rows = list(conn.execute(data_sql, params).fetchall())
        dir_mul = 1 if sort_dir == "asc" else -1

        def _order_key(row: Any) -> tuple:
            info = forecast_map.get((row["project"], row["task_id"]))
            # Missing forecast → sort last for asc, first for desc via large sentinel.
            ord_val = int(info["order"]) if info else 10**9
            start = float(info["start_sec"]) if info else 10**9
            return (ord_val * dir_mul, start * dir_mul, row["project"], row["task_id"])

        rows.sort(key=_order_key)
        rows = rows[offset: offset + limit]
    else:
        data_sql += (
            f"ORDER BY {_queue_order_by(sort, sort_dir)} "
            f"LIMIT ? OFFSET ?"
        )
        rows = conn.execute(data_sql, params + [limit, offset]).fetchall()

    if timings is not None:
        timings["queue_sql"] = round((time.perf_counter() - _t_sql0) * 1000.0)

    _t_en0 = time.perf_counter()
    result = []
    ar = active_runs or {}
    open_for_prior: dict[tuple[str, str], Any] = {}
    for row in rows:
        run = ar.get((row["project"], row["task_id"]))
        if run is not None:
            open_for_prior[(row["project"], row["task_id"])] = {"run_id": run.run_id}
    priors = bulk_prior_run_seconds(conn, open_for_prior) if open_for_prior else {}
    for row in rows:
        d = _task_dict(row, strip_secrets=True)
        for _k in (
            "lr_run_id", "lr_started_at", "lr_peak_ram_gib", "lr_peak_vram_gib",
            "lr_peak_cpu_pct", "lr_worker_id", "lr_metrics_json", "lr_progress_pct",
        ):
            d.pop(_k, None)
        run = ar.get((row["project"], row["task_id"]))
        d["started_at"] = row["lr_started_at"]
        d["last_worker_id"] = row["lr_worker_id"]
        fo = forecast_map.get((row["project"], row["task_id"]))
        if fo:
            d["forecast_order"] = int(fo["order"])
            d["forecast_start_sec"] = float(fo["start_sec"])
        else:
            d["forecast_order"] = None
            d["forecast_start_sec"] = None
        if run:
            d["elapsed_sec"] = round(run.elapsed_sec(), 1)
            d["progress_pct"] = run.progress_pct
            d["idle_sec"] = round(run.idle_sec(), 1)
            d["active_idle_sec"] = round(run.active_idle_sec(), 1)
            d["run_id"] = run.run_id
            _attach_progress_health(d, run=run)
            d["prior_elapsed_sec"] = priors.get((row["project"], row["task_id"]), 0.0)
            d["peak_ram_gib"] = round(run.peak_ram_gib, 2)
            d["peak_vram_gib"] = round(run.peak_vram_gib, 2)
            d["peak_cpu_pct"] = round(run.peak_cpu_pct, 1)
            _attach_log_field(d, run=run)
        else:
            d["run_id"] = row["lr_run_id"]
            d["peak_ram_gib"] = round(row["lr_peak_ram_gib"], 2) if row["lr_peak_ram_gib"] is not None else None
            d["peak_vram_gib"] = round(row["lr_peak_vram_gib"], 2) if row["lr_peak_vram_gib"] is not None else None
            d["peak_cpu_pct"] = round(row["lr_peak_cpu_pct"], 1) if row["lr_peak_cpu_pct"] is not None else None
            # Last run's progress for failed / interrupted / requeued rows (UI decides when to show it).
            d["progress_pct"] = row_get(row, "lr_progress_pct")
            _attach_log_field(d, metrics_json=row_get(row, "lr_metrics_json"))
        result.append(d)
    if any(d.get("last_error_code") == "tq.block.mutex" for d in result):
        _attach_lock_waiting_on(result, lock_occupancy(conn))

    from taskqueue._db_tasks import run_counts
    _rc = run_counts(conn, [(d["project"], d["task_id"]) for d in result])
    for d in result:
        d["run_number"] = _rc.get((d["project"], d["task_id"]), 0)

    # Attach missing_deps per task and expose broken_deps summary.
    try:
        from taskqueue.queries import find_broken_deps_cached
        broken = find_broken_deps_cached(conn, project=project)
        broken_by_id = {b["task_id"]: b for b in broken}
    except Exception:
        broken = []
        broken_by_id = {}
    for d in result:
        b = broken_by_id.get(d["task_id"])
        d["missing_deps"] = b["missing"] if b else []

    # Dependency statuses straight from the DB: the UI only holds one queue page, so it cannot
    # tell a completed dep that sits on another page from an unknown one.
    try:
        from taskqueue.queries import _fetch_status_map, resolve_dep_ref

        def _refs(raw: Any) -> list[str]:
            if isinstance(raw, str):
                try:
                    raw = json.loads(raw)
                except Exception:
                    return []
            return [str(x) for x in raw] if isinstance(raw, list) else []

        per_task: dict[int, list[tuple[str, tuple[str, str]]]] = {}
        needed: set[tuple[str, str]] = set()
        for i, d in enumerate(result):
            pairs = []
            for ref in _refs(d.get("depends_on")) + _refs(d.get("depends_on_any")):
                dp, di, _lbl = resolve_dep_ref(d["project"], ref)
                pairs.append((ref, (dp, di)))
                needed.add((dp, di))
            per_task[i] = pairs
        smap = _fetch_status_map(conn, needed) if needed else {}
        for i, d in enumerate(result):
            d["dep_status"] = {ref: smap[k] for ref, k in per_task[i] if k in smap}
    except Exception:
        for d in result:
            d.setdefault("dep_status", {})

    if timings is not None:
        timings["queue_enrich"] = round((time.perf_counter() - _t_en0) * 1000.0)

    return {
        "tasks": result,
        "total": total,
        "limit": limit,
        "offset": offset,
        "broken_deps": broken,
    }


def format_server_timing(phases: dict[str, float]) -> str:
    """Build a Server-Timing header value from phase name → duration ms."""
    parts: list[str] = []
    for name, ms in phases.items():
        safe = re.sub(r"[^A-Za-z0-9_-]", "_", str(name))
        parts.append(f"{safe};dur={float(ms):.0f}")
    return ", ".join(parts)


def _get_groups_response(
    conn: sqlite3.Connection,
    *,
    project: str | None = None,
) -> dict[str, Any]:
    """W5.3: Pure data layer for /api/groups — testable outside make_handler."""
    if project:
        rows = conn.execute(
            "SELECT grp, project, status, COUNT(*) as n FROM tasks "
            "WHERE project = ? GROUP BY grp, project, status",
            (project,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT grp, project, status, COUNT(*) as n FROM tasks "
            "GROUP BY grp, project, status"
        ).fetchall()
    # "Ungrouped" (empty name) is split per project: it is only meaningful inside one project, and the
    # UI scopes its stop/retry buttons with ``project`` (an unscoped ungrouped op hit every project).
    groups: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        grp = row["grp"] or ""
        key = (grp, row["project"] if grp == "" else "")
        if key not in groups:
            groups[key] = {
                "name": grp,
                "project": row["project"] if grp == "" else None,
                "total": 0,
                "by_status": {},
                "projects": set(),
            }
        g = groups[key]
        g["by_status"][row["status"]] = g["by_status"].get(row["status"], 0) + int(row["n"])
        g["total"] += int(row["n"])
        g["projects"].add(row["project"])
    out = []
    for g in groups.values():
        g["projects"] = sorted(g["projects"])
        out.append(g)
    return {"groups": out}


def _get_suites_response(
    conn: sqlite3.Connection,
    *,
    project: str | None = None,
) -> dict[str, Any]:
    """List named suites with per-status counts (empty suite omitted)."""
    if project:
        rows = conn.execute(
            "SELECT suite, project, status, COUNT(*) as n FROM tasks "
            "WHERE project = ? AND suite != '' AND suite IS NOT NULL "
            "GROUP BY suite, project, status",
            (project,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT suite, project, status, COUNT(*) as n FROM tasks "
            "WHERE suite != '' AND suite IS NOT NULL "
            "GROUP BY suite, project, status"
        ).fetchall()
    suites: dict[str, dict[str, Any]] = {}
    for row in rows:
        name = row["suite"] or ""
        if not name:
            continue
        if name not in suites:
            suites[name] = {"name": name, "total": 0, "by_status": {}, "projects": set()}
        suites[name]["by_status"][row["status"]] = (
            suites[name]["by_status"].get(row["status"], 0) + int(row["n"])
        )
        suites[name]["total"] += int(row["n"])
        suites[name]["projects"].add(row["project"])
    out = []
    for s in suites.values():
        s["projects"] = sorted(s["projects"])
        out.append(s)
    return {"suites": out}


def _get_cost_response(conn: sqlite3.Connection) -> dict[str, Any]:
    """W5.3: Pure data layer for /api/cost — testable outside make_handler."""
    rows = conn.execute(
        """
        SELECT r.project AS project,
               SUM(
                   MAX(0, unixepoch(r.finished_at) - unixepoch(r.started_at))
                   * COALESCE(json_extract(t.resources, '$.cpu_cores'), 1)
               ) / 60.0 AS cpu_minutes,
               COUNT(*) AS task_count,
               SUM(CASE WHEN r.status = 'failed' THEN 1 ELSE 0 END) AS failed_count
        FROM (
            SELECT project, task_id, started_at, finished_at, status
            FROM runs
            WHERE finished_at IS NOT NULL AND started_at IS NOT NULL
            ORDER BY run_id DESC
            LIMIT 10000
        ) r
        LEFT JOIN tasks t ON t.project = r.project AND t.task_id = r.task_id
        GROUP BY r.project
        """
    ).fetchall()
    by_project: dict[str, Any] = {}
    total_cpu_min = 0.0
    total_tasks = 0
    for row in rows:
        proj = row["project"] or "unknown"
        cpu_min = float(row["cpu_minutes"] or 0.0)
        task_count = int(row["task_count"] or 0)
        failed_count = int(row["failed_count"] or 0)
        by_project[proj] = {
            "cpu_minutes": round(cpu_min, 1),
            "task_count": task_count,
            "failed_count": failed_count,
        }
        total_cpu_min += cpu_min
        total_tasks += task_count
    return {
        "by_project": by_project,
        "total_cpu_minutes": round(total_cpu_min, 1),
        "total_tasks": total_tasks,
    }


def _get_usage_response(
    conn: sqlite3.Connection,
    *,
    project: str | None = None,
    days: int = 7,
    cutoff_ts: int | None = None,
) -> dict[str, Any]:
    """W5.3: Pure data layer for /api/usage — testable outside make_handler."""
    import time as _time
    if cutoff_ts is None:
        cutoff_ts = int(_time.time()) - days * 86400
    if project:
        rows = conn.execute(
            "SELECT ts, project, operator, cpu_seconds, gpu_seconds "
            "FROM usage_snapshot WHERE ts >= ? AND project = ? ORDER BY ts",
            (cutoff_ts, project),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT ts, project, operator, cpu_seconds, gpu_seconds "
            "FROM usage_snapshot WHERE ts >= ? ORDER BY ts",
            (cutoff_ts,),
        ).fetchall()
    by_project: dict[str, list[dict]] = {}
    projects_seen: set[str] = set()
    for row in rows:
        proj = row["project"]
        projects_seen.add(proj)
        if proj not in by_project:
            by_project[proj] = []
        by_project[proj].append({
            "ts": row["ts"],
            "cpu_hours": round(float(row["cpu_seconds"]) / 3600.0, 4),
            "gpu_hours": round(float(row["gpu_seconds"]) / 3600.0, 4),
        })
    from taskqueue.db import get_all_project_limits, gpu_hours_today

    limits = get_all_project_limits(conn)
    gpu_today: dict[str, dict] = {}
    for name in sorted(projects_seen | set(limits)):
        if project and name != project:
            continue
        cap = float(limits.get(name, {}).get("max_gpu_hours_day", 0) or 0)
        used = gpu_hours_today(conn, name)
        if used > 0 or cap > 0:
            gpu_today[name] = {
                "used_hours": round(used, 3),
                "limit_hours": cap,
                "remaining_hours": round(max(cap - used, 0.0), 3) if cap > 0 else None,
            }
    return {"by_project": by_project, "projects": sorted(projects_seen), "gpu_today": gpu_today}


_DEPS_MODES = frozenset({"active", "attention", "all"})
_DEPS_ACTIVE_STATUSES = frozenset({
    "queued", "running", "failed", "stopped", "interrupted", "suspended", "unknown",
})
_DEPS_ATTENTION_STATUSES = frozenset({"failed", "stopped", "interrupted", "unknown", "cancelled"})


def _deps_is_blocked_queued(node: dict[str, Any]) -> bool:
    if node.get("status") != "queued":
        return False
    if node.get("block_reason"):
        return True
    return str(node.get("last_error_code") or "").startswith("tq.block.")


def _apply_deps_mode(
    nodes: list[dict[str, Any]],
    edges: list[dict[str, Any]],
    mode: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Filter deps graph to incomplete chains + direct upstream stubs.

    ``mode``:
      - ``all`` — unchanged
      - ``active`` — non-terminal statuses (+ direct upstream stubs)
      - ``attention`` — failed/stopped/interrupted/unknown + blocked-queued (+ stubs)
    Raises ``ValueError`` for unknown mode.
    """
    if mode not in _DEPS_MODES:
        raise ValueError(f"invalid deps mode: {mode!r}")
    if mode == "all":
        return nodes, edges

    kept: set[str] = set()
    for n in nodes:
        st = n.get("status") or ""
        if mode == "active":
            if st in _DEPS_ACTIVE_STATUSES:
                kept.add(n["id"])
        elif st in _DEPS_ATTENTION_STATUSES or _deps_is_blocked_queued(n):
            kept.add(n["id"])

    stub_ids: set[str] = set()
    for e in edges:
        if e["to"] in kept and e["from"] not in kept:
            stub_ids.add(e["from"])

    node_ids = {n["id"] for n in nodes}
    keep_all = (kept | stub_ids) & node_ids
    out_nodes: list[dict[str, Any]] = []
    for n in nodes:
        if n["id"] not in keep_all:
            continue
        if n["id"] in stub_ids:
            n["stub"] = True
        out_nodes.append(n)
    out_edges = [e for e in edges if e["from"] in keep_all and e["to"] in keep_all]
    return out_nodes, out_edges


def deps_to_dot(graph: dict[str, Any]) -> str:
    """Render a ``_get_deps_response`` result as Graphviz DOT (every id quoted)."""

    def q(text: Any) -> str:
        return '"' + str(text).replace("\\", "\\\\").replace('"', '\\"') + '"'

    lines = ["digraph tq_deps {", "  rankdir=LR;"]
    for n in graph.get("nodes", []):
        label = q(n["id"])[1:-1] + "\\n" + q(n.get("status", ""))[1:-1]
        attrs = [f'label="{label}"']
        if n.get("stub"):
            attrs.append("style=dashed")
        lines.append(f"  {q(n['id'])} [{', '.join(attrs)}];")
    for e in graph.get("edges", []):
        attrs = []
        health = e.get("health")
        if health == "dead":
            attrs.append("color=red")
        elif health == "wait":
            attrs.append("style=dashed")
        if e.get("kind") == "any":
            attrs.append('label="any"')
        suffix = f" [{', '.join(attrs)}]" if attrs else ""
        lines.append(f"  {q(e['from'])} -> {q(e['to'])}{suffix};")
    lines.append("}")
    return "\n".join(lines) + "\n"


DEPS_MAX_NODES = 500


def _get_deps_response(
    conn: sqlite3.Connection,
    *,
    project: str | None = None,
    focus: str | None = None,
    mode: str = "all",
    suite: str | None = None,
) -> dict[str, Any]:
    """W5.3: Pure data layer for /api/deps — testable outside make_handler.

    Optional ``focus`` is ``task_id`` (when ``project`` set) or ``project/task_id``.
    When set, returns the focused node plus transitive ancestors and descendants.
    Optional ``mode`` (``all``|``active``|``attention``) filters after focus prune.
    Optional ``suite`` keeps only tasks whose suite field matches.
    """
    if project:
        rows = conn.execute(
            "SELECT task_id, status, description, depends_on, depends_on_any, spawned_by, "
            "block_reason, last_error_code, priority, transfer, yaml_num, "
            "tags, grp, suite "
            "FROM tasks WHERE project = ? ORDER BY priority DESC, task_id",
            (project,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT t.task_id, t.status, t.description, t.depends_on, t.depends_on_any, "
            "t.spawned_by, t.project, "
            "t.block_reason, t.last_error_code, t.priority, t.transfer, t.yaml_num, "
            "t.tags, t.grp, t.suite "
            "FROM tasks t ORDER BY t.priority DESC, t.task_id"
        ).fetchall()

    def _health(status: str) -> str:
        if status == "completed":
            return "ok"
        if status in ("failed", "stopped", "interrupted", "disabled", "missing"):
            return "dead"
        return "wait"

    status_by_id: dict[str, str] = {}
    nodes = []
    edges = []
    for row in rows:
        tid = row["task_id"]
        full_id = (f"{row['project']}/{tid}") if not project and "project" in row.keys() else tid
        try:
            transfer_raw = row["transfer"] if "transfer" in row.keys() else "[]"
            transfer = json_list(transfer_raw)
        except Exception:
            transfer = []
        try:
            tags = json_list(row["tags"]) if "tags" in row.keys() else []
        except Exception:
            tags = []
        status = row["status"]
        status_by_id[full_id] = status
        spawned = ""
        if "spawned_by" in row.keys() and row["spawned_by"]:
            spawned = str(row["spawned_by"])
        nodes.append({
            "id": full_id,
            "status": status,
            "description": row["description"] or "",
            "block_reason": row["block_reason"] or "",
            "last_error_code": row["last_error_code"] or "",
            "priority": row["priority"] if "priority" in row.keys() else 50,
            "transfer": transfer,
            "yaml_num": row_get(row, "yaml_num"),
            "tags": tags if isinstance(tags, list) else [],
            "grp": (row["grp"] if "grp" in row.keys() else "") or "",
            "suite": (row["suite"] if "suite" in row.keys() else "") or "",
            "spawned_by": spawned or None,
        })
        for col, kind in (("depends_on", "all"), ("depends_on_any", "any")):
            try:
                raw = row[col] if col in row.keys() else "[]"
                deps = json_list(raw)
                for dep in (deps if isinstance(deps, list) else []):
                    if "/" in str(dep):
                        dep_id = str(dep)
                    elif not project and "project" in row.keys():
                        dep_id = f"{row['project']}/{dep}"
                    else:
                        dep_id = str(dep)
                    edge = {"from": dep_id, "to": full_id}
                    if kind == "any":
                        edge["kind"] = "any"
                    edges.append(edge)
            except Exception:
                pass

    if suite:
        keep_suite = {n["id"] for n in nodes if (n.get("suite") or "") == suite}
        nodes = [n for n in nodes if n["id"] in keep_suite]
        edges = [e for e in edges if e["from"] in keep_suite and e["to"] in keep_suite]

    # Add stub nodes for dependency references that don't exist in the DB.
    node_ids = {n["id"] for n in nodes}
    for e in edges:
        if e["from"] not in node_ids:
            nodes.append({
                "id": e["from"],
                "status": "missing",
                "stub": True,
                "description": "",
                "block_reason": "",
                "last_error_code": "",
                "priority": 50,
                "transfer": [],
                "yaml_num": None,
                "tags": [],
                "grp": "",
                "suite": "",
                "spawned_by": None,
            })
            node_ids.add(e["from"])
        e["health"] = _health(status_by_id.get(e["from"], "missing"))

    if focus:
        focus_id = focus
        if project and "/" not in focus:
            focus_id = focus  # bare task_id within project scope
        elif not project and "/" not in focus and nodes:
            # ambiguous — leave as-is; caller should pass project/task_id
            focus_id = focus
        # Build adjacency
        incoming: dict[str, list[str]] = {}
        outgoing: dict[str, list[str]] = {}
        for e in edges:
            incoming.setdefault(e["to"], []).append(e["from"])
            outgoing.setdefault(e["from"], []).append(e["to"])
        keep: set[str] = set()
        if any(n["id"] == focus_id for n in nodes):
            keep.add(focus_id)
            # ancestors
            stack = list(incoming.get(focus_id, []))
            while stack:
                cur = stack.pop()
                if cur in keep:
                    continue
                keep.add(cur)
                stack.extend(incoming.get(cur, []))
            # descendants
            stack = list(outgoing.get(focus_id, []))
            while stack:
                cur = stack.pop()
                if cur in keep:
                    continue
                keep.add(cur)
                stack.extend(outgoing.get(cur, []))
            nodes = [n for n in nodes if n["id"] in keep]
            edges = [e for e in edges if e["from"] in keep and e["to"] in keep]

    nodes, edges = _apply_deps_mode(nodes, edges, mode)
    total_nodes = len(nodes)
    truncated = False
    if not focus and total_nodes > DEPS_MAX_NODES:
        # An unfocused graph of thousands of nodes is unreadable and MBs of JSON: keep the
        # highest-priority slice and let the UI ask for a focus/project/suite.
        nodes = nodes[:DEPS_MAX_NODES]
        keep_ids = {n["id"] for n in nodes}
        edges = [e for e in edges if e["from"] in keep_ids and e["to"] in keep_ids]
        truncated = True
    return {
        "nodes": nodes,
        "edges": edges,
        "focus": focus,
        "mode": mode,
        "suite": suite,
        "truncated": truncated,
        "total_nodes": total_nodes,
    }


def _get_history_response(
    conn: sqlite3.Connection,
    *,
    project: str | None = None,
    search: str = "",
    offset: int = 0,
    limit: int = 100,
    hide_blocked: bool = False,
    started_by: str | None = None,
) -> dict[str, Any]:
    """W5.3: Pure data layer for /api/history — testable outside make_handler.

    ``hide_blocked`` drops runs of frozen or paused projects — the history "active" view.
    ``started_by`` keeps runs whose task is attributed to that operator (R8 mine view).
    """
    from taskqueue.db import blocked_projects, count_recent_runs, get_recent_runs

    exclude = (blocked_projects(conn) or None) if hide_blocked and not project else None
    total = count_recent_runs(
        conn, project=project, search=search, exclude_projects=exclude, started_by=started_by
    )
    rows = get_recent_runs(
        conn,
        limit,
        project=project,
        offset=offset,
        search=search,
        exclude_projects=exclude,
        started_by=started_by,
    )
    runs = [dict(r) for r in rows]
    from taskqueue.operator_features import run_anomaly

    for r in runs:
        if r.get("status") == "completed":
            flags = run_anomaly(conn, r)["flags"]
            if flags:
                r["anomalies"] = flags
    return {"runs": runs, "total": total, "offset": offset, "limit": limit}




from taskqueue.worker_api import claim_next_remote_task as _claim_next_remote_task

# F-073: fixed chunk size for streaming request bodies to disk.
_UPLOAD_CHUNK = 1024 * 1024
_DEFAULT_HTTP_WORKERS = 32
SSE_MAX_STREAMS = max(2, _DEFAULT_HTTP_WORKERS // 4)


def stream_request_body_to_file(rfile, length: int, dest: Path, *, chunk: int = _UPLOAD_CHUNK) -> None:
    """Copy exactly `length` bytes from rfile to dest via a sibling temp + os.replace.

    Raises OSError on truncated upload. Never materializes the full body in RAM.
    """
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=dest.name + ".", suffix=".partial", dir=str(dest.parent))
    tmp_path = Path(tmp_name)
    remaining = int(length)
    try:
        with os.fdopen(fd, "wb") as out:
            while remaining > 0:
                buf = rfile.read(min(chunk, remaining))
                if not buf:
                    raise OSError("truncated upload")
                out.write(buf)
                remaining -= len(buf)
        os.replace(tmp_path, dest)
    except Exception:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


class BoundedThreadingHTTPServer(HTTPServer):
    """F-073: ThreadPoolExecutor-backed HTTP server (bounded concurrency)."""

    def __init__(
        self,
        server_address,
        RequestHandlerClass,
        *,
        max_workers: int = _DEFAULT_HTTP_WORKERS,
    ):
        self._pool = ThreadPoolExecutor(
            max_workers=max(1, int(max_workers)),
            thread_name_prefix="tq-http",
        )
        # Connections accepted but not yet finished (running + queued); past the cap new
        # ones are dropped so a flood cannot grow the executor queue without bound.
        self._max_inflight = max(1, int(max_workers)) * 8
        self._inflight = 0
        self._inflight_lock = threading.Lock()
        super().__init__(server_address, RequestHandlerClass)

    def process_request(self, request, client_address) -> None:
        with self._inflight_lock:
            if self._inflight >= self._max_inflight:
                full = True
            else:
                full = False
                self._inflight += 1
        if full:
            self.shutdown_request(request)
            return
        try:
            self._pool.submit(self._process_request_worker, request, client_address)
        except RuntimeError:  # pool already shut down
            with self._inflight_lock:
                self._inflight -= 1
            self.shutdown_request(request)

    def _process_request_worker(self, request, client_address) -> None:
        try:
            self.finish_request(request, client_address)
        except Exception:
            self.handle_error(request, client_address)
        finally:
            self.shutdown_request(request)
            with self._inflight_lock:
                self._inflight -= 1

    def handle_error(self, request, client_address) -> None:
        """A client that closed its socket (tab closed, `tq top` exit) is normal, not an error."""
        import sys as _sys

        if isinstance(_sys.exc_info()[1], (ConnectionAbortedError, ConnectionResetError, BrokenPipeError)):
            return
        super().handle_error(request, client_address)

    def server_close(self) -> None:
        super().server_close()
        self._pool.shutdown(wait=False, cancel_futures=True)



def _is_loopback(host: str) -> bool:
    from taskqueue.netutil import is_loopback_host

    return is_loopback_host(host)
