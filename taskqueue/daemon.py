"""Queue daemon — always-running scheduler loop, all projects, one resource budget.

Start with: tq start
Or directly: python -m taskqueue.daemon
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sqlite3
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable
from pathlib import Path

from taskqueue import config as tqconfig
from taskqueue.config import Project, load_projects
from taskqueue import errors as tqerr
from taskqueue.db import (
    DAEMON_EVENT_CLEAN_SHUTDOWN,
    DAEMON_EVENT_DIRTY_RESTART,
    DAEMON_EVENT_ORPHAN_REQUEUE,
    DAEMON_EVENT_START,
    get_config,
    insert_daemon_event,
    master_holder_process_dead,
    new_master_instance_id,
    open_db,
    prune_daemon_events,
    release_master_lease,
    set_master_instance_id,
)
from taskqueue._time import _now
from taskqueue._db_tasks import get_task, set_task_error, set_task_status
from taskqueue._db_runs import finish_run, mark_stale_workers_offline, reclaim_expired_leases
from taskqueue.models import (
    EXIT_SUSPENDED,
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_INTERRUPTED,
    STATUS_QUEUED,
    STATUS_STOPPED,
    STATUS_SUSPENDED,
    task_key,
)
from taskqueue.victim import pick_victim
from taskqueue._fmt import fmt_duration as _fmt_duration  # H1: shared helper
from taskqueue._time import _ts  # noqa: E402 (after local imports)
# YAML cache + queue-definition sync live in yaml_sync; re-exported for existing callers.
from taskqueue.yaml_sync import (  # noqa: F401
    YAML_PARSER_GEN,
    _YAML_CACHE_MAX,
    _sync_project,
    _yaml_mtime_cache,
    clear_yaml_mtime_cache,
    iter_queue_files,
)
from taskqueue.resources import detect_system, gpu_refresh_interval, ram_used_gib
from taskqueue.runner import ActiveRun, build_process_children_map
from taskqueue.scheduler import _note_block
from taskqueue.web import start_web_server
from taskqueue.cron import check_cron_schedule as _check_cron_schedule

RunKey = tuple[str, str]


HEARTBEAT_LOG_SEC = 300.0


def is_negative_exit(row, code: int | None) -> bool:
    """True when ``code`` is in the task's ``negative_exit_codes``."""
    if row is None or code in (None, 0):
        return False
    try:
        raw = row["negative_exit_codes"] if "negative_exit_codes" in row.keys() else "[]"
        return int(code) in {int(c) for c in json.loads(raw or "[]")}
    except (TypeError, ValueError, KeyError, IndexError):
        return False


def _control_requester(home: Path, project: str, task_id: str) -> str | None:
    """`requested_by` from the task's control file, if the stop request recorded one."""
    try:
        from taskqueue.config import control_path

        data = json.loads(control_path(home, project, task_id).read_text(encoding="utf-8") or "{}")
        val = data.get("requested_by") if isinstance(data, dict) else None
        return str(val) if val else None
    except (OSError, ValueError, TypeError):
        return None


def _replace_registry(current: dict, fresh: dict) -> None:
    """Swap the project registry contents in place: the web server holds a reference to this
    dict (projects_ref), so a new projects.yaml entry reaches `tq add` / `tq sync` / worker
    claims without a daemon restart."""
    current.clear()
    current.update(fresh)
DAEMON_LOG_MAX_BYTES = 50 * 1024 * 1024


def rotate_own_log(home: Path, max_bytes: int = DAEMON_LOG_MAX_BYTES) -> bool:
    """Keep daemon.log bounded while the daemon runs: copy it to daemon.log.1, then truncate it.

    The daemon's stdout/stderr are that file (opened in append mode by `tq start`), so it cannot be
    renamed on Windows; truncating the open descriptor works and later writes start at offset 0.
    Returns True when it rotated. Only acts when stdout really is home/daemon.log.
    """
    import shutil

    path = Path(home) / "daemon.log"
    try:
        st = path.stat()
        if st.st_size <= max_bytes:
            return False
        out = os.fstat(sys.stdout.fileno())
        if (out.st_dev, out.st_ino) != (st.st_dev, st.st_ino):
            return False
        sys.stdout.flush()
        sys.stderr.flush()
        shutil.copyfile(path, path.with_name("daemon.log.1"))
        # The inherited handle is not in append mode on Windows: seek back too, or the next
        # write lands at the old offset and leaves a zero-filled gap.
        os.ftruncate(sys.stdout.fileno(), 0)
        os.lseek(sys.stdout.fileno(), 0, os.SEEK_SET)
        try:
            err = os.fstat(sys.stderr.fileno())
            if (err.st_dev, err.st_ino) == (st.st_dev, st.st_ino):
                os.ftruncate(sys.stderr.fileno(), 0)
                os.lseek(sys.stderr.fileno(), 0, os.SEEK_SET)
        except (OSError, ValueError):
            pass
        print(f"[queue] {_ts()} daemon.log rotated ({st.st_size / 1024 ** 2:.0f} MiB -> daemon.log.1)", flush=True)
        return True
    except (OSError, ValueError, AttributeError):
        return False


def clock_jump_threshold(scheduler_interval_sec: float, cfg_override: float = 0.0) -> float:
    """Wall-clock gap that counts as sleep/hibernate wake (F-001).

    cfg_override > 0 uses an explicit value from clock_jump_threshold_sec config;
    0 falls back to auto: max(2 * scheduler_interval, 30).
    """
    if cfg_override > 0:
        return float(cfg_override)
    return max(2.0 * float(scheduler_interval_sec), 30.0)


def is_clock_jump(prev_wall: float, now_wall: float, threshold: float) -> bool:
    return prev_wall > 0.0 and (now_wall - prev_wall) > threshold


def _count_recent_clock_jump_runs(
    conn,
    project: str,
    task_id: str,
    window_sec: int,
) -> int:
    """Runs finalized with tq.infra.clock_jump within the rolling window."""
    # finished_at is ISO-8601 with 'T'; compare against an ISO cutoff, not datetime('now').
    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=int(window_sec))).isoformat()
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
    from taskqueue.db import (
        DAEMON_EVENT_CLEAN_SHUTDOWN,
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
        except (OSError, ValueError, Exception):
            prev_pid = None
            prev_alive = False
    had_clean = clean_file.exists()
    last = latest_daemon_event(conn)
    last_kind = str(last["kind"]) if last else None

    dirty = False
    if prev_pid is not None and not prev_alive and not had_clean:
        dirty = True
    elif (
        prev_pid is None
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


def requeue_orphaned_running(
    conn,
    *,
    reason: str = "orphan",
    graceful_sec: float = 5.0,
    gap_sec: float | None = None,
) -> int:
    """Reap local orphan OS processes, then re-queue local ghosts.

    Local-only (``worker_id IS NULL``). Remotes use lease reclaim.
    F-080: flip DB status to ``interrupted`` **before** killing process trees
    so a slow graceful kill cannot leave ``running`` rows if the daemon dies
    mid-reap (create_time still guards against PID reuse on kill).
    """
    from taskqueue.reap import kill_process_tree

    rows = conn.execute(
        "SELECT run_id, project, task_id, pid, pid_create_time, sleep_tier FROM runs "
        "WHERE status = 'running' AND worker_id IS NULL AND pid IS NOT NULL"
    ).fetchall()

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
        "WHERE status = 'running' AND worker_id IS NULL",
        (now_iso, now_iso, err_code, _stopped_by, _stop_reason,
         tqerr.classify_error(err_code, -1, _stopped_by)),
    )
    try:
        conn.commit()
    except Exception:
        pass

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
            except Exception as _re:
                print(f"[queue] {_ts()} warn: NtResumeProcess pid={pid}: {_re}", file=sys.stderr)
        result = kill_process_tree(pid, create_time=create_time, graceful_sec=graceful_sec)
        print(
            f"[queue] {_ts()} reap orphan pid={pid} "
            f"{task_key(row['project'], row['task_id'])} → {result}",
            flush=True,
        )
    orphaned = conn.execute(
        "SELECT DISTINCT t.project, t.task_id FROM tasks t "
        "WHERE t.status = 'running' AND NOT EXISTS ("
        "  SELECT 1 FROM runs r WHERE r.project = t.project AND r.task_id = t.task_id "
        "  AND r.status = 'running' AND r.worker_id IS NOT NULL"
        ")"
    ).fetchall()
    n = 0
    for row in orphaned:
        task_row = conn.execute(
            "SELECT stop_requested FROM tasks "
            "WHERE project = ? AND task_id = ? AND status = 'running'",
            (row["project"], row["task_id"]),
        ).fetchone()
        if task_row is None:
            continue
        stop_req = bool(task_row["stop_requested"])
        task_status = STATUS_INTERRUPTED
        task_err = err_code
        task_block: str | None = tqerr.describe(err_code) if err_code else None
        if not stop_req:
            if err_code and reason == "clock-jump" and _max_requeue > 0:
                recent = _count_recent_clock_jump_runs(
                    conn, row["project"], row["task_id"], _requeue_window
                )
                if recent > _max_requeue:
                    task_err = tqerr.INFRA_CLOCK_JUMP_FLAP
                    task_block = (
                        f"clock-jump flap: {recent} interrupts in {_requeue_window}s — "
                        "tq retry when host is stable"
                    )
                else:
                    task_status = STATUS_QUEUED
            else:
                task_status = STATUS_QUEUED
        cur = conn.execute(
            "UPDATE tasks SET status = ?, stop_requested = 0, updated_at = ? "
            "WHERE project = ? AND task_id = ? AND status = 'running'",
            (task_status, now_iso, row["project"], row["task_id"]),
        )
        if (cur.rowcount or 0) == 0:
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
            f"[queue] {_ts()} {action} {reason} task: "
            f"{task_key(row['project'], row['task_id'])}",
            flush=True,
        )
    conn.commit()
    return n


def _split_dying(active_runs: dict, attr: str) -> tuple[dict, float]:
    """Split runs already being stopped from live eviction candidates.

    A run with ``stop_reason`` set is on its way out: re-picking it burns the
    per-tick stop budget without freeing anything new, so it is excluded and its
    reading (*attr*) is returned as already-pending relief.
    """
    live: dict = {}
    pending = 0.0
    for k, r in active_runs.items():
        if getattr(r, "stop_reason", None):
            pending += float(getattr(r, attr, 0.0) or 0.0)
        else:
            live[k] = r
    return live, pending


def _evict_one_suspend(
    conn: sqlite3.Connection,
    home: Path,
    active_runs: dict,
    keys: list,
    label: str,
    reason: str = "evicted",
) -> None:
    """Cooperatively suspend the cheapest-to-restart task from *keys*.

    D7: previously this picked purely on lowest priority, ignoring whether the
    task could be restarted at all.
    """
    from taskqueue.db import request_stop
    from taskqueue.victim import pick_victim as _pick

    evict_key, scored = _pick(
        conn, [(k, active_runs[k]) for k in keys if k in active_runs], resource="ram"
    )
    if evict_key is None:
        return
    runner_up = (
        f"; runner-up {task_key(*scored[1][0])} score={scored[1][1]:.2f}"
        if len(scored) > 1 else ""
    )
    print(
        f"[queue] {_ts()} {label}: suspending {task_key(*evict_key)} "
        f"(score={scored[0][1]:.2f}{runner_up})",
        flush=True,
    )
    request_stop(conn, *evict_key)
    run = active_runs.get(evict_key)
    if run:
        run.stop_reason = reason
        run.request_cooperative_suspend(home)


