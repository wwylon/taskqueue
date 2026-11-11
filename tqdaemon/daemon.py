"""Queue daemon — always-running scheduler loop, all projects, one resource budget.

Start with: tq start
Or directly: python -m tqdaemon.daemon
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sqlite3
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from tqdaemon import config as tqconfig
from tqdaemon import errors as tqerr
from tqdaemon._db_runs import finish_run, mark_stale_workers_offline, reclaim_expired_leases
from tqdaemon._db_tasks import get_task, set_task_error, set_task_status, transition_task
from tqdaemon._fmt import fmt_duration as _fmt_duration  # H1: shared helper
from tqdaemon._time import (
    _now,
    _ts,  # noqa: E402 (after local imports)
)
from tqdaemon.config import Project, load_projects
from tqdaemon.cron import check_cron_schedule as _check_cron_schedule
from tqdaemon.db import (
    DAEMON_EVENT_CLEAN_SHUTDOWN,
    DAEMON_EVENT_DIRTY_RESTART,
    DAEMON_EVENT_ORPHAN_REQUEUE,
    DAEMON_EVENT_START,
    get_config,
    insert_daemon_event,
    new_master_instance_id,
    open_db,
    prune_daemon_events,
    release_master_lease,
    set_master_instance_id,
)
from tqdaemon.lease import (  # noqa: F401  (re-exported for callers/tests)
    MasterLease,
    MasterLeaseLockTimeout,
    _acquire_master_lease,
    _cas_master_lease,
    _start_lease_renew_thread,
)
from tqdaemon.models import (
    EXIT_SUSPENDED,
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_INTERRUPTED,
    STATUS_QUEUED,
    STATUS_STOPPED,
    STATUS_SUSPENDED,
    task_key,
)
from tqdaemon.resources import detect_system, gpu_refresh_interval, ram_used_gib
from tqdaemon.runner import ActiveRun, build_process_children_map
from tqdaemon.scheduler import _note_block
from tqdaemon.victim import pick_victim
from tqdaemon.web import start_web_server

# YAML cache + queue-definition sync live in yaml_sync; re-exported for existing callers.
from tqdaemon.yaml_sync import (  # noqa: F401
    _YAML_CACHE_MAX,
    YAML_PARSER_GEN,
    _sync_project,
    _yaml_mtime_cache,
    clear_yaml_mtime_cache,
    iter_queue_files,
)

RunKey = tuple[str, str]
_log = logging.getLogger(__name__)


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
        from tqdaemon.config import control_path

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
        print(f"[queue] {_ts()} daemon.log rotated ({st.st_size / 1024**2:.0f} MiB -> daemon.log.1)", flush=True)
        return True
    except (OSError, ValueError, AttributeError):
        return False


# F-208: pure helpers live in daemon_clock (re-exported for existing imports/tests).
from tqdaemon.daemon_clock import clock_jump_threshold, is_clock_jump  # noqa: E402


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
    from tqdaemon.db import request_stop
    from tqdaemon.victim import pick_victim as _pick

    evict_key, scored = _pick(conn, [(k, active_runs[k]) for k in keys if k in active_runs], resource="ram")
    if evict_key is None:
        return
    runner_up = f"; runner-up {task_key(*scored[1][0])} score={scored[1][1]:.2f}" if len(scored) > 1 else ""
    print(
        f"[queue] {_ts()} {label}: suspending {task_key(*evict_key)} (score={scored[0][1]:.2f}{runner_up})",
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
    scavs = [k for k, r in active_runs.items() if r.task_row["task_class"] == "scavenger"] if active_runs else []
    if not scavs:
        return False
    cooldown = float(cfg.get("scavenger_preempt_cooldown_sec", 60))
    if now - float(state.get("at", 0.0)) < cooldown:
        return False

    def _stop_flag(k) -> int:
        r = conn.execute("SELECT stop_requested FROM tasks WHERE project=? AND task_id=?", k).fetchone()
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
        conn,
        home,
        active_runs,
        scavs,
        f"scavenger preempt (capacity wanted by {contender['project']}/{contender['task_id']})",
        reason="scavenger_preempt",
    )
    state["at"] = now
    return True


def _release_hung_remote(
    conn: sqlite3.Connection, row: Any, *, run_status: str, task_status: str, now_iso: str
) -> None:
    """Finish a hung remote run and move its task to *task_status*, atomically (I-17).

    ds2 §1.5: a savepoint, so a partial failure cannot leave ``stop_requested=1`` with the
    task still 'running'. ``stop_requested`` is raised first (the live worker is told to stop
    via heartbeat) and cleared in the final update. The caller commits.
    """
    key = (row["project"], row["task_id"])
    conn.execute("SAVEPOINT hung_release")
    try:
        conn.execute(
            "UPDATE tasks SET stop_requested = 1, updated_at = ? WHERE project = ? AND task_id = ? AND status = 'running'",
            (now_iso, *key),
        )
        finish_run(
            conn, row["run_id"], -1, run_status, last_error_code=tqerr.INFRA_HUNG, only_if_running=True, commit=False
        )
        transition_task(
            conn,
            *key,
            to=task_status,
            from_="running",
            now=now_iso,
            last_error_code=tqerr.INFRA_HUNG,
            block_reason=None,
            stop_requested=0,
        )
        conn.execute("RELEASE hung_release")
    except Exception:
        conn.execute("ROLLBACK TO hung_release")
        conn.execute("RELEASE hung_release")
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
        from tqdaemon.fencing import (
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
            semantics = resolve_execution_semantics(raw_sem, idempotent=bool(int(row["idempotent"] or 0)))
            if not may_auto_requeue(semantics):
                # F-019 / R-011: non-requeueable hung tasks can't be auto-reassigned;
                # after 3×hung_k ticks escalate to unknown (operator decides).
                if count >= hung_k * 3:
                    dest = lost_attempt_task_status(semantics)
                    _release_hung_remote(conn, row, run_status="failed", task_status=dest, now_iso=now_iso)
                    conn.commit()
                    print(
                        f"[queue] {_ts()} hung {semantics} task "
                        f"{task_key(row['project'], row['task_id'])} "
                        f"({count} ticks no progress) → {dest}",
                        flush=True,
                    )
                else:
                    _note_block(
                        conn,
                        row["project"],
                        row["task_id"],
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
            # The heartbeat fences the old worker (run no longer 'running'); F-008: the task
            # is re-queued with stop_requested cleared so the new attempt is not stopped.
            _release_hung_remote(conn, row, run_status="interrupted", task_status="queued", now_iso=now_iso)
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


from tqdaemon.stop_reasons import FORCED_STOP_REASONS as _FORCED_STOP_REASONS  # noqa: E402
from tqdaemon.stop_reasons import PRESSURE_STOP_REASONS, stopped_by_for  # noqa: E402


def _recover_after_tick_failure(conn, active_runs: dict, home: Path | None, frozen_alerted=None) -> None:
    """Roll back a failed tick and still reap finished runs.

    A phase that raises before ``_collect_finished_runs`` would otherwise leave finished
    runs unreaped (and hard-pressure stops skipped) every tick for as long as it keeps failing.
    """
    try:
        conn.rollback()
    except sqlite3.Error:
        pass
    try:
        _collect_finished_runs(conn, active_runs, home, frozen_alerted=frozen_alerted)
    except Exception as exc:  # noqa: BLE001 - the reap after a tick failure must not raise again
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
    elif reader_crashed and (code in (0, -1) or negative_exit) and stop_reason not in _FORCED_STOP_REASONS:
        # A crashed reader means we never saw the run finish: never "completed".
        # F-237: that includes a declared negative exit — on Windows the reader's own
        # terminate() exits 1, which a task may list in negative_exit_codes.
        status = STATUS_FAILED
        err_code = tqerr.RUN_NONZERO
    elif stop_reason in PRESSURE_STOP_REASONS:
        # F-192 / F-219: a pressure victim always lands auto-restartable (stopped),
        # whatever it exited with — including 0 after a graceful handler and 75
        # after a checkpoint. ``suspended`` / ``interrupted`` would need an operator.
        status = STATUS_STOPPED
        err_code = tqerr.STOP_VRAM if stop_reason == "vram_hard" else tqerr.STOP_RAM
    elif stop_reason == "wall_clock_cap":
        # F-219: before EXIT_SUSPENDED — the exit-75 checkpoint window exists so that
        # auto_retry_on_cap can re-queue (F-087); ``suspended`` skipped that retry.
        status = STATUS_INTERRUPTED
        err_code = tqerr.RUN_CAP
    elif code == 0 and stop_reason in _FORCED_STOP_REASONS:
        # tq forced a stop (frozen, evicted, ...): exit 0 after a cooperative
        # suspend is not a finished run; dependents must not be released.
        status = STATUS_INTERRUPTED
    elif code == 0:
        status = STATUS_COMPLETED
    elif stop_reason == "evicted":
        # F-219: limit_decrease_policy=evict is a tq-initiated suspend; land
        # auto-restartable (like scavenger preemption), not operator-only suspended.
        status = STATUS_STOPPED
    elif code == EXIT_SUSPENDED:
        status = STATUS_SUSPENDED
    elif stop_reason in ("frozen", "frozen_restart"):
        status = STATUS_INTERRUPTED
        err_code = tqerr.RUN_FROZEN
    elif stop_requested:
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
        stopped_by = stopped_by_for(stop_reason) or ("user" if stop_requested else None)
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
                    _sr = (
                        conn.execute(
                            "SELECT stop_reason FROM runs WHERE run_id = ?",
                            (run.run_id,),
                        ).fetchone()
                        if run.run_id
                        else None
                    )
                    if _sr and _sr["stop_reason"]:
                        run.stop_reason = _sr["stop_reason"]
                except sqlite3.Error:
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
            except Exception as _fe:  # noqa: BLE001 - any finalize error defers to the next tick (F-097)
                # F-097: DB lock or transient error; leave run in active_runs so the
                # next tick retries finalize rather than double-finalizing with stale state.
                print(f"[queue] {_ts()} warn: finalize {task_key(*key)} deferred: {_fe}", flush=True)
                continue
            # Finalized: drop it now so a later DB error in the follow-ups below can
            # not leave it in active_runs and get it finalized (and alerted) twice.
            del active_runs[key]
            # F-241: drop this run from the host GPU lock (file gone when last GPU ends).
            if home is not None:
                try:
                    from tqdaemon.gpu_lock import release as _gpu_lock_release
                    from tqdaemon.resources import task_requires_gpu as _task_requires_gpu

                    _res = run.task_row["resources"] if getattr(run, "task_row", None) is not None else None
                    if _task_requires_gpu(_res):
                        _gpu_lock_release(
                            home,
                            project=key[0],
                            task_id=key[1],
                            run_id=int(run.run_id or 0),
                        )
                except Exception:  # noqa: BLE001 - lock file is advisory
                    _log.debug("gpu.lock release failed", exc_info=True)
            try:
                from tqdaemon.system_alerts import on_run_terminal

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
            except Exception:  # noqa: BLE001 - alert hook must not block finalize
                _log.debug("finish hook failed", exc_info=True)
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
                    from tqdaemon.db import apply_cap_auto_retry

                    outcome = apply_cap_auto_retry(conn, key[0], key[1])
                    if outcome == "queued":
                        status = STATUS_QUEUED
            except Exception as _pe:  # noqa: BLE001 - post-finalize follow-ups must not undo the finalize
                print(
                    f"[queue] {_ts()} warn: post-finalize update {task_key(*key)} failed: {_pe}",
                    file=sys.stderr,
                    flush=True,
                )
            emoji = (
                "OK"
                if status == STATUS_COMPLETED
                else ("--" if status in (STATUS_INTERRUPTED, STATUS_STOPPED) else "!!")
            )
            code_tag = f" [{err_code}]" if err_code else ""
            print(
                f"[queue] {_ts()} {emoji} {task_key(*key)} -> {status} "
                f"(exit {code}, {_fmt_duration(run.elapsed_sec())}){code_tag}",
                flush=True,
            )


_cpu_gate_log: dict[str, Any] = {"blocked": False, "at": float("-inf")}


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
        _cooldown_reason = f"admission frozen for {admit_frozen_until - now:.0f}s after resource-pressure stop"
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
        _cpu_reason = f"admission skipped (CPU): live load {cpu_ema:.1f} cores > limit {_cpu_limit_cores:.1f}"
        # F-224: log when the gate closes, then at most every HEARTBEAT_LOG_SEC (was every tick).
        if now - _cpu_gate_log["at"] >= HEARTBEAT_LOG_SEC or not _cpu_gate_log["blocked"]:
            print(f"[queue] {_ts()} {_cpu_reason}", flush=True)
            _cpu_gate_log["at"] = now
    elif _cpu_gate_log["blocked"]:
        print(f"[queue] {_ts()} admission resumed (CPU): live load {cpu_ema:.1f} cores", flush=True)
    _cpu_gate_log["blocked"] = _cpu_blocked
    from tqdaemon.scheduler import _local_admit_queued

    _local_admit_queued(
        conn,
        home,
        projects,
        active_runs,
        system_info,
        cfg,
        graceful_stop,
        shutdown,
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
        conn,
        home,
        projects,
        active_runs,
        system_info,
        cfg,
        graceful_stop,
        shutdown,
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
        self.op_since: float | None = None  # wall time of the oldest unflushed op_usage sample

    def accumulate(self, active_runs: dict, interval: float, *, host_sleeping: bool = False) -> None:
        if active_runs and not host_sleeping:
            if self.op_since is None:
                self.op_since = time.time()
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
                except (AttributeError, KeyError, IndexError):
                    started_by = None
                op_key = (proj, started_by)
                if op_key not in self.op_usage:
                    self.op_usage[op_key] = {"gpu_seconds": 0.0, "cpu_seconds": 0.0}
                self.op_usage[op_key]["gpu_seconds"] += gpu_delta
                self.op_usage[op_key]["cpu_seconds"] += cpu_delta

    def flush_if_due(self, conn, cfg: dict, now: float, *, wall: float | None = None) -> None:
        # R9: hourly decay + flush project_usage to DB
        if now - self.last_decay > 3600 or self.last_decay == 0.0:
            elapsed_h = (now - self.last_decay) / 3600.0 if self.last_decay > 0 else 0.0
            # F-046: read fairshare_halflife_days from config (default 7 days)
            halflife_h = float(cfg.get("fairshare_halflife_days", 7)) * 24
            decay = 0.5 ** (elapsed_h / halflife_h) if elapsed_h > 0 else 1.0
            try:
                # Apply decay to existing project_usage rows
                conn.execute(
                    "UPDATE project_usage SET gpu_seconds = gpu_seconds * ?, cpu_seconds = cpu_seconds * ?",
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
            except sqlite3.Error as exc:
                print(f"[queue] {_ts()} warn: project_usage flush failed: {exc}", file=sys.stderr)
            self.last_decay = now

        # R10: hourly usage_snapshot flush. F-227: stamp the clock hour the samples were
        # taken in (it used to be the flush hour, i.e. one hour late) and flush when that
        # hour has rolled over, so each bucket holds only its own hour.
        wall = time.time() if wall is None else wall
        since = self.op_since if self.op_since is not None else wall
        bucket_ts = (int(since) // 3600) * 3600
        rolled = int(wall) // 3600 * 3600 != bucket_ts
        if self.op_usage and (rolled or self.last_flush == 0.0):
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
                self.op_since = None
            except sqlite3.Error as exc:
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
        except Exception as exc:  # noqa: BLE001 - hardware probes (NVML, psutil, WMI) fail in many ways
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
        f"[queue] {_ts()} clock jump detected ({gap:.0f}s > {jump_thr:.0f}s); reclaiming running tasks",
        flush=True,
    )
    # Stop locals but do not finalize — requeue_orphaned_running owns DB
    # (finalize→interrupted would skip the status='running' requeue filter).
    for key, run in list(active_runs.items()):
        try:
            run.stop()
        except Exception as exc:  # noqa: BLE001 - a stop failure must not stop the wake loop
            print(f"[queue] {_ts()} warn: stop after wake {task_key(*key)}: {exc}", file=sys.stderr)
        active_runs.pop(key, None)
    grace = float(get_config(conn).get("graceful_stop_sec", 30))
    requeue_orphaned_running(conn, reason="clock-jump", graceful_sec=grace, gap_sec=gap)


def _revalidate_running_deps(conn, home: Path, active_runs: dict) -> None:
    # Re-check depends_on for every running task (local + remote).
    # Catches upstream retry while a dependent is mid-run, and any
    # other path that leaves peers non-completed after admit.
    from tqdaemon.db import revalidate_running_dependencies

    for _dproj, _dtid, _dunmet in revalidate_running_dependencies(conn, home=home):
        _dkey = (_dproj, _dtid)
        if _dkey in active_runs:
            active_runs[_dkey].stop_reason = "deps_invalidated"
        print(
            f"[queue] {_ts()} deps invalidated while running: {task_key(_dproj, _dtid)} (unmet: {_dunmet}) — stopping",
            flush=True,
        )


def _enforce_stops_and_caps(conn, active_runs: dict, home: Path, suspend_grace: float) -> None:
    """Honour stop requests and wall-clock caps for every active local run."""
    for key, run in list(active_runs.items()):
        task_row = get_task(conn, *key)
        # The cap branch below owns a cap stop (it sets stop_requested itself).
        if task_row and task_row["stop_requested"] and not getattr(run, "_cap_stop_requested", False):
            # F-126: Tier-2 ``tq sleep`` freeze must not escalate to SIGTERM while frozen.
            try:
                _tier_row = conn.execute(
                    "SELECT sleep_tier, stop_reason FROM runs "
                    "WHERE project = ? AND task_id = ? AND status = 'running' "
                    "ORDER BY run_id DESC LIMIT 1",
                    key,
                ).fetchone()
            except sqlite3.Error:
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
                    f"[queue] {_ts()} cooperative suspend requested {task_key(*key)}{by} (grace {suspend_grace:.0f}s)",
                    flush=True,
                )
            run._stop_logged = True

        cap = task_row["wall_clock_cap_sec"] if task_row else None
        if cap and run.elapsed_sec() > cap:
            run.stop_reason = "wall_clock_cap"
            # F-224: request + log once; later ticks only wait out the grace window.
            first = not getattr(run, "_cap_stop_requested", False)
            if first:
                from tqdaemon.db import request_stop

                request_stop(conn, *key)
                run._cap_stop_requested = True
                run.note_cap_reached()
            run.request_cooperative_suspend(home)
            if run.suspend_grace_elapsed(suspend_grace):
                if not getattr(run, "_cap_kill_logged", False):
                    print(f"[queue] {_ts()} stopping {task_key(*key)} (wall_clock_cap {cap}s)", flush=True)
                    run._cap_kill_logged = True
                run.stop(background=True)
            elif first:
                print(
                    f"[queue] {_ts()} cooperative suspend requested "
                    f"{task_key(*key)} (wall_clock_cap {cap}s, grace {suspend_grace:.0f}s)",
                    flush=True,
                )


@dataclass(frozen=True)
class PressureSpec:
    """One hard-pressure resource (host RAM or GPU VRAM) and how its stops are worded."""

    resource: str  # pick_victim resource
    attr: str  # ActiveRun reading
    stop_reason: str
    err_code: str
    label: str  # "RAM" / "VRAM"
    used_prefix: str  # "host" / "VRAM" (log line)
    block_prefix: str  # "RAM: host" / "VRAM: device" (block_reason)
    held_word: str  # "RSS" / "VRAM"


RAM_PRESSURE = PressureSpec("ram", "current_ram_gib", "ram_hard", tqerr.STOP_RAM, "RAM", "host", "RAM: host", "RSS")
VRAM_PRESSURE = PressureSpec(
    "vram", "current_vram_gib", "vram_hard", tqerr.STOP_VRAM, "VRAM", "VRAM", "VRAM: device", "VRAM"
)


def _stop_pressure_victims(
    conn,
    remaining: dict,
    spec: PressureSpec,
    *,
    used: float,
    projected: float,
    soft_limit: float,
    hard_limit: float,
    total: float,
    stop_cap: int,
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
            conn,
            list(remaining.items()),
            needed_gib=max(0.0, projected - soft_limit),
            resource=spec.resource,
        )
        if victim_key is None:
            break
        victim = remaining.pop(victim_key)
        held = float(getattr(victim, spec.attr) or 0.0)
        runner_up = f"; runner-up {task_key(*scored[1][0])} score={scored[1][1]:.2f}" if len(scored) > 1 else ""
        victim.stop_reason = spec.stop_reason
        print(
            f"[queue] {_ts()} {spec.label} hard limit: stopping {task_key(*victim_key)} "
            f"({spec.used_prefix} {used:.1f} > hard {hard_limit:.1f}; "
            f"task {spec.held_word} {held:.2f} GiB; "
            f"score={scored[0][1]:.2f}{runner_up})",
            flush=True,
        )
        set_task_error(
            conn,
            victim_key[0],
            victim_key[1],
            spec.err_code,
            block_reason=(
                f"{spec.block_prefix} {used:.1f} > hard {hard_limit:.1f} "
                f"(soft {soft_limit:.1f}, total {total:.1f}); "
                f"victim {spec.held_word} {held:.2f} GiB"
            ),
        )
        victim.stop()
        projected -= held
        stopped += 1
    return stopped


def _pressure_used(spec: PressureSpec, system_info: dict, active_runs: dict, *, refreshed: bool) -> float:
    """Current usage for *spec*: host RAM, or device VRAM (task sum as fallback / after refresh)."""
    if spec is RAM_PRESSURE:
        return ram_used_gib()
    if refreshed:
        return sum(float(r.current_vram_gib or 0.0) for r in active_runs.values())
    # F-143: prefer host-wide GPU usage (includes non-task CUDA) over task-only sum.
    used = sum(float(g.get("vram_used_gib") or 0.0) for g in (system_info.get("gpus") or []))
    return used if used > 0 else sum(float(r.current_vram_gib or 0.0) for r in active_runs.values())


def _pressure_limits(spec: PressureSpec, cfg: dict, system_info: dict) -> tuple[float, float, float]:
    """(total, hard_limit, soft_limit) GiB for *spec*; total 0 disables the check."""
    if spec is RAM_PRESSURE:
        total = float(system_info.get("ram_total_gib") or 0.0)
        soft_pct = float(cfg.get("ram_limit_pct", 80))
        hard_pct = max(float(cfg.get("ram_hard_limit_pct", 95)), soft_pct)
    else:
        hard_pct = float(cfg.get("vram_hard_limit_pct", 95))
        total = float(system_info.get("gpu_vram_total_gib") or 0.0) if hard_pct > 0 else 0.0
        soft_pct = float(cfg.get("gpu_limit_pct", 100))
    return total, total * hard_pct / 100.0, total * soft_pct / 100.0


def _enforce_hard_pressure(conn, cfg: dict, system_info: dict, active_runs: dict, now: float) -> float | None:
    """Hard RAM / VRAM response: stop victims among local runs.

    Local processes consume host memory regardless of master-lease or sleep
    state, so this is also called while the lease is lost.  Returns the new
    ``admit_frozen_until`` (monotonic) when a victim was stopped, else None.
    """
    admit_frozen_until: float | None = None
    for spec in (RAM_PRESSURE, VRAM_PRESSURE):
        if not active_runs:
            break
        total, hard_limit, soft_limit = _pressure_limits(spec, cfg, system_info)
        if total <= 0 or hard_limit <= 0:
            continue
        used = _pressure_used(spec, system_info, active_runs, refreshed=False)
        if used <= hard_limit:
            continue
        # Refresh readings before choosing victims (VRAM: only under pressure, nvidia-smi is slow).
        for run in list(active_runs.values()):
            run.update_resources()
        if spec is VRAM_PRESSURE:
            used = _pressure_used(spec, system_info, active_runs, refreshed=True)
        # D9: stop until projected usage is back under the SOFT limit, not merely under
        # hard — stopping just enough to clear hard leaves the host one allocation away
        # from thrashing again.
        remaining, dying = _split_dying(active_runs, spec.attr)
        if _stop_pressure_victims(
            conn,
            remaining,
            spec,
            used=used,
            projected=used - dying,
            soft_limit=soft_limit,
            hard_limit=hard_limit,
            total=total,
            stop_cap=int(cfg.get("stop_max_per_tick", 2)),
        ):
            # D9: freeze admission while the OS reclaims, or admit later in this tick
            # sizes the next task against memory the victim has not released (F7).
            admit_frozen_until = now + float(cfg.get("recovery_cooldown_sec", 30))
    return admit_frozen_until


def _heartbeat_and_sanity(
    conn, home: Path, cfg: dict, active_runs: dict, frozen_threshold: float, frozen_alerted: set
) -> None:
    """Heartbeat: persist progress and run frozen / progress-sanity / GPU / leak checks."""
    from tqdaemon.progress_sanity import sanity_snapshot

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
            elif run.gpu_util_ema is not None and run._gpu_util_samples >= 3 and run.gpu_util_ema < gpu_util_warn_pct:
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
        gpu_ema_str = f" gpu_util={run.gpu_util_ema:.0f}%" if run.gpu_util_ema is not None else ""
        pct_str = f" {run.progress_pct:.0f}%" if run.progress_pct is not None else ""
        eta_str = ""
        if run.progress_pct and run.progress_pct > 0:
            total_est = run.elapsed_sec() / (run.progress_pct / 100.0)
            eta_str = f" ETA ~{_fmt_duration(total_est - run.elapsed_sec())}"
        frozen_str = ""
        if snap["frozen"]:
            frozen_str = f" [FROZEN out={_fmt_duration(run.idle_sec())} cpu={_fmt_duration(run.active_idle_sec())}]"
        suspect_str = " [PROGRESS_SUSPECT]" if snap["progress_suspect"] else ""
        # Heartbeat line only when something changed or every HEARTBEAT_LOG_SEC (was every ~30 s
        # per run: hundreds of MiB of daemon.log per day with many runs).
        _now_m = time.monotonic()
        _pct_i = None if run.progress_pct is None else int(run.progress_pct)
        if (
            frozen_str
            or suspect_str
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
                    from tqdaemon.system_alerts import emit_task_frozen

                    if emit_task_frozen(
                        conn, home, project=key[0], task_id=key[1], elapsed_sec=run.elapsed_sec(), run_id=run.run_id
                    ):
                        frozen_alerted.add(int(rid))
                except Exception:  # noqa: BLE001 - alert hook must not block the frozen check
                    _log.debug("frozen-task alert failed", exc_info=True)
            # I-06: frozen_action — "log" (default), "stop", "restart"
            frozen_action = str(cfg.get("frozen_action", "log")).lower()
            if frozen_action in ("stop", "restart") and key in active_runs:
                print(
                    f"[queue] {_ts()} frozen_action={frozen_action}: stopping frozen task {task_key(*key)}",
                    flush=True,
                )
                # restart: finalize will re-queue after the process exits
                # (do not set queued here — that races finalize → failed).
                active_runs[key].stop_reason = "frozen_restart" if frozen_action == "restart" else "frozen"
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
    # Called with the fresh registry after each reload (re-targets the YAML file watcher).
    on_registry_change: Callable[[dict], Any] | None = None


class _QueueDirWatches:
    """Keep one watchdog watch per registered project's queue dir (F-226).

    Watches used to be scheduled once at startup, so a project added later was only
    seen by the 60 s poll and a removed one kept its watch.

    F-240: ``release(name)`` drops that project's watch and keeps it out of
    ``update()`` across ``_resync`` until ``clear_release`` (so Windows can rename
    the project folder without stopping the daemon).
    """

    def __init__(self, observer: Any, handler: Any) -> None:
        self._observer = observer
        self._handler = handler
        self._watches: dict[str, Any] = {}
        self._released: set[str] = set()  # project names

    def release(self, project_name: str, projects: dict) -> bool:
        """Unwatch ``project_name``'s queue dir and mark it released. True if known."""
        self._released.add(project_name)
        proj = projects.get(project_name)
        if proj is None:
            return False
        d = str(proj.queue_dir)
        if d in self._watches:
            try:
                self._observer.unschedule(self._watches.pop(d))
            except Exception as exc:  # noqa: BLE001 - third-party watchdog observer
                print(f"[queue] {_ts()} warn: unwatch {d}: {exc}", file=sys.stderr)
        return True

    def clear_release(self, project_name: str) -> None:
        self._released.discard(project_name)

    def is_released(self, project_name: str) -> bool:
        return project_name in self._released

    def update(self, projects: dict) -> int:
        want = {
            str(p.queue_dir)
            for name, p in projects.items()
            if name not in self._released and p.queue_dir.is_dir()
        }
        for d in set(self._watches) - want:
            try:
                self._observer.unschedule(self._watches.pop(d))
            except Exception as exc:  # noqa: BLE001 - third-party watchdog observer
                print(f"[queue] {_ts()} warn: unwatch {d}: {exc}", file=sys.stderr)
        for d in sorted(want - set(self._watches)):
            try:
                self._watches[d] = self._observer.schedule(self._handler, d, recursive=False)
            except Exception as exc:  # noqa: BLE001 - third-party watchdog observer
                print(f"[queue] {_ts()} warn: watch {d}: {exc}", file=sys.stderr)
        return len(self._watches)


