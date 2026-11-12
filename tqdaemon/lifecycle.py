"""Daemon startup: boot classification, adopting live wrapper runs, re-queuing orphans (split from daemon.py; daemon re-exports these names)."""

from __future__ import annotations

import sqlite3
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from tqdaemon import config as tqconfig
from tqdaemon import errors as tqerr
from tqdaemon._db_tasks import get_task, set_task_error, transition_task
from tqdaemon._time import (
    _now,
    _ts,  # noqa: E402 (after local imports)
)
from tqdaemon.db import (
    DAEMON_EVENT_CLEAN_SHUTDOWN,
    get_config,
)
from tqdaemon.lease import (  # noqa: F401  (re-exported for callers/tests)
    MasterLease,
    MasterLeaseLockTimeout,
    _acquire_master_lease,
    _cas_master_lease,
    _start_lease_renew_thread,
)
from tqdaemon.models import (
    STATUS_INTERRUPTED,
    STATUS_QUEUED,
    task_key,
)
from tqdaemon.pressure import (  # noqa: E402,F401 - re-exported for existing callers
    RAM_PRESSURE,
    VRAM_PRESSURE,
    PressureSpec,
    _enforce_hard_pressure,
    _pressure_limits,
    _pressure_used,
    _split_dying,
    _stop_pressure_victims,
)
from tqdaemon.runner import ActiveRun
from tqdaemon.yaml_sync import (  # noqa: F401
    _YAML_CACHE_MAX,
    YAML_PARSER_GEN,
    _sync_project,
    _yaml_mtime_cache,
    clear_yaml_mtime_cache,
    iter_queue_files,
)


def _count_recent_clock_jump_runs(
    conn,
    project: str,
    task_id: str,
    window_sec: int,
) -> int:
    """Runs finalized with tq.infra.clock_jump within the rolling window."""
    # finished_at is ISO-8601 with 'T'; compare against an ISO cutoff, not datetime('now').
    cutoff = (datetime.now(UTC) - timedelta(seconds=int(window_sec))).isoformat()
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM runs "
        "WHERE project = ? AND task_id = ? "
        "AND last_error_code = ? "
        "AND finished_at IS NOT NULL "
        "AND finished_at >= ?",
        (project, task_id, tqerr.INFRA_CLOCK_JUMP, cutoff),
    ).fetchone()
    return int(row["n"] or 0)


@dataclass
class BootClass:
    """Result of classifying the previous daemon incarnation at boot."""

    dirty: bool
    prev_pid: int | None
    prev_alive: bool
    had_clean: bool
    last_kind: str | None


def classify_daemon_boot(home: Path, conn) -> BootClass:
    """Decide whether the previous master exit was unclean.

    See plan: stale ``daemon.pid`` without ``daemon.clean``, or missing pid
    when the last event was not ``clean_shutdown``.
    """
    from tqdaemon.db import (
        latest_daemon_event,
    )

    pid_file = tqconfig.pid_path(home)
    clean_file = tqconfig.clean_shutdown_path(home)
    prev_pid: int | None = None
    prev_alive = False
    if pid_file.exists():
        try:
            raw = pid_file.read_text(encoding="utf-8").strip()
            import psutil as _psutil

            if ":" in raw:
                # F-103: PID:create_time format
                pid_str, ct_str = raw.split(":", 1)
                prev_pid = int(pid_str)
                expected_ct = float(ct_str)
                if _psutil.pid_exists(prev_pid):
                    actual_ct = _psutil.Process(prev_pid).create_time()
                    prev_alive = abs(actual_ct - expected_ct) <= 1.0
            else:
                prev_pid = int(raw)
                prev_alive = bool(_psutil.pid_exists(prev_pid))
        except (OSError, ValueError, _psutil.Error):
            prev_pid = None
            prev_alive = False
    had_clean = clean_file.exists()
    last = latest_daemon_event(conn)
    last_kind = str(last["kind"]) if last else None

    dirty = False
    if (
        prev_pid is not None
        and not prev_alive
        and not had_clean
        or prev_pid is None
        and not had_clean
        and last_kind is not None
        and last_kind != DAEMON_EVENT_CLEAN_SHUTDOWN
    ):
        dirty = True
    return BootClass(
        dirty=dirty,
        prev_pid=prev_pid,
        prev_alive=prev_alive,
        had_clean=had_clean,
        last_kind=last_kind,
    )