def _preempt_scavenger(
    conn: sqlite3.Connection,
    home: Path,
    active_runs: dict,
    state: dict,
    cfg: dict,
    now: float,
) -> bool:
    """T-204: cooperatively suspend one running scavenger when other work is capacity-blocked.

    Contention is read from the block reasons the previous admit pass recorded.
    Own-project caps (``project max_tasks``) are ignored: freeing a scavenger
    slot of another project would not help. One preemption per cooldown window,
    and none while a previous victim is still stopping.
    """
    scavs = [
        k for k, r in active_runs.items()
        if r.task_row["task_class"] == "scavenger"
    ] if active_runs else []
    if not scavs:
        return False
    cooldown = float(cfg.get("scavenger_preempt_cooldown_sec", 60))
    if now - float(state.get("at", 0.0)) < cooldown:
        return False
    def _stop_flag(k) -> int:
        r = conn.execute(
            "SELECT stop_requested FROM tasks WHERE project=? AND task_id=?", k
        ).fetchone()
        return int(r["stop_requested"] or 0) if r else 0

    if any(_stop_flag(k) for k in scavs):
        return False
    contender = conn.execute(
        "SELECT project, task_id FROM tasks WHERE status IN ('queued','stopped') "
        "AND enabled = 1 AND task_class != 'scavenger' AND last_error_code = ? "
        "AND COALESCE(block_reason,'') NOT LIKE 'project %' LIMIT 1",
        (tqerr.BLOCK_RESOURCES,),
    ).fetchone()
    if contender is None:
        return False
    _evict_one_suspend(
        conn, home, active_runs, scavs,
        f"scavenger preempt (capacity wanted by {contender['project']}/{contender['task_id']})",
        reason="scavenger_preempt",
    )
    state["at"] = now
    return True


class MasterLeaseLockTimeout(Exception):
    """F-082: BEGIN IMMEDIATE timed out — not the same as lease held by another."""


def _cas_master_lease(
    conn: sqlite3.Connection,
    holder: str,
    ttl: int | None = None,
) -> bool:
    """W7.1/F-058: Atomic compare-and-swap for master lease.

    Returns True if this daemon incarnation now holds the lease (claimed or renewed).
    Uses BEGIN IMMEDIATE so only one writer can race through the check-then-set.
    On takeover of an expired/unowned row, bumps master_lease.epoch.

    Same-host holders whose OS PID is gone are treated as expired so a restart
    does not wait out ``MASTER_LEASE_TTL_SEC`` before binding the web UI.

    F-082: raises ``MasterLeaseLockTimeout`` when BEGIN IMMEDIATE fails (busy
    SQLite), so callers do not log a misleading "held by …" message.
    """
    from taskqueue.config import MASTER_LEASE_TTL_SEC

    if ttl is None:
        ttl = MASTER_LEASE_TTL_SEC
    now = int(time.time())
    try:
        conn.execute("BEGIN IMMEDIATE")
    except sqlite3.OperationalError as exc:
        # F-020 / F-082: only a busy/locked database is a lock timeout; anything
        # else (e.g. "cannot start a transaction within a transaction" on a
        # shared connection) is a programming error and must not be masked.
        msg = str(exc).lower()
        if "locked" in msg or "busy" in msg:
            raise MasterLeaseLockTimeout(str(exc)) from exc
        raise
    try:
        row = conn.execute(
            "SELECT holder, expires_at, epoch FROM master_lease WHERE singleton=1"
        ).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO master_lease (singleton, holder, expires_at, epoch) "
                "VALUES (1, ?, ?, 1)",
                (holder, now + ttl),
            )
            conn.commit()
            return True
        epoch = int(row["epoch"] or 0)
        expired = int(row["expires_at"]) <= now
        stale_dead = (not expired) and row["holder"] != holder and master_holder_process_dead(
            str(row["holder"])
        )
        if expired or stale_dead:
            conn.execute(
                "UPDATE master_lease SET holder=?, expires_at=?, epoch=? WHERE singleton=1",
                (holder, now + ttl, epoch + 1),
            )
            conn.commit()
            return True
        if row["holder"] == holder:
            conn.execute(
                "UPDATE master_lease SET expires_at=? WHERE singleton=1 AND holder=? AND epoch=?",
                (now + ttl, holder, epoch),
            )
            conn.commit()
            return True
        conn.rollback()
        return False
    except MasterLeaseLockTimeout:
        raise
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise


