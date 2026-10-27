"""Task CRUD — extracted from db.py to keep that module manageable.

All public names are re-exported via ``from taskqueue._db_tasks import *``
in ``db.py``, so callers continue to use ``from taskqueue.db import X``.
"""

from __future__ import annotations
from taskqueue.deprefs import row_dep_refs
from taskqueue.jsonutil import json_list
from taskqueue.project_limits import get_all_project_limits

import json
import sqlite3
from collections import deque
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from taskqueue.models import STATUS_QUEUED, QueueTask, TaskResources  # noqa: F401
from taskqueue._time import _now  # noqa: F401  # canonical ISO timestamp helper


SUBMIT_CHANNELS = frozenset({"cli", "mcp", "web", "api", "yaml", "daemon"})


def stamp_provenance(
    conn: sqlite3.Connection,
    project: str,
    task_ids: Iterable[str],
    via: str,
    by: str | None = None,
    client: str | None = None,
    *,
    commit: bool = True,
) -> None:
    """Record who/how a task was created. Set once: never overwrites a real stamp.

    An explicit channel (cli/mcp/web/api) may replace a provisional ``yaml`` stamp
    (the daemon's directory scan can race the submitting call); ``yaml``/``daemon``
    only fill empty rows. Fail-open: analytics must never break submission.
    """
    via = via if via in SUBMIT_CHANNELS else "api"
    by = (by or "").strip()[:64] or None
    client = (client or "").strip()[:64] or None
    guard = (
        "submitted_via IS NULL"
        if via in ("yaml", "daemon")
        else "(submitted_via IS NULL OR submitted_via = 'yaml')"
    )
    try:
        for tid in task_ids:
            conn.execute(
                "UPDATE tasks SET submitted_via = ?, submitted_by = ?, client_info = ? "
                f"WHERE project = ? AND task_id = ? AND {guard}",
                (via, by, client, project, tid),
            )
        if commit:
            conn.commit()
    except sqlite3.Error:
        pass


# --- task reads ---

def _raw_failed_run_count(conn: sqlite3.Connection, project: str, task_id: str) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM runs WHERE project = ? AND task_id = ? AND status = 'failed'",
        (project, task_id),
    ).fetchone()
    return int(row["n"] if row else 0)


def run_summary(conn: sqlite3.Connection, project: str, task_id: str) -> dict:
    """Run-history stamp so agents can tell whether a task was (re-)run.

    ``run_number`` is the 1-based count of runs of this task (0 = never ran);
    it increments when a retry actually starts executing, while ``run_id``
    stays the global id of the latest run.
    """
    row = conn.execute(
        "SELECT COUNT(*) AS n, MAX(run_id) AS last_id FROM runs WHERE project = ? AND task_id = ?",
        (project, task_id),
    ).fetchone()
    n = int(row["n"] or 0) if row else 0
    out: dict = {"run_number": n, "last_run_id": None,
                 "last_run_started_at": None, "last_run_finished_at": None}
    if n and row["last_id"] is not None:
        r = conn.execute(
            "SELECT run_id, started_at, finished_at FROM runs WHERE run_id = ?",
            (row["last_id"],),
        ).fetchone()
        if r:
            out.update(last_run_id=r["run_id"], last_run_started_at=r["started_at"],
                       last_run_finished_at=r["finished_at"])
    return out


def run_counts(conn: sqlite3.Connection, pairs: list[tuple[str, str]]) -> dict[tuple[str, str], int]:
    """Batched ``run_number`` for many tasks (one query per 400 task ids)."""
    out: dict[tuple[str, str], int] = {}
    by_project: dict[str, set[str]] = {}
    for project, task_id in pairs:
        by_project.setdefault(project, set()).add(task_id)
    # project is the leading column of idx_runs_project_task, so each query is an
    # index range scan instead of a full-table scan on task_id alone.
    for project, tids in by_project.items():
        ids = sorted(tids)
        for i in range(0, len(ids), 400):
            chunk = ids[i:i + 400]
            ph = ",".join("?" * len(chunk))
            for r in conn.execute(
                f"SELECT task_id, COUNT(*) AS n FROM runs WHERE project = ? AND task_id IN ({ph}) "
                "GROUP BY task_id",
                [project, *chunk],
            ):
                out[(project, r["task_id"])] = int(r["n"])
    return out


def prior_run_seconds(
    conn: sqlite3.Connection, project: str, task_id: str, run_id: int | None
) -> float:
    """Wall time of earlier runs of this task that the run ``run_id`` resumed.

    Counts runs before ``run_id`` and after the task's last completed run, so a restarted task's
    ETA can use its whole running time instead of only the time since the restart.
    """
    if not run_id:
        return 0.0
    row = conn.execute(
        "SELECT COALESCE(SUM(COALESCE(wall_sec, "
        "  MAX(0, (julianday(finished_at) - julianday(started_at)) * 86400.0))), 0.0) AS s "
        "FROM runs WHERE project = ? AND task_id = ? AND run_id < ? AND finished_at IS NOT NULL "
        "AND run_id > COALESCE((SELECT MAX(run_id) FROM runs WHERE project = ? AND task_id = ? "
        "                      AND status = 'completed'), 0)",
        (project, task_id, run_id, project, task_id),
    ).fetchone()
    return round(float(row["s"] or 0.0), 1)


