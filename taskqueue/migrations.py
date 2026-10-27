"""Schema, config seeding and migrations for the queue DB (extracted from db.py).

``taskqueue.db`` re-exports ``open_db`` and friends; import from there in application code.
"""

from __future__ import annotations

import contextlib
import json
import sys
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from taskqueue.jsonutil import json_list

SCHEMA_VERSION = 30

# Wave D6: keep-set for prune is _CORE_CONFIG_KEYS ∪ VALID_CONFIG_KEYS.
# New tunables MUST be registered in taskqueue.config.VALID_CONFIG_KEYS (and
# optionally _DEFAULT_CONFIG for a seeded default). Never add a config key
# only to _DEFAULT_CONFIG — seed will prune it on the next open_db.
_CORE_CONFIG_KEYS = (
    "cpu_limit_pct", "gpu_limit_pct", "ram_limit_pct", "ram_hard_limit_pct", "graceful_stop_sec",
    "scheduler_interval_sec", "heartbeat_sec", "frozen_threshold_sec", "web_port",
    "bind_host", "auth_token", "prevent_sleep", "suspend_grace_sec",
    "schema_version", "priority_higher_wins", "notify_allow_remote",
    "max_running_tasks", "gpu_vram_limit_gib",
    "max_cpu_cores", "max_ram_gib",
    "fairshare_halflife_days", "fairshare_weight",
    "max_queued_tasks_per_project", "hung_task_k",
    # F-034: these were missing and got pruned on every restart
    "notify_on_attention", "toast_notifications",
    # Also in VALID_CONFIG_KEYS (string / enum). Listed here so _seed_config
    # does not prune them on restart (F-034).
    "notify_url", "frozen_action", "limit_decrease_policy",
    # Admission control (plan/archive/scheduler_resources.md) — must be listed here or
    # _seed_config prunes them on every restart (F-034).
    "default_task_ram_gib", "resource_predict_runs",
    "admit_max_per_tick", "admit_settle_sec", "max_concurrent_unknown",
    "settle_window_sec", "stop_max_per_tick", "recovery_cooldown_sec",
    "admission_shadow_mode", "scavenger_preempt_cooldown_sec",
    # P6: reservation + backfill
    "reservation_max_ticks",
    # P7: live CPU gating
    "cpu_measure_ema_sec",
    # P8: VRAM pressure response
    "vram_hard_limit_pct",
    # F-001 anti-flap: limit auto-requeue after repeated clock-jump kills
    "clock_jump_threshold_sec",
    "clock_jump_max_requeue",
    "clock_jump_requeue_window_sec",
    # PC-1: remote best-fit placement
    "placement_mode", "placement_age_sec", "placement_shadow_mode",
    "placement_prefer_last_worker",
    # F-075: heartbeat warnings + local concurrency cap (docs say `tq config set`)
    "gpu_util_warn_pct", "ram_settle_sec", "ram_leak_warn_gib_per_hour",
    "max_concurrent_tasks",
    # admission candidate window (was in VALID_CONFIG_KEYS but missing here — pruned on restart)
    "admit_candidate_window",
)