def _detect_hung_remote(
    conn: sqlite3.Connection,
    cfg: dict,
    hung_state: dict,
    tick_sec: float = 0.0,
) -> None:
    """I-17: Detect remote tasks with no stdout/progress for K ticks; re-queue idempotent ones.

    With *tick_sec* > 0 a "tick" must also be K * tick_sec of wall-clock: extra
    fast-reap passes call this more often than the scheduler interval and would
    otherwise burn the budget early.
    """
    global_hung_k = int(cfg.get("hung_task_k", 5))
    if global_hung_k == 0:
        return
    try:
        remote_running = conn.execute(
            "SELECT t.project, t.task_id, t.idempotent, t.execution_semantics, t.hung_task_k, "
            "  r.run_id, r.last_log_line, r.progress_pct, r.worker_id "
            "FROM tasks t "
            "JOIN runs r ON r.project = t.project AND r.task_id = t.task_id "
            "  AND r.status = 'running' AND r.worker_id IS NOT NULL "
            "JOIN workers w ON w.worker_id = r.worker_id AND w.status = 'online' "
            "WHERE t.status = 'running'"
        ).fetchall()
        live_keys: set[tuple] = set()
        now_iso = _now()
        now_mono = time.monotonic()
        from taskqueue.fencing import (
            lost_attempt_task_status,
            may_auto_requeue,
            resolve_execution_semantics,
        )
        for row in remote_running:
            k = (row["project"], row["task_id"], row["run_id"])
            live_keys.add(k)
            prev = hung_state.get(k, (None, None, 0, now_mono))
            prev_log, prev_pct, count = prev[0], prev[1], prev[2]
            since = prev[3] if len(prev) > 3 else now_mono
            curr_log = row["last_log_line"]
            curr_pct = row["progress_pct"]
            if curr_log == prev_log and curr_pct == prev_pct:
                count += 1
                if tick_sec > 0:
                    count = min(count, int((now_mono - since) // tick_sec))
            else:
                count = 0
                since = now_mono
            hung_state[k] = (curr_log, curr_pct, count, since)
            # S8: per-task override; None falls back to global
            hung_k = int(row["hung_task_k"]) if row["hung_task_k"] is not None else global_hung_k
            if count < hung_k:
                continue
            raw_sem = ""
            if "execution_semantics" in row.keys():
                raw_sem = row["execution_semantics"] or ""
            semantics = resolve_execution_semantics(
                raw_sem, idempotent=bool(int(row["idempotent"] or 0))
            )
            if not may_auto_requeue(semantics):
                # F-019 / R-011: non-requeueable hung tasks can't be auto-reassigned;
                # after 3×hung_k ticks escalate to unknown (operator decides).
                if count >= hung_k * 3:
                    dest = lost_attempt_task_status(semantics)
                    # ds2 §1.5: wrap in a savepoint so a partial failure can't leave
                    # stop_requested=1 with the task still 'running'.
                    conn.execute("SAVEPOINT hung_escalate")
                    try:
                        # Fence the live worker before clearing ownership (stop via heartbeat).
                        conn.execute(
                            "UPDATE tasks SET stop_requested = 1, updated_at = ? "
                            "WHERE project = ? AND task_id = ? AND status = 'running'",
                            (now_iso, row["project"], row["task_id"]),
                        )
                        finish_run(
                            conn, row["run_id"], -1, "failed",
                            last_error_code=tqerr.INFRA_HUNG, only_if_running=True, commit=False,
                        )
                        conn.execute(
                            "UPDATE tasks SET status=?, last_error_code=?, "
                            "block_reason=NULL, stop_requested=0, updated_at=? "
                            "WHERE project=? AND task_id=? AND status='running'",
                            (dest, tqerr.INFRA_HUNG, now_iso, row["project"], row["task_id"]),
                        )
                        conn.execute("RELEASE hung_escalate")
                    except Exception:
                        conn.execute("ROLLBACK TO hung_escalate")
                        conn.execute("RELEASE hung_escalate")
                        raise
                    conn.commit()
                    print(
                        f"[queue] {_ts()} hung {semantics} task "
                        f"{task_key(row['project'], row['task_id'])} "
                        f"({count} ticks no progress) → {dest}",
                        flush=True,
                    )
                else:
                    _note_block(
                        conn, row["project"], row["task_id"],
                        tqerr.INFRA_HUNG,
                        f"hung for {count} ticks on worker {row['worker_id']}; "
                        f"set idempotent: true or execution_semantics: idempotent "
                        f"in YAML to enable auto-reassign",
                    )
                continue
            print(
                f"[queue] {_ts()} hung remote task "
                f"{task_key(row['project'], row['task_id'])} "
                f"({count} ticks no progress on worker {row['worker_id']}) "
                f"→ re-queuing (idempotent)",
                flush=True,
            )
            # Ask the still-alive worker to stop this run (heartbeat stop_run_ids)
            # before we release the task for another claim.
            # ds2 §1.5: savepoint so a partial failure can't leave stop_requested=1.
            conn.execute("SAVEPOINT hung_requeue")
            try:
                conn.execute(
                    "UPDATE tasks SET stop_requested = 1, updated_at = ? "
                    "WHERE project = ? AND task_id = ? AND status = 'running'",
                    (now_iso, row["project"], row["task_id"]),
                )
                finish_run(
                    conn, row["run_id"], -1, "interrupted",
                    last_error_code=tqerr.INFRA_HUNG, only_if_running=True, commit=False,
                )
                conn.execute(
                    # F-008: clear stop_requested so the *new* attempt isn't immediately stopped.
                    # Heartbeat still fences the old worker because run.status != 'running'.
                    "UPDATE tasks SET status='queued', last_error_code=?, "
                    "block_reason=NULL, stop_requested=0, updated_at=? "
                    "WHERE project=? AND task_id=? AND status='running'",
                    (tqerr.INFRA_HUNG, now_iso, row["project"], row["task_id"]),
                )
                conn.execute("RELEASE hung_requeue")
            except Exception:
                conn.execute("ROLLBACK TO hung_requeue")
                conn.execute("RELEASE hung_requeue")
                raise
            conn.commit()
            hung_state.pop(k, None)
        # Evict state for tasks no longer in the remote running set
        for k in list(hung_state.keys()):
            if k not in live_keys:
                hung_state.pop(k)
    except Exception as exc:
        # F-096: re-raise so the outer daemon tick handler can rollback the connection.
        # A swallowed exception here can leave a savepoint's partial DB writes committed.
        print(f"[queue] {_ts()} warn: hung-task check failed: {exc}", file=sys.stderr, flush=True)
        raise


_FORCED_STOP_REASONS = ("wall_clock_cap", "ram_hard", "vram_hard", "frozen", "frozen_restart", "evicted")


def _recover_after_tick_failure(conn, active_runs: dict, home: Path | None, frozen_alerted=None) -> None:
    """Roll back a failed tick and still reap finished runs.

    A phase that raises before ``_collect_finished_runs`` would otherwise leave finished
    runs unreaped (and hard-pressure stops skipped) every tick for as long as it keeps failing.
    """
    try:
        conn.rollback()
    except Exception:
        pass
    try:
        _collect_finished_runs(conn, active_runs, home, frozen_alerted=frozen_alerted)
    except Exception as exc:
        print(f"[queue] {_ts()} warn: post-failure reap failed: {exc}", file=sys.stderr)


def decide_terminal_status(
    code: int,
    stop_reason: str | None,
    *,
    reader_crashed: bool,
    stop_requested: bool,
    negative_exit: bool,
) -> tuple[str, str | None, str | None]:
    """(status, last_error_code, stopped_by) for a local run that has exited.

    Pure: branch order is the policy, so keep it when adding cases.
    """
    err_code: str | None = None
    if stop_reason == "deps_invalidated":
        # Cooperative suspend (75) or SIGTERM must not look like a clean
        # suspend/complete — peer work was invalidated mid-run.
        status = STATUS_STOPPED
        err_code = tqerr.STOP_DEPS
    elif stop_reason == "scavenger_preempt" and code != 0:
        # Exit 75 (cooperative suspend) must not become ``suspended``:
        # that is not auto-restarted; ``stopped`` is, once capacity is idle.
        status = STATUS_STOPPED
        err_code = tqerr.STOP_SCAVENGER
    elif reader_crashed and code in (0, -1) and stop_reason not in _FORCED_STOP_REASONS:
        # A crashed reader means we never saw the run finish: never "completed".
        status = STATUS_FAILED
        err_code = tqerr.RUN_NONZERO
    elif code == 0 and stop_reason in _FORCED_STOP_REASONS:
        # tq forced a stop (cap, RAM, frozen, ...): exit 0 after a cooperative
        # suspend is not a finished run; dependents must not be released.
        status = STATUS_INTERRUPTED
        err_code = tqerr.RUN_CAP if stop_reason == "wall_clock_cap" else None
    elif code == 0:
        status = STATUS_COMPLETED
    elif stop_reason in ("ram_hard", "vram_hard"):
        # Before EXIT_SUSPENDED: a pressure victim that checkpoints and exits 75
        # must stay ``stopped`` (auto-restarts), not ``suspended`` (operator resumes).
        status = STATUS_STOPPED
        err_code = tqerr.STOP_VRAM if stop_reason == "vram_hard" else tqerr.STOP_RAM
    elif code == EXIT_SUSPENDED:
        status = STATUS_SUSPENDED
    elif stop_reason in ("frozen", "frozen_restart"):
        status = STATUS_INTERRUPTED
        err_code = tqerr.RUN_FROZEN
    elif stop_reason == "wall_clock_cap":
        status = STATUS_INTERRUPTED
        err_code = tqerr.RUN_CAP
    elif stop_reason == "evicted" or stop_requested:
        status = STATUS_INTERRUPTED
    elif negative_exit:
        # "Ran fine, the answer is no": completed (dependents run), marked tq.run.negative.
        status = STATUS_COMPLETED
        err_code = tqerr.RUN_NEGATIVE
    else:
        status = STATUS_FAILED
        err_code = tqerr.RUN_NONZERO
    stopped_by = None
    if status in (STATUS_INTERRUPTED, STATUS_STOPPED):
        if stop_reason in (
            "wall_clock_cap", "ram_hard", "vram_hard", "frozen", "frozen_restart",
            "evicted", "deps_invalidated", "scavenger_preempt",
        ):
            stopped_by = "tq"
        elif stop_reason == "host-sleep":
            stopped_by = "host-sleep"
        elif stop_requested:
            stopped_by = "user"
    return status, err_code, stopped_by


def _collect_finished_runs(
    conn,
    active_runs: dict,
    home: Path | None = None,
    *,
    frozen_alerted: set[int] | None = None,
) -> None:
    """Reap finished local runs; mutates active_runs."""
    for key, run in list(active_runs.items()):
        if run.poll():
            code = run._exit_code if run._exit_code is not None else -1
            row = get_task(conn, *key)
            if not run.stop_reason or run.stop_reason == "stop_requested":
                try:
                    _sr = conn.execute(
                        "SELECT stop_reason FROM runs WHERE run_id = ?",
                        (run.run_id,),
                    ).fetchone() if run.run_id else None
                    if _sr and _sr["stop_reason"]:
                        run.stop_reason = _sr["stop_reason"]
                except Exception:
                    pass
            status, err_code, _stopped_by = decide_terminal_status(
                code,
                run.stop_reason,
                reader_crashed=bool(getattr(run, "reader_crashed", False)),
                stop_requested=bool(row and row["stop_requested"]),
                negative_exit=is_negative_exit(row, code),
            )
            try:
                run.finalize(status, last_error_code=err_code, stopped_by=_stopped_by)
            except Exception as _fe:
                # F-097: DB lock or transient error; leave run in active_runs so the
                # next tick retries finalize rather than double-finalizing with stale state.
                print(f"[queue] {_ts()} warn: finalize {task_key(*key)} deferred: {_fe}", flush=True)
                continue
            # Finalized: drop it now so a later DB error in the follow-ups below can
            # not leave it in active_runs and get it finalized (and alerted) twice.
            del active_runs[key]
            try:
                from taskqueue.system_alerts import on_run_terminal

                on_run_terminal(
                    conn,
                    home,
                    project=key[0],
                    task_id=key[1],
                    status=status,
                    run_id=run.run_id,
                    last_error_code=err_code,
                    stopped_by=_stopped_by,
                    stop_reason=run.stop_reason,
                    exit_code=code,
                    elapsed_sec=run.elapsed_sec(),
                )
            except Exception:
                pass
            if run.run_id is not None and frozen_alerted is not None:
                frozen_alerted.discard(int(run.run_id))
            try:
                if run.stop_reason == "frozen_restart":
                    set_task_status(conn, key[0], key[1], STATUS_QUEUED)
                    conn.execute(
                        "UPDATE tasks SET last_error_code = NULL, block_reason = NULL "
                        "WHERE project = ? AND task_id = ?",
                        (key[0], key[1]),
                    )
                elif err_code == tqerr.RUN_CAP:
                    from taskqueue.db import apply_cap_auto_retry

                    outcome = apply_cap_auto_retry(conn, key[0], key[1])
                    if outcome == "queued":
                        status = STATUS_QUEUED
            except Exception as _pe:
                print(
                    f"[queue] {_ts()} warn: post-finalize update {task_key(*key)} failed: {_pe}",
                    file=sys.stderr,
                    flush=True,
                )
            emoji = "OK" if status == STATUS_COMPLETED else (
                "--" if status in (STATUS_INTERRUPTED, STATUS_STOPPED) else "!!"
            )
            code_tag = f" [{err_code}]" if err_code else ""
            print(
                f"[queue] {_ts()} {emoji} {task_key(*key)} -> {status} "
                f"(exit {code}, {_fmt_duration(run.elapsed_sec())}){code_tag}",
                flush=True,
            )


def _run_local_admission(
    conn,
    home: Path,
    projects: dict,
    active_runs: dict,
    system_info: dict,
    cfg: dict,
    graceful_stop: float,
    shutdown: bool,
    *,
    now: float,
    admit_frozen_until: float,
    cooldown_logged: bool,
    reservation_state: dict,
    cpu_ema: float,
) -> bool:
    """Run local admission if gates allow. Returns updated cooldown_logged."""
    _cooldown = now < admit_frozen_until
    _cooldown_reason = ""
    if _cooldown:
        _cooldown_reason = (
            f"admission frozen for {admit_frozen_until - now:.0f}s "
            f"after resource-pressure stop"
        )
        if not cooldown_logged:
            print(f"[queue] {_ts()} {_cooldown_reason}", flush=True)
            cooldown_logged = True
    else:
        cooldown_logged = False
    _cpu_total = float(system_info.get("cpu_total_cores", 1))
    _cpu_limit_cores = _cpu_total * float(cfg.get("cpu_limit_pct", 50)) / 100.0
    _cpu_blocked = cpu_ema > _cpu_limit_cores
    _cpu_reason = ""
    if _cpu_blocked:
        _cpu_reason = (
            f"admission skipped (CPU): live load "
            f"{cpu_ema:.1f} cores > limit {_cpu_limit_cores:.1f}"
        )
        print(f"[queue] {_ts()} {_cpu_reason}", flush=True)
    from taskqueue.scheduler import _local_admit_queued

    _local_admit_queued(
        conn, home, projects, active_runs, system_info, cfg,
        graceful_stop, shutdown,
        reservation_state=reservation_state,
        host_cpu_blocked=_cpu_blocked,
        host_cpu_reason=_cpu_reason,
        admit_frozen=_cooldown,
        admit_frozen_reason=_cooldown_reason,
    )
    return cooldown_logged


def _fast_reap_and_admit(
    conn,
    home: Path,
    projects: dict,
    active_runs: dict,
    system_info: dict,
    cfg: dict,
    graceful_stop: float,
    shutdown: bool,
    *,
    admit_frozen_until: float,
    cooldown_logged: bool,
    reservation_state: dict,
    cpu_ema: float,
    frozen_alerted: set[int] | None = None,
) -> bool:
    """Minimal tick when a local run finishes: reap then admit."""
    _collect_finished_runs(conn, active_runs, home, frozen_alerted=frozen_alerted)
    return _run_local_admission(
        conn, home, projects, active_runs, system_info, cfg,
        graceful_stop, shutdown,
        now=time.monotonic(),
        admit_frozen_until=admit_frozen_until,
        cooldown_logged=cooldown_logged,
        reservation_state=reservation_state,
        cpu_ema=cpu_ema,
    )


class _UsageAccumulator:
    """R9/R10: decayed per-project GPU/CPU seconds (fair-share) and hourly per-operator snapshot."""

    def __init__(self) -> None:
        self.proj_gpu: dict[str, float] = {}
        self.proj_cpu: dict[str, float] = {}
        self.op_usage: dict[tuple[str, str | None], dict[str, float]] = {}
        self.last_decay = 0.0
        self.last_flush = 0.0

    def accumulate(self, active_runs: dict, interval: float, *, host_sleeping: bool = False) -> None:
        if active_runs and not host_sleeping:
            for key, run in active_runs.items():
                proj = key[0]
                vram = float(getattr(run, "current_vram_gib", 0.0))
                cpu_delta = float(run.drain_cpu_seconds())
                gpu_delta = vram * interval
                self.proj_gpu[proj] = self.proj_gpu.get(proj, 0.0) + gpu_delta
                self.proj_cpu[proj] = self.proj_cpu.get(proj, 0.0) + cpu_delta
                # R10 / F-070: use task_row started_by — no per-tick SQL
                started_by = None
                try:
                    if "started_by" in run.task_row.keys():
                        started_by = run.task_row["started_by"]
                except Exception:
                    started_by = None
                op_key = (proj, started_by)
                if op_key not in self.op_usage:
                    self.op_usage[op_key] = {"gpu_seconds": 0.0, "cpu_seconds": 0.0}
                self.op_usage[op_key]["gpu_seconds"] += gpu_delta
                self.op_usage[op_key]["cpu_seconds"] += cpu_delta

    def flush_if_due(self, conn, cfg: dict, now: float) -> None:
        # R9: hourly decay + flush project_usage to DB
        if now - self.last_decay > 3600 or self.last_decay == 0.0:
            elapsed_h = (now - self.last_decay) / 3600.0 if self.last_decay > 0 else 0.0
            # F-046: read fairshare_halflife_days from config (default 7 days)
            halflife_h = float(cfg.get("fairshare_halflife_days", 7)) * 24
            decay = 0.5 ** (elapsed_h / halflife_h) if elapsed_h > 0 else 1.0
            try:
                # Apply decay to existing project_usage rows
                conn.execute(
                    "UPDATE project_usage SET"
                    " gpu_seconds = gpu_seconds * ?,"
                    " cpu_seconds = cpu_seconds * ?",
                    (decay, decay),
                )
                # Merge in-memory accumulators into DB
                for p, gpu_s in list(self.proj_gpu.items()):
                    cpu_s = self.proj_cpu.get(p, 0.0)
                    conn.execute(
                        "INSERT INTO project_usage (project, gpu_seconds, cpu_seconds, updated_at) "
                        "VALUES (?, ?, ?, ?) "
                        "ON CONFLICT(project) DO UPDATE SET "
                        "  gpu_seconds = project_usage.gpu_seconds + excluded.gpu_seconds, "
                        "  cpu_seconds = project_usage.cpu_seconds + excluded.cpu_seconds, "
                        "  updated_at = excluded.updated_at",
                        (p, gpu_s, cpu_s, int(time.time())),
                    )
                conn.commit()
                self.proj_gpu.clear()
                self.proj_cpu.clear()
            except Exception as exc:
                print(f"[queue] {_ts()} warn: project_usage flush failed: {exc}", file=sys.stderr)
            self.last_decay = now

        # R10: hourly usage_snapshot flush
        if now - self.last_flush > 3600 or (self.last_flush == 0.0 and self.op_usage):
            bucket_ts = (int(time.time()) // 3600) * 3600
            try:
                for (proj, operator), acc in list(self.op_usage.items()):
                    conn.execute(
                        "INSERT INTO usage_snapshot (ts, project, operator, cpu_seconds, gpu_seconds) "
                        "VALUES (?, ?, ?, ?, ?) "
                        "ON CONFLICT(ts, project, operator) DO UPDATE SET "
                        "  cpu_seconds = usage_snapshot.cpu_seconds + excluded.cpu_seconds, "
                        "  gpu_seconds = usage_snapshot.gpu_seconds + excluded.gpu_seconds",
                        (bucket_ts, proj, operator, acc["cpu_seconds"], acc["gpu_seconds"]),
                    )
                conn.commit()
                self.op_usage.clear()
            except Exception as exc:
                print(f"[queue] {_ts()} warn: usage_snapshot flush failed: {exc}", file=sys.stderr)
            self.last_flush = now


class _HardwareRefresher:
    """Periodic GPU/CPU snapshot refresh; failed probes retry on a short backoff."""

    def __init__(self) -> None:
        self.last_refresh = 0.0
        self.interval = gpu_refresh_interval(0)
        self.fail_streak = 0
        self.log_key: tuple | None = None

    def maybe_refresh(self, system_info: dict, now: float, refresh_now) -> bool:
        """Refresh ``system_info`` in place when due (or forced by ``refresh_now``)."""
        if not (now - self.last_refresh > self.interval or refresh_now.is_set()):
            return False
        refresh_now.clear()
        try:
            live = detect_system()
            # Mutate in place so the web server (which captured this dict
            # at startup) sees the same snapshot as admission. Do not
            # clear() first — a concurrent /api/status read would see {}.
            for k in list(system_info.keys()):
                if k not in live and k != "cpu_used_cores_ema":
                    del system_info[k]
            system_info.update(live)
        except Exception as exc:
            print(f"[queue] {_ts()} warn: hardware refresh failed: {exc}", file=sys.stderr)
        unhealthy = bool(
            system_info.get("gpu_probe_unhealthy")
            or system_info.get("gpu_probe_stale")
            or system_info.get("gpu_probe_error")
        )
        if unhealthy:
            self.fail_streak += 1
        else:
            self.fail_streak = 0
        self.interval = gpu_refresh_interval(self.fail_streak)
        _log_key = (
            system_info.get("gpu_probe_error"),
            bool(system_info.get("gpu_probe_stale")),
            len(system_info.get("gpus") or []),
        )
        if _log_key != self.log_key:
            self.log_key = _log_key
            _n = _log_key[2]
            _tot = float(system_info.get("gpu_vram_total_gib") or 0.0)
            _err = system_info.get("gpu_probe_error")
            if _err:
                _age = float(system_info.get("gpu_last_good_age_sec") or 0.0)
                print(
                    f"[queue] {_ts()} GPU probe: {_err} "
                    f"devices={_n} total={_tot:.1f} GiB"
                    f"{f' stale={_age:.0f}s' if system_info.get('gpu_probe_stale') else ''}"
                    f" retry_in={self.interval:.0f}s",
                    flush=True,
                )
            else:
                print(
                    f"[queue] {_ts()} GPU: {_n} device(s), {_tot:.1f} GiB total",
                    flush=True,
                )
        self.last_refresh = now
        return True


def _handle_clock_jump(conn, active_runs: dict, *, gap: float, jump_thr: float) -> None:
    """F-001: wall-clock jump (sleep/hibernate wake) → stop locals + re-queue ghosts."""
    print(
        f"[queue] {_ts()} clock jump detected ({gap:.0f}s > {jump_thr:.0f}s); "
        f"reclaiming running tasks",
        flush=True,
    )
    # Stop locals but do not finalize — requeue_orphaned_running owns DB
    # (finalize→interrupted would skip the status='running' requeue filter).
    for key, run in list(active_runs.items()):
        try:
            run.stop()
        except Exception as exc:
            print(f"[queue] {_ts()} warn: stop after wake {task_key(*key)}: {exc}", file=sys.stderr)
        active_runs.pop(key, None)
    grace = float(get_config(conn).get("graceful_stop_sec", 30))
    requeue_orphaned_running(
        conn, reason="clock-jump", graceful_sec=grace, gap_sec=gap
    )


def _revalidate_running_deps(conn, home: Path, active_runs: dict) -> None:
    # Re-check depends_on for every running task (local + remote).
    # Catches upstream retry while a dependent is mid-run, and any
    # other path that leaves peers non-completed after admit.
    from taskqueue.db import revalidate_running_dependencies
    for _dproj, _dtid, _dunmet in revalidate_running_dependencies(conn, home=home):
        _dkey = (_dproj, _dtid)
        if _dkey in active_runs:
            active_runs[_dkey].stop_reason = "deps_invalidated"
        print(
            f"[queue] {_ts()} deps invalidated while running: "
            f"{task_key(_dproj, _dtid)} (unmet: {_dunmet}) — stopping",
            flush=True,
        )


def _enforce_stops_and_caps(conn, active_runs: dict, home: Path, suspend_grace: float) -> None:
    """Honour stop requests and wall-clock caps for every active local run."""
    for key, run in list(active_runs.items()):
        task_row = get_task(conn, *key)
        if task_row and task_row["stop_requested"]:
            # F-126: Tier-2 ``tq sleep`` freeze must not escalate to SIGTERM while frozen.
            try:
                _tier_row = conn.execute(
                    "SELECT sleep_tier, stop_reason FROM runs "
                    "WHERE project = ? AND task_id = ? AND status = 'running' "
                    "ORDER BY run_id DESC LIMIT 1",
                    key,
                ).fetchone()
            except Exception:
                _tier_row = None
            if _tier_row and int(_tier_row["sleep_tier"] or 0) == 2:
                continue
            if run.stop_reason is None or run.stop_reason == "stop_requested":
                if _tier_row and _tier_row["stop_reason"]:
                    run.stop_reason = _tier_row["stop_reason"]
            run.stop_reason = run.stop_reason or "stop_requested"
            if getattr(run, "stop_requested_by", None) is None:
                # R7: name the requester once (the control file is rewritten by the suspend below).
                run.stop_requested_by = _control_requester(home, *key) or "unknown (no requester recorded)"
            by = f" by {run.stop_requested_by}"
            first_notice = not getattr(run, "_stop_logged", False)
            run.request_cooperative_suspend(home)
            if run.suspend_grace_elapsed(suspend_grace):
                print(f"[queue] {_ts()} stopping {task_key(*key)} (stop requested{by})", flush=True)
                run.stop(background=True)
            elif first_notice:
                print(
                    f"[queue] {_ts()} cooperative suspend requested "
                    f"{task_key(*key)}{by} (grace {suspend_grace:.0f}s)",
                    flush=True,
                )
            run._stop_logged = True

        cap = task_row["wall_clock_cap_sec"] if task_row else None
        if cap and run.elapsed_sec() > cap:
            run.stop_reason = "wall_clock_cap"
            from taskqueue.db import request_stop
            request_stop(conn, *key)
            run.request_cooperative_suspend(home)
            if run.suspend_grace_elapsed(suspend_grace):
                print(f"[queue] {_ts()} stopping {task_key(*key)} (wall_clock_cap {cap}s)", flush=True)
                run.stop(background=True)
            else:
                print(
                    f"[queue] {_ts()} cooperative suspend requested "
                    f"{task_key(*key)} (wall_clock_cap {cap}s, grace {suspend_grace:.0f}s)",
                    flush=True,
                )


def _stop_pressure_victims(
    conn, remaining: dict, *, resource: str, attr: str, projected: float, soft_limit: float,
    stop_cap: int, stop_reason: str, err_code: str, log_label: str, log_used: str,
    hard_limit: float, task_label: str, victim_label: str, block_used: str, total: float,
) -> int:
    """Stop victims (cheapest to kill first) until ``projected`` drops under ``soft_limit``.

    Shared by the hard-RAM and hard-VRAM paths. ``remaining`` holds runs not already dying and
    is consumed. Reclaim is asynchronous, so usage is projected rather than re-read. Returns
    how many runs were stopped (at most ``stop_cap``).
    """
    stopped = 0
    while projected > soft_limit and remaining and stopped < stop_cap:
        # D7: pick by cost-of-killing, not just biggest RSS, so an idempotent task dies
        # before a non-idempotent one that is nearly finished.
        victim_key, scored = pick_victim(
            conn, list(remaining.items()),
            needed_gib=max(0.0, projected - soft_limit), resource=resource,
        )
        if victim_key is None:
            break
        victim = remaining.pop(victim_key)
        held = float(getattr(victim, attr) or 0.0)
        runner_up = (
            f"; runner-up {task_key(*scored[1][0])} score={scored[1][1]:.2f}"
            if len(scored) > 1 else ""
        )
        victim.stop_reason = stop_reason
        print(
            f"[queue] {_ts()} {log_label} hard limit: stopping {task_key(*victim_key)} "
            f"({log_used} > hard {hard_limit:.1f}; "
            f"{task_label} {held:.2f} GiB; "
            f"score={scored[0][1]:.2f}{runner_up})",
            flush=True,
        )
        set_task_error(
            conn, victim_key[0], victim_key[1], err_code,
            block_reason=(
                f"{block_used} > hard {hard_limit:.1f} "
                f"(soft {soft_limit:.1f}, total {total:.1f}); "
                f"{victim_label} {held:.2f} GiB"
            ),
        )
        victim.stop()
        projected -= held
        stopped += 1
    return stopped


def _enforce_hard_pressure(conn, cfg: dict, system_info: dict, active_runs: dict, now: float) -> float | None:
    """Hard RAM / VRAM response: stop victims among local runs.

    Local processes consume host memory regardless of master-lease or sleep
    state, so this is also called while the lease is lost.  Returns the new
    ``admit_frozen_until`` (monotonic) when a victim was stopped, else None.
    """
    admit_frozen_until: float | None = None
    # Host hard RAM: stop highest-RSS local task (one per tick).
    hard_pct = float(cfg.get("ram_hard_limit_pct", 95))
    soft_pct = float(cfg.get("ram_limit_pct", 80))
    if hard_pct < soft_pct:
        hard_pct = soft_pct
    ram_total = float(system_info.get("ram_total_gib") or 0.0)
    hard_limit = ram_total * hard_pct / 100.0
    host_used = ram_used_gib()
    if hard_limit > 0 and host_used > hard_limit and active_runs:
        for run in list(active_runs.values()):
            run.update_resources()
        # D9: stop until projected usage is back under the SOFT limit,
        # not merely under hard — stopping just enough to clear hard
        # leaves the host one allocation away from thrashing again.
        soft_limit = ram_total * soft_pct / 100.0
        _remaining, _dying_ram = _split_dying(active_runs, "current_ram_gib")
        _stopped_now = _stop_pressure_victims(
            conn, _remaining, resource="ram", attr="current_ram_gib",
            projected=host_used - _dying_ram, soft_limit=soft_limit,
            stop_cap=int(cfg.get("stop_max_per_tick", 2)), stop_reason="ram_hard",
            err_code=tqerr.STOP_RAM, log_label="RAM", log_used=f"host {host_used:.1f}",
            hard_limit=hard_limit, task_label="task RSS", victim_label="victim RSS",
            block_used=f"RAM: host {host_used:.1f}", total=ram_total,
        )
        if _stopped_now:
            # D9: freeze admission while the OS reclaims. Without this,
            # admit (step 5) runs later in this same tick against memory
            # the dying process has not released yet (F7).
            admit_frozen_until = now + float(cfg.get("recovery_cooldown_sec", 30))

    # P8: VRAM pressure response — mirrors the hard-RAM path for GPU.
    # Uses the most recently measured current_vram_gib per run (updated
    # on heartbeat). Only refreshes via update_resources() when we detect
    # pressure, to avoid a per-tick nvidia-smi subprocess on every tick.
    _vram_hard_pct = float(cfg.get("vram_hard_limit_pct", 95))
    if _vram_hard_pct > 0 and active_runs:
        _vram_total = float(system_info.get("gpu_vram_total_gib") or 0.0)
        if _vram_total > 0:
            _vram_hard_limit = _vram_total * _vram_hard_pct / 100.0
            # F-143: prefer host-wide GPU usage (includes non-task CUDA) over task-only sum.
            _vram_used_now = sum(
                float(g.get("vram_used_gib") or 0.0)
                for g in (system_info.get("gpus") or [])
            )
            if _vram_used_now <= 0:
                _vram_used_now = sum(
                    float(r.current_vram_gib or 0.0) for r in active_runs.values()
                )
            if _vram_used_now > _vram_hard_limit:
                # Refresh VRAM readings before making stop decisions.
                for _rv in list(active_runs.values()):
                    _rv.update_resources()
                _vram_used_now = sum(
                    float(r.current_vram_gib or 0.0) for r in active_runs.values()
                )
                _vram_soft_limit = _vram_total * float(cfg.get("gpu_limit_pct", 100)) / 100.0
                _vram_remaining, _dying_vram = _split_dying(active_runs, "current_vram_gib")
                _vram_stopped = _stop_pressure_victims(
                    conn, _vram_remaining, resource="vram", attr="current_vram_gib",
                    projected=_vram_used_now - _dying_vram, soft_limit=_vram_soft_limit,
                    stop_cap=int(cfg.get("stop_max_per_tick", 2)), stop_reason="vram_hard",
                    err_code=tqerr.STOP_VRAM, log_label="VRAM", log_used=f"VRAM {_vram_used_now:.1f}",
                    hard_limit=_vram_hard_limit, task_label="task VRAM", victim_label="victim VRAM",
                    block_used=f"VRAM: device {_vram_used_now:.1f}", total=_vram_total,
                )
                if _vram_stopped:
                    admit_frozen_until = now + float(cfg.get("recovery_cooldown_sec", 30))
    return admit_frozen_until


def _heartbeat_and_sanity(
    conn, home: Path, cfg: dict, active_runs: dict, frozen_threshold: float, frozen_alerted: set
) -> None:
    """Heartbeat: persist progress and run frozen / progress-sanity / GPU / leak checks."""
    from taskqueue.progress_sanity import sanity_snapshot

    for key, run in active_runs.items():
        run.update_db_progress(conn)
        cur_active = run.refresh_active_cpu()
        try:
            task_res = json.loads(run.task_row["resources"] or "{}")
        except (TypeError, ValueError, KeyError):
            task_res = {}
        gpu_requested_gib = float(task_res.get("gpu_vram_gib", 0.0))
        gpu_requested = gpu_requested_gib > 0
        wsl_dispatched = str(run.task_row["command"]).lower() == "wsl"
        # GPU health checks after the settle window.
        settle_sec = float(cfg.get("settle_window_sec", 180))
        gpu_util_warn_pct = float(cfg.get("gpu_util_warn_pct", 15))
        if gpu_requested and run.elapsed_sec() > settle_sec:
            if run.peak_vram_gib < 0.1:
                # VRAM near zero: model is almost certainly on CPU.
                print(
                    f"[queue] {_ts()} WARN {task_key(*key)} declares "
                    f"gpu_vram_gib={gpu_requested_gib:.1f} but peak_vram={run.peak_vram_gib:.3f} GiB "
                    f"after {_fmt_duration(run.elapsed_sec())} — likely running on CPU. "
                    "Check CUDA availability, driver, and library installation.",
                    flush=True,
                )
            elif (
                run.gpu_util_ema is not None
                and run._gpu_util_samples >= 3
                and run.gpu_util_ema < gpu_util_warn_pct
            ):
                # VRAM allocated but GPU barely active: pipeline is CPU-bound.
                print(
                    f"[queue] {_ts()} WARN {task_key(*key)} GPU util EMA "
                    f"{run.gpu_util_ema:.1f}% < {gpu_util_warn_pct:.0f}% threshold "
                    f"(peak_vram={run.peak_vram_gib:.2f} GiB, "
                    f"elapsed={_fmt_duration(run.elapsed_sec())}). "
                    "Possible causes: data not in mmap/pinned format, "
                    "small batch size causing CPU-GPU sync overhead, "
                    "inefficient kernel (e.g. non-contiguous tensors, "
                    "Python-loop ops). Check DataLoader num_workers and pin_memory.",
                    flush=True,
                )
        # RSS trend detection: warn if RSS is growing faster than threshold
        # after ram_settle_sec (default 30 min). Using a longer window than
        # GPU settle_sec (3 min) because:
        #   - initial ramp-up (model load, mmap pre-load) is legitimately large
        #   - multi-stage tasks (shard rotate) show RSS jumps that average out
        #   - 10-min OLS window averages shard cycles correctly once past 30 min
        ram_settle_sec = float(cfg.get("ram_settle_sec", 1800))
        ram_leak_warn = float(cfg.get("ram_leak_warn_gib_per_hour", 1.0))
        if run.elapsed_sec() > ram_settle_sec and not run._rss_leak_warned:
            growth = run.rss_growth_rate_gib_per_hour()
            if growth is not None and growth > ram_leak_warn:
                print(
                    f"[queue] {_ts()} WARN {task_key(*key)} RSS growing at "
                    f"{growth:.2f} GiB/h (threshold {ram_leak_warn:.1f} GiB/h, "
                    f"current_rss={run.current_ram_gib:.2f} GiB, "
                    f"elapsed={_fmt_duration(run.elapsed_sec())}). "
                    "Possible memory leak or unbounded cache growth. "
                    "Check for accumulating Python lists, mmap standby not released, "
                    "or PyTorch graph retention.",
                    flush=True,
                )
                run._rss_leak_warned = True

        snap = sanity_snapshot(
            idle_stdout_sec=run.idle_sec(),
            active_idle_sec=run.active_idle_sec(),
            threshold_sec=frozen_threshold,
            prev_pct=run._sanity_prev_pct,
            cur_pct=run.progress_pct,
            prev_active=run._sanity_prev_active,
            cur_active=cur_active,
            gpu_requested=gpu_requested,
            wsl_dispatched=wsl_dispatched,
        )
        gpu_ema_str = (
            f" gpu_util={run.gpu_util_ema:.0f}%"
            if run.gpu_util_ema is not None else ""
        )
        pct_str = f" {run.progress_pct:.0f}%" if run.progress_pct is not None else ""
        eta_str = ""
        if run.progress_pct and run.progress_pct > 0:
            total_est = run.elapsed_sec() / (run.progress_pct / 100.0)
            eta_str = f" ETA ~{_fmt_duration(total_est - run.elapsed_sec())}"
        frozen_str = ""
        if snap["frozen"]:
            frozen_str = (
                f" [FROZEN out={_fmt_duration(run.idle_sec())} "
                f"cpu={_fmt_duration(run.active_idle_sec())}]"
            )
        suspect_str = " [PROGRESS_SUSPECT]" if snap["progress_suspect"] else ""
        # Heartbeat line only when something changed or every HEARTBEAT_LOG_SEC (was every ~30 s
        # per run: hundreds of MiB of daemon.log per day with many runs).
        _now_m = time.monotonic()
        _pct_i = None if run.progress_pct is None else int(run.progress_pct)
        if (
            frozen_str or suspect_str
            or _pct_i != getattr(run, "_hb_last_pct", "unset")
            or _now_m - getattr(run, "_hb_last_print", 0.0) >= HEARTBEAT_LOG_SEC
        ):
            run._hb_last_pct = _pct_i
            run._hb_last_print = _now_m
            print(
                f"[queue] {_ts()} >> {task_key(*key)} | "
                f"elapsed={_fmt_duration(run.elapsed_sec())}{pct_str}{eta_str}"
                f"{gpu_ema_str}{frozen_str}{suspect_str}",
                flush=True,
            )
        if snap["frozen_attention"]:
            _note_block(conn, key[0], key[1], tqerr.RUN_FROZEN, "frozen: no stdout and no CPU ≥2× threshold")
            rid = run.run_id
            if rid is not None and rid not in frozen_alerted:
                try:
                    from taskqueue.system_alerts import emit_task_frozen

                    if emit_task_frozen(conn, home, project=key[0], task_id=key[1], elapsed_sec=run.elapsed_sec(), run_id=run.run_id):
                        frozen_alerted.add(int(rid))
                except Exception:
                    pass
            # I-06: frozen_action — "log" (default), "stop", "restart"
            frozen_action = str(cfg.get("frozen_action", "log")).lower()
            if frozen_action in ("stop", "restart") and key in active_runs:
                print(
                    f"[queue] {_ts()} frozen_action={frozen_action}: "
                    f"stopping frozen task {task_key(*key)}",
                    flush=True,
                )
                # restart: finalize will re-queue after the process exits
                # (do not set queued here — that races finalize → failed).
                active_runs[key].stop_reason = (
                    "frozen_restart" if frozen_action == "restart" else "frozen"
                )
                active_runs[key].stop(background=True)
        elif snap["progress_suspect"]:
            _note_block(conn, key[0], key[1], tqerr.RUN_PROGRESS_SUSPECT, "progress % advanced without process CPU")
        run._sanity_prev_pct = run.progress_pct
        run._sanity_prev_active = cur_active



_PROC_CHILDREN_REFRESH_TICKS = 3


@dataclass
class TickEnv:
    """Handles fixed for the lifetime of the daemon loop."""

    conn: sqlite3.Connection
    home: Path
    active_runs: dict
    system_info: dict
    my_instance: str
    sync_requested: Any
    hw_refresh_now: Any
    lease_lost: Any
    is_shutdown: Callable[[], bool]
    host_sleeping: Callable[[], bool]


class TickState:
    """Mutable state carried from one scheduler tick to the next."""

    def __init__(self, projects: dict) -> None:
        self.projects = projects
        self.cfg: dict = {}
        self.interval = 5.0  # fallback if a tick fails before reading scheduler_interval_sec
        self.graceful_stop = 30
        self.last_sync = 0.0
        self.last_heartbeat = 0.0
        self.last_wall = time.time()
        self.last_history_prune = 0.0  # C5: hourly resource_history pruning
        self.admit_frozen_until = 0.0  # D9: monotonic time until admission is frozen
        self.cooldown_logged = False
        self.cpu_ema = 0.0  # P7: EMA of live host CPU load (cores) for the soft CPU gate
        self.reservation_state: dict = {}  # P6: top-priority resource-blocked task
        self.scav_state: dict = {"at": 0.0}
        self.cron_state: dict = {}  # S3: last-fired timestamps
        self.frozen_alerted: set[int] = set()  # run_ids that already got a task_frozen toast
        self.hung_state: dict[tuple, tuple] = {}  # I-17: no-progress tick counters
        self.proc_children_cache: dict[int, list[int]] | None = None
        self.proc_children_tick = 0
        self.usage = _UsageAccumulator()
        self.hw = _HardwareRefresher()


def _run_hourly_maintenance(conn, home) -> None:
    """C5: resource_history (7 d) + daemon_events (90 d) + usage pruning, log rotation, retention."""
    try:
        conn.execute(
            "DELETE FROM resource_history WHERE ts < unixepoch('now','-7 days')"
        )
        conn.commit()
    except Exception:
        pass
    try:
        prune_daemon_events(conn, keep_days=90)
    except Exception:
        pass
    try:
        from taskqueue.usage_track import (
            DEFAULT_RETENTION_DAYS,
            RECORDER,
            prune_api_usage,
        )

        RECORDER.flush(conn)
        prune_api_usage(
            conn, int(get_config(conn).get("retention_usage_days", DEFAULT_RETENTION_DAYS))
        )
    except Exception:
        pass
    try:
        _cfg_ret = get_config(conn)
        # Append-only notify logs are size-rotated even when retention is off.
        from taskqueue._cli_prune import rotate_logs

        rotate_logs(home, dry_run=False)
        if _cfg_ret.get("retention_enabled"):
            from taskqueue._cli_prune import run_maintenance

            _ret = run_maintenance(home, conn, _cfg_ret)
            if _ret["runs_deleted"] or _ret["audit_deleted"] or _ret["logs_deleted"]:
                print(
                    f"[queue] {_ts()} retention: "
                    f"{ {k: v for k, v in _ret.items() if k != 'per_project'} }",
                    flush=True,
                )
    except Exception as _re:
        print(f"[queue] {_ts()} warn: retention pass failed: {_re}", file=sys.stderr, flush=True)


_maintenance_thread: "threading.Thread | None" = None


def _start_hourly_maintenance(home) -> None:
    global _maintenance_thread
    if _maintenance_thread is not None and _maintenance_thread.is_alive():
        return

    def _work() -> None:
        from taskqueue.config import db_path as _db_path
        from taskqueue.db import open_db as _open_db

        mconn = None
        try:
            mconn = _open_db(_db_path(Path(home)), migrate=False)
            _run_hourly_maintenance(mconn, home)
        except Exception as exc:
            print(f"[queue] {_ts()} warn: hourly maintenance failed: {exc}", file=sys.stderr, flush=True)
        finally:
            if mconn is not None:
                try:
                    mconn.close()
                except Exception:
                    pass

    _maintenance_thread = threading.Thread(target=_work, name="tq-maintenance", daemon=True)
    _maintenance_thread.start()


def daemon_tick(env: TickEnv, st: TickState) -> bool:
    """One scheduler tick (sync, reap, pressure, admit, housekeeping).

    Returns True when the tick ran to completion (caller then idles and may
    fast-admit), False when the caller should just sleep ``st.interval`` and
    start the next tick (master lease lost, or the host is asleep).
    Exceptions propagate; the caller recovers via ``_recover_after_tick_failure``.
    """
    conn, home = env.conn, env.home
    active_runs, system_info = env.active_runs, env.system_info
    now = time.monotonic()
    wall = time.time()
    cfg = get_config(conn)
    st.interval = float(cfg.get("scheduler_interval_sec", 5))
    heartbeat_sec = float(cfg.get("heartbeat_sec", 30))
    st.graceful_stop = int(cfg.get("graceful_stop_sec", 30))
    st.cfg = cfg

    # UI colors for newly seen projects (cheap, at most every 30 s).
    if wall - getattr(st, "_colors_at", 0.0) > 30.0:
        st._colors_at = wall
        try:
            from taskqueue.project_colors import ensure_project_colors

            _known = {r[0] for r in conn.execute("SELECT DISTINCT project FROM tasks").fetchall()}
            ensure_project_colors(conn, sorted(_known))
        except Exception:
            pass

    # Refresh GPU/CPU snapshot periodically (multi-GPU free VRAM drifts).
    # Failed probes retry on a short backoff; success uses 60s.
    # Windows resume forces an immediate re-probe.
    st.hw.maybe_refresh(system_info, now, env.hw_refresh_now)
    # P7: update CPU EMA every tick. psutil.cpu_percent(percpu=True) is
    # non-blocking and cheap; the first call returns 0.0 which the EMA
    # absorbs without distorting the steady state.
    from taskqueue.resources import cpu_cores_from_load
    _cpu_raw = cpu_cores_from_load()
    _ema_sec = float(cfg.get("cpu_measure_ema_sec", 30))
    _alpha = min(1.0, st.interval / max(_ema_sec, st.interval))
    st.cpu_ema = _alpha * _cpu_raw + (1.0 - _alpha) * st.cpu_ema
    # Expose via system_info so ResourceBudget can seed cpu_reserved from it.
    system_info["cpu_used_cores_ema"] = st.cpu_ema

    # F-074 fence: if the renew thread lost the lease, try to reclaim before
    # any DB-writing step (lease reclaim, sync, finalize); else skip the rest of the tick.
    if env.lease_lost.is_set():
        try:
            if _cas_master_lease(conn, env.my_instance):
                print(
                    f"[queue] {_ts()} master lease reclaimed: holder={env.my_instance}",
                    flush=True,
                )
                env.lease_lost.clear()
            else:
                _fz = _enforce_hard_pressure(conn, cfg, system_info, active_runs, now)
                if _fz is not None:
                    st.admit_frozen_until = _fz
                return False
        except Exception as exc:
            print(
                f"[queue] {_ts()} warn: master lease reclaim error: {exc}",
                file=sys.stderr,
            )
            try:
                _fz = _enforce_hard_pressure(conn, cfg, system_info, active_runs, now)
                if _fz is not None:
                    st.admit_frozen_until = _fz
            except Exception:
                pass
            return False

    # Leases: reclaim dead remote runs; demote stale workers
    for proj, tid in reclaim_expired_leases(conn, heartbeat_sec=heartbeat_sec):
        reclaimed_row = get_task(conn, proj, tid)
        code = (
            reclaimed_row["last_error_code"]
            if reclaimed_row and "last_error_code" in reclaimed_row.keys()
            else tqerr.INFRA_WORKER_LOST
        )
        print(
            f"[queue] {_ts()} reclaimed expired lease: {task_key(proj, tid)} [{code}]",
            flush=True,
        )
    n_off = mark_stale_workers_offline(conn)
    if n_off:
        print(f"[queue] {_ts()} marked {n_off} worker(s) offline", flush=True)

    # F-001: wall-clock jump (sleep/hibernate wake) → stop locals + re-queue ghosts
    jump_thr = clock_jump_threshold(st.interval, float(cfg.get("clock_jump_threshold_sec", 0)))
    gap = wall - st.last_wall if st.last_wall > 0 else 0.0
    if st.last_wall > 0 and gap > jump_thr * 0.8 and gap <= jump_thr:
        print(
            f"[queue] {_ts()} WARN scheduler tick slow: {gap:.0f}s "
            f"(clock-jump threshold {jump_thr:.0f}s)",
            flush=True,
        )
    if is_clock_jump(st.last_wall, wall, jump_thr):
        _handle_clock_jump(conn, active_runs, gap=gap, jump_thr=jump_thr)
    st.last_wall = wall

    # Re-read registry + sync every project's YAML dir every 60s or on file change (I-07)
    if now - st.last_sync > 60 or env.sync_requested.is_set():
        rotate_own_log(env.home)
        try:
            _replace_registry(st.projects, load_projects(home))
        except Exception as exc:
            print(f"[queue] {_ts()} warn: projects.yaml invalid, keeping previous registry: {exc}", file=sys.stderr)
        for project in st.projects.values():
            _sync_project(conn, project, home=home)
        st.last_sync = now
        env.sync_requested.clear()

    # Refresh live RAM + CPU for all active runs (fast psutil, no nvidia-smi)
    # W4.8: batch resource_history inserts into one transaction.
    # F-081: one process-table snapshot shared across all active runs.
    _history_dirty = False
    if active_runs:
        st.proc_children_tick += 1
        if (
            st.proc_children_cache is None
            or st.proc_children_tick >= _PROC_CHILDREN_REFRESH_TICKS
        ):
            st.proc_children_cache = build_process_children_map()
            st.proc_children_tick = 0
        _proc_children = st.proc_children_cache
    else:
        _proc_children = None
        st.proc_children_cache = None
        st.proc_children_tick = 0
    for run in list(active_runs.values()):
        run.refresh_ram_cpu(children_map=_proc_children)
        # C2: flush pending resource history sample
        if run._pending_history_sample is not None:
            try:
                from taskqueue.db import record_resource_sample
                record_resource_sample(
                    conn, run.run_id, *run._pending_history_sample, commit=False
                )
                _history_dirty = True
            except Exception as _hist_exc:
                # F-129: do not silently drop history; tick continues.
                print(
                    f"[queue] {_ts()} warn: resource_history sample failed "
                    f"run={run.run_id}: {_hist_exc}",
                    file=sys.stderr,
                )
            run._pending_history_sample = None
    if _history_dirty:
        try:
            conn.commit()
        except Exception as _hist_commit_exc:
            print(
                f"[queue] {_ts()} warn: resource_history commit failed: {_hist_commit_exc}",
                file=sys.stderr,
            )

    # I-17: hung remote task detection
    _detect_hung_remote(conn, cfg, st.hung_state, tick_sec=float(cfg.get("scheduler_interval_sec", 5)))

    # Check stop requests and wall-clock caps
    frozen_threshold = float(cfg.get("frozen_threshold_sec", 600))
    suspend_grace = float(cfg.get("suspend_grace_sec", 10))

    _revalidate_running_deps(conn, home, active_runs)
    _enforce_stops_and_caps(conn, active_runs, home, suspend_grace)

    # Collect finished runs (before heavy tick work — PR4 handoff)
    _collect_finished_runs(conn, active_runs, home, frozen_alerted=st.frozen_alerted)

    # R9/R10: accumulate GPU/CPU usage for fair-share and hourly snapshot
    st.usage.accumulate(active_runs, st.interval, host_sleeping=env.host_sleeping())
    st.usage.flush_if_due(conn, cfg, now)

    # R12: skip scheduling when host is asleep
    if env.host_sleeping():
        return False

    # Heartbeat + frozen / progress-sanity (active CPU time, not wall idle alone)
    if now - st.last_heartbeat > heartbeat_sec:
        _heartbeat_and_sanity(
            conn, home, cfg, active_runs, frozen_threshold, st.frozen_alerted
        )
        st.last_heartbeat = now

    _frozen_until = _enforce_hard_pressure(conn, cfg, system_info, active_runs, now)
    if _frozen_until is not None:
        st.admit_frozen_until = _frozen_until

    # I-28: Graceful eviction when resource limits decrease
    limit_decrease_policy = str(cfg.get("limit_decrease_policy", "ignore")).lower()
    if limit_decrease_policy == "evict" and active_runs:
        max_tasks_evict = int(cfg.get("max_running_tasks", 0))
        local_keys = list(active_runs.keys())
        if max_tasks_evict > 0 and len(local_keys) > max_tasks_evict:
            _evict_one_suspend(
                conn, home, active_runs, local_keys,
                f"limit_decrease_policy=evict "
                f"(local={len(local_keys)} > max_running_tasks={max_tasks_evict})",
            )

    # Host sleep inhibit while local runs are active (Windows; complements F-001).
    if int(cfg.get("prevent_sleep", 1)):
        from taskqueue.power import set_prevent_sleep

        set_prevent_sleep(bool(active_runs))
    else:
        from taskqueue.power import set_prevent_sleep

        set_prevent_sleep(False)

    # S3: re-queue tasks whose cron schedule fired
    _check_cron_schedule(conn, st.cron_state)

    try:
        from taskqueue.system_alerts import evaluate_all_done

        evaluate_all_done(conn, home)
    except Exception as exc:
        print(f"[queue] {_ts()} warn: system alert evaluation failed: {exc}", file=sys.stderr)

    # Schedule new tasks if resources allow (one budget across all projects).
    # D9: recovering from a pressure stop — the victim's memory is not
    # reclaimed synchronously, so admitting now would size the next task
    # against memory that is still held (F7). Ordinary tasks skip; force_run
    # (UI Force / `tq force`) still goes through admission.
    try:
        from taskqueue.queries import cancel_dead_dependents, cancel_negative_dependents

        cancel_dead_dependents(conn, home, cfg)
        cancel_negative_dependents(conn)
    except Exception as _cde:
        print(f"[queue] {_ts()} warn: dependency-cancel sweep failed: {_cde}", file=sys.stderr)
    try:
        _preempt_scavenger(conn, home, active_runs, st.scav_state, cfg, now)
    except Exception as _spe:
        print(f"[queue] {_ts()} warn: scavenger preempt failed: {_spe}", file=sys.stderr)
    st.cooldown_logged = _run_local_admission(
        conn, home, st.projects, active_runs, system_info, cfg,
        st.graceful_stop, env.is_shutdown(),
        now=now,
        admit_frozen_until=st.admit_frozen_until,
        cooldown_logged=st.cooldown_logged,
        reservation_state=st.reservation_state,
        cpu_ema=st.cpu_ema,
    )

    # Hourly maintenance runs on its own thread + connection: retention deletes and log
    # rotation must not stall the scheduler tick.
    if now - st.last_history_prune > 3600:
        st.last_history_prune = now
        _start_hourly_maintenance(env.home)
    return True


def idle_wait_and_fast_admit(env: TickEnv, st: TickState) -> None:
    """Wait for the next tick; a wake (finished run / YAML change) reaps and admits at once."""
    conn, home = env.conn, env.home
    active_runs, system_info = env.active_runs, env.system_info
    cfg = st.cfg
    from taskqueue import sched_wake

    # F-001: stamp wall time at the start of the idle wait so the next
    # tick's gap measures only the wait, not this tick's own work (a slow
    # tick, e.g. a 30 s graceful stop, must not look like a host sleep).
    st.last_wall = time.time()
    if sched_wake.wait_or_timeout(st.interval):
        sched_wake.clear()
        if not env.is_shutdown() and not env.host_sleeping() and not env.lease_lost.is_set():
            # T-200: YAML wake must sync before admit so new tasks are visible.
            if env.sync_requested.is_set():
                try:
                    _replace_registry(st.projects, load_projects(home))
                except Exception as exc:
                    print(
                        f"[queue] {_ts()} warn: projects.yaml invalid, "
                        f"keeping previous registry: {exc}",
                        file=sys.stderr,
                    )
                for project in st.projects.values():
                    _sync_project(conn, project, home=home)
                st.last_sync = time.time()
                env.sync_requested.clear()
            st.cooldown_logged = _fast_reap_and_admit(
                conn, home, st.projects, active_runs, system_info, cfg,
                st.graceful_stop, env.is_shutdown(),
                admit_frozen_until=st.admit_frozen_until,
                cooldown_logged=st.cooldown_logged,
                reservation_state=st.reservation_state,
                cpu_ema=st.cpu_ema,
                frozen_alerted=st.frozen_alerted,
            )


def _acquire_master_lease(
    conn: sqlite3.Connection, my_instance: str, claim_master: bool, pre_stop: Any,
) -> None:
    """Force-claim the master lease, or wait (interruptibly) until it is ours."""
    from taskqueue.config import MASTER_LEASE_TTL_SEC

    if claim_master:
        # --claim-master --force: unconditionally overwrite (caller checked --force)
        now = int(time.time())
        row = conn.execute(
            "SELECT epoch FROM master_lease WHERE singleton=1"
        ).fetchone()
        epoch = int(row["epoch"] or 0) + 1 if row else 1
        conn.execute(
            "INSERT OR REPLACE INTO master_lease (singleton, holder, expires_at, epoch) "
            "VALUES (1, ?, ?, ?)",
            (my_instance, now + MASTER_LEASE_TTL_SEC, epoch),
        )
        conn.commit()
        print(
            f"[queue] {time.strftime('%H:%M:%S')} master lease force-claimed: "
            f"holder={my_instance} epoch={epoch}",
            flush=True,
        )
    else:
        _lease_lock_timeouts = 0
        while not pre_stop.is_set():
            try:
                if _cas_master_lease(conn, my_instance):
                    print(
                        f"[queue] {time.strftime('%H:%M:%S')} master lease acquired: "
                        f"holder={my_instance}",
                        flush=True,
                    )
                    break
                _lease_lock_timeouts = 0
            except MasterLeaseLockTimeout as e:
                _lease_lock_timeouts += 1
                print(
                    f"[queue] {time.strftime('%H:%M:%S')} master lease: SQLite lock timeout "
                    f"({e}) — retrying ({_lease_lock_timeouts})",
                    flush=True,
                )
                if _lease_lock_timeouts >= 3:
                    print(
                        f"[queue] {time.strftime('%H:%M:%S')} hint: SQLite lock starvation — "
                        "another process may be holding queue.db open; check for stuck tq/daemon",
                        file=sys.stderr,
                        flush=True,
                    )
                pre_stop.wait(10)
                continue
            except Exception as e:
                print(f"[queue] {time.strftime('%H:%M:%S')} master lease CAS error: {e}", flush=True)
            row = conn.execute("SELECT holder, expires_at FROM master_lease WHERE singleton=1").fetchone()
            if row:
                rem = int(row["expires_at"]) - int(time.time())
                if master_holder_process_dead(str(row["holder"])):
                    print(
                        f"[queue] {time.strftime('%H:%M:%S')} master lease held by dead "
                        f"'{row['holder']}' — reclaiming...",
                        flush=True,
                    )
                    time.sleep(0.5)
                    continue
                print(
                    f"[queue] {time.strftime('%H:%M:%S')} master lease held by '{row['holder']}' "
                    f"(expires in {rem}s), waiting...",
                    flush=True,
                )
            pre_stop.wait(10)



@dataclass
class _HostSleepState:
    sleeping: bool = False
    suspended_at: float = 0.0


def _power_listener_main(
    home: Path, active_runs: dict, host: _HostSleepState, hw_refresh_now: Any,
) -> None:
    """Windows power-event listener: drain local runs on suspend, refresh hardware on resume."""
    import ctypes
    import ctypes.wintypes

    user32 = ctypes.windll.user32
    WM_POWERBROADCAST = 0x0218
    PBT_APMSUSPEND = 4
    PBT_APMRESUMESUSPEND = 7
    PBT_APMRESUMEAUTOMATIC = 18

    # 64-bit Windows: WPARAM/LPARAM are pointer-sized; use c_int64 to avoid overflow
    WNDPROCTYPE = ctypes.WINFUNCTYPE(
        ctypes.c_int64,
        ctypes.wintypes.HWND,
        ctypes.c_uint,
        ctypes.c_int64,
        ctypes.c_int64,
    )

    def _wndproc(hwnd, msg, wparam, lparam):
        if msg == WM_POWERBROADCAST:
            if wparam == PBT_APMSUSPEND:
                host.sleeping = True
                host.suspended_at = time.time()
                print(
                    f"[queue] {_ts()} power: host suspend detected "
                    f"— draining {len(active_runs)} local run(s)",
                    flush=True,
                )
                for key in list(active_runs.keys()):
                    try:
                        from taskqueue.db import write_control_action
                        write_control_action(home, key[0], key[1], "suspend")
                    except Exception as exc:
                        print(f"[queue] {_ts()} warn: suspend drain {key}: {exc}", file=sys.stderr)
            elif wparam in (PBT_APMRESUMESUSPEND, PBT_APMRESUMEAUTOMATIC):
                slept = time.time() - host.suspended_at if host.suspended_at else 0.0
                host.sleeping = False
                host.suspended_at = 0.0
                hw_refresh_now.set()
                if slept > 0:
                    print(
                        f"[queue] {_ts()} power: host resumed after {slept:.0f}s",
                        flush=True,
                    )
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    user32.DefWindowProcW.restype = ctypes.c_int64
    user32.DefWindowProcW.argtypes = [
        ctypes.wintypes.HWND, ctypes.c_uint, ctypes.c_int64, ctypes.c_int64
    ]
    wndproc_cb = WNDPROCTYPE(_wndproc)
    try:
        WNDCLASS = type("WNDCLASS", (ctypes.Structure,), {
            "_fields_": [
                ("style", ctypes.c_uint),
                ("lpfnWndProc", WNDPROCTYPE),
                ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int),
                ("hInstance", ctypes.wintypes.HMODULE),
                ("hIcon", ctypes.wintypes.HANDLE),
                ("hCursor", ctypes.wintypes.HANDLE),
                ("hbrBackground", ctypes.wintypes.HANDLE),
                ("lpszMenuName", ctypes.wintypes.LPCWSTR),
                ("lpszClassName", ctypes.wintypes.LPCWSTR),
            ]
        })
        wc = WNDCLASS()
        wc.lpfnWndProc = wndproc_cb
        wc.lpszClassName = "TQPowerListener"
        user32.RegisterClassW(ctypes.byref(wc))
        hwnd = user32.CreateWindowExW(
            0, "TQPowerListener", "TQPowerListener",
            0, 0, 0, 0, 0, None, None, None, None
        )
        if hwnd:
            msg = ctypes.wintypes.MSG()
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) != 0:
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
    except Exception as exc:
        print(f"[queue] {_ts()} warn: power listener failed to start: {exc}", file=sys.stderr)


def _start_lease_renew_thread(
    db_path: Path, my_instance: str, lease_lost: threading.Event, is_shutdown: Callable[[], bool],
) -> threading.Thread:
    """F-074: renew master_lease on a dedicated thread so a blocked scheduler
    tick (heavy sync / SQLite busy) cannot expire fencing and 503 workers.
    """
    from taskqueue.config import MASTER_LEASE_RENEW_SEC, MASTER_LEASE_TTL_SEC

    def _master_lease_renew_loop() -> None:
        from taskqueue.db import open_write_conn as _owc

        while not is_shutdown():
            time.sleep(MASTER_LEASE_RENEW_SEC)
            if is_shutdown():
                break
            try:
                wconn = _owc(db_path, timeout=5.0)
                try:
                    cur = wconn.execute(
                        "UPDATE master_lease SET expires_at=? WHERE singleton=1 AND holder=?",
                        (int(time.time()) + MASTER_LEASE_TTL_SEC, my_instance),
                    )
                    wconn.commit()
                    if cur.rowcount == 0:
                        row = wconn.execute(
                            "SELECT holder, expires_at FROM master_lease WHERE singleton=1"
                        ).fetchone()
                        holder_now = row["holder"] if row else "unknown"
                        print(
                            f"[queue] {_ts()} WARN: master lease lost to '{holder_now}' "
                            f"(renew thread); halting remote admit until reclaim",
                            file=sys.stderr,
                            flush=True,
                        )
                        lease_lost.set()
                    else:
                        lease_lost.clear()
                finally:
                    wconn.close()
            except Exception as exc:
                print(
                    f"[queue] {_ts()} warn: master lease renew thread error: {exc}",
                    file=sys.stderr,
                )

    thread = threading.Thread(target=_master_lease_renew_loop, name="tq-master-lease", daemon=True)
    thread.start()
    return thread


def _stop_and_finalize_all(active_runs: "dict[RunKey, ActiveRun]") -> None:
    """Stop every active run (in parallel); finalize each as interrupted/completed."""
    def _safe_stop(run) -> None:
        try:
            run.stop()
        except Exception as _e:
            print(f"[queue] {_ts()} warn: shutdown stop failed: {_e}", file=sys.stderr)

    def _shutdown_one(key, run) -> None:
        # run.stop() already happened (in parallel); finalize uses the shared
        # DB connection so it must stay on this thread.
        # F-043: don't overwrite completed status — subprocess may have finished between
        # the collect pass and now; use its actual exit code if available.
        _ec = run._exit_code
        if _ec is not None:
            _final_status = STATUS_COMPLETED if _ec == 0 else STATUS_INTERRUPTED
        else:
            _final_status = STATUS_INTERRUPTED
        run.stop_reason = run.stop_reason or "daemon_shutdown"
        # F-101: only_if_running=True prevents clobbering a run already finalized
        # (e.g. with status=failed) if the reader thread completed it between the
        # last _collect_finished_runs pass and shutdown.
        # E5: retry finalize up to 3 times with 0.5s backoff on transient DB errors.
        for _fin_attempt in range(3):
            try:
                run.finalize(_final_status, stopped_by="tq", only_if_running=True)
                break
            except Exception as _fe:
                if _fin_attempt < 2:
                    time.sleep(0.5)
                else:
                    print(
                        f"[queue] {_ts()} warn: shutdown finalize {task_key(*key)} failed after 3 attempts: {_fe}",
                        file=sys.stderr,
                    )

    # Stop process trees in parallel so N runs do not cost N x graceful_stop_sec.
    _items = list(active_runs.items())
    if len(_items) > 1:
        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(max_workers=min(8, len(_items))) as _ex:
            list(_ex.map(lambda kr: _safe_stop(kr[1]), _items))
    elif _items:
        _safe_stop(_items[0][1])
    for _k, _r in _items:
        _shutdown_one(_k, _r)


def run_daemon(home: Path, *, claim_master: bool = False) -> None:
    home.mkdir(parents=True, exist_ok=True)
    pid_path = tqconfig.pid_path(home)
    clean_path = tqconfig.clean_shutdown_path(home)

    conn = open_db(tqconfig.db_path(home))

    boot = classify_daemon_boot(home, conn)
    if boot.prev_alive:
        print(
            f"[queue] {_ts()} ERROR: daemon.pid points to live pid={boot.prev_pid}; "
            "refusing to start (stop the other master first)",
            file=sys.stderr,
            flush=True,
        )
        sys.exit(1)

    if boot.dirty:
        insert_daemon_event(
            conn,
            DAEMON_EVENT_DIRTY_RESTART,
            pid=os.getpid(),
            detail={
                "prev_pid": boot.prev_pid,
                "had_clean": boot.had_clean,
                "last_kind": boot.last_kind,
            },
        )
        print(
            f"[queue] {_ts()} dirty restart detected "
            f"(prev_pid={boot.prev_pid}, had_clean={boot.had_clean}, last_kind={boot.last_kind})",
            flush=True,
        )

    # F-103: include process create_time so _daemon_running can detect PID reuse.
    try:
        import psutil as _psutil
        _ct = _psutil.Process(os.getpid()).create_time()
        pid_path.write_text(f"{os.getpid()}:{_ct:.3f}")
    except Exception:
        pid_path.write_text(str(os.getpid()))

    # W7.1/F-058: master failover lease — incarnation identity, not hostname alone
    from taskqueue.config import MASTER_LEASE_TTL_SEC, MASTER_LEASE_RENEW_SEC

    _my_instance = new_master_instance_id()
    set_master_instance_id(_my_instance)
    _lease_epoch: int | None = None
    # Standby wait for the lease must be interruptible by SIGTERM/SIGINT; the
    # full handler is installed only after the main loop is set up.
    import threading as _threading

    _pre_stop = _threading.Event()

    def _pre_lease_sig(sig, frame):
        _pre_stop.set()

    _prev_handlers: dict = {}
    for _sn in ("SIGTERM", "SIGINT", "SIGBREAK"):
        _sg = getattr(signal, _sn, None)
        if _sg is None:
            continue
        try:
            _prev_handlers[_sg] = signal.signal(_sg, _pre_lease_sig)
        except ValueError:  # not the main thread
            pass
    _acquire_master_lease(conn, _my_instance, claim_master, _pre_stop)

    if _pre_stop.is_set():
        print(f"[queue] {_ts()} stop requested while waiting for master lease", flush=True)
        try:
            pid_path.unlink(missing_ok=True)
        except OSError:
            pass
        return

    row_ep = conn.execute("SELECT epoch FROM master_lease WHERE singleton=1").fetchone()
    if row_ep is not None:
        _lease_epoch = int(row_ep["epoch"] or 0)

    insert_daemon_event(
        conn,
        DAEMON_EVENT_START,
        pid=os.getpid(),
        instance_id=_my_instance,
        epoch=_lease_epoch,
        detail={"dirty": boot.dirty},
    )
    try:
        clean_path.unlink(missing_ok=True)
    except OSError:
        pass

    system_info = detect_system()
    active_runs: dict[RunKey, ActiveRun] = {}
    projects: dict[str, Project] = load_projects(home)

    # Re-queue tasks and close stale run records left by a previous daemon session.
    # Also SIGTERM/SIGKILL any local orphan PIDs recorded on those runs.
    cfg0 = get_config(conn)
    grace0 = float(cfg0.get("graceful_stop_sec", 30))
    n_orphans = requeue_orphaned_running(conn, reason="orphan", graceful_sec=grace0)
    if n_orphans:
        insert_daemon_event(
            conn,
            DAEMON_EVENT_ORPHAN_REQUEUE,
            pid=os.getpid(),
            instance_id=_my_instance,
            epoch=_lease_epoch,
            detail={"orphans": n_orphans},
        )
        print(f"[queue] {_ts()} startup: re-queued {n_orphans} orphaned task(s)", flush=True)

    # I-17: also reclaim any remote tasks whose leases expired during downtime
    from taskqueue.db import reclaim_expired_leases as _reclaim
    heartbeat0 = float(cfg0.get("heartbeat_sec", 30))
    stale_remote = _reclaim(conn, heartbeat_sec=heartbeat0)
    if stale_remote:
        print(f"[queue] {_ts()} startup: reclaimed {len(stale_remote)} stale remote lease(s)", flush=True)

    # Initial sync
    for project in projects.values():
        _sync_project(conn, project, home=home)

    # I-07: file-watch triggered sync — supplement the 60s poll with instant YAML change detection
    import threading as _threading
    _sync_requested = _threading.Event()
    _hw_refresh_now = _threading.Event()
    _watchdog_observer = None
    try:
        from watchdog.observers import Observer as _WdObserver  # type: ignore
        from watchdog.events import FileSystemEventHandler as _WdHandler  # type: ignore

        class _YamlWatcher(_WdHandler):
            def on_any_event(self, ev):  # type: ignore
                if not ev.is_directory and str(getattr(ev, "src_path", "")).endswith(
                    (".yaml", ".yml")
                ):
                    _sync_requested.set()
                    try:
                        from taskqueue.sched_wake import wake_scheduler
                        wake_scheduler()
                    except Exception:
                        pass

        _watchdog_observer = _WdObserver()
        watched = 0
        for _proj in projects.values():
            if _proj.queue_dir.is_dir():
                _watchdog_observer.schedule(_YamlWatcher(), str(_proj.queue_dir), recursive=False)
                watched += 1
        if watched:
            _watchdog_observer.start()
            print(f"[queue] {_ts()} file-watch active for {watched} project queue dir(s)", flush=True)
    except ImportError:
        print(f"[queue] {_ts()} watchdog not installed — using 60s poll for YAML sync", flush=True)

    _host = _HostSleepState()
    if sys.platform == "win32":
        _threading.Thread(
            target=_power_listener_main,
            args=(home, active_runs, _host, _hw_refresh_now),
            daemon=True,
        ).start()

    cfg = get_config(conn)
    port = int(cfg.get("web_port", 8765))
    bind_host = str(cfg.get("bind_host") or "127.0.0.1")
    start_web_server(
        conn,
        active_runs,
        system_info,
        projects_ref=lambda: projects,
        port=port,
        bind_host=bind_host,
    )
    auth_on = "on" if (cfg.get("auth_token") or "") else "off"
    print(
        f"[queue] {_ts()} daemon started | pid={os.getpid()} | "
        f"projects={','.join(projects) or '(none registered)'} | "
        f"web=http://{bind_host}:{port} | auth={auth_on}",
        flush=True,
    )
    _n_gpu = len(system_info.get("gpus") or [])
    _gpu_err = system_info.get("gpu_probe_error")
    _gpu_total = float(system_info.get("gpu_vram_total_gib") or 0.0)
    if _gpu_err:
        print(
            f"[queue] {_ts()} GPU probe: {_gpu_err} "
            f"(devices={_n_gpu} total={_gpu_total:.1f} GiB"
            f"{' stale' if system_info.get('gpu_probe_stale') else ''})",
            flush=True,
        )
    else:
        print(
            f"[queue] {_ts()} GPU: {_n_gpu} device(s), {_gpu_total:.1f} GiB total",
            flush=True,
        )

    try:
        from taskqueue.notify import winotify_available

        if not winotify_available():
            n_toast = 0
            try:
                n_toast = int(
                    conn.execute(
                        "SELECT COUNT(*) AS n FROM system_alerts "
                        "WHERE enabled = 1 AND windows_toast = 1"
                    ).fetchone()["n"]
                    or 0
                )
            except Exception:
                pass
            if n_toast > 0:
                print(
                    f"[queue] {_ts()} WARN: {n_toast} system alert(s) want Windows popup "
                    "but winotify is missing — pip install winotify "
                    "(or: pip install 'taskqueue[toast]')",
                    flush=True,
                )
    except Exception:
        pass

    shutdown = False

    # F-074: renew master_lease on a dedicated thread so a blocked scheduler
    # tick (heavy sync / SQLite busy) cannot expire fencing and 503 workers.
    _lease_lost = _threading.Event()
    _start_lease_renew_thread(
        tqconfig.db_path(home), _my_instance, _lease_lost, lambda: shutdown
    )

    def _handle_sigterm(sig, frame):
        nonlocal shutdown
        shutdown = True
        print(f"[queue] {_ts()} shutdown requested (signal {sig}); active_runs={len(active_runs)}", flush=True)

    signal.signal(signal.SIGTERM, _handle_sigterm)
    try:
        signal.signal(signal.SIGINT, _handle_sigterm)
    except ValueError:  # not the main thread
        pass
    if hasattr(signal, "SIGBREAK"):  # Windows Ctrl+Break
        signal.signal(signal.SIGBREAK, _handle_sigterm)

    env = TickEnv(
        conn=conn, home=home, active_runs=active_runs, system_info=system_info,
        my_instance=_my_instance, sync_requested=_sync_requested,
        hw_refresh_now=_hw_refresh_now, lease_lost=_lease_lost,
        is_shutdown=lambda: shutdown, host_sleeping=lambda: _host.sleeping,
    )
    st = TickState(projects)

    while not shutdown:
        try:
            if not daemon_tick(env, st):
                time.sleep(st.interval)
                continue
            idle_wait_and_fast_admit(env, st)
        except Exception as exc:
            import traceback
            print(
                f"[queue] {_ts()} ERROR: scheduler tick failed unexpectedly: {exc} "
                "(daemon continues -- see traceback below)",
                file=sys.stderr,
            )
            traceback.print_exc()
            # Discard partial writes from the failed tick and still reap finished runs.
            _recover_after_tick_failure(conn, active_runs, home, st.frozen_alerted)
            time.sleep(st.interval)

    from taskqueue.power import set_prevent_sleep

    set_prevent_sleep(False)

    if _watchdog_observer is not None:
        try:
            _watchdog_observer.stop()
            _watchdog_observer.join(timeout=2)
        except Exception:
            pass

    # Graceful shutdown: stop all running tasks
    print(f"[queue] {_ts()} shutdown: stopping running tasks", flush=True)
    try:
        _stop_and_finalize_all(active_runs)
    finally:
        pid_path.unlink(missing_ok=True)
    try:
        clean_path.write_text(time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), encoding="utf-8")
    except OSError as exc:
        print(f"[queue] {_ts()} warn: could not write daemon.clean: {exc}", file=sys.stderr)
    try:
        if release_master_lease(conn, _my_instance):
            print(f"[queue] {_ts()} master lease released", flush=True)
    except Exception as exc:
        print(f"[queue] {_ts()} warn: could not release master lease: {exc}", file=sys.stderr)
    try:
        insert_daemon_event(
            conn,
            DAEMON_EVENT_CLEAN_SHUTDOWN,
            pid=os.getpid(),
            instance_id=_my_instance,
            epoch=_lease_epoch,
        )
    except Exception as exc:
        print(f"[queue] {_ts()} warn: could not record clean_shutdown: {exc}", file=sys.stderr)
    print(f"[queue] {_ts()} daemon exited", flush=True)