def bulk_prior_run_seconds(
    conn: sqlite3.Connection,
    open_runs: dict[tuple[str, str], Any],
) -> dict[tuple[str, str], float]:
    """``prior_run_seconds`` for many open runs in one scan (Timeline forecast)."""
    if not open_runs:
        return {}
    keys = list(open_runs.keys())
    out: dict[tuple[str, str], float] = {k: 0.0 for k in keys}
    chunk = 400
    for i in range(0, len(keys), chunk):
        part = keys[i : i + chunk]
        tokens = [f"{p}/{t}" for p, t in part]
        placeholders = ",".join("(?)" for _ in part)
        rows = conn.execute(
            "SELECT project, task_id, run_id, status,"
            " COALESCE(wall_sec,"
            "  CASE WHEN finished_at IS NOT NULL AND started_at IS NOT NULL"
            "       THEN MAX(0, (julianday(finished_at) - julianday(started_at)) * 86400.0)"
            "       ELSE 0 END) AS wall"
            " FROM runs"
            f" WHERE (project || '/' || task_id) IN ({placeholders})"
            " ORDER BY project, task_id, run_id",
            tokens,
        ).fetchall()
        by_task: dict[tuple[str, str], list[Any]] = {}
        for r in rows:
            by_task.setdefault((r["project"] or "", r["task_id"] or ""), []).append(r)
        for key, runs in by_task.items():
            open_row = open_runs.get(key)
            if open_row is None:
                continue
            open_id = int(open_row["run_id"])
            last_completed = 0
            for r in runs:
                rid = int(r["run_id"])
                if rid >= open_id:
                    break
                if r["status"] == "completed":
                    last_completed = rid
            total = 0.0
            for r in runs:
                rid = int(r["run_id"])
                if rid <= last_completed or rid >= open_id:
                    continue
                if r["wall"] is not None:
                    total += float(r["wall"])
            out[key] = round(total, 1)
    return out


def failed_attempt_count(
    conn: sqlite3.Connection, project: str, task_id: str, *, baseline: int | None = None,
) -> int:
    """Failed runs since the last explicit retry; pass *baseline* from a loaded task row to skip a query."""
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM runs WHERE project = ? AND task_id = ? AND status = 'failed'",
        (project, task_id),
    ).fetchone()
    total = int(row["n"] if row else 0)
    # Subtract baseline set on explicit retry so the max_attempts window resets.
    if baseline is None:
        baseline_row = conn.execute(
            "SELECT retry_attempt_baseline FROM tasks WHERE project = ? AND task_id = ?",
            (project, task_id),
        ).fetchone()
        baseline = int((baseline_row["retry_attempt_baseline"] if baseline_row else None) or 0)
    return max(0, total - int(baseline))


def failed_attempt_counts_bulk(
    conn: sqlite3.Connection,
    keys: list[tuple[str, str]],
    *,
    baselines: dict[tuple[str, str], int] | None = None,
) -> dict[tuple[str, str], int]:
    """Batch failed-run counts for the admit candidate window (Wave C)."""
    if not keys:
        return {}
    baselines = baselines or {}
    out: dict[tuple[str, str], int] = {k: 0 for k in keys}
    chunk = 400
    uniq = list(dict.fromkeys(keys))
    for i in range(0, len(uniq), chunk):
        part = uniq[i : i + chunk]
        marks = ",".join("(?, ?)" for _ in part)
        flat = [x for k in part for x in k]
        for r in conn.execute(
            f"SELECT project, task_id, COUNT(*) AS n FROM runs "
            f"WHERE status = 'failed' AND (project, task_id) IN ({marks}) "
            f"GROUP BY project, task_id",
            flat,
        ):
            key = (str(r["project"]), str(r["task_id"]))
            out[key] = max(0, int(r["n"]) - int(baselines.get(key, 0)))
    return out


# --- task upsert / sync ---

def _yaml_num_from_source(yaml_source: str) -> "int | None":
    import re as _re
    m = _re.match(r"^(\d+)", Path(yaml_source).name) if yaml_source else None
    return int(m.group(1)) if m else None


# Column order here IS the parameter order of _upsert_task_params.
_UPSERT_TASK_COLUMNS = (
    "project", "task_id", "yaml_source", "yaml_num", "priority", "status", "command", "args",
    "resume_args", "wall_clock_cap_sec", "ledger_subdir", "requires_paths",
    "resources", "exclusive", "exclusive_project", "description", "enabled", "depends_on",
    "depends_on_any", "enqueued_at", "updated_at", "dispatch", "requires", "bundle_hash",
    "code_provider", "max_attempts", "notify_url", "tags", "suite", "env", "idempotent",
    "execution_semantics", "metadata", "grp", "artifacts", "artifacts_required",
    "on_complete", "on_fail", "transfer", "requires_services", "overridable_args",
    "hung_task_k", "schedule", "rearm_on", "teardown", "auto_retry_on_cap", "max_auto_retries",
    "data", "min_trust", "task_class", "on_dependency_failure", "negative_exit_codes",
    "on_dependency_negative",
)
# Not refreshed from YAML on conflict: identity, live status (CASE below), first-enqueue time.
_UPSERT_KEEP = frozenset({"project", "task_id", "status", "enqueued_at"})
_UPSERT_SET = ",\n            ".join(
    f"{c} = excluded.{c}" for c in _UPSERT_TASK_COLUMNS if c not in _UPSERT_KEEP
)
_UPSERT_TASK_SQL = f"""
        INSERT INTO tasks ({", ".join(_UPSERT_TASK_COLUMNS)})
        VALUES ({", ".join("?" * len(_UPSERT_TASK_COLUMNS))})
        ON CONFLICT(project, task_id) DO UPDATE SET
            {_UPSERT_SET},
            status             = CASE
                WHEN excluded.enabled = 1 AND tasks.status = 'disabled' THEN 'queued'
                WHEN excluded.enabled = 0 AND tasks.status IN ('queued', 'interrupted', 'failed', 'suspended', 'unknown')
                    THEN 'disabled'
                ELSE tasks.status
            END,
            args_override      = CASE WHEN excluded.args != tasks.args THEN NULL ELSE tasks.args_override END
        WHERE tasks.status NOT IN ('running')
        """


