"""SQLite persistence for the task queue.

Single file at ~/.taskqueue/queue.db, WAL mode so the CLI and daemon can
read/write concurrently. Schema v2: tasks are keyed (project, task_id).

# SCHEMA POLICY: Fields are only ever ADDED with safe defaults, never removed or repurposed.
# Bump SCHEMA_VERSION and add a migration in taskqueue/migrations.py (_open_db_impl) for any structural change.
"""

from __future__ import annotations
from taskqueue.jsonutil import json_list

import json
import sqlite3
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from taskqueue.config import Project
from taskqueue.models import (
    QueueTask,
    TaskResources,
)

from taskqueue.migrations import (  # noqa: E402,F401
    _CORE_CONFIG_KEYS,
    _DEFAULT_CONFIG,
    _SCHEMA,
    SCHEMA_VERSION,
    _migration_lock,
    _open_db_impl,
    bump_seed_config_writes,
    _seed_config,
    open_db,
)


def open_write_conn(db_path: Path, timeout: float = 10.0) -> sqlite3.Connection:
    """Short-lived write connection with WAL + foreign-keys pragmas."""
    conn = sqlite3.connect(str(db_path), timeout=timeout)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def record_resource_sample(
    conn: sqlite3.Connection,
    run_id: int,
    cpu_pct: float,
    ram_gib: float,
    vram_gib: float,
    gpu_util_pct: float | None = None,
    *,
    commit: bool = True,
) -> None:
    """Insert one resource-usage sample for a run.

    Pass commit=False when batching multiple samples in the same transaction;
    the caller is then responsible for calling conn.commit() afterwards.
    """
    import time as _time
    conn.execute(
        "INSERT INTO resource_history (run_id, ts, cpu_pct, ram_gib, vram_gib, gpu_util_pct) "
        "VALUES (?,?,?,?,?,?)",
        (run_id, int(_time.time()), cpu_pct, ram_gib, vram_gib, gpu_util_pct),
    )
    if commit:
        conn.commit()


# re-exported for callers using `from taskqueue.db import _now`
from taskqueue._time import _now, parse_iso  # noqa: F401,E402


# --- config ---

# Short-lived per-thread cache: avoid re-SELECT + json.loads of the whole config table on
# every call. Each thread (daemon tick, web handlers, ...) has its own connection and its
# own slot, so threads neither thrash a shared slot nor share the returned dict. The slot
# holds the connection object strongly (an id() could be recycled) and expires after
# _CONFIG_CACHE_TTL_SEC so a write by another process (`tq config set`) is picked up.
# set_config / clear_config_cache / _seed_config bump a generation to drop every slot.
_CONFIG_CACHE_TTL_SEC = 5.0
_config_gen = 0
_config_tls = threading.local()


def clear_config_cache() -> None:
    """Drop every thread's get_config cache (tests / after bulk seed)."""
    global _config_gen
    _config_gen += 1


def get_config(conn: sqlite3.Connection) -> dict[str, Any]:
    """Read config (prune of unknown keys happens in open_db / _seed_config only)."""
    now = time.monotonic()
    slot = getattr(_config_tls, "slot", None)
    if (
        slot is not None
        and slot[0] is conn
        and slot[1] == _config_gen
        and now - slot[2] < _CONFIG_CACHE_TTL_SEC
    ):
        return slot[3]
    gen = _config_gen
    rows = conn.execute("SELECT key, value FROM config").fetchall()
    cfg = {r["key"]: json.loads(r["value"]) for r in rows}
    _config_tls.slot = (conn, gen, now, cfg)
    return cfg


def read_config(home: Path) -> dict[str, Any]:
    """Config of a state home via a read-only connection: no migrations, no writes.

    For CLI helpers that only need a value (port, bind host, token) while a daemon may
    be running. Returns {} when the DB is missing or unreadable.
    """
    from taskqueue.config import db_path

    path = db_path(Path(home))
    if not path.is_file():
        return {}
    try:
        conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=5.0)
        try:
            rows = conn.execute("SELECT key, value FROM config").fetchall()
        finally:
            conn.close()
        return {k: json.loads(v) for k, v in rows}
    except (sqlite3.Error, ValueError):
        return {}


# F-058: process-local daemon incarnation id (set by run_daemon).
_current_master_instance_id: str | None = None


def new_master_instance_id() -> str:
    """Unique holder string for this daemon process incarnation."""
    import os
    import socket
    import uuid

    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex}"


def parse_master_instance_id(holder: str) -> tuple[str, int, str] | None:
    """Parse ``host:pid:uuid`` from :func:`new_master_instance_id`. None if malformed."""
    parts = str(holder).split(":", 2)
    if len(parts) != 3:
        return None
    host, pid_s, uid = parts
    if not host or not uid:
        return None
    try:
        pid = int(pid_s)
    except ValueError:
        return None
    if pid <= 0:
        return None
    return host, pid, uid