def run_standby(home: Path, *, port: int = 8765, bind_host: str = "127.0.0.1") -> bool:
    """W7.3: Standby daemon mode — open DB, serve /api/status + /api/import only.

    Does NOT claim the master lease, does NOT schedule tasks.
    Safe to run alongside or after a master daemon for inspection or hot-standby.

    Returns True if a promote signal was received (caller should run_daemon next).
    """
    import signal as _signal
    import threading as _threading

    home.mkdir(parents=True, exist_ok=True)
    conn = open_db(tqconfig.db_path(home))

    print(f"[standby] {time.strftime('%H:%M:%S')} starting standby on {bind_host}:{port}", flush=True)

    _promote_event = _threading.Event()

    from taskqueue.web import start_standby_server
    start_standby_server(conn, home, port=port, bind_host=bind_host, promote_event=_promote_event)

    shutdown = False

    def _handle_sig(sig, frame):
        nonlocal shutdown
        shutdown = True

    _signal.signal(_signal.SIGTERM, _handle_sig)
    if hasattr(_signal, "SIGBREAK"):
        _signal.signal(_signal.SIGBREAK, _handle_sig)

    try:
        while not shutdown and not _promote_event.is_set():
            time.sleep(1)
    except KeyboardInterrupt:
        pass

    if _promote_event.is_set():
        print(f"[standby] {time.strftime('%H:%M:%S')} promote received — transitioning to master", flush=True)
        return True
    print(f"[standby] {time.strftime('%H:%M:%S')} standby exited", flush=True)
    return False