def _upsert_task_params(task: QueueTask) -> tuple:
    yaml_num = _yaml_num_from_source(task.yaml_source)
    resources = json.dumps({
        "cpu_cores": task.resources.cpu_cores,
        "ram_gib": task.resources.ram_gib,
        "gpu_vram_gib": task.resources.gpu_vram_gib,
        "locks": list(task.resources.locks),
    })
    now = _now()
    return (
        task.project,
        task.task_id,
        task.yaml_source,
        yaml_num,
        task.priority,
        STATUS_QUEUED if task.enabled else "disabled",
        str(task.command),
        json.dumps(list(task.args)),
        json.dumps(list(task.resume_args)) if task.resume_args else None,
        task.wall_clock_cap_sec,
        task.ledger_subdir,
        json.dumps([str(p) for p in task.requires_paths]),
        resources,
        int(task.exclusive),
        int(task.exclusive_project),
        task.description,
        int(task.enabled),
        json.dumps(list(task.depends_on)),
        json.dumps(list(task.depends_on_any)),
        now,
        now,
        task.dispatch,
        json.dumps(task.requires),
        task.bundle_hash,
        task.code_provider or "",
        int(task.max_attempts or 0),
        task.notify_url,
        json.dumps(list(task.tags)),
        task.suite,
        json.dumps(task.env),
        int(task.idempotent),
        task.execution_semantics or "non_idempotent",
        json.dumps(task.metadata),
        task.group,
        json.dumps(list(task.artifacts)),
        int(task.artifacts_required),
        json.dumps(list(task.on_complete)),
        json.dumps(list(task.on_fail)),
        json.dumps(list(task.transfer)),
        json.dumps(list(task.requires_services)),
        json.dumps(list(task.overridable_args)),
        task.hung_task_k,
        json.dumps(task.schedule) if task.schedule else None,
        task.rearm_on or "",
        json.dumps(task.teardown) if task.teardown else None,
        int(bool(task.auto_retry_on_cap)),
        int(task.max_auto_retries if task.max_auto_retries is not None else 20),
        json.dumps(task.data) if task.data is not None else None,
        task.min_trust or "",
        task.task_class or "normal",
        task.on_dependency_failure or "block",
        json.dumps(list(getattr(task, "negative_exit_codes", ()) or ())),
        getattr(task, "on_dependency_negative", "run") or "run",
    )


def _upsert_task_sql(conn: sqlite3.Connection, task: QueueTask) -> None:
    conn.execute(_UPSERT_TASK_SQL, _upsert_task_params(task))


def upsert_task(conn: sqlite3.Connection, task: QueueTask) -> None:
    """Insert or update a task definition. Does not change status of running tasks.

    When a previously-disabled task is re-enabled in its YAML, the sync promotes
    its status from 'disabled' back to 'queued' so the scheduler picks it up.
    Already-completed tasks are NOT re-queued automatically.
    """
    _upsert_task_sql(conn, task)
    conn.commit()


def _upsert_task_no_commit(conn: sqlite3.Connection, task: "QueueTask") -> None:
    """Like upsert_task but skips conn.commit() — caller batches the commit."""
    _upsert_task_sql(conn, task)


# --- task reads ---

def get_queued_tasks(
    conn: sqlite3.Connection,
    *,
    limit: int | None = None,
    status_map: dict[tuple[str, str], str] | None = None,
    skip_projects: set[str] | frozenset[str] | None = None,
    extra_scan: int = 5000,
    statuses: tuple[str, ...] = ("queued", "stopped"),
) -> list[sqlite3.Row]:
    """Return enabled queued/stopped tasks across all projects, by priority then age.

    With *limit* and *status_map*, the window is the first *limit* rows plus up to
    *limit* further rows whose dependencies are met and whose project is not in
    *skip_projects* (scanning at most *extra_scan* rows past the window). Without
    this, *limit* blocked high-priority tasks hide every runnable lower-priority one.

    ``stopped`` (host RAM eviction) is auto-restartable when soft pressure clears.
    Higher ``priority`` runs sooner (DESC), then older ``enqueued_at``.
    Optional ``limit`` caps the candidate window (local admit / worker claim).
    """
    from taskqueue.rows import admit_task_select_sql

    marks = ",".join("?" for _ in statuses)
    # F-128: omit env/yaml_source/args_override/metadata on the hot admit scan.
    base = admit_task_select_sql(conn)
    sql = (
        f"{base} WHERE status IN ({marks}) AND enabled = 1 "
        "ORDER BY priority DESC, enqueued_at ASC"
    )
    if limit is not None and int(limit) > 0:
        n = int(limit)
        if status_map is None:
            return conn.execute(sql + " LIMIT ?", (*statuses, n)).fetchall()
        cur = conn.execute(sql + " LIMIT ?", (*statuses, n + max(0, int(extra_scan))))
        head = cur.fetchmany(n)
        if len(head) < n:
            return head
        extra: list[sqlite3.Row] = []
        skip = skip_projects or ()
        scanned = 0
        for row in cur:
            scanned += 1
            if row["project"] not in skip and unmet_dependency_label(
                conn, row["project"], row["task_id"], status_map=status_map, row=row
            ) is None:
                extra.append(row)
                if len(extra) >= n:
                    break
        return head + extra
    return conn.execute(sql, tuple(statuses)).fetchall()


def count_queued_tasks(conn: sqlite3.Connection, project: str | None = None) -> int:
    """Count enabled queued/stopped tasks — cheap alternative to len(get_queued_tasks())."""
    sql = (
        "SELECT COUNT(*) AS n FROM tasks "
        "WHERE status IN ('queued', 'stopped') AND enabled = 1"
    )
    params: list = []
    if project:
        sql += " AND project = ?"
        params.append(project)
    row = conn.execute(sql, params).fetchone()
    return int(row["n"] if row else 0)


def count_online_workers(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM workers WHERE status = 'online'"
    ).fetchone()
    return int(row["n"] if row else 0)


def get_running_tasks(conn: sqlite3.Connection, project: str | None = None) -> list[sqlite3.Row]:
    from taskqueue.rows import admit_task_select_sql

    base = admit_task_select_sql(conn)
    if project:
        return conn.execute(
            f"{base} WHERE status = 'running' AND project = ?",
            (project,),
        ).fetchall()
    return conn.execute(f"{base} WHERE status = 'running'").fetchall()


def get_all_tasks(conn: sqlite3.Connection, project: str | None = None) -> list[sqlite3.Row]:
    if project:
        return conn.execute(
            "SELECT * FROM tasks WHERE project = ? ORDER BY priority DESC, enqueued_at ASC",
            (project,),
        ).fetchall()
    return conn.execute(
        "SELECT * FROM tasks ORDER BY priority DESC, enqueued_at ASC"
    ).fetchall()


def get_task(conn: sqlite3.Connection, project: str, task_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM tasks WHERE project = ? AND task_id = ?", (project, task_id)
    ).fetchone()