_SCHEMA = """
-- SCHEMA POLICY: Fields are only ever ADDED with safe defaults, never removed or
-- repurposed. Bump schema_version in _DEFAULT_CONFIG for any structural change.

PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS tasks (
    project             TEXT NOT NULL,
    task_id             TEXT NOT NULL,
    yaml_source         TEXT NOT NULL DEFAULT '',
    priority            INTEGER NOT NULL DEFAULT 50,
    status              TEXT NOT NULL DEFAULT 'queued',
    command             TEXT NOT NULL,
    args                TEXT NOT NULL DEFAULT '[]',
    resume_args         TEXT,
    wall_clock_cap_sec  INTEGER,
    ledger_subdir       TEXT,
    requires_paths      TEXT NOT NULL DEFAULT '[]',
    resources           TEXT NOT NULL DEFAULT '{}',
    exclusive           INTEGER NOT NULL DEFAULT 0,
    exclusive_project   INTEGER NOT NULL DEFAULT 0,
    description         TEXT NOT NULL DEFAULT '',
    enabled             INTEGER NOT NULL DEFAULT 1,
    stop_requested      INTEGER NOT NULL DEFAULT 0,
    depends_on          TEXT NOT NULL DEFAULT '[]',
    enqueued_at         TEXT NOT NULL,
    updated_at          TEXT NOT NULL,
    force_run           INTEGER NOT NULL DEFAULT 0,
    dispatch            TEXT NOT NULL DEFAULT 'local',
    requires            TEXT NOT NULL DEFAULT '{}',
    bundle_hash         TEXT,
    last_error_code     TEXT,
    block_reason        TEXT,
    started_by          TEXT,
    max_attempts        INTEGER NOT NULL DEFAULT 0,
    notify_url          TEXT,
    tags                TEXT NOT NULL DEFAULT '[]',
    suite               TEXT NOT NULL DEFAULT '',
    metadata            TEXT NOT NULL DEFAULT '{}',
    grp                 TEXT NOT NULL DEFAULT '',
    artifacts           TEXT NOT NULL DEFAULT '[]',
    artifacts_required  INTEGER NOT NULL DEFAULT 0,
    processed_at        TEXT,
    last_run_id         INTEGER REFERENCES runs(run_id),
    submitted_via       TEXT,
    submitted_by        TEXT,
    client_info         TEXT,
    PRIMARY KEY (project, task_id)
);

CREATE TABLE IF NOT EXISTS runs (
    run_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    project         TEXT NOT NULL DEFAULT '',
    task_id         TEXT NOT NULL,
    started_at      TEXT NOT NULL,
    finished_at     TEXT,
    exit_code       INTEGER,
    log_path        TEXT,
    status          TEXT NOT NULL,
    progress_pct    REAL,
    last_log_line   TEXT,
    peak_ram_gib    REAL,
    peak_vram_gib   REAL,
    peak_cpu_pct    REAL,
    worker_id       TEXT,
    last_error_code TEXT,
    lease_expires_at TEXT,
    pid             INTEGER,
    pid_create_time REAL,
    stopped_by      TEXT,
    error_category  TEXT
);

CREATE TABLE IF NOT EXISTS config (
    key     TEXT PRIMARY KEY,
    value   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS workers (
    worker_id      TEXT PRIMARY KEY,
    os             TEXT NOT NULL,
    arch           TEXT NOT NULL,
    ram_gib        REAL NOT NULL,
    cpu_cores      INTEGER NOT NULL,
    gpu_vram_gib   REAL NOT NULL,
    tags           TEXT NOT NULL DEFAULT '[]',
    projects       TEXT NOT NULL DEFAULT '[]',
    last_seen      TEXT NOT NULL,
    status         TEXT NOT NULL DEFAULT 'online',
    gpus_json      TEXT NOT NULL DEFAULT '[]',
    load_json      TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS resource_history (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id  INTEGER NOT NULL,
    ts      INTEGER NOT NULL,
    cpu_pct REAL,
    ram_gib REAL,
    vram_gib REAL,
    gpu_util_pct REAL
);

CREATE INDEX IF NOT EXISTS idx_resource_history_run_ts ON resource_history(run_id, ts);
CREATE INDEX IF NOT EXISTS idx_resource_history_ts ON resource_history(ts);
CREATE INDEX IF NOT EXISTS idx_tasks_yaml_source ON tasks(yaml_source);

-- W0.1: runs indexes — these were absent; /api/queue did a full table scan (O(n²))
CREATE INDEX IF NOT EXISTS idx_runs_project_task ON runs(project, task_id, run_id DESC);
CREATE INDEX IF NOT EXISTS idx_runs_status_worker ON runs(status, worker_id);
CREATE INDEX IF NOT EXISTS idx_tasks_updated_at ON tasks(updated_at);
CREATE INDEX IF NOT EXISTS idx_tasks_priority_enqueued ON tasks(priority DESC, enqueued_at ASC);
CREATE INDEX IF NOT EXISTS idx_tasks_project ON tasks(project);

CREATE TABLE IF NOT EXISTS project_usage (
    project     TEXT NOT NULL PRIMARY KEY,
    gpu_seconds REAL NOT NULL DEFAULT 0,
    cpu_seconds REAL NOT NULL DEFAULT 0,
    updated_at  INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS usage_snapshot (
    ts          INTEGER NOT NULL,
    project     TEXT NOT NULL,
    operator    TEXT,
    cpu_seconds REAL NOT NULL DEFAULT 0,
    gpu_seconds REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (ts, project, operator)
);

CREATE INDEX IF NOT EXISTS idx_usage_snapshot_ts ON usage_snapshot(ts);

CREATE TABLE IF NOT EXISTS master_lease (
    singleton  INTEGER NOT NULL DEFAULT 1 PRIMARY KEY CHECK (singleton = 1),
    holder     TEXT NOT NULL,
    expires_at INTEGER NOT NULL,
    epoch      INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS daemon_events (
    event_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           TEXT NOT NULL,
    kind         TEXT NOT NULL,
    pid          INTEGER,
    instance_id  TEXT,
    epoch        INTEGER,
    detail_json  TEXT
);

CREATE INDEX IF NOT EXISTS idx_daemon_events_ts ON daemon_events(ts);

CREATE TABLE IF NOT EXISTS alert_rules (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT NOT NULL,
    kind          TEXT NOT NULL,
    threshold     REAL NOT NULL,
    window_sec    INTEGER NOT NULL DEFAULT 3600,
    project       TEXT,
    action        TEXT NOT NULL DEFAULT 'notify',
    enabled       INTEGER NOT NULL DEFAULT 1,
    fire_count    INTEGER NOT NULL DEFAULT 0,
    last_fired_at TEXT
);

CREATE TABLE IF NOT EXISTS system_alerts (
    key           TEXT PRIMARY KEY,
    enabled       INTEGER NOT NULL DEFAULT 1,
    windows_toast INTEGER NOT NULL DEFAULT 1,
    last_fired_at TEXT,
    fire_count    INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS api_usage (
    bucket_ts   TEXT NOT NULL,
    channel     TEXT NOT NULL,
    client      TEXT NOT NULL DEFAULT '',
    operator    TEXT NOT NULL DEFAULT '',
    project     TEXT NOT NULL DEFAULT '',
    op          TEXT NOT NULL,
    error_code  TEXT NOT NULL DEFAULT '',
    calls       INTEGER NOT NULL DEFAULT 0,
    total_ms    INTEGER NOT NULL DEFAULT 0,
    max_ms      INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (bucket_ts, channel, client, operator, project, op, error_code)
);
CREATE INDEX IF NOT EXISTS idx_api_usage_bucket ON api_usage(bucket_ts);

CREATE TABLE IF NOT EXISTS operator_audit (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    ts       TEXT NOT NULL,
    actor    TEXT NOT NULL,
    action   TEXT NOT NULL,
    project  TEXT,
    target   TEXT,
    detail   TEXT
);

CREATE INDEX IF NOT EXISTS idx_operator_audit_ts ON operator_audit(ts DESC);

-- Webhooks that exhausted their retries; replayable via /api/notify/dead/<id>/retry.
CREATE TABLE IF NOT EXISTS notify_dead (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    url       TEXT NOT NULL,
    payload   TEXT NOT NULL,
    error     TEXT,
    attempts  INTEGER NOT NULL DEFAULT 0,
    failed_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_operator_audit_project_ts ON operator_audit(project, ts);

CREATE TABLE IF NOT EXISTS api_tokens (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT NOT NULL,
    token_hash   TEXT NOT NULL UNIQUE,
    prefix       TEXT NOT NULL,
    scope        TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    last_used_at TEXT,
    revoked_at   TEXT,
    projects     TEXT
);
"""