def master_holder_process_dead(holder: str, *, hostname: str | None = None) -> bool:
    """True when ``holder`` is this host's incarnation and that PID no longer exists.

    Used so a same-host restart can reclaim ``master_lease`` immediately instead of
    waiting out ``MASTER_LEASE_TTL_SEC`` (web UI stays down until the lease is held).
    Other hosts / unparseable holders are never treated as dead here.
    """
    import os
    import socket
    import sys

    parsed = parse_master_instance_id(holder)
    if parsed is None:
        return False
    host, pid, _uid = parsed
    me = hostname if hostname is not None else socket.gethostname()
    if sys.platform == "win32":
        if host.lower() != me.lower():
            return False
    elif host != me:
        return False
    try:
        import psutil

        return not psutil.pid_exists(pid)
    except Exception:
        pass
    if sys.platform == "win32":
        return False
    try:
        os.kill(pid, 0)
        return False
    except ProcessLookupError:
        return True
    except OSError:
        return False


def release_master_lease(conn: sqlite3.Connection, holder: str) -> bool:
    """Drop the lease row if we still own it (clean shutdown / yield)."""
    cur = conn.execute(
        "DELETE FROM master_lease WHERE singleton=1 AND holder=?",
        (holder,),
    )
    conn.commit()
    return cur.rowcount > 0


def set_master_instance_id(instance_id: str | None) -> None:
    """Record which incarnation this process claims as master (HTTP fencing)."""
    global _current_master_instance_id
    _current_master_instance_id = instance_id


def get_master_instance_id() -> str | None:
    return _current_master_instance_id


def holds_master_lease(
    conn: sqlite3.Connection,
    *,
    holder: str | None = None,
    now: int | None = None,
) -> bool:
    """F-057/F-058: True iff this daemon incarnation owns a non-expired lease.

    A missing row (after `/api/yield-master`) or a different/expired holder
    means this HTTP process must not claim or finalize remote work.
    """
    import socket
    import time as _time

    if holder is None:
        holder = _current_master_instance_id or socket.gethostname()
    if now is None:
        now = int(_time.time())
    row = conn.execute(
        "SELECT holder, expires_at FROM master_lease WHERE singleton=1"
    ).fetchone()
    if row is None:
        return False
    try:
        exp = int(row["expires_at"])
    except (TypeError, ValueError):
        return False
    return exp > now and str(row["holder"]) == holder


def set_config(conn: sqlite3.Connection, key: str, value: Any) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO config (key, value) VALUES (?, ?)",
        (key, json.dumps(value)),
    )
    conn.commit()
    clear_config_cache()
    bump_seed_config_writes()


def pause_key(project: str) -> str:
    return f"project_paused_{project}"


def _freeze_key(project: str) -> str:
    return f"project_frozen_{project}"


def set_project_frozen(conn: sqlite3.Connection, project: str, frozen: bool) -> None:
    """W6.1: Freeze/unfreeze a project. Frozen projects are completely skipped by the scheduler."""
    key = _freeze_key(project)
    if frozen:
        set_config(conn, key, 1)
    else:
        conn.execute("DELETE FROM config WHERE key = ?", (key,))
        conn.commit()
        clear_config_cache()


def is_project_frozen(conn: sqlite3.Connection, project: str) -> bool:
    row = conn.execute(
        "SELECT value FROM config WHERE key = ?", (_freeze_key(project),)
    ).fetchone()
    if row is None:
        return False
    try:
        return bool(json.loads(row["value"]))
    except Exception:
        return False


def set_project_paused(conn: sqlite3.Connection, project: str, paused: bool) -> None:
    key = pause_key(project)
    if paused:
        set_config(conn, key, 1)
    else:
        conn.execute("DELETE FROM config WHERE key = ?", (key,))
        conn.commit()
        clear_config_cache()


def is_project_paused(conn: sqlite3.Connection, project: str) -> bool:
    row = conn.execute(
        "SELECT value FROM config WHERE key = ?", (pause_key(project),)
    ).fetchone()
    if row is None:
        return False
    try:
        return bool(json.loads(row["value"]))
    except Exception:
        return False


def blocked_projects(conn: sqlite3.Connection) -> list[str]:
    """Frozen or paused projects. The scheduler skips their tasks without a tq.block.* code."""
    names: set[str] = set()
    rows = conn.execute(
        "SELECT key FROM config WHERE key GLOB 'project_frozen_*' OR key GLOB 'project_paused_*'"
    ).fetchall()
    for (key,) in rows:
        name = key.split("_", 2)[2]
        if is_project_frozen(conn, name) or is_project_paused(conn, name):
            names.add(name)
    return sorted(names)