def get_task_status(conn: sqlite3.Connection, project: str, task_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT status FROM tasks WHERE project = ? AND task_id = ?", (project, task_id)
    ).fetchone()


def check_ownership(started_by: str | None, operator: str | None, *, force: bool = False) -> str | None:
    """Return an error message if a cross-operator mutation should be
    blocked, or None if it's OK to proceed.

    Back-compat by design: a task with no `started_by` (never attributed,
    or from before attribution existed) is never blocked -- this is a
    warning gate for known ownership conflicts, not a real access-control
    system (no auth is tied to `operator`; it's a self-reported TQ_OPERATOR
    string). `force=True` always overrides; callers are expected to log the
    override when they honor it.
    """
    if force:
        return None
    if not started_by:
        return None
    if operator and operator == started_by:
        return None
    return (
        f"owned by operator '{started_by}' (you are "
        f"'{operator or '(unset TQ_OPERATOR)'}') -- pass --force to override"
    )


# --- task status mutations ---

_TERMINAL_STATUSES = frozenset(
    {"completed", "failed", "interrupted", "stopped", "suspended", "unknown", "cancelled"}
)

# Statuses accepted by retry_task / suite retry / group retry (incl. completed).
RETRYABLE_STATUSES = frozenset(
    {"failed", "interrupted", "suspended", "stopped", "completed", "unknown", "cancelled"}
)


def set_task_status(conn: sqlite3.Connection, project: str, task_id: str, status: str) -> None:
    now = _now()
    if status in _TERMINAL_STATUSES:
        conn.execute(
            "UPDATE tasks SET status = ?, updated_at = ?, finished_at = ? WHERE project = ? AND task_id = ?",
            (status, now, now, project, task_id),
        )
    else:
        conn.execute(
            "UPDATE tasks SET status = ?, updated_at = ? WHERE project = ? AND task_id = ?",
            (status, now, project, task_id),
        )
    conn.commit()


def claim_task_running(
    conn: sqlite3.Connection,
    project: str,
    task_id: str,
    *,
    commit: bool = True,
) -> bool:
    """Atomically claim a queued/stopped enabled task. Returns False if already claimed/disabled.

    Pass ``commit=False`` when the caller holds ``BEGIN IMMEDIATE`` and will
    commit together with ``insert_run`` (F-088 local start).
    """
    cur = conn.execute(
        "UPDATE tasks SET status = 'running', force_run = 0, block_reason = NULL, "
        "last_error_code = NULL, updated_at = ? "
        "WHERE project = ? AND task_id = ? AND status IN ('queued', 'stopped') AND enabled = 1",
        (_now(), project, task_id),
    )
    if commit:
        conn.commit()
    return (cur.rowcount or 0) > 0


def claim_and_insert_local_run(
    conn: sqlite3.Connection,
    project: str,
    task_id: str,
    log_path: str,
    *,
    clear_args_override: bool = False,
) -> int | None:
    """F-088: BEGIN IMMEDIATE claim + insert_run in one transaction.

    Returns ``run_id`` on success, or ``None`` if the claim lost the race.
    On failure after claim (unexpected), rolls back so the task is not left
    ``running`` without a run row.
    """
    if conn.in_transaction:
        conn.rollback()
    conn.execute("BEGIN IMMEDIATE")
    try:
        if not claim_task_running(conn, project, task_id, commit=False):
            conn.rollback()
            return None
        cur = conn.execute(
            "INSERT INTO runs (project, task_id, started_at, status, log_path, worker_id) "
            "VALUES (?, ?, ?, 'running', ?, NULL)",
            (project, task_id, _now(), log_path),
        )
        run_id = int(cur.lastrowid or 0)
        conn.execute(
            "UPDATE tasks SET last_run_id = ? WHERE project = ? AND task_id = ?",
            (run_id, project, task_id),
        )
        if clear_args_override:
            conn.execute(
                "UPDATE tasks SET args_override = NULL WHERE project = ? AND task_id = ?",
                (project, task_id),
            )
        conn.commit()
        return run_id
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise


def set_task_error(
    conn: sqlite3.Connection,
    project: str,
    task_id: str,
    code: str | None,
    *,
    block_reason: str | None = None,
    commit: bool = True,
) -> None:
    """Set last_error_code and optional block_reason.

    When ``code`` is set and ``block_reason`` is omitted/None, fill
    ``block_reason`` from ``errors.describe(code)`` so the UI never has to
    show a bare ``tq.*`` code as the only text. Pass ``code=None`` to clear both.
    Pass ``commit=False`` to batch writes (caller commits once).
    """
    if code and not block_reason:
        from taskqueue.errors import describe
        block_reason = describe(code) or code
    conn.execute(
        "UPDATE tasks SET last_error_code = ?, block_reason = ?, updated_at = ? "
        "WHERE project = ? AND task_id = ?",
        (code, block_reason, _now(), project, task_id),
    )
    if commit:
        conn.commit()


def clear_task_error(conn: sqlite3.Connection, project: str, task_id: str) -> None:
    set_task_error(conn, project, task_id, None, block_reason=None)


def set_spawned_by(
    conn: sqlite3.Connection,
    project: str,
    task_id: str,
    parent_ref: str,
) -> None:
    """Record which task re-queued this one via on_complete / on_fail / rearm_on."""
    conn.execute(
        "UPDATE tasks SET spawned_by = ?, updated_at = ? WHERE project = ? AND task_id = ?",
        (str(parent_ref), _now(), project, task_id),
    )
    conn.commit()


def set_started_by(
    conn: sqlite3.Connection,
    project: str,
    task_id: str,
    operator: str,
    *,
    overwrite: bool = False,
) -> None:
    if not operator:
        return
    if overwrite:
        conn.execute(
            "UPDATE tasks SET started_by = ?, updated_at = ? WHERE project = ? AND task_id = ?",
            (operator, _now(), project, task_id),
        )
    else:
        conn.execute(
            "UPDATE tasks SET started_by = ?, updated_at = ? "
            "WHERE project = ? AND task_id = ? AND (started_by IS NULL OR started_by = '')",
            (operator, _now(), project, task_id),
        )
    conn.commit()