class TickState:
    """Mutable state carried from one scheduler tick to the next."""

    def __init__(self, projects: dict) -> None:
        self.projects = projects
        self.cfg: dict = {}
        self.interval = 5.0  # fallback if a tick fails before reading scheduler_interval_sec
        self.graceful_stop = 30
        self.last_sync = 0.0  # monotonic
        self.last_log_rotate = 0.0  # monotonic
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
        conn.execute("DELETE FROM resource_history WHERE ts < unixepoch('now','-7 days')")
        conn.commit()
    except sqlite3.Error:
        pass
    try:
        prune_daemon_events(conn, keep_days=90)
    except sqlite3.Error:
        pass
    try:
        from tqdaemon.usage_track import (
            DEFAULT_RETENTION_DAYS,
            RECORDER,
            prune_api_usage,
        )

        RECORDER.flush(conn)
        prune_api_usage(conn, int(get_config(conn).get("retention_usage_days", DEFAULT_RETENTION_DAYS)))
    except Exception:  # noqa: BLE001 - usage tracking is best-effort
        _log.debug("usage flush/prune failed", exc_info=True)
    try:
        _cfg_ret = get_config(conn)
        # Append-only notify logs are size-rotated even when retention is off.
        from tqdaemon._cli_prune import rotate_logs

        rotate_logs(home, dry_run=False)
        if _cfg_ret.get("retention_enabled"):
            from tqdaemon._cli_prune import run_maintenance

            _ret = run_maintenance(home, conn, _cfg_ret)
            if _ret["runs_deleted"] or _ret["audit_deleted"] or _ret["logs_deleted"]:
                print(
                    f"[queue] {_ts()} retention: { {k: v for k, v in _ret.items() if k != 'per_project'} }",
                    flush=True,
                )
    except Exception as _re:  # noqa: BLE001 - retention must not stop the tick
        print(f"[queue] {_ts()} warn: retention pass failed: {_re}", file=sys.stderr, flush=True)