_DEFAULT_CONFIG: dict[str, Any] = {
    "cpu_limit_pct": 50,
    "gpu_limit_pct": 100,
    "ram_limit_pct": 80,
    "ram_hard_limit_pct": 95,
    "graceful_stop_sec": 30,
    "scheduler_interval_sec": 5,
    "heartbeat_sec": 30,
    "frozen_threshold_sec": 600,
    "web_port": 8765,
    "bind_host": "127.0.0.1",
    "auth_token": "",
    "prevent_sleep": 1,
    # Wall-clock window between the cooperative-suspend control-file write
    # and SIGTERM, so a task actually has time to checkpoint before being
    # killed. 0 = old behavior (SIGTERM immediately, best-effort only).
    "suspend_grace_sec": 10,
    # Allow notify_url hosts outside loopback (SSRF risk — off by default).
    "notify_allow_remote": 0,
    "max_running_tasks": 0,
    "gpu_vram_limit_gib": 0,
    "max_cpu_cores": 0,
    "max_ram_gib": 0,
    # I-17: number of consecutive no-progress ticks before a hung remote task is re-queued
    # (only applies to tasks with idempotent: true; 0 = disabled)
    "hung_task_k": 5,
    # R9: decayed fair-share — half-life for exponential decay of project GPU/CPU usage
    "fairshare_halflife_days": 7,
    # Weight multiplier applied to accumulated gpu_seconds when sorting queued tasks
    "fairshare_weight": 1e-6,
    # W4.11: warn when a project's YAML queue exceeds this count (0 = disabled)
    "max_queued_tasks_per_project": 500,
    # F-034: keys in VALID_CONFIG_KEYS that need defaults to survive restart
    "notify_on_attention": 0,
    "toast_notifications": 0,
    # --- admission control (plan/archive/scheduler_resources.md) ---
    # Floor estimate for tasks that declare no ram_gib and have no run history,
    # so an undeclared task is never accounted as free.
    "default_task_ram_gib": 1.0,
    "resource_predict_runs": 5,
    # Max tasks started per scheduler tick. 1 staggers starts so each task's
    # real footprint becomes visible before the next admit. 0 = unlimited.
    "admit_max_per_tick": 1,
    # Optional extra soak between starts within a tick (0 = off).
    "admit_settle_sec": 0.0,
    # Max tasks running at once with no declaration and no history. Guards
    # matrix: expansions, which produce N same-shaped zero-history tasks.
    "max_concurrent_unknown": 1,
    # How long a freshly started task's estimate is reserved above its measured
    # usage, while it ramps up (model load can take 30-120s).
    "settle_window_sec": 180,
    # Victims stopped per tick under memory pressure, and the admission freeze
    # afterwards while the OS reclaims.
    "stop_max_per_tick": 2,
    "recovery_cooldown_sec": 30,
    # T-204: min seconds between scavenger preemptions.
    "scavenger_preempt_cooldown_sec": 60,
    # Log admission decisions without enforcing them.
    "admission_shadow_mode": 0,
    # PC-1: remote placement — binpack skips claim if a fresher peer scores better;
    # aging lets any feasible worker take a long-waiting task (anti-starvation).
    "placement_mode": "binpack",
    "placement_age_sec": 120,
    "placement_shadow_mode": 0,
    "placement_prefer_last_worker": 1,
    # P6: how many consecutive ticks the top-priority resource-blocked task holds a
    # reservation before it expires (and the queue falls back to normal for one tick).
    # 60 ticks * 5s = 5 min default. Also used as wall-time budget for remote P6 hold.
    "reservation_max_ticks": 60,
    # P7: EMA half-window for smoothing the live host CPU reading (seconds).
    # The scheduler tick is ~5s so 30s ≈ 6 ticks of smoothing.
    "cpu_measure_ema_sec": 30,
    # P8: per-device VRAM threshold at which a running GPU task is stopped.
    # Mirrors ram_hard_limit_pct; 0 = disabled.
    "vram_hard_limit_pct": 95,
    # F-001: minimum wall-clock gap (seconds) that counts as a sleep/hibernate
    # wake. 0 = use auto (max(2*scheduler_interval, 30)). Raise this if lid-close
    # wakes are too aggressively killing tasks on a laptop.
    "clock_jump_threshold_sec": 0,
    # After this many clock-jump interrupts per task in the window, leave the task
    # interrupted instead of auto-requeueing (0 = unlimited / disabled).
    "clock_jump_max_requeue": 3,
    "clock_jump_requeue_window_sec": 3600,
    # F-075
    "gpu_util_warn_pct": 15,
    "ram_settle_sec": 1800,
    "ram_leak_warn_gib_per_hour": 1.0,
    "max_concurrent_tasks": 0,
}