def set_task_priority(conn: sqlite3.Connection, project: str, task_id: str, priority: int) -> None:
    conn.execute(
        "UPDATE tasks SET priority = ?, updated_at = ? WHERE project = ? AND task_id = ?",
        (priority, _now(), project, task_id),
    )
    conn.commit()


def request_stop(conn: sqlite3.Connection, project: str, task_id: str) -> None:
    conn.execute(
        "UPDATE tasks SET stop_requested = 1, updated_at = ? WHERE project = ? AND task_id = ?",
        (_now(), project, task_id),
    )
    conn.commit()


def request_stop_with_control(
    conn: sqlite3.Connection,
    home: Path,
    project: str,
    task_id: str,
    action: str = "suspend",
    requested_by: str | None = None,
) -> Path:
    """Cooperative stop: control file + stop_requested (CLI and HTTP)."""
    path = write_control_action(home, project, task_id, action, requested_by=requested_by)
    request_stop(conn, project, task_id)
    return path


def clear_stop_request(conn: sqlite3.Connection, project: str, task_id: str) -> None:
    conn.execute(
        "UPDATE tasks SET stop_requested = 0, updated_at = ? WHERE project = ? AND task_id = ?",
        (_now(), project, task_id),
    )
    conn.commit()


def retry_skip_reason(conn: sqlite3.Connection, project: str, task_id: str) -> str | None:
    """Return why retry would be skipped, or None if retry can proceed."""
    row = get_task(conn, project, task_id)
    if row is None:
        return "not_found"
    if not int(row["enabled"] or 0):
        return "disabled"
    status = str(row["status"] or "")
    if status not in RETRYABLE_STATUSES:
        return f"status={status}"
    max_a = int(row["max_attempts"] or 0) if "max_attempts" in row.keys() else 0
    if max_a > 0 and failed_attempt_count(conn, project, task_id) >= max_a:
        return "max_attempts"
    return None


# Preflight / missing-path failures often flap (path appears briefly, then vanishes).
# Keep the diagnostic on the row across explicit retry until a successful claim clears it.
_STICKY_PREFLIGHT_CODES = frozenset({"tq.run.preflight", "tq.block.requires_paths"})


def _requeue_row(
    conn: sqlite3.Connection, project: str, task_id: str, *, require_enabled: bool
) -> bool:
    """UPDATE one retryable task back to ``queued`` (no commit). True if changed.

    ``require_enabled=False`` also re-enables a disabled task (group restart).
    Sticky: when ``last_error_code`` is a preflight code, keep code + ``block_reason``
    so the UI does not lose the reason between retries (cleared on claim/start).
    """
    # Snapshot the current failed-run count so max_attempts counts only new
    # failures after this explicit retry (not accumulated history).
    baseline = _raw_failed_run_count(conn, project, task_id)
    placeholders = ",".join("?" * len(RETRYABLE_STATUSES))
    enabled_clause = "AND enabled = 1 " if require_enabled else ""
    prior = conn.execute(
        "SELECT last_error_code, block_reason FROM tasks WHERE project = ? AND task_id = ?",
        (project, task_id),
    ).fetchone()
    sticky_code = None
    sticky_reason = None
    if prior is not None:
        code = (prior["last_error_code"] or "") if hasattr(prior, "keys") else (prior[0] or "")
        reason = (prior["block_reason"] if hasattr(prior, "keys") else prior[1])
        if code in _STICKY_PREFLIGHT_CODES:
            sticky_code, sticky_reason = code, reason
    cur = conn.execute(
        "UPDATE tasks SET status = 'queued', enabled = 1, stop_requested = 0, "
        "cap_auto_retry_count = 0, last_error_code = ?, block_reason = ?, "
        "retry_attempt_baseline = ?, "
        "updated_at = ?, finished_at = NULL "
        f"WHERE project = ? AND task_id = ? {enabled_clause}AND status IN ({placeholders})",
        (sticky_code, sticky_reason, baseline, _now(), project, task_id, *sorted(RETRYABLE_STATUSES)),
    )
    return (cur.rowcount or 0) > 0


def retry_task(
    conn: sqlite3.Connection,
    project: str,
    task_id: str,
    *,
    home: Path | None = None,
    force_enable: bool = False,
) -> bool:
    """Re-queue a retryable enabled task. Returns True if changed.

    When ``force_enable=True``, a disabled task is re-enabled as part of the
    retry (used by group "Restart all" to un-disable completed pipeline tasks).

    When a previously-completed task is re-queued, any *running* dependents that
    list it in ``depends_on`` are cooperatively stopped (``tq.stop.deps``) so
    they cannot keep executing against invalidated upstream work.
    """
    skip = retry_skip_reason(conn, project, task_id)
    # F-123: explicit retry resets the max_attempts window via retry_attempt_baseline.
    if skip == "max_attempts":
        skip = None
    if skip is not None and not (force_enable and skip == "disabled"):
        return False
    changed = _requeue_row(conn, project, task_id, require_enabled=not force_enable)
    conn.commit()
    if not changed:
        return False
    invalidate_running_dependents(conn, project, task_id, home=home)
    try:
        from taskqueue.sched_wake import wake_scheduler
        wake_scheduler()
    except Exception:
        pass
    return True


def _retry_task_no_commit(
    conn: sqlite3.Connection,
    project: str,
    task_id: str,
) -> bool:
    """Like retry_task but no commit / invalidate — for batch transactions."""
    skip = retry_skip_reason(conn, project, task_id)
    if skip == "max_attempts":
        skip = None  # F-123: batch retry also resets the attempts window
    if skip is not None:
        return False
    return _requeue_row(conn, project, task_id, require_enabled=True)