_maintenance_thread: threading.Thread | None = None


def _start_hourly_maintenance(home) -> None:
    global _maintenance_thread
    if _maintenance_thread is not None and _maintenance_thread.is_alive():
        return

    def _work() -> None:
        from tqdaemon.config import db_path as _db_path
        from tqdaemon.db import open_db as _open_db

        mconn = None
        try:
            mconn = _open_db(_db_path(Path(home)), migrate=False)
            _run_hourly_maintenance(mconn, home)
        except Exception as exc:  # noqa: BLE001 - hourly maintenance must not stop the tick
            print(f"[queue] {_ts()} warn: hourly maintenance failed: {exc}", file=sys.stderr, flush=True)
        finally:
            if mconn is not None:
                try:
                    mconn.close()
                except sqlite3.Error:
                    pass

    _maintenance_thread = threading.Thread(target=_work, name="tq-maintenance", daemon=True)
    _maintenance_thread.start()


def _resync(env: TickEnv, st: TickState) -> None:
    """Re-read projects.yaml and sync every project's YAML dir.

    ``last_sync`` is monotonic (the tick compares it with ``time.monotonic()``).
    """
    # Clear before sync so a watcher set() during sync is preserved for next tick.
    env.sync_requested.clear()
    try:
        _replace_registry(st.projects, load_projects(env.home))
    except Exception as exc:  # noqa: BLE001 - a bad projects.yaml keeps the previous registry
        print(f"[queue] {_ts()} warn: projects.yaml invalid, keeping previous registry: {exc}", file=sys.stderr)
    if env.on_registry_change is not None:
        try:
            env.on_registry_change(st.projects)
        except Exception as exc:  # noqa: BLE001 - registry hook must not stop the tick
            print(f"[queue] {_ts()} warn: registry change hook failed: {exc}", file=sys.stderr)
    for project in st.projects.values():
        _sync_project(env.conn, project, home=env.home)
    st.last_sync = time.monotonic()


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
            from tqdaemon.project_colors import ensure_project_colors

            _known = {r[0] for r in conn.execute("SELECT DISTINCT project FROM tasks").fetchall()}
            ensure_project_colors(conn, sorted(_known))
        except Exception:  # noqa: BLE001 - colour assignment is cosmetic
            _log.debug("project colour assignment failed", exc_info=True)

    # Refresh GPU/CPU snapshot periodically (multi-GPU free VRAM drifts).
    # Failed probes retry on a short backoff; success uses 60s.
    # Windows resume forces an immediate re-probe.
    st.hw.maybe_refresh(system_info, now, env.hw_refresh_now)
    # P7: update CPU EMA every tick. psutil.cpu_percent(percpu=True) is
    # non-blocking and cheap; the first call returns 0.0 which the EMA
    # absorbs without distorting the steady state.
    from tqdaemon.resources import cpu_cores_from_load

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
                # F-193: stamp wall on every return so a long lease-lost gap is
                # not misread as host-sleep on the next successful tick.
                st.last_wall = wall
                return False
        except Exception as exc:  # noqa: BLE001 - lease reclaim errors are retried next tick
            print(
                f"[queue] {_ts()} warn: master lease reclaim error: {exc}",
                file=sys.stderr,
            )
            try:
                _fz = _enforce_hard_pressure(conn, cfg, system_info, active_runs, now)
                if _fz is not None:
                    st.admit_frozen_until = _fz
            except Exception:  # noqa: BLE001 - pressure check while lease is lost is best-effort
                _log.debug("hard-pressure check failed while the lease is lost", exc_info=True)
            st.last_wall = wall
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
            f"[queue] {_ts()} WARN scheduler tick slow: {gap:.0f}s (clock-jump threshold {jump_thr:.0f}s)",
            flush=True,
        )
    if is_clock_jump(st.last_wall, wall, jump_thr):
        _handle_clock_jump(conn, active_runs, gap=gap, jump_thr=jump_thr)
    st.last_wall = wall

    # Keep daemon.log bounded on its own timer (it used to ride the sync block).
    if now - st.last_log_rotate > 60:
        st.last_log_rotate = now
        rotate_own_log(env.home)

    # Re-read registry + sync every project's YAML dir every 60s or on file change (I-07)
    if now - st.last_sync > 60 or env.sync_requested.is_set():
        _resync(env, st)

    # Refresh live RAM + CPU for all active runs (fast psutil, no nvidia-smi)
    # W4.8: batch resource_history inserts into one transaction.
    # F-081: one process-table snapshot shared across all active runs.
    _history_dirty = False
    if active_runs:
        st.proc_children_tick += 1
        if st.proc_children_cache is None or st.proc_children_tick >= _PROC_CHILDREN_REFRESH_TICKS:
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
                from tqdaemon.db import record_resource_sample

                record_resource_sample(conn, run.run_id, *run._pending_history_sample, commit=False)
                _history_dirty = True
            except sqlite3.Error as _hist_exc:
                # F-129: do not silently drop history; tick continues.
                print(
                    f"[queue] {_ts()} warn: resource_history sample failed run={run.run_id}: {_hist_exc}",
                    file=sys.stderr,
                )
            run._pending_history_sample = None
    if _history_dirty:
        try:
            conn.commit()
        except sqlite3.Error as _hist_commit_exc:
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
        _heartbeat_and_sanity(conn, home, cfg, active_runs, frozen_threshold, st.frozen_alerted)
        st.last_heartbeat = now

    _frozen_until = _enforce_hard_pressure(conn, cfg, system_info, active_runs, now)
    if _frozen_until is not None:
        st.admit_frozen_until = _frozen_until
    # F-192: clear recovery freeze once host RAM is back under the soft limit
    # (dying RSS reclaimed / idle). Otherwise queued work stays blocked with a
    # stale "admission frozen for Ns" reason after pressure is gone.
    elif st.admit_frozen_until > now:
        from tqdaemon.resources import ram_used_gib

        _ram_total = float(system_info.get("ram_total_gib") or 0.0)
        _soft_pct = float(cfg.get("ram_limit_pct", 80))
        if _ram_total > 0 and ram_used_gib() <= _ram_total * _soft_pct / 100.0:
            st.admit_frozen_until = 0.0

    # I-28: Graceful eviction when resource limits decrease
    limit_decrease_policy = str(cfg.get("limit_decrease_policy", "ignore")).lower()
    if limit_decrease_policy == "evict" and active_runs:
        max_tasks_evict = int(cfg.get("max_running_tasks", 0))
        local_keys = list(active_runs.keys())
        if max_tasks_evict > 0 and len(local_keys) > max_tasks_evict:
            _evict_one_suspend(
                conn,
                home,
                active_runs,
                local_keys,
                f"limit_decrease_policy=evict (local={len(local_keys)} > max_running_tasks={max_tasks_evict})",
            )

    # Host sleep inhibit while local runs are active (Windows; complements F-001).
    from tqdaemon.power import set_prevent_sleep

    set_prevent_sleep(bool(active_runs) and bool(int(cfg.get("prevent_sleep", 1))))

    # S3: re-queue tasks whose cron schedule fired
    _check_cron_schedule(conn, st.cron_state)

    try:
        from tqdaemon.system_alerts import evaluate_all_done

        evaluate_all_done(conn, home)
    except Exception as exc:  # noqa: BLE001 - system alerts must not stop the tick
        print(f"[queue] {_ts()} warn: system alert evaluation failed: {exc}", file=sys.stderr)

    # Schedule new tasks if resources allow (one budget across all projects).
    # D9: recovering from a pressure stop — the victim's memory is not
    # reclaimed synchronously, so admitting now would size the next task
    # against memory that is still held (F7). Ordinary tasks skip; force_run
    # (UI Force / `tq force`) still goes through admission.
    try:
        from tqdaemon.queries import cancel_dead_dependents, cancel_negative_dependents

        cancel_dead_dependents(conn, home, cfg)
        cancel_negative_dependents(conn, home, cfg)
    except Exception as _cde:  # noqa: BLE001 - dependency sweep must not stop the tick
        print(f"[queue] {_ts()} warn: dependency-cancel sweep failed: {_cde}", file=sys.stderr)
    try:
        _preempt_scavenger(conn, home, active_runs, st.scav_state, cfg, now)
    except Exception as _spe:  # noqa: BLE001 - scavenger preemption must not stop the tick
        print(f"[queue] {_ts()} warn: scavenger preempt failed: {_spe}", file=sys.stderr)
    st.cooldown_logged = _run_local_admission(
        conn,
        home,
        st.projects,
        active_runs,
        system_info,
        cfg,
        st.graceful_stop,
        env.is_shutdown(),
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
    from tqdaemon import sched_wake

    # F-001: stamp wall time at the start of the idle wait so the next
    # tick's gap measures only the wait, not this tick's own work (a slow
    # tick, e.g. a 30 s graceful stop, must not look like a host sleep).
    st.last_wall = time.time()
    if sched_wake.wait_or_timeout(st.interval):
        sched_wake.clear()
        if not env.is_shutdown() and not env.host_sleeping() and not env.lease_lost.is_set():
            # T-200: YAML wake must sync before admit so new tasks are visible.
            if env.sync_requested.is_set():
                _resync(env, st)
            st.cooldown_logged = _fast_reap_and_admit(
                conn,
                home,
                st.projects,
                active_runs,
                system_info,
                cfg,
                st.graceful_stop,
                env.is_shutdown(),
                admit_frozen_until=st.admit_frozen_until,
                cooldown_logged=st.cooldown_logged,
                reservation_state=st.reservation_state,
                cpu_ema=st.cpu_ema,
                frozen_alerted=st.frozen_alerted,
            )
            # F-193: long fast-admit must not look like host-sleep on next tick.
            st.last_wall = time.time()


@dataclass
class _HostSleepState:
    sleeping: bool = False
    suspended_at: float = 0.0


def _power_listener_main(
    home: Path,
    active_runs: dict,
    host: _HostSleepState,
    hw_refresh_now: Any,
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
                    f"[queue] {_ts()} power: host suspend detected — draining {len(active_runs)} local run(s)",
                    flush=True,
                )
                for key in list(active_runs.keys()):
                    try:
                        from tqdaemon.db import write_control_action

                        write_control_action(home, key[0], key[1], "suspend")
                    except OSError as exc:
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
    user32.DefWindowProcW.argtypes = [ctypes.wintypes.HWND, ctypes.c_uint, ctypes.c_int64, ctypes.c_int64]
    wndproc_cb = WNDPROCTYPE(_wndproc)
    try:
        WNDCLASS = type(
            "WNDCLASS",
            (ctypes.Structure,),
            {
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
            },
        )
        wc = WNDCLASS()
        wc.lpfnWndProc = wndproc_cb
        wc.lpszClassName = "TQPowerListener"
        user32.RegisterClassW(ctypes.byref(wc))
        hwnd = user32.CreateWindowExW(0, "TQPowerListener", "TQPowerListener", 0, 0, 0, 0, 0, None, None, None, None)
        if hwnd:
            msg = ctypes.wintypes.MSG()
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) != 0:
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
    except Exception as exc:  # noqa: BLE001 - Win32 power listener thread must not crash the daemon
        print(f"[queue] {_ts()} warn: power listener failed to start: {exc}", file=sys.stderr)