def main(argv: list[str] | None = None) -> int:
    # Ensure stdout/stderr tolerate non-ASCII even when piped to a cp1252 log file.
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace", line_buffering=True)
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace", line_buffering=True)
    # Module loggers (migrations, notify, runner, system_alerts, ...) were silently dropped:
    # route them to stderr, which `tq start` sends to daemon.log next to the [queue] lines.
    import logging

    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stderr,
        format="[%(name)s] %(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    parser = argparse.ArgumentParser(description="taskqueue daemon")
    parser.add_argument("--home", default=None, help="State home (default: ~/.taskqueue or $TASKQUEUE_HOME)")
    parser.add_argument("--claim-master", action="store_true", help="Force-claim the master lease; requires --force")
    parser.add_argument("--force", action="store_true", help="Required with --claim-master to prevent accidental takeover")
    parser.add_argument("--standby", action="store_true", help="W7.3: run in standby mode (no scheduling, no lease, serves /api/status + /api/import)")
    parser.add_argument("--port", type=int, default=8765, help="HTTP port (default 8765)")
    parser.add_argument("--bind", default="127.0.0.1", help="Bind address (default 127.0.0.1)")
    args = parser.parse_args(argv)

    if args.claim_master and not args.force:
        print("error: --claim-master requires --force to prevent accidental lease takeover", file=sys.stderr)
        return 1

    home = Path(args.home) if args.home else tqconfig.state_home()
    if args.standby:
        promoted = run_standby(home, port=args.port, bind_host=args.bind)
        if promoted:
            # W7.2: promote signal received from master-migrate; claim the lease and run.
            run_daemon(home, claim_master=True)
    else:
        run_daemon(home, claim_master=args.claim_master)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