def adopt_live_runs(conn, home: Path, projects: dict, *, graceful_sec: float = 30.0) -> dict:
    """Wave 7: keep supervising wrapper-started runs a previous daemon left behind.

    A local ``running`` run is adopted when its wrapper is still alive (same pid and create time,
    ``tqdaemon.run_wrapper`` on the command line) or already wrote its exit file (it finished
    during the restart); it then finalizes through the normal collect path with its real exit code.
    Everything else is left to requeue_orphaned_running. Returns {(project, task_id): ActiveRun}.
    """
    from tqdaemon.adopt import exit_file_for, live_wrapper

    adopted: dict = {}
    rows = conn.execute(
        "SELECT run_id, project, task_id, pid, pid_create_time, log_path, started_at, progress_pct FROM runs "
        "WHERE status = 'running' AND worker_id IS NULL AND pid IS NOT NULL AND log_path IS NOT NULL"
    ).fetchall()
    for row in rows:
        key = (str(row["project"]), str(row["task_id"]))
        alive = live_wrapper(row["pid"], row["pid_create_time"])
        if not alive and not exit_file_for(row["log_path"]).exists():
            continue
        task_row = get_task(conn, *key)
        if task_row is None or key in adopted:
            continue
        proj = projects.get(key[0])
        try:
            run = ActiveRun.adopt(
                conn,
                task_row,
                row,
                log_dir=tqconfig.logs_dir(home, key[0]),
                cwd=proj.root if proj is not None else Path(row["log_path"]).parent,
                graceful_stop_sec=int(graceful_sec),
                db_path=tqconfig.db_path(home),
            )
        except (OSError, ValueError, TypeError, KeyError) as exc:  # unreadable log, bad row: orphan handling
            print(f"[queue] {_ts()} could not adopt {task_key(*key)} run {row['run_id']}: {exc}", flush=True)
            continue
        adopted[key] = run
        state = "running" if alive else "finished during the restart"
        print(f"[queue] {_ts()} adopted {task_key(*key)} run {row['run_id']} pid={row['pid']} ({state})", flush=True)
    return adopted