def _detach_adoptable_runs(active_runs: dict[RunKey, ActiveRun]) -> list[RunKey]:
    """Drop live wrapper-started runs from supervision without stopping them (DB stays running)."""
    kept: list[RunKey] = []
    for key, run in list(active_runs.items()):
        if not getattr(run, "adoptable", False) or run.proc is None or run.proc.poll() is not None:
            continue
        del active_runs[key]
        kept.append(key)
        try:
            if run._log_file:
                run._log_file.write(f"[{time.strftime('%H:%M:%S')}] [queue] daemon restarting; task left running\n")
                run._log_file.close()
        except OSError:
            pass
        print(f"[queue] {_ts()} shutdown: left {task_key(*key)} running for the next daemon", flush=True)
    return kept


def _stop_and_finalize_all(active_runs: dict[RunKey, ActiveRun]) -> None:
    """Stop every active run (in parallel); finalize each as interrupted/completed."""

    def _safe_stop(run) -> None:
        try:
            run.stop()
        except Exception as _e:  # noqa: BLE001 - shutdown continues with the other runs
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
            except Exception as _fe:  # noqa: BLE001 - shutdown finalize retries any error
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
    except (OSError, _psutil.Error):
        pid_path.write_text(str(os.getpid()))

    # W7.1/F-058: master failover lease — incarnation identity, not hostname alone

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
    _lease = MasterLease(tqconfig.db_path(home), _my_instance)
    _lease.acquire(conn, force=claim_master, pre_stop=_pre_stop)

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
    # Wave 7: wrapper-started runs left by the previous daemon are adopted, not reaped. A keep-runs
    # marker the previous daemon never consumed (killed rather than signalled) is cleared here, so a
    # later `tq stop-daemon` still stops tasks.
    from tqdaemon.adopt import consume_keep_runs as _clear_keep_runs

    _clear_keep_runs(home)
    active_runs.update(adopt_live_runs(conn, home, projects, graceful_sec=grace0))
    n_orphans = requeue_orphaned_running(
        conn,
        reason="orphan",
        graceful_sec=grace0,
        keep_run_ids=frozenset(int(r.run_id) for r in active_runs.values()),
    )
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
    from tqdaemon.db import reclaim_expired_leases as _reclaim

    heartbeat0 = float(cfg0.get("heartbeat_sec", 30))
    stale_remote = _reclaim(conn, heartbeat_sec=heartbeat0)
    if stale_remote:
        print(f"[queue] {_ts()} startup: reclaimed {len(stale_remote)} stale remote lease(s)", flush=True)

    # F-241: scrub stale gpu.lock pids, then rebuild from adopted local GPU runs.
    try:
        from tqdaemon.gpu_lock import holders_from_active_runs, scrub_stale, sync_from_holders

        n_stale = scrub_stale(home)
        sync_from_holders(home, holders_from_active_runs(active_runs))
        if n_stale:
            print(f"[queue] {_ts()} startup: scrubbed {n_stale} stale gpu.lock holder(s)", flush=True)
    except Exception:  # noqa: BLE001 - lock file is advisory
        _log.debug("gpu.lock startup scrub failed", exc_info=True)

    # Initial sync
    for project in projects.values():
        _sync_project(conn, project, home=home)

    # I-07: file-watch triggered sync — supplement the 60s poll with instant YAML change detection
    import threading as _threading

    _sync_requested = _threading.Event()
    _hw_refresh_now = _threading.Event()
    _watchdog_observer = None
    _queue_watches: _QueueDirWatches | None = None
    try:
        from watchdog.events import FileSystemEventHandler as _WdHandler  # type: ignore
        from watchdog.observers import Observer as _WdObserver  # type: ignore

        class _YamlWatcher(_WdHandler):
            def on_any_event(self, ev):  # type: ignore
                if not ev.is_directory and str(getattr(ev, "src_path", "")).endswith((".yaml", ".yml")):
                    _sync_requested.set()
                    try:
                        from tqdaemon.sched_wake import wake_scheduler

                        wake_scheduler()
                    except Exception:  # noqa: BLE001 - scheduler wake is best-effort
                        _log.debug("scheduler wake failed", exc_info=True)

        _watchdog_observer = _WdObserver()
        _queue_watches = _QueueDirWatches(_watchdog_observer, _YamlWatcher())
        watched = _queue_watches.update(projects)
        _watchdog_observer.start()
        print(f"[queue] {_ts()} file-watch active for {watched} project queue dir(s)", flush=True)
    except ImportError:
        print(f"[queue] {_ts()} watchdog not installed — using 60s poll for YAML sync", flush=True)

    _host = _HostSleepState()
    if sys.platform == "win32":
        _threading.Thread(
            target=_power_listener_main,
            args=(home, active_runs, _host, _hw_refresh_now),
            name="tq-power-listener",
            daemon=True,
        ).start()

    def _release_project_cb(name: str) -> dict:
        """F-240: drop one project's queue-dir watch so the folder can be renamed."""
        if name not in projects:
            return {"ok": False, "error": f"project {name!r} not registered"}
        if _queue_watches is None:
            return {
                "ok": True,
                "project": name,
                "watched": False,
                "note": "no file watches active (poll-only mode)",
            }
        _queue_watches.release(name, projects)
        return {"ok": True, "project": name, "released": True, "watched": True}

    def _relocate_project_cb(name: str, new_root_s: str) -> dict:
        """F-240: release → rewrite root/yaml_source → re-watch → sync; compare states."""
        from tqdaemon.project_relocate import (
            count_running,
            relocate_project_files,
            status_histogram,
        )

        if name not in projects and name not in load_projects(home):
            return {"ok": False, "error": f"project {name!r} not registered"}
        n_run = count_running(conn, name)
        if n_run:
            return {"ok": False, "error": f"{n_run} task(s) running in {name}; stop them first"}
        if _queue_watches is not None:
            _queue_watches.release(name, projects)
        try:
            summary = relocate_project_files(home, conn, name, Path(new_root_s))
        except (KeyError, ValueError, FileNotFoundError, OSError) as exc:
            return {"ok": False, "error": str(exc)}
        summary.pop("project_obj", None)
        try:
            _replace_registry(projects, load_projects(home))
        except Exception as exc:  # noqa: BLE001 - keep previous registry on bad yaml
            return {"ok": False, "error": f"files rewritten but projects.yaml reload failed: {exc}", **summary}
        if _queue_watches is not None:
            _queue_watches.clear_release(name)
            _queue_watches.update(projects)
        proj = projects.get(name)
        sync_stats = {"upserted": 0, "deleted": 0}
        if proj is not None:
            sync_stats = _sync_project(conn, proj, home=home, force=True)
        after = status_histogram(conn, name)
        after_n = sum(after.values())
        before = summary.get("before") or {}
        matched = before.get("count") == after_n and before.get("status") == after
        summary["after"] = {"count": after_n, "status": after}
        summary["sync"] = sync_stats
        summary["state_match"] = matched
        if not matched:
            summary["ok"] = False
            summary["error"] = "task count/status mismatch after relocate sync"
        return summary

    cfg = get_config(conn)
    port = int(cfg.get("web_port", 8765))
    bind_host = str(cfg.get("bind_host") or "127.0.0.1")
    start_web_server(
        conn,
        active_runs,
        system_info,
        projects_ref=lambda: projects,
        db_path=tqconfig.db_path(home),
        port=port,
        bind_host=bind_host,
        release_project=_release_project_cb,
        relocate_project=_relocate_project_cb,
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
        from tqdaemon.notify import winotify_available

        if not winotify_available():
            n_toast = 0
            try:
                n_toast = int(
                    conn.execute(
                        "SELECT COUNT(*) AS n FROM system_alerts WHERE enabled = 1 AND windows_toast = 1"
                    ).fetchone()["n"]
                    or 0
                )
            except sqlite3.Error:
                pass
            if n_toast > 0:
                print(
                    f"[queue] {_ts()} WARN: {n_toast} system alert(s) want Windows popup "
                    "but winotify is missing — pip install winotify "
                    "(or: pip install 'tqdaemon[toast]')",
                    flush=True,
                )
    except Exception:  # noqa: BLE001 - optional-dependency check is advisory
        _log.debug("toast dependency check failed", exc_info=True)

    shutdown = False

    # F-074: renew master_lease on a dedicated thread so a blocked scheduler
    # tick (heavy sync / SQLite busy) cannot expire fencing and 503 workers.
    _lease.start_renewal(lambda: shutdown)

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
        conn=conn,
        home=home,
        active_runs=active_runs,
        system_info=system_info,
        my_instance=_my_instance,
        sync_requested=_sync_requested,
        hw_refresh_now=_hw_refresh_now,
        lease_lost=_lease.lost,
        is_shutdown=lambda: shutdown,
        host_sleeping=lambda: _host.sleeping,
        on_registry_change=_queue_watches.update if _queue_watches is not None else None,
    )
    st = TickState(projects)

    while not shutdown:
        try:
            if not daemon_tick(env, st):
                time.sleep(st.interval)
                continue
            idle_wait_and_fast_admit(env, st)
        except Exception as exc:  # noqa: BLE001 - top-level tick guard keeps the daemon alive
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

    from tqdaemon.power import set_prevent_sleep

    set_prevent_sleep(False)

    if _watchdog_observer is not None:
        try:
            _watchdog_observer.stop()
            _watchdog_observer.join(timeout=2)
        except Exception:  # noqa: BLE001 - third-party watchdog observer at shutdown
            _log.debug("file watcher stop failed", exc_info=True)

    # `tq restart` with adoptable (wrapper-started) runs: leave them running for the next daemon.
    from tqdaemon.adopt import consume_keep_runs

    if consume_keep_runs(home):
        _detach_adoptable_runs(active_runs)
    # Graceful shutdown: stop all running tasks
    print(f"[queue] {_ts()} shutdown: stopping running tasks", flush=True)
    try:
        _stop_and_finalize_all(active_runs)
        # F-241: drop holders whose pids we just killed; keep adoptable (still-live) ones.
        try:
            from tqdaemon.gpu_lock import scrub_stale

            scrub_stale(home)
        except Exception:  # noqa: BLE001 - lock file is advisory
            _log.debug("gpu.lock shutdown scrub failed", exc_info=True)
    finally:
        pid_path.unlink(missing_ok=True)
    try:
        clean_path.write_text(time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), encoding="utf-8")
    except OSError as exc:
        print(f"[queue] {_ts()} warn: could not write daemon.clean: {exc}", file=sys.stderr)
    try:
        if release_master_lease(conn, _my_instance):
            print(f"[queue] {_ts()} master lease released", flush=True)
    except sqlite3.Error as exc:
        print(f"[queue] {_ts()} warn: could not release master lease: {exc}", file=sys.stderr)
    try:
        insert_daemon_event(
            conn,
            DAEMON_EVENT_CLEAN_SHUTDOWN,
            pid=os.getpid(),
            instance_id=_my_instance,
            epoch=_lease_epoch,
        )
    except sqlite3.Error as exc:
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

    print(f"[standby] {_ts()} starting standby on {bind_host}:{port}", flush=True)

    _promote_event = _threading.Event()

    from tqdaemon.web import start_standby_server

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
        print(f"[standby] {_ts()} promote received — transitioning to master", flush=True)
        return True
    print(f"[standby] {_ts()} standby exited", flush=True)
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

    parser = argparse.ArgumentParser(description="tqdaemon daemon")
    parser.add_argument("--home", default=None, help="State home (default: ~/.tqdaemon or $TQDAEMON_HOME)")
    parser.add_argument("--claim-master", action="store_true", help="Force-claim the master lease; requires --force")
    parser.add_argument(
        "--force", action="store_true", help="Required with --claim-master to prevent accidental takeover"
    )
    parser.add_argument(
        "--standby",
        action="store_true",
        help="W7.3: run in standby mode (no scheduling, no lease, serves /api/status + /api/import)",
    )
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