def retry_tasks_batch(
    conn: sqlite3.Connection,
    keys: list[tuple[str, str]],
    *,
    home: Path | None = None,
) -> dict[str, list]:
    """Re-queue many tasks in one IMMEDIATE transaction.

    Returns ``{"requeued": ["proj/tid", ...], "skipped": [{"key": ..., "reason": ...}, ...]}``.
    Dependent invalidation runs after commit to avoid nested commits.
    """
    requeued: list[str] = []
    skipped: list[dict] = []
    seen: set[tuple[str, str]] = set()
    ordered: list[tuple[str, str]] = []
    for project, task_id in keys:
        k = (project, task_id)
        if k in seen:
            continue
        seen.add(k)
        ordered.append(k)

    try:
        conn.commit()
    except Exception:
        pass
    try:
        conn.execute("BEGIN IMMEDIATE")
    except sqlite3.OperationalError as exc:
        # Already inside a transaction is fine; a lock timeout must not degrade
        # the batch into non-atomic per-row writes.
        if "within a transaction" not in str(exc):
            raise
    try:
        for project, task_id in ordered:
            reason = retry_skip_reason(conn, project, task_id)
            if reason is not None:
                skipped.append({"key": f"{project}/{task_id}", "reason": reason})
                continue
            if _retry_task_no_commit(conn, project, task_id):
                requeued.append(f"{project}/{task_id}")
            else:
                skipped.append({"key": f"{project}/{task_id}", "reason": "unchanged"})
        conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    for key in requeued:
        project, task_id = key.split("/", 1)
        invalidate_running_dependents(conn, project, task_id, home=home)
    if requeued:
        try:
            from taskqueue.sched_wake import wake_scheduler
            wake_scheduler()
        except Exception:
            pass
    return {"requeued": requeued, "skipped": skipped}


CAP_AUTO_RETRY_SEMANTICS = frozenset({"checkpointable", "idempotent"})


def apply_cap_auto_retry(conn: sqlite3.Connection, project: str, task_id: str) -> str:
    """After a wall-clock cap interrupt: optionally re-queue.

    Returns ``queued``, ``exhausted``, or ``skipped``.
    Keeps ``last_error_code=tq.run.cap`` while grinding so ``tq list`` shows why.
    """
    row = get_task(conn, project, task_id)
    if row is None:
        return "skipped"
    try:
        opted = int(row["auto_retry_on_cap"] or 0)
    except (KeyError, IndexError, TypeError):
        opted = 0
    if not opted:
        return "skipped"
    try:
        sem = str(row["execution_semantics"] or "")
    except (KeyError, IndexError, TypeError):
        sem = ""
    if sem not in CAP_AUTO_RETRY_SEMANTICS:
        return "skipped"
    try:
        max_r = int(row["max_auto_retries"] if row["max_auto_retries"] is not None else 20)
    except (KeyError, IndexError, TypeError):
        max_r = 20
    try:
        count = int(row["cap_auto_retry_count"] or 0)
    except (KeyError, IndexError, TypeError):
        count = 0
    if max_r > 0 and count >= max_r:
        conn.execute(
            "UPDATE tasks SET block_reason = ?, updated_at = ? "
            "WHERE project = ? AND task_id = ?",
            ("cap_auto_retries exhausted", _now(), project, task_id),
        )
        conn.commit()
        return "exhausted"
    cur = conn.execute(
        "UPDATE tasks SET status = 'queued', "
        "cap_auto_retry_count = COALESCE(cap_auto_retry_count, 0) + 1, "
        "stop_requested = 0, block_reason = NULL, updated_at = ? "
        "WHERE project = ? AND task_id = ? "
        "AND status NOT IN ('running', 'completed', 'disabled', 'cancelled')",
        (_now(), project, task_id),
    )
    conn.commit()
    if (cur.rowcount or 0) == 0:
        return "skipped"
    return "queued"


def unmet_dependency_label(
    conn: sqlite3.Connection,
    project: str,
    task_id: str,
    *,
    status_map: dict[tuple[str, str], str] | None = None,
    row: sqlite3.Row | None = None,
) -> str | None:
    """Return the first unmet dep label, or None if AND + OR peers are satisfied.

    ``depends_on`` is AND (all must be completed). ``depends_on_any`` is OR
    (at least one must be completed). Both gates apply when both are set.
    Pass *row* (a full ``tasks`` row) to skip the task lookup.
    """
    if row is None:
        row = get_task(conn, project, task_id)
    if row is None:
        return None

    def _status(dep_proj: str, dep_id: str) -> str | None:
        if status_map is not None:
            return status_map.get((dep_proj, dep_id))
        dep_row = get_task(conn, dep_proj, dep_id)
        if dep_row is None:
            return None
        return str(dep_row["status"] or "")

    for dep_proj, dep_id, label in row_dep_refs(row, "depends_on", project):
        st = _status(dep_proj, dep_id)
        if st is None:
            return f"{label} (not found)"
        if st != "completed":
            return f"{label} [{st}]"

    deps_any = row_dep_refs(row, "depends_on_any", project)
    if deps_any:
        waiting: list[str] = []
        for dep_proj, dep_id, label in deps_any:
            st = _status(dep_proj, dep_id)
            if st == "completed":
                return None
            waiting.append(f"{label} (not found)" if st is None else f"{label} [{st}]")
        return f"any of: {', '.join(waiting)}"
    return None


def collect_running_dependents(
    conn: sqlite3.Connection, project: str, task_id: str
) -> list[tuple[str, str]]:
    """Running tasks whose AND/OR deps include ``project/task_id`` and are now unmet.

    For ``depends_on`` (AND): any match is enough to include the dependent.
    For ``depends_on_any`` (OR): include only when no *other* OR peer is still completed.
    """
    rows = conn.execute(
        "SELECT project, task_id, depends_on, depends_on_any FROM tasks WHERE status = 'running'"
    ).fetchall()
    out: list[tuple[str, str]] = []
    for row in rows:
        owner = str(row["project"])
        tid = str(row["task_id"])
        if any((dp, di) == (project, task_id) for dp, di, _ in row_dep_refs(row, "depends_on", owner)):
            out.append((owner, tid))
            continue

        lists_peer = False
        other_completed = False
        for dep_proj, dep_id, _ in row_dep_refs(row, "depends_on_any", owner):
            if dep_proj == project and dep_id == task_id:
                lists_peer = True
                continue
            peer = get_task(conn, dep_proj, dep_id)
            if peer is not None and str(peer["status"] or "") == "completed":
                other_completed = True
        if lists_peer and not other_completed:
            out.append((owner, tid))
    return out