_MIGRATE_THREAD_LOCK = threading.Lock()


@contextlib.contextmanager
def _migration_lock(db_path: Path):
    """Serialize schema migration across threads and processes.

    Two daemons/CLIs upgrading at once otherwise race on ``ALTER TABLE`` and fail
    with "duplicate column". The second opener waits, then finds the work done.
    """
    lock_path = db_path.with_name(db_path.name + ".migrate.lock")
    with _MIGRATE_THREAD_LOCK:
        fh = open(lock_path, "a+b")
        try:
            deadline = time.monotonic() + 180
            while True:
                try:
                    if os.name == "nt":
                        import msvcrt

                        fh.seek(0)
                        msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl

                        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError:
                    if time.monotonic() > deadline:
                        raise TimeoutError(
                            f"schema migration lock {lock_path} held >180s by another process"
                        )
                    time.sleep(0.1)
            yield
        finally:
            try:
                if os.name == "nt":
                    import msvcrt

                    fh.seek(0)
                    msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
            fh.close()


def open_db(db_path: Path, *, migrate: bool = True) -> sqlite3.Connection:
    """Open the queue DB (migrations are serialized by a lock file)."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if not migrate:
        return _open_db_impl(db_path, migrate=False)
    # Hot path: an up-to-date schema needs no lock, executescript or ALTER probes.
    if db_path.exists():
        conn = _open_if_current(db_path)
        if conn is not None:
            return conn
    with _migration_lock(db_path):
        return _open_db_impl(db_path, migrate=True)


def _open_if_current(db_path: Path) -> sqlite3.Connection | None:
    """Connection when ``schema_version`` already equals ``SCHEMA_VERSION``, else None."""
    conn = sqlite3.connect(str(db_path), check_same_thread=False, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT value FROM config WHERE key='schema_version'").fetchone()
        if row and json.loads(row["value"]) == SCHEMA_VERSION:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            return conn
    except (sqlite3.Error, ValueError):
        pass
    conn.close()
    return None


def _open_db_impl(db_path: Path, *, migrate: bool = True) -> sqlite3.Connection:
    """Open the queue DB.

    When ``migrate=False`` and ``schema_version`` already matches
    ``SCHEMA_VERSION``, skip executescript / ALTER probes / seed writes.
    Outdated or missing version always falls through to a full migrate.
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), check_same_thread=False, timeout=30)
    conn.row_factory = sqlite3.Row
    if not migrate:
        try:
            row = conn.execute(
                "SELECT value FROM config WHERE key='schema_version'"
            ).fetchone()
            stored = json.loads(row["value"]) if row else None
            if stored == SCHEMA_VERSION:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA foreign_keys=ON")
                return conn
        except sqlite3.Error:
            pass  # fall through to full migrate
    conn.executescript(_SCHEMA)
    conn.commit()
    try:
        cursor = conn.execute("PRAGMA table_info(runs)")
        columns = [row["name"] for row in cursor.fetchall()]
        if "peak_ram_gib" not in columns:
            conn.execute("ALTER TABLE runs ADD COLUMN peak_ram_gib REAL")
        if "peak_vram_gib" not in columns:
            conn.execute("ALTER TABLE runs ADD COLUMN peak_vram_gib REAL")
        if "peak_cpu_pct" not in columns:
            conn.execute("ALTER TABLE runs ADD COLUMN peak_cpu_pct REAL")
        if "worker_id" not in columns:
            conn.execute("ALTER TABLE runs ADD COLUMN worker_id TEXT")
        if "last_error_code" not in columns:
            conn.execute("ALTER TABLE runs ADD COLUMN last_error_code TEXT")
        if "lease_expires_at" not in columns:
            conn.execute("ALTER TABLE runs ADD COLUMN lease_expires_at TEXT")
        if "pid" not in columns:
            conn.execute("ALTER TABLE runs ADD COLUMN pid INTEGER")
        if "pid_create_time" not in columns:
            conn.execute("ALTER TABLE runs ADD COLUMN pid_create_time REAL")
        if "sleep_tier" not in columns:
            conn.execute("ALTER TABLE runs ADD COLUMN sleep_tier INTEGER")
        if "stopped_by" not in columns:
            conn.execute("ALTER TABLE runs ADD COLUMN stopped_by TEXT")
        if "error_category" not in columns:
            conn.execute("ALTER TABLE runs ADD COLUMN error_category TEXT")
        if "metrics_json" not in columns:
            conn.execute("ALTER TABLE runs ADD COLUMN metrics_json TEXT")
        if "stop_reason" not in columns:
            conn.execute("ALTER TABLE runs ADD COLUMN stop_reason TEXT")
        if "finalize_token" not in columns:
            # F-055: per-claim secret so only the claiming worker can finalize
            conn.execute("ALTER TABLE runs ADD COLUMN finalize_token TEXT")
        # P9: estimate telemetry — store predicted alongside peak so accuracy
        # is queryable without re-running the estimator.
        if "estimated_ram_gib" not in columns:
            conn.execute("ALTER TABLE runs ADD COLUMN estimated_ram_gib REAL")
        if "estimated_vram_gib" not in columns:
            conn.execute("ALTER TABLE runs ADD COLUMN estimated_vram_gib REAL")

        # Resource history — GPU util samples (nullable for older rows)
        rh_cols = {row["name"] for row in conn.execute("PRAGMA table_info(resource_history)").fetchall()}
        if "gpu_util_pct" not in rh_cols:
            conn.execute("ALTER TABLE resource_history ADD COLUMN gpu_util_pct REAL")

        cursor = conn.execute("PRAGMA table_info(tasks)")
        task_columns = [row["name"] for row in cursor.fetchall()]
        if "force_run" not in task_columns:
            conn.execute("ALTER TABLE tasks ADD COLUMN force_run INTEGER NOT NULL DEFAULT 0")
        if "exclusive_project" not in task_columns:
            conn.execute(
                "ALTER TABLE tasks ADD COLUMN exclusive_project INTEGER NOT NULL DEFAULT 0"
            )
        if "dispatch" not in task_columns:
            conn.execute("ALTER TABLE tasks ADD COLUMN dispatch TEXT NOT NULL DEFAULT 'local'")
        if "requires" not in task_columns:
            conn.execute("ALTER TABLE tasks ADD COLUMN requires TEXT NOT NULL DEFAULT '{}'")
        if "bundle_hash" not in task_columns:
            conn.execute("ALTER TABLE tasks ADD COLUMN bundle_hash TEXT")
        if "code_provider" not in task_columns:
            conn.execute("ALTER TABLE tasks ADD COLUMN code_provider TEXT NOT NULL DEFAULT ''")
        if "last_error_code" not in task_columns:
            conn.execute("ALTER TABLE tasks ADD COLUMN last_error_code TEXT")
        if "block_reason" not in task_columns:
            conn.execute("ALTER TABLE tasks ADD COLUMN block_reason TEXT")
        # v22: provenance (who/how a task was created) -- analytics only, nullable.
        for _col in ("submitted_via", "submitted_by", "client_info"):
            if _col not in task_columns:
                conn.execute(f"ALTER TABLE tasks ADD COLUMN {_col} TEXT")
        if "started_by" not in task_columns:
            conn.execute("ALTER TABLE tasks ADD COLUMN started_by TEXT")
        if "max_attempts" not in task_columns:
            conn.execute("ALTER TABLE tasks ADD COLUMN max_attempts INTEGER NOT NULL DEFAULT 0")
        if "notify_url" not in task_columns:
            conn.execute("ALTER TABLE tasks ADD COLUMN notify_url TEXT")
        if "tags" not in task_columns:
            conn.execute("ALTER TABLE tasks ADD COLUMN tags TEXT NOT NULL DEFAULT '[]'")
        if "suite" not in task_columns:
            conn.execute("ALTER TABLE tasks ADD COLUMN suite TEXT NOT NULL DEFAULT ''")
        if "env" not in task_columns:
            conn.execute("ALTER TABLE tasks ADD COLUMN env TEXT NOT NULL DEFAULT '{}'")
        if "idempotent" not in task_columns:
            conn.execute("ALTER TABLE tasks ADD COLUMN idempotent INTEGER NOT NULL DEFAULT 0")
        if "execution_semantics" not in task_columns:
            # R-011: explicit attempt recovery policy (default non_idempotent)
            conn.execute(
                "ALTER TABLE tasks ADD COLUMN execution_semantics TEXT NOT NULL DEFAULT 'non_idempotent'"
            )
        if "metadata" not in task_columns:
            conn.execute("ALTER TABLE tasks ADD COLUMN metadata TEXT NOT NULL DEFAULT '{}'")
        if "grp" not in task_columns:
            conn.execute("ALTER TABLE tasks ADD COLUMN grp TEXT NOT NULL DEFAULT ''")
        if "artifacts" not in task_columns:
            conn.execute("ALTER TABLE tasks ADD COLUMN artifacts TEXT NOT NULL DEFAULT '[]'")
        if "artifacts_required" not in task_columns:
            conn.execute("ALTER TABLE tasks ADD COLUMN artifacts_required INTEGER NOT NULL DEFAULT 0")
        if "on_complete" not in task_columns:
            conn.execute("ALTER TABLE tasks ADD COLUMN on_complete TEXT NOT NULL DEFAULT '[]'")
        if "on_fail" not in task_columns:
            conn.execute("ALTER TABLE tasks ADD COLUMN on_fail TEXT NOT NULL DEFAULT '[]'")
        if "transfer" not in task_columns:
            conn.execute("ALTER TABLE tasks ADD COLUMN transfer TEXT NOT NULL DEFAULT '[]'")
        if "requires_services" not in task_columns:
            conn.execute("ALTER TABLE tasks ADD COLUMN requires_services TEXT NOT NULL DEFAULT '[]'")
        if "overridable_args" not in task_columns:
            conn.execute("ALTER TABLE tasks ADD COLUMN overridable_args TEXT NOT NULL DEFAULT '[]'")
        if "args_override" not in task_columns:
            # R2: one-shot arg override written by tq retry --set, cleared after pickup
            conn.execute("ALTER TABLE tasks ADD COLUMN args_override TEXT")
        if "processed_at" not in task_columns:
            conn.execute("ALTER TABLE tasks ADD COLUMN processed_at TEXT")
        if "hung_task_k" not in task_columns:
            conn.execute("ALTER TABLE tasks ADD COLUMN hung_task_k INTEGER")
        if "finished_at" not in task_columns:
            conn.execute("ALTER TABLE tasks ADD COLUMN finished_at TEXT")
        if "schedule" not in task_columns:
            conn.execute("ALTER TABLE tasks ADD COLUMN schedule TEXT")
        if "rearm_on" not in task_columns:
            conn.execute("ALTER TABLE tasks ADD COLUMN rearm_on TEXT NOT NULL DEFAULT ''")
        if "teardown" not in task_columns:
            conn.execute("ALTER TABLE tasks ADD COLUMN teardown TEXT")
        if "yaml_num" not in task_columns:
            conn.execute("ALTER TABLE tasks ADD COLUMN yaml_num INTEGER")
        if "auto_retry_on_cap" not in task_columns:
            conn.execute(
                "ALTER TABLE tasks ADD COLUMN auto_retry_on_cap INTEGER NOT NULL DEFAULT 0"
            )
        if "max_auto_retries" not in task_columns:
            conn.execute(
                "ALTER TABLE tasks ADD COLUMN max_auto_retries INTEGER NOT NULL DEFAULT 20"
            )
        if "min_trust" not in task_columns:
            # C2: minimum worker trust tier required for this task
            conn.execute(
                "ALTER TABLE tasks ADD COLUMN min_trust TEXT NOT NULL DEFAULT ''"
            )
        if "task_class" not in task_columns:
            # T-204: scheduling class (normal | scavenger)
            conn.execute(
                "ALTER TABLE tasks ADD COLUMN task_class TEXT NOT NULL DEFAULT 'normal'"
            )
        if "on_dependency_failure" not in task_columns:
            # T-206: block (default) | cancel dependents of a failed dependency
            conn.execute(
                "ALTER TABLE tasks ADD COLUMN on_dependency_failure TEXT NOT NULL DEFAULT 'block'"
            )
        if "negative_exit_codes" not in task_columns:
            conn.execute("ALTER TABLE tasks ADD COLUMN negative_exit_codes TEXT NOT NULL DEFAULT '[]'")
        if "on_dependency_negative" not in task_columns:
            conn.execute("ALTER TABLE tasks ADD COLUMN on_dependency_negative TEXT NOT NULL DEFAULT 'run'")
        if "cap_auto_retry_count" not in task_columns:
            conn.execute(
                "ALTER TABLE tasks ADD COLUMN cap_auto_retry_count INTEGER NOT NULL DEFAULT 0"
            )
        if "retry_attempt_baseline" not in task_columns:
            conn.execute(
                "ALTER TABLE tasks ADD COLUMN retry_attempt_baseline INTEGER NOT NULL DEFAULT 0"
            )
        # T-202 / T-203: OR-deps + spawner lineage (runtime column; not wiped by sync)
        if "depends_on_any" not in task_columns:
            conn.execute(
                "ALTER TABLE tasks ADD COLUMN depends_on_any TEXT NOT NULL DEFAULT '[]'"
            )
        if "spawned_by" not in task_columns:
            conn.execute("ALTER TABLE tasks ADD COLUMN spawned_by TEXT")
        # D1: content-addressed data transfer block
        if "data" not in task_columns:
            conn.execute("ALTER TABLE tasks ADD COLUMN data TEXT")
        # G3: p50 of last-N completed run durations for this task
        if "predicted_duration_sec" not in task_columns:
            conn.execute("ALTER TABLE tasks ADD COLUMN predicted_duration_sec REAL")
        # P1: denormalized last run id — eliminates MAX(run_id) subquery in list
        if "last_run_id" not in task_columns:
            conn.execute("ALTER TABLE tasks ADD COLUMN last_run_id INTEGER REFERENCES runs(run_id)")
            conn.execute(
                "UPDATE tasks SET last_run_id = ("
                "  SELECT MAX(run_id) FROM runs"
                "  WHERE runs.project = tasks.project AND runs.task_id = tasks.task_id"
                ")"
            )

        # I-15: index for fast tag filtering
        # tags is a JSON string matched with LIKE/json: this index never helped and costs writes.
        conn.execute("DROP INDEX IF EXISTS idx_tasks_tags")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_runs_finished_at ON runs(finished_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_runs_started_at ON runs(started_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_project ON tasks(project)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_grp ON tasks(grp)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_project_status ON tasks(project, status)")

        # resource_history already created in _SCHEMA above (W5.6: removed duplicate block)

        cursor = conn.execute("PRAGMA table_info(workers)")
        worker_columns = [row["name"] for row in cursor.fetchall()]
        if "projects" not in worker_columns and len(worker_columns) > 0:
            conn.execute("ALTER TABLE workers ADD COLUMN projects TEXT NOT NULL DEFAULT '[]'")
        if "gpus_json" not in worker_columns and len(worker_columns) > 0:
            conn.execute("ALTER TABLE workers ADD COLUMN gpus_json TEXT NOT NULL DEFAULT '[]'")
        if "version" not in worker_columns and len(worker_columns) > 0:
            conn.execute("ALTER TABLE workers ADD COLUMN version TEXT")
        if "schema_version" not in worker_columns and len(worker_columns) > 0:
            conn.execute("ALTER TABLE workers ADD COLUMN schema_version INTEGER")
        if "registered_ip" not in worker_columns and len(worker_columns) > 0:
            # F-007: track registration IP to detect worker impersonation
            conn.execute("ALTER TABLE workers ADD COLUMN registered_ip TEXT")
        if "load_json" not in worker_columns and len(worker_columns) > 0:
            # R-008: last heartbeat live-load snapshot (RAM/VRAM/CPU used)
            conn.execute("ALTER TABLE workers ADD COLUMN load_json TEXT NOT NULL DEFAULT '{}'")
        if "session_id" not in worker_columns and len(worker_columns) > 0:
            # R-005: per-agent-start session fencing
            conn.execute("ALTER TABLE workers ADD COLUMN session_id TEXT")
        if "worker_epoch" not in worker_columns and len(worker_columns) > 0:
            conn.execute("ALTER TABLE workers ADD COLUMN worker_epoch INTEGER NOT NULL DEFAULT 0")
        if "credential_hash" not in worker_columns and len(worker_columns) > 0:
            # C1: per-worker credential hash (sha256 of worker_token)
            conn.execute("ALTER TABLE workers ADD COLUMN credential_hash TEXT")
        if "trust" not in worker_columns and len(worker_columns) > 0:
            # C2: worker trust tier
            conn.execute("ALTER TABLE workers ADD COLUMN trust TEXT NOT NULL DEFAULT 'trusted'")
        if "instance_id" not in worker_columns and len(worker_columns) > 0:
            # C3: worker instance fencing
            conn.execute("ALTER TABLE workers ADD COLUMN instance_id TEXT")

        # A1: Migrate requires_paths from absolute to relative (normalize separators)
        try:
            rows = conn.execute(
                "SELECT task_id, project, requires_paths FROM tasks "
                "WHERE requires_paths LIKE '%\\\\%'"
            ).fetchall()
            for row in rows:
                paths = json_list(row["requires_paths"])
                normalized = [str(p).replace("\\", "/") for p in paths]
                if paths and normalized != [str(p) for p in paths]:
                    conn.execute(
                        "UPDATE tasks SET requires_paths = ? WHERE project = ? AND task_id = ?",
                        (json.dumps(normalized), row["project"], row["task_id"]),
                    )
            conn.commit()
        except Exception as exc:
            # Best-effort (paths stay un-normalised), but never silent.
            conn.rollback()
            print(f"[queue] warn: requires_paths normalisation skipped: {exc}", file=sys.stderr, flush=True)

        # Per-token project allowlist (JSON list; NULL/empty = unrestricted).
        _tok_cols = [row["name"] for row in conn.execute("PRAGMA table_info(api_tokens)").fetchall()]
        if _tok_cols and "projects" not in _tok_cols:
            conn.execute("ALTER TABLE api_tokens ADD COLUMN projects TEXT")

        cursor = conn.execute("PRAGMA table_info(runs)")
        run_columns = [row["name"] for row in cursor.fetchall()]
        if "worker_session_id" not in run_columns and len(run_columns) > 0:
            conn.execute("ALTER TABLE runs ADD COLUMN worker_session_id TEXT")
        if "package_hash" not in run_columns and len(run_columns) > 0:
            conn.execute("ALTER TABLE runs ADD COLUMN package_hash TEXT")
        if "wall_sec" not in run_columns and len(run_columns) > 0:
            # G3: elapsed wall-clock seconds recorded at finalize time
            conn.execute("ALTER TABLE runs ADD COLUMN wall_sec INTEGER")
        # Run diff: spec snapshot (command, effective args, env *names* only - never values).
        for _col in ("spec_command", "args_json", "env_keys_json"):
            if _col not in run_columns and len(run_columns) > 0:
                conn.execute(f"ALTER TABLE runs ADD COLUMN {_col} TEXT")
        conn.execute(
            "CREATE TRIGGER IF NOT EXISTS trg_runs_spec_snapshot AFTER INSERT ON runs "
            "BEGIN "
            "UPDATE runs SET "
            "spec_command = (SELECT command FROM tasks WHERE project = NEW.project AND task_id = NEW.task_id), "
            "args_json = (SELECT COALESCE(args_override, args) FROM tasks "
            "WHERE project = NEW.project AND task_id = NEW.task_id), "
            "env_keys_json = (SELECT json_group_array(key) FROM json_each("
            "(SELECT CASE WHEN json_valid(env) THEN env ELSE '{}' END FROM tasks "
            "WHERE project = NEW.project AND task_id = NEW.task_id))) "
            "WHERE run_id = NEW.run_id; "
            "END"
        )

        # F-058: fencing epoch for master_lease (daemon incarnation identity)
        lease_cols = [
            row["name"] for row in conn.execute("PRAGMA table_info(master_lease)").fetchall()
        ]
        if lease_cols and "epoch" not in lease_cols:
            conn.execute(
                "ALTER TABLE master_lease ADD COLUMN epoch INTEGER NOT NULL DEFAULT 0"
            )
        conn.commit()
    except Exception:
        # W0.4: migration failures must surface loudly — a silently-ignored failed
        # ALTER is worse than a crash because the DB appears valid while columns are missing.
        import logging as _logging
        _logging.getLogger(__name__).exception("open_db: migration failed — check schema")
        raise
    _seed_config(conn)
    try:
        from taskqueue.system_alerts import seed_system_alerts

        seed_system_alerts(conn)
    except Exception:
        import logging as _logging
        _logging.getLogger(__name__).exception("open_db: system_alerts seed failed")
        raise
    # Only write schema_version when it differs — avoids a write on every CLI
    # invocation (tq status, tq list, …) which contended the WAL write lock with
    # the daemon and caused spurious "database is locked" errors.
    row = conn.execute("SELECT value FROM config WHERE key='schema_version'").fetchone()
    stored = json.loads(row["value"]) if row else None
    if stored != SCHEMA_VERSION:
        conn.execute(
            "INSERT OR REPLACE INTO config (key, value) VALUES ('schema_version', ?)",
            (json.dumps(SCHEMA_VERSION),),
        )
        conn.commit()
    return conn


def _seed_config(conn: sqlite3.Connection) -> None:
    from taskqueue.config import VALID_CONFIG_KEYS as _VCK
    _keep = set(_CORE_CONFIG_KEYS) | set(_VCK.keys())
    # Read-only pre-check: skip all writes if the DB is already fully seeded and
    # contains no stale keys.  This matters for CLI commands (tq status, tq list,
    # etc.) that call open_db while the daemon may be mid-transaction: a pure read
    # never contends on the WAL write lock, avoiding spurious "database is locked".
    # Monotonic write-counter avoids false hits on 1-second mtime-resolution FSes.
    global _seed_config_writes
    try:
        db_path_str = conn.execute("PRAGMA database_list").fetchone()[2] or ""
    except Exception:
        db_path_str = ""

    if db_path_str and _seed_config_cache.get(db_path_str) == _seed_config_writes:
        return

    existing = {r["key"] for r in conn.execute("SELECT key FROM config").fetchall()}
    needs_insert = any(k not in existing for k in _DEFAULT_CONFIG) or "schema_version" not in existing
    stale = {
        k for k in existing
        if k not in _keep
        and not k.startswith("project_paused_")
        and not k.startswith("project_limit_")
        and not k.startswith("project_frozen_")
        and not k.startswith("project_color_")
        and not k.startswith("freeze_")
    }
    if not needs_insert and not stale:
        if db_path_str:
            _seed_config_cache[db_path_str] = _seed_config_writes
        return

    for key, value in _DEFAULT_CONFIG.items():
        conn.execute(
            "INSERT OR IGNORE INTO config (key, value) VALUES (?, ?)",
            (key, json.dumps(value)),
        )
    conn.execute(
        "INSERT OR IGNORE INTO config (key, value) VALUES ('schema_version', ?)",
        (json.dumps(SCHEMA_VERSION),),
    )
    # F-034: keep-set is the union of hard-coded core keys and every key
    # reachable via `tq config set` (VALID_CONFIG_KEYS).  Any key not in
    # either bucket is pruned — this prevents accumulation of stale entries
    # without silently deleting operator-configured values.
    for k in stale:
        conn.execute("DELETE FROM config WHERE key = ?", (k,))
    conn.commit()
    _seed_config_writes += 1
    if db_path_str:
        _seed_config_cache[db_path_str] = _seed_config_writes


_seed_config_writes: int = 0
_seed_config_cache: dict[str, int] = {}


def bump_seed_config_writes() -> None:
    """Invalidate the "already seeded" cache after any config write (called by ``db.set_config``)."""
    global _seed_config_writes
    _seed_config_writes += 1