from taskqueue.project_limits import _PROJECT_LIMIT_FIELDS, get_all_project_limits  # noqa: E402,F401


def _limit_key(project: str, field: str) -> str:
    return f"project_limit_{project}_{field}"


def set_project_limit(
    conn: sqlite3.Connection,
    project: str,
    field: str,
    value: float | int,
    *,
    commit: bool = True,
) -> None:
    if field not in _PROJECT_LIMIT_FIELDS:
        raise ValueError(f"Unknown project limit field: {field!r}")
    key = _limit_key(project, field)
    if value == 0:
        conn.execute("DELETE FROM config WHERE key = ?", (key,))
    else:
        conn.execute(
            "INSERT OR REPLACE INTO config (key, value) VALUES (?, ?)",
            (key, json.dumps(value)),
        )
    if commit:
        conn.commit()


def get_project_limits(conn: sqlite3.Connection, project: str) -> dict[str, float]:
    """Return {field: value} for this project; missing fields default to 0 (unlimited)."""
    out: dict[str, float] = {f: 0.0 for f in _PROJECT_LIMIT_FIELDS}
    keys = [_limit_key(project, f) for f in _PROJECT_LIMIT_FIELDS]
    rows = conn.execute(
        f"SELECT key, value FROM config WHERE key IN ({','.join('?' * len(keys))})",
        keys,
    ).fetchall()
    prefix = f"project_limit_{project}_"
    for r in rows:
        field = r["key"][len(prefix):]
        if field in out:
            try:
                out[field] = float(json.loads(r["value"]))
            except Exception:
                pass
    return out


def with_gpu_usage(conn: sqlite3.Connection, project: str, limits: dict[str, float]) -> dict[str, float]:
    """Add ``gpu_hours_today`` to *limits* when the project has a daily GPU budget.

    The usage query only runs for budgeted projects, so unbudgeted projects cost nothing.
    """
    if float(limits.get("max_gpu_hours_day", 0) or 0) <= 0:
        return limits
    out = dict(limits)
    out["gpu_hours_today"] = gpu_hours_today(conn, project)
    return out


# --- task + run functions (extracted to submodules) ---
from taskqueue._db_tasks import (  # noqa: F401
    stamp_provenance,
    run_summary,
    run_counts,
    prior_run_seconds,
    failed_attempt_count,
    upsert_task,
    get_queued_tasks,
    count_queued_tasks,
    count_online_workers,
    get_running_tasks,
    get_all_tasks,
    get_task,
    get_task_status,
    check_ownership,
    set_task_status,
    claim_task_running,
    claim_and_insert_local_run,
    set_task_error,
    clear_task_error,
    set_spawned_by,
    set_started_by,
    set_task_priority,
    request_stop,
    request_stop_with_control,
    clear_stop_request,
    retry_skip_reason,
    retry_task,
    retry_tasks_batch,
    apply_cap_auto_retry,
    unmet_dependency_label,
    collect_running_dependents,
    stop_running_for_unmet_deps,
    invalidate_running_dependents,
    revalidate_running_dependencies,
    collect_downstream_task_ids,
    retry_task_with_downstream,
    collect_ancestors_to_root,
    lock_occupancy,
    lock_contention_summary,
    write_control_action,
    clear_control_file,
    enable_task,
    SUBMIT_CHANNELS,
    RETRYABLE_STATUSES,
    CAP_AUTO_RETRY_SEMANTICS,
    _upsert_task_no_commit,
)
def set_args_override(conn: sqlite3.Connection, project: str, task_id: str, args: list[str]) -> None:
    """R2: Write a one-shot arg override; cleared by runner after pickup."""
    conn.execute(
        "UPDATE tasks SET args_override = ? WHERE project = ? AND task_id = ?",
        (json.dumps(args), project, task_id),
    )
    conn.commit()


def check_arg_overrides(row: sqlite3.Row, override: list[str]) -> str | None:
    """Return an error if ``override`` changes args outside the task's ``overridable_args``.

    Tokens already present in the task's base args are unchanged; every other
    token must name an allowlisted flag/key (``name`` or ``name=value``).
    """
    try:
        allowed = set(json_list(row["overridable_args"]))
    except Exception:
        allowed = set()
    try:
        base = set(json_list(row["args"]))
    except Exception:
        base = set()
    bad = sorted({t.split("=")[0] for t in override if t not in base and t.split("=")[0] not in allowed})
    if not bad:
        return None
    return (
        f"not in overridable_args: {', '.join(bad)}; "
        f"allowed: {', '.join(sorted(allowed)) or '(none)'}"
    )