def stop_running_for_unmet_deps(
    conn: sqlite3.Connection,
    project: str,
    task_id: str,
    *,
    unmet_label: str,
    home: Path | None = None,
) -> bool:
    """Request a cooperative stop of a running task whose deps are no longer met.

    Records ``runs.stop_reason=deps_invalidated`` and ``tq.stop.deps`` so local
    and remote finalize paths land the task in auto-restartable ``stopped``.
    """
    from taskqueue import errors as tqerr
    from taskqueue._db_runs import set_run_stop_reason

    row = get_task(conn, project, task_id)
    if row is None or str(row["status"] or "") != "running":
        return False
    already_stopping = bool(int(row["stop_requested"] or 0))
    run = conn.execute(
        "SELECT run_id, stop_reason FROM runs "
        "WHERE project = ? AND task_id = ? AND status = 'running' "
        "ORDER BY run_id DESC LIMIT 1",
        (project, task_id),
    ).fetchone()
    if already_stopping:
        # Do not clobber a user/other stop; only stamp deps reason if we
        # already marked this task with STOP_DEPS on a prior tick.
        if (
            run is not None
            and (row["last_error_code"] or "") == tqerr.STOP_DEPS
            and (run["stop_reason"] or "") in ("", "stop_requested")
        ):
            set_run_stop_reason(conn, int(run["run_id"]), "deps_invalidated")
        return False
    if run is not None:
        existing = run["stop_reason"] or ""
        if existing in ("", "stop_requested"):
            set_run_stop_reason(conn, int(run["run_id"]), "deps_invalidated")
    set_task_error(
        conn,
        project,
        task_id,
        tqerr.STOP_DEPS,
        block_reason=f"depends_on unmet while running: {unmet_label}",
    )
    if home is not None:
        request_stop_with_control(conn, home, project, task_id)
    else:
        request_stop(conn, project, task_id)
    return True


def invalidate_running_dependents(
    conn: sqlite3.Connection,
    project: str,
    task_id: str,
    *,
    home: Path | None = None,
) -> list[tuple[str, str]]:
    """Stop running dependents of ``project/task_id`` (e.g. after upstream retry)."""
    stopped: list[tuple[str, str]] = []
    label = f"{project}/{task_id}"
    for dep_proj, dep_tid in collect_running_dependents(conn, project, task_id):
        if stop_running_for_unmet_deps(
            conn, dep_proj, dep_tid, unmet_label=label, home=home
        ):
            stopped.append((dep_proj, dep_tid))
    return stopped


def revalidate_running_dependencies(
    conn: sqlite3.Connection,
    *,
    home: Path | None = None,
) -> list[tuple[str, str, str]]:
    """Stop every running task whose ``depends_on`` peers are no longer all completed.

    Returns ``[(project, task_id, unmet_label), ...]`` for tasks newly (or again)
    marked for stop.
    """
    # Wave B1: one status map for the whole revalidate pass.
    status_map = {
        (str(r["project"]), str(r["task_id"])): str(r["status"])
        for r in conn.execute("SELECT project, task_id, status FROM tasks").fetchall()
    }
    out: list[tuple[str, str, str]] = []
    for row in get_running_tasks(conn):
        proj = str(row["project"])
        tid = str(row["task_id"])
        unmet = unmet_dependency_label(conn, proj, tid, status_map=status_map, row=row)
        if unmet is None:
            continue
        if stop_running_for_unmet_deps(
            conn, proj, tid, unmet_label=unmet, home=home
        ):
            out.append((proj, tid, unmet))
    return out


def collect_downstream_task_ids(
    conn: sqlite3.Connection, project: str, task_id: str
) -> list[str]:
    """Same-project task_ids that transitively ``depends_on`` / ``depends_on_any`` ``task_id``."""
    rows = conn.execute(
        "SELECT task_id, depends_on, depends_on_any FROM tasks WHERE project = ?",
        (project,),
    ).fetchall()
    children: dict[str, list[str]] = {}
    for row in rows:
        tid = str(row["task_id"])
        for col in ("depends_on", "depends_on_any"):
            for dp, di, _ in row_dep_refs(row, col, project):
                if dp == project:
                    children.setdefault(di, []).append(tid)
    out: list[str] = []
    seen = {task_id}
    queue = deque(children.get(task_id, []))
    while queue:
        cur = queue.popleft()
        if cur in seen:
            continue
        seen.add(cur)
        out.append(cur)
        queue.extend(children.get(cur, []))
    return out


def retry_task_with_downstream(
    conn: sqlite3.Connection,
    project: str,
    task_id: str,
    *,
    home: Path | None = None,
) -> dict[str, list]:
    """Re-queue ``task_id`` and same-project transitive dependents.

    Returns ``{"requeued": [ids], "skipped": [{"task_id", "reason"}, ...]}``.
    """
    ids = [task_id] + collect_downstream_task_ids(conn, project, task_id)
    requeued: list[str] = []
    skipped: list[dict[str, str]] = []
    for tid in ids:
        reason = retry_skip_reason(conn, project, tid)
        if reason is not None:
            skipped.append({"task_id": tid, "reason": reason})
            continue
        if retry_task(conn, project, tid, home=home):
            requeued.append(tid)
        else:
            skipped.append({"task_id": tid, "reason": "unchanged"})
    return {"requeued": requeued, "skipped": skipped}


def collect_ancestors_to_root(
    conn: sqlite3.Connection, project: str, task_id: str, *, root: str
) -> set[str] | None:
    """Task ids on paths from ``root`` to ``task_id`` within the project DAG.

    Returns None if ``task_id`` is unreachable from ``root`` (root not among ancestors).
    """
    rows = conn.execute(
        "SELECT task_id, depends_on FROM tasks WHERE project = ?",
        (project,),
    ).fetchall()
    parents: dict[str, list[str]] = {}
    for row in rows:
        tid = str(row["task_id"])
        for dp, di, _ in row_dep_refs(row, "depends_on", project):
            if dp == project:
                parents.setdefault(tid, []).append(di)
    keep: set[str] = {task_id}
    queue = deque([task_id])
    seen = {task_id}
    while queue:
        cur = queue.popleft()
        for p in parents.get(cur, []):
            if p in seen:
                continue
            seen.add(p)
            keep.add(p)
            queue.append(p)
    if root not in keep:
        return None
    # Restrict to the downstream cone of root (exclude unrelated ancestors).
    cone = {root} | set(collect_downstream_task_ids(conn, project, root))
    return keep & cone