def requeue_orphaned_running(
    conn,
    *,
    reason: str = "orphan",
    graceful_sec: float = 5.0,
    gap_sec: float | None = None,
    keep_run_ids: frozenset[int] = frozenset(),
) -> int:
    """Reap local orphan OS processes, then re-queue local ghosts.

    Local-only (``worker_id IS NULL``). Remotes use lease reclaim. ``keep_run_ids`` (adopted
    runs) are left alone, and so are their tasks.
    F-080: flip DB status to ``interrupted`` **before** killing process trees
    so a slow graceful kill cannot leave ``running`` rows if the daemon dies
    mid-reap (create_time still guards against PID reuse on kill).
    """
    from tqdaemon.reap import kill_process_tree

    rows = [
        r
        for r in conn.execute(
            "SELECT run_id, project, task_id, pid, pid_create_time, sleep_tier FROM runs "
            "WHERE status = 'running' AND worker_id IS NULL AND pid IS NOT NULL"
        ).fetchall()
        if int(r["run_id"]) not in keep_run_ids
    ]
    keep_sql = ""
    keep_args: tuple = ()
    if keep_run_ids:
        keep_sql = f" AND run_id NOT IN ({','.join('?' * len(keep_run_ids))})"
        keep_args = tuple(sorted(keep_run_ids))

    now_iso = _now()
    err_code = tqerr.INFRA_CLOCK_JUMP if reason == "clock-jump" else tqerr.INFRA_ORPHAN
    # W6.3: attribute clock-jump interrupts to "host-sleep"
    _stopped_by = "host-sleep" if reason == "clock-jump" else "tq"
    _stop_reason: str | None = None
    if reason == "clock-jump":
        if gap_sec is not None and gap_sec > 0:
            _stop_reason = f"clock-jump:gap={int(gap_sec)}s"
        else:
            _stop_reason = "clock-jump"
    else:
        _stop_reason = "orphan"
    cfg = get_config(conn) if reason == "clock-jump" else {}
    _max_requeue = int(cfg.get("clock_jump_max_requeue", 3) or 0) if reason == "clock-jump" else 0
    _requeue_window = int(cfg.get("clock_jump_requeue_window_sec", 3600)) if reason == "clock-jump" else 0
    # Broad UPDATE still covers pid-less local running rows (stop() / clock-jump).
    conn.execute(
        "UPDATE runs SET status = 'interrupted', finished_at = ?, exit_code = -1, "
        "wall_sec = CAST(ROUND((julianday(?) - julianday(started_at)) * 86400) AS INTEGER), "
        "last_error_code = COALESCE(?, last_error_code), stopped_by = ?, "
        "stop_reason = COALESCE(?, stop_reason), error_category = ?, lease_expires_at = NULL "
        "WHERE status = 'running' AND worker_id IS NULL" + keep_sql,
        (
            now_iso,
            now_iso,
            err_code,
            _stopped_by,
            _stop_reason,
            tqerr.classify_error(err_code, -1, _stopped_by),
            *keep_args,
        ),
    )
    try:
        conn.commit()
    except sqlite3.Error:
        pass

    # F-152: track kill failures so we do not re-queue while the OS process may still live.
    kill_failed: set[tuple[str, str]] = set()
    for row in rows:
        pid = int(row["pid"])
        ct = row["pid_create_time"]
        create_time = float(ct) if ct is not None else None
        # F-010: resume Tier-2 (NtSuspendProcess) frozen processes before kill so
        # graceful SIGTERM can be delivered — a suspended process cannot be woken by kill.
        if row["sleep_tier"] == 2 and sys.platform == "win32":
            try:
                import ctypes

                _SUSPEND_RESUME = 0x0800
                _handle = ctypes.windll.kernel32.OpenProcess(_SUSPEND_RESUME, False, pid)
                if _handle:
                    ctypes.windll.ntdll.NtResumeProcess(_handle)
                    ctypes.windll.kernel32.CloseHandle(_handle)
            except (AttributeError, OSError) as _re:
                print(f"[queue] {_ts()} warn: NtResumeProcess pid={pid}: {_re}", file=sys.stderr)
        result = kill_process_tree(pid, create_time=create_time, graceful_sec=graceful_sec)
        print(
            f"[queue] {_ts()} reap orphan pid={pid} {task_key(row['project'], row['task_id'])} → {result}",
            flush=True,
        )
        if result in ("denied", "skipped_reuse"):
            kill_failed.add((str(row["project"]), str(row["task_id"])))
    orphaned = conn.execute(
        "SELECT DISTINCT t.project, t.task_id FROM tasks t "
        "WHERE t.status = 'running' AND NOT EXISTS ("
        "  SELECT 1 FROM runs r WHERE r.project = t.project AND r.task_id = t.task_id "
        "  AND r.status = 'running' AND (r.worker_id IS NOT NULL"
        + (f" OR r.run_id IN ({','.join('?' * len(keep_args))})" if keep_args else "")
        + ")"
        ")",
        keep_args,
    ).fetchall()
    n = 0
    for row in orphaned:
        task_row = conn.execute(
            "SELECT stop_requested FROM tasks WHERE project = ? AND task_id = ? AND status = 'running'",
            (row["project"], row["task_id"]),
        ).fetchone()
        if task_row is None:
            continue
        stop_req = bool(task_row["stop_requested"])
        task_status = STATUS_INTERRUPTED
        task_err = err_code
        task_block: str | None = tqerr.describe(err_code) if err_code else None
        alive_key = (str(row["project"]), str(row["task_id"]))
        if not stop_req and alive_key in kill_failed:
            task_status = STATUS_INTERRUPTED
            task_err = tqerr.INFRA_ORPHAN_ALIVE
            task_block = tqerr.describe(tqerr.INFRA_ORPHAN_ALIVE)
        elif not stop_req:
            if reason == "clock-jump" and _max_requeue > 0:
                recent = _count_recent_clock_jump_runs(conn, row["project"], row["task_id"], _requeue_window)
                if recent > _max_requeue:
                    task_err = tqerr.INFRA_CLOCK_JUMP_FLAP
                    task_block = (
                        f"clock-jump flap: {recent} interrupts in {_requeue_window}s — tq retry when host is stable"
                    )
                else:
                    task_status = STATUS_QUEUED
            else:
                task_status = STATUS_QUEUED
        if not transition_task(
            conn, row["project"], row["task_id"], to=task_status, from_="running", now=now_iso, stop_requested=0
        ):
            continue
        n += 1
        if task_err:
            set_task_error(
                conn,
                row["project"],
                row["task_id"],
                task_err,
                block_reason=task_block,
            )
        action = "re-queued" if task_status == STATUS_QUEUED else "left interrupted"
        print(
            f"[queue] {_ts()} {action} {reason} task: {task_key(row['project'], row['task_id'])}",
            flush=True,
        )
    conn.commit()
    return n