def clear_args_override(conn: sqlite3.Connection, project: str, task_id: str) -> None:
    conn.execute(
        "UPDATE tasks SET args_override = NULL WHERE project = ? AND task_id = ?",
        (project, task_id),
    )
    conn.commit()


from taskqueue._db_runs import (  # noqa: F401
    insert_run,
    set_run_pid,
    update_run_progress,
    update_run_metrics,
    set_run_stop_reason,
    finish_run,
    finalize_run_and_task,
    lease_expiry_iso,
    renew_run_lease,
    renew_run_lease_for_worker,
    get_run_owned,
    worker_health,
    reclaim_expired_leases,
    mark_stale_workers_offline,
    force_task,
    remove_task,
    get_recent_runs,
    count_recent_runs,
    get_run,
    get_runs_for_task,
    gpu_hours_today,
    get_last_run_for_task,
    get_last_runs_for_tasks,
)

# --- yaml parsing (lives in taskspec.py; re-exported here for compatibility) ---
from taskqueue.taskspec import (  # noqa: E402,F401
    _KNOWN_SERVICES,
    _expand_service,
    _parse_raw_task_dict,
    _relativize_path,
    parse_task_yaml,
    parse_task_yaml_many,
    validate_task_spec,
)


# --- daemon lifecycle events (schema 15) ---

DAEMON_EVENT_DIRTY_RESTART = "dirty_restart"
DAEMON_EVENT_START = "start"
DAEMON_EVENT_ORPHAN_REQUEUE = "orphan_requeue"
DAEMON_EVENT_CLEAN_SHUTDOWN = "clean_shutdown"

_DAEMON_EVENT_KINDS = frozenset({
    DAEMON_EVENT_DIRTY_RESTART,
    DAEMON_EVENT_START,
    DAEMON_EVENT_ORPHAN_REQUEUE,
    DAEMON_EVENT_CLEAN_SHUTDOWN,
})


def insert_daemon_event(
    conn: sqlite3.Connection,
    kind: str,
    *,
    pid: int | None = None,
    instance_id: str | None = None,
    epoch: int | None = None,
    detail: dict[str, Any] | None = None,
) -> int:
    """Append a daemon lifecycle event. Returns event_id."""
    if kind not in _DAEMON_EVENT_KINDS:
        raise ValueError(f"unknown daemon event kind: {kind!r}")
    detail_json = json.dumps(detail, ensure_ascii=False) if detail is not None else None
    cur = conn.execute(
        "INSERT INTO daemon_events (ts, kind, pid, instance_id, epoch, detail_json) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (_now(), kind, pid, instance_id, epoch, detail_json),
    )
    conn.commit()
    return int(cur.lastrowid or 0)


def list_daemon_events(conn: sqlite3.Connection, *, limit: int = 20) -> list[dict[str, Any]]:
    """Newest-first daemon lifecycle events."""
    lim = max(1, min(int(limit), 500))
    rows = conn.execute(
        "SELECT event_id, ts, kind, pid, instance_id, epoch, detail_json "
        "FROM daemon_events ORDER BY event_id DESC LIMIT ?",
        (lim,),
    ).fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        out.append({
            "event_id": row["event_id"],
            "ts": row["ts"],
            "kind": row["kind"],
            "pid": row["pid"],
            "instance_id": row["instance_id"],
            "epoch": row["epoch"],
            "detail_json": row["detail_json"],
        })
    return out


def latest_daemon_event(conn: sqlite3.Connection) -> dict[str, Any] | None:
    rows = list_daemon_events(conn, limit=1)
    return rows[0] if rows else None


def last_boot_kind(conn: sqlite3.Connection) -> str:
    """Return 'dirty', 'clean', or 'unknown' for the most recent boot sequence.

    Looks at events from the newest ``start`` backward: if the preceding event
    is ``dirty_restart``, the boot was dirty; otherwise clean (or unknown if
    no start event exists).
    """
    rows = list_daemon_events(conn, limit=50)
    if not rows:
        return "unknown"
    # rows are newest-first
    for i, ev in enumerate(rows):
        if ev["kind"] != DAEMON_EVENT_START:
            continue
        # Look at the next-older event (higher index)
        if i + 1 < len(rows) and rows[i + 1]["kind"] == DAEMON_EVENT_DIRTY_RESTART:
            return "dirty"
        return "clean"
    return "unknown"


def prune_daemon_events(conn: sqlite3.Connection, *, keep_days: int = 90) -> int:
    """Delete daemon_events older than keep_days. Returns rows deleted."""
    from datetime import timedelta

    days = max(1, int(keep_days))
    cutoff = (datetime.now(UTC) - timedelta(days=days)).isoformat()
    cur = conn.execute("DELETE FROM daemon_events WHERE ts < ?", (cutoff,))
    conn.commit()
    return int(cur.rowcount or 0)