def lock_occupancy(conn: sqlite3.Connection) -> dict[str, dict]:
    """Map lock name → {holder, holder_started_at, waiters: [project/task_id, ...]}."""
    out: dict[str, dict] = {}
    running = conn.execute(
        "SELECT t.project, t.task_id, t.resources, "
        "(SELECT r.started_at FROM runs r WHERE r.project = t.project AND r.task_id = t.task_id "
        " ORDER BY r.run_id DESC LIMIT 1) AS started_at "
        "FROM tasks t "
        "WHERE t.status = 'running'"
    ).fetchall()
    for row in running:
        try:
            res = json.loads(row["resources"] or "{}")
        except Exception:
            res = {}
        locks = res.get("locks") or []
        if not isinstance(locks, list):
            continue
        key = f"{row['project']}/{row['task_id']}"
        for lk in locks:
            name = str(lk)
            slot = out.setdefault(
                name, {"holder": None, "holder_started_at": None, "waiters": []}
            )
            if slot["holder"] is None:
                slot["holder"] = key
                slot["holder_started_at"] = row["started_at"]
    waiting = conn.execute(
        "SELECT project, task_id, resources, last_error_code FROM tasks "
        "WHERE status = 'queued' AND last_error_code = 'tq.block.mutex'"
    ).fetchall()
    for row in waiting:
        try:
            res = json.loads(row["resources"] or "{}")
        except Exception:
            res = {}
        locks = res.get("locks") or []
        if not isinstance(locks, list):
            continue
        key = f"{row['project']}/{row['task_id']}"
        for lk in locks:
            name = str(lk)
            slot = out.setdefault(
                name, {"holder": None, "holder_started_at": None, "waiters": []}
            )
            if key not in slot["waiters"] and key != slot.get("holder"):
                slot["waiters"].append(key)
    return out


def lock_contention_summary(conn: sqlite3.Connection) -> list[dict]:
    """Locks with waiters, plus holder-project headroom vs max_tasks.

    Used by the web UI to explain cases where ``project_limit_*_max_tasks``
    allows more concurrency but a shared ``resources.locks`` mutex serializes
    work (e.g. 1/3 running while dozens wait on the same lock).
    """
    occ = lock_occupancy(conn)
    if not occ:
        return []
    limits = get_all_project_limits(conn)
    running_by: dict[str, int] = {
        str(r["project"]): int(r["n"])
        for r in conn.execute(
            "SELECT project, COUNT(*) AS n FROM tasks WHERE status = 'running' GROUP BY project"
        ).fetchall()
    }
    out: list[dict] = []
    for lock_name, info in sorted(occ.items()):
        waiters = list(info.get("waiters") or [])
        if not waiters:
            continue
        holder = info.get("holder") or ""
        holder_proj = holder.split("/", 1)[0] if "/" in holder else ""
        max_tasks_f = float((limits.get(holder_proj) or {}).get("max_tasks") or 0)
        max_tasks = int(max_tasks_f) if max_tasks_f > 0 else None
        running = int(running_by.get(holder_proj, 0))
        headroom = (max_tasks - running) if max_tasks is not None else None
        if headroom is not None and headroom < 0:
            headroom = 0
        below_limit = bool(max_tasks is not None and headroom is not None and headroom > 0)
        out.append({
            "lock": lock_name,
            "holder": holder,
            "holder_started_at": info.get("holder_started_at"),
            "waiter_count": len(waiters),
            "waiters_sample": waiters[:5],
            "project": holder_proj or None,
            "running": running,
            "max_tasks": max_tasks,
            "headroom": headroom,
            "below_project_limit": below_limit,
        })
    return out


def write_control_action(home: Path, project: str, task_id: str, action: str, requested_by: str | None = None) -> Path:
    """Write cooperative control JSON for a running task (``requested_by`` names who asked, for the log)."""
    import json as _json
    from taskqueue.config import control_path, controls_dir

    controls_dir(home).mkdir(parents=True, exist_ok=True)
    path = control_path(home, project, task_id)
    payload = {"action": action}
    if requested_by:
        payload["requested_by"] = str(requested_by)[:120]
    path.write_text(_json.dumps(payload), encoding="utf-8")
    return path


def clear_control_file(home: Path, project: str, task_id: str) -> None:
    from taskqueue.config import control_path

    path = control_path(home, project, task_id)
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def enable_task(conn: sqlite3.Connection, project: str, task_id: str) -> tuple[bool, str | None]:
    """Enable a disabled task: patch its YAML file and set status to queued.

    Returns (True, None) on success. Idempotent when already queued and enabled
    (e.g. YAML sync promoted the task before the UI Enable click).
    """
    import re

    row = get_task(conn, project, task_id)
    if row is None:
        return False, "task not found"
    status = row["status"]
    enabled = int(row["enabled"])
    if status == "running":
        return False, "cannot enable a running task"
    if enabled and status == "queued":
        return True, None
    if enabled and status != "disabled":
        return False, f"task is {status}; use retry to re-queue"
    yaml_path = row["yaml_source"]
    if yaml_path:
        from taskqueue.yaml_patch import patch_task_yaml

        try:
            patch_task_yaml(Path(yaml_path), {"enabled": True}, task_id=task_id)
        except (OSError, ValueError) as exc:
            # A YAML that still says enabled:false would flip the DB back on the next sync.
            if Path(yaml_path).exists():
                return False, f"could not update {Path(yaml_path).name}: {exc}"
    conn.execute(
        "UPDATE tasks SET status = 'queued', enabled = 1, stop_requested = 0, "
        "last_error_code = NULL, block_reason = NULL, updated_at = ? "
        "WHERE project = ? AND task_id = ?",
        (_now(), project, task_id),
    )
    conn.commit()
    return True, None
