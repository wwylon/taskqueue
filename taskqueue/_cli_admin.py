"""Daemon, config, and reporting CLI commands — extracted from cli.py."""
from __future__ import annotations
from taskqueue._time import parse_iso as _parse_iso
import json
import os
import subprocess
import time
import sys
from pathlib import Path
from taskqueue._db_tasks import get_task_status
from taskqueue._cli_helpers import (
    emit_json,
    _db,
    _daemon_running,
    _ui_url,
    _ensure_python,
    _split_key,
    _check_ownership_or_print,
)
from taskqueue._fmt import fmt_duration as _fmt_dur, fmt_bytes as _fmt_bytes  # H1: shared helpers
from taskqueue.rows import row_get

DAEMON_LOG_ROTATE_BYTES = 20 * 1024 * 1024


def cmd_start(home: Path) -> int:
    pid = _daemon_running(home)
    if pid:
        print(f"Daemon already running (pid {pid}). Web UI: {_ui_url(home)}")
        return 0

    home.mkdir(parents=True, exist_ok=True)
    log_path = home / "daemon.log"
    # daemon.log is the daemon's stdout: rotate it at start so it cannot grow forever.
    try:
        if log_path.is_file() and log_path.stat().st_size > DAEMON_LOG_ROTATE_BYTES:
            log_path.replace(log_path.with_name("daemon.log.1"))
    except OSError:
        pass
    python = _ensure_python()
    cmd = [str(python), "-m", "taskqueue.daemon", "--home", str(home)]

    if os.name == "nt":
        DETACHED_PROCESS = 0x00000008
        CREATE_NEW_PROCESS_GROUP = 0x00000200
        CREATE_NO_WINDOW = 0x08000000
        popen_kw: dict = {"creationflags": DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW}
    else:
        popen_kw = {"start_new_session": True}
    # The child keeps its own copy of the handle; the parent's copy is closed right away.
    with open(log_path, "a", encoding="utf-8", errors="replace") as log_fh:
        proc = subprocess.Popen(cmd, stdout=log_fh, stderr=subprocess.STDOUT, close_fds=True, **popen_kw)

    import time
    from taskqueue.master_client import api_request

    ready = False
    for _ in range(30):
        if proc.poll() is not None:
            print(f"Daemon failed to start (exit {proc.returncode}). Check: {log_path}")
            return 1
        if api_request(home, "GET", "/api/status", timeout=0.5) is not None:
            ready = True
            break
        time.sleep(0.5)
    if not ready and proc.poll() is not None:
        print(f"Daemon failed to start (exit {proc.returncode}). Check: {log_path}")
        return 1
    if not ready:
        print(f"Daemon started (pid {proc.pid}) but /api/status not ready yet.")
    else:
        print(f"Daemon started (pid {proc.pid})")
    print(f"Web UI: {_ui_url(home)}")
    print(f"Log:    {log_path}")
    return 0



def _running_runs(home: Path) -> list[str]:
    """``project/task`` of runs the daemon would stop on shutdown (read-only; [] if unreadable)."""
    import sqlite3
    db_path = _db(home)
    if not Path(db_path).exists():
        return []
    try:
        conn = sqlite3.connect(f"file:{Path(db_path).as_posix()}?mode=ro", uri=True, timeout=2)
        try:
            rows = conn.execute(
                "SELECT project, task_id FROM runs WHERE status='running' ORDER BY run_id"
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        return []
    return [f"{r[0]}/{r[1]}" for r in rows]


def _refuse_if_running(home: Path, verb: str) -> bool:
    """Print a refusal and return True when ``verb`` would kill running tasks."""
    running = _running_runs(home)
    if not running:
        return False
    shown = ", ".join(running[:8]) + (f", +{len(running) - 8} more" if len(running) > 8 else "")
    print(
        f"Refusing to {verb}: {len(running)} task(s) running and would be stopped and re-queued "
        f"from scratch ({shown}). Use --force to proceed anyway."
    )
    return True


def cmd_stop_daemon(home: Path, *, force: bool = False) -> int:
    pid = _daemon_running(home)
    if not pid:
        print("Daemon not running.")
        return 0
    if not force and _refuse_if_running(home, "stop the daemon"):
        return 1
    import signal
    import sys
    if sys.platform == "win32":
        # SIGTERM on Windows is an abrupt kill; use CTRL_BREAK_EVENT so the
        # daemon's signal handler runs and clean-shutdown fires.
        import ctypes
        CTRL_BREAK_EVENT = 1
        try:
            ok = ctypes.windll.kernel32.GenerateConsoleCtrlEvent(CTRL_BREAK_EVENT, pid)
            if ok:
                print(f"Sent CTRL_BREAK to daemon (pid {pid}). It will stop after current tasks checkpoint.")
                return 0
        except Exception:
            pass
        # Fall back to SIGTERM if GenerateConsoleCtrlEvent is unavailable.
    try:
        os.kill(pid, signal.SIGTERM)
        print(f"Sent SIGTERM to daemon (pid {pid}). It will stop after current tasks checkpoint.")
        return 0
    except OSError as e:
        print(f"Failed to signal daemon: {e}")
        return 1


def cmd_restart(home: Path, *, timeout_sec: float = 180.0, force: bool = False) -> int:
    """Stop the daemon (if running), wait for exit, then start again.

    Prefer this over ``stop-daemon`` + ``start``: start refuses while the old
    PID is still shutting down, and a dirty kill left the master lease held for
    up to ``MASTER_LEASE_TTL_SEC`` (web UI down until reclaim).
    """
    import time

    pid = _daemon_running(home)
    if pid:
        if cmd_stop_daemon(home, force=force) != 0:
            return 1
        deadline = time.monotonic() + max(5.0, timeout_sec)
        last_msg = 0.0
        while time.monotonic() < deadline:
            if _daemon_running(home) is None:
                break
            now = time.monotonic()
            if now - last_msg >= 5.0:
                rem = int(deadline - now)
                print(f"Waiting for daemon pid {pid} to exit... ({rem}s left)")
                last_msg = now
            time.sleep(0.5)
        else:
            print(
                f"Daemon pid {pid} still running after {timeout_sec:.0f}s; "
                "not starting a second master. Retry `tq restart` or stop it manually."
            )
            return 1
    else:
        print("Daemon was not running.")
    return cmd_start(home) or 0


def _run_progress_health(r) -> dict:
    """age of the last % change, stalled flag and recent-rate ETA (from runs.metrics_json._progress)."""
    import json as _json

    from taskqueue.log_issues import progress_health

    try:
        timing = (_json.loads(r["metrics_json"] or "{}") or {}).get("_progress")
    except (ValueError, TypeError, KeyError, IndexError):
        return {}
    return progress_health(timing, r["progress_pct"], time.time())


def cmd_status(home: Path, project: str | None, *, as_json: bool = False) -> int:
    from taskqueue.db import open_db

    pid = _daemon_running(home)
    if not as_json:
        print(f"Daemon: {'running (pid ' + str(pid) + ')' if pid else 'STOPPED'}")
        if pid:
            print(f"Web UI: {_ui_url(home)}")
        if project:
            print(f"Filter: project={project}")

    db = _db(home)
    if not db.exists():
        print("No queue DB yet. Run `tq start`.")
        return 1

    conn = open_db(db, migrate=False)

    where = "AND t.project=?" if project else ""
    where_t = "AND project=?" if project else ""
    params = (project,) if project else ()

    # I-09: include started_at and progress_pct from the latest run
    running = conn.execute(
        f"SELECT t.project,t.task_id,t.priority,t.last_error_code,"
        f" r.started_at, r.progress_pct, r.metrics_json"
        f" FROM tasks t"
        f" LEFT JOIN runs r ON r.project=t.project AND r.task_id=t.task_id"
        f"   AND r.run_id=(SELECT MAX(run_id) FROM runs WHERE project=t.project AND task_id=t.task_id)"
        f" WHERE t.status='running' {where}",
        params,
    ).fetchall()
    queued  = conn.execute(f"SELECT project,task_id,priority,last_error_code,block_reason FROM tasks WHERE status='queued' {where_t} ORDER BY priority DESC, enqueued_at ASC", params).fetchall()
    # Last run's progress rides along so `tq status` shows how far failed / stopped runs got.
    _ended = (
        "SELECT t.project, t.task_id, t.status, t.last_error_code, t.block_reason, r.progress_pct "
        "FROM tasks t LEFT JOIN runs r ON r.run_id = t.last_run_id WHERE t.status = ? "
        + ("AND t.project = ?" if project else "")
    )
    failed  = conn.execute(_ended, ("failed", *params)).fetchall()
    interrupted = conn.execute(_ended, ("interrupted", *params)).fetchall()
    stopped = conn.execute(_ended, ("stopped", *params)).fetchall()

    from taskqueue._fmt import shown_progress

    def _key(r):
        base = f"{r['project']}/{r['task_id']}"
        code = row_get(r, "last_error_code")
        reason = ""
        if "block_reason" in r.keys() and r["block_reason"]:
            reason = f" — {r['block_reason']}"
        pct = shown_progress(row_get(r, "status"), row_get(r, "progress_pct")) if "status" in r.keys() else None
        if pct is not None:
            base += f" {pct:.0f}%"
        if code:
            return f"{base} [{code}{reason}]"
        if reason:
            return f"{base} [{reason.lstrip(' —')}]"
        return base

    def _fmt_list(items, max_show=5):
        if not items:
            return "none"
        names = [_key(r) for r in items[:max_show]]
        if len(items) > max_show:
            names.append(f"... (+{len(items) - max_show} more)")
        return ", ".join(names)

    # I-09: print running tasks with elapsed time and ETA
    from datetime import datetime, timezone

    from taskqueue._fmt import color_status
    if running:
        print(f"\nRunning ({len(running)}):")
        for r in running:
            base = f"{r['project']}/{r['task_id']}"
            elapsed_str = ""
            eta_str = ""
            bar_str = ""
            if r["started_at"]:
                try:
                    iso = r["started_at"].replace("Z", "+00:00")
                    started = datetime.fromisoformat(iso)
                    now_utc = datetime.now(timezone.utc)
                    elapsed_sec = (now_utc - started).total_seconds()
                    h = int(elapsed_sec // 3600)
                    m = int((elapsed_sec % 3600) // 60)
                    elapsed_str = f"  elapsed {h}h{m:02d}m" if h else f"  elapsed {m}m"
                    pct = r["progress_pct"]
                    health = _run_progress_health(r)
                    if pct and pct > 0:
                        filled = int(pct / 10)
                        bar = "█" * filled + "░" * (10 - filled)
                        bar_str = f"  [{bar}] {pct:.0f}%"
                        # Recent rate when known (a run that slowed down shows a realistic ETA).
                        eta_sec = health.get("eta_sec", elapsed_sec / pct * (100 - pct))
                        eh = int(eta_sec // 3600)
                        em = int((eta_sec % 3600) // 60)
                        eta_str = f"  ETA ~{eh}h{em:02d}m" if eh else f"  ETA ~{em}m"
                    if "age_sec" in health:
                        eta_str += f"  last progress {_fmt_dur(health['age_sec'])} ago"
                        if health.get("stalled"):
                            eta_str += "  " + color_status("POSSIBLY STALLED", "failed")
                except Exception:
                    pass
            print(f"  {color_status('running', 'running')}  {base}{bar_str}{elapsed_str}{eta_str}")
    else:
        print("\nRunning (0): none")
    print(f"Queued  ({len(queued)}): ", _fmt_list(queued))
    if failed:
        print(color_status(f"Failed  ({len(failed)}): ", "failed"), _fmt_list(failed))
    if interrupted:
        print(color_status(f"Interrupted ({len(interrupted)}): ", "interrupted"), _fmt_list(interrupted))
    if stopped:
        print(color_status(f"Stopped ({len(stopped)}): ", "stopped"), _fmt_list(stopped))

    where_runs = "WHERE project=?" if project else ""
    params_runs = (project,) if project else ()
    recent = conn.execute(
        f"SELECT project,task_id,status,started_at,finished_at,exit_code,worker_id,last_error_code FROM runs "
        f"{where_runs} "
        f"ORDER BY run_id DESC LIMIT 5",
        params_runs
    ).fetchall()

    if as_json:
        out = {
            "daemon": {"pid": pid, "running": pid is not None},
            "running": [
                {**{k: r[k] for k in r.keys() if k != "metrics_json"}, "progress_health": _run_progress_health(r)}
                for r in running
            ],
            "queued": [dict(r) for r in queued],
            "failed": [dict(r) for r in failed],
            "interrupted": [dict(r) for r in interrupted],
            "stopped": [dict(r) for r in stopped],
            "recent_runs": [dict(r) for r in recent],
        }
        emit_json(out, ensure_ascii=False)
        conn.close()
        return 0

    if recent:
        print("\nRecent runs:")
        for r in recent:
            dur = ""
            ts_str = "—"
            from datetime import datetime
            if r["started_at"]:
                try:
                    iso_str = r["started_at"].replace("Z", "+00:00")
                    dt = datetime.fromisoformat(iso_str)
                    dt_local = dt.astimezone()
                    ts_str = dt_local.strftime("%Y-%m-%d %H:%M:%S")
                except Exception:
                    ts_str = r["started_at"][:19]
            if r["started_at"] and r["finished_at"]:
                try:
                    s = _parse_iso(r["started_at"])
                    f = _parse_iso(r["finished_at"])
                    sec = int((f - s).total_seconds())
                    dur = f" ({sec//60}m{sec%60:02d}s)"
                except Exception:
                    pass
            code = f" exit={r['exit_code']}" if r["exit_code"] is not None else ""
            worker_str = f" @{r['worker_id']}" if r["worker_id"] else ""
            err = row_get(r, "last_error_code")
            err_str = f" [{err}]" if err else ""
            print(f"  {ts_str}  {r['project']}/{r['task_id']:30s}  {color_status(f"{r['status']:12s}", r['status'])}{dur}{code}{worker_str}{err_str}")
    conn.close()
    return 0



def cmd_migrate(home: Path, rest: list[str]) -> int:
    """One-shot migrations. Currently: priority-invert."""
    if not rest or rest[0] in ("-h", "--help"):
        print("Usage: tq migrate priority-invert [--dry-run]")
        return 0 if rest and rest[0] in ("-h", "--help") else 1
    kind = rest[0]
    dry_run = "--dry-run" in rest[1:]
    if kind != "priority-invert":
        print(f"Unknown migrate target '{kind}'. Supported: priority-invert", file=sys.stderr)
        return 1
    from taskqueue.priority_migrate import migrate_priority_invert

    summary = migrate_priority_invert(home, dry_run=dry_run)
    if not summary.get("ok"):
        print(f"migrate priority-invert: {summary.get('error')}", file=sys.stderr)
        return 1
    mode = "dry-run" if dry_run else "applied"
    print(
        f"migrate priority-invert ({mode}): "
        f"db={summary['db_updated']} yaml={summary['yaml_updated']} "
        f"marker={summary['marker']}"
    )
    for project, task_id, old, new in summary.get("db_samples") or []:
        print(f"  db  {project}/{task_id}: {old} -> {new}")
    for pname, path_str, old, new in summary.get("yaml_samples") or []:
        print(f"  yaml {pname}: {path_str}: {old} -> {new}")
    if dry_run:
        print("(no changes written; re-run without --dry-run to apply)")
    else:
        print("Restart the daemon if it is running so listings match the new order.")
    return 0



def cmd_priority(home: Path, key: str, priority: int, *, force: bool = False) -> int:
    from taskqueue._cli_helpers import EXIT_UNREACHABLE, master_mutation

    project, task_id = _split_key(key)
    resp, mode = master_mutation(home, f"/api/priority/{project}/{task_id}/{int(priority)}", force=force)
    if mode == "unreachable":
        return EXIT_UNREACHABLE
    if mode == "master":
        if not resp.get("ok"):
            print(f"Error: {resp.get('error', 'failed')}", file=sys.stderr)
            return 1
        print(f"Priority of {key} set to {priority}.")
        return 0
    from taskqueue.db import get_task, open_db, set_task_priority
    db = open_db(_db(home))
    row = get_task(db, project, task_id)
    if row is None:
        db.close()
        print(f"Unknown task '{key}'.", file=sys.stderr)
        return 1
    if _check_ownership_or_print(row, key, force=force):
        db.close()
        return 1
    set_task_priority(db, project, task_id, priority)
    db.close()
    print(f"Priority of {key} set to {priority}.")
    return 0


def cmd_config_get(home: Path) -> None:
    from taskqueue.db import get_config, open_db

    home.mkdir(parents=True, exist_ok=True)
    conn = open_db(_db(home))
    try:
        cfg = get_config(conn)
        for k, v in sorted(cfg.items()):
            if k == "auth_token" and v:
                print(f"  {k} = ***")
            else:
                print(f"  {k} = {v}")
    finally:
        conn.close()



def cmd_config_set(home: Path, pairs: list[str]) -> int:
    import re

    from taskqueue.config import VALID_CONFIG_KEYS, validate_config
    from taskqueue.db import open_db, set_config

    if pairs and all(
        "=" in a and not a.startswith("=") and not a.endswith("=") for a in pairs
    ):
        # Each shell argument is already one k=v pair; keep spaces in values.
        clean_pairs = list(pairs)
    else:
        combined = " ".join(pairs)
        normalized = re.sub(r"\s*=\s*", "=", combined)
        clean_pairs = normalized.split()

    if not clean_pairs:
        print("Usage: tq config set k=v [k=v ...]")
        return 1

    # Validate every pair before writing any, so one bad pair does not leave a partial update.
    for pair in clean_pairs:
        if "=" not in pair:
            print(f"Error: Invalid configuration format: '{pair}'. Expected key=value")
            return 1
        _k, _, _v = pair.partition("=")
        err = validate_config(_k.strip(), _v.strip())
        if err:
            print(f"Error: {err}")
            return 1

    home.mkdir(parents=True, exist_ok=True)
    conn = open_db(_db(home))
    try:
        for pair in clean_pairs:
            if "=" not in pair:
                print(f"Error: Invalid configuration format: '{pair}'. Expected key=value")
                return 1
            k, _, v = pair.partition("=")
            k = k.strip()
            v = v.strip()

            err = validate_config(k, v)
            if err:
                print(f"Error: {err}")
                return 1

            expected_type = VALID_CONFIG_KEYS[k][0]
            parsed = expected_type(v)

            set_config(conn, k, parsed)
            print(f"  {k} = {'***' if k == 'auth_token' else parsed}")
            if k == "admit_candidate_window" and parsed == 0:
                print(
                    "  warn: admit_candidate_window=0 loads all queued tasks every scheduler tick. "
                    "Use a positive value (e.g. 256) for large queues."
                )
    finally:
        conn.close()
    return 0



def cmd_detect() -> None:
    from taskqueue.resources import detect_system
    info = detect_system()
    emit_json(info)



def cmd_my_tasks(home: Path, operator: str | None, project: str | None) -> int:
    from taskqueue.db import open_db
    from taskqueue.queries import list_operator_task_summaries

    op = operator or os.environ.get("TQ_OPERATOR")
    if not op:
        print("Usage: tq my-tasks --operator NAME  (or set TQ_OPERATOR)")
        return 1
    db = _db(home)
    if not db.exists():
        print("No queue DB.")
        return 1
    conn = open_db(db, migrate=False)
    rows = list_operator_task_summaries(conn, operator=op, project=project)
    conn.close()
    if not rows:
        print(f"No tasks for operator={op!r}.")
        return 0
    print(f"{'STATUS':12s}  {'PROGRESS':8s}  TASK")
    print("-" * 60)
    for r in rows:
        pct = f"{r['progress_pct']:.0f}%" if r["progress_pct"] is not None else "-"
        err = f"  [{r['last_error_code']}]" if r["last_error_code"] else ""
        w = f" @{r['worker_id']}" if r["worker_id"] else ""
        print(f"{r['status']:12s}  {pct:8s}  {r['project']}/{r['task_id']}{w}{err}")
    return 0



def cmd_report(home: Path, project: str | None, days: int) -> None:
    from taskqueue.db import open_db
    from taskqueue.reports import build_report

    db = _db(home)
    if not db.exists():
        print("No queue DB.")
        return
    conn = open_db(db, migrate=False)
    try:
        rep = build_report(conn, project=project, days=days)
        print(f"report days={rep['days']} since={rep['since']} project={rep['project'] or '*'}")
        print(f"runs_by_status: {rep['runs_by_status']}")
        print(f"tasks_by_status: {rep['tasks_by_status']}")
        print(f"avg_completed_sec: {rep['avg_completed_sec']}")
        if rep["error_codes"]:
            print("error_codes:")
            for code, n in rep["error_codes"].items():
                print(f"  {code}: {n}")
        if rep["recent_failures"]:
            print("recent_failures:")
            for r in rep["recent_failures"][:15]:
                print(f"  {r['status']:12s}  {r['project']}/{r['task_id']}  exit={r['exit_code']}")
        # P9 / F-069: estimate accuracy — must run before conn.close().
        try:
            acc_rows = conn.execute(
                """
                SELECT estimated_ram_gib, peak_ram_gib,
                       estimated_vram_gib, peak_vram_gib
                FROM runs
                WHERE status = 'completed'
                  AND estimated_ram_gib IS NOT NULL AND estimated_ram_gib > 0
                  AND peak_ram_gib IS NOT NULL AND peak_ram_gib > 0
                  AND started_at >= datetime('now', ?)
                ORDER BY run_id DESC
                LIMIT 200
                """,
                (f"-{rep['days']} days",),
            ).fetchall()
            if acc_rows:
                ratios = [r["peak_ram_gib"] / r["estimated_ram_gib"] for r in acc_rows]
                ratios.sort()
                p50 = ratios[len(ratios) // 2]
                p95_idx = min(int(0.95 * len(ratios)), len(ratios) - 1)
                p95 = ratios[p95_idx]
                over = sum(1 for x in ratios if x > 2.0)
                under = sum(1 for x in ratios if x < 0.5)
                print(
                    f"estimate_accuracy (RAM, n={len(ratios)}): "
                    f"median ratio={p50:.2f}x  p95={p95:.2f}x  "
                    f"over2x={over}  under0.5x={under}"
                )
                vram_rows = [r for r in acc_rows if r["estimated_vram_gib"] and r["peak_vram_gib"]]
                if vram_rows:
                    vr = sorted(r["peak_vram_gib"] / r["estimated_vram_gib"] for r in vram_rows)
                    vp50 = vr[len(vr) // 2]
                    print(f"estimate_accuracy (VRAM, n={len(vr)}): median ratio={vp50:.2f}x")
            else:
                print("estimate_accuracy: no completed runs with estimate data yet")
        except Exception as exc:
            print(f"estimate_accuracy: unavailable ({exc})")
    finally:
        conn.close()



def cmd_health(home: Path, project: str | None, *, as_json: bool = False) -> int:
    """Report task health: missing deps, attention items, disabled tasks."""
    from taskqueue.db import open_db
    from taskqueue.queries import build_task_health

    db = _db(home)
    if not db.exists():
        print("No queue DB.")
        return 1
    conn = open_db(db, migrate=False)
    report = build_task_health(conn, project=project)
    conn.close()
    if as_json:
        emit_json(report, indent=None)
    else:
        ok = report["ok"]
        print(f"Health: {'OK' if ok else 'ISSUES FOUND'}")
        if report["missing_deps"]:
            print(f"  Missing deps ({len(report['missing_deps'])}):")
            for b in report["missing_deps"]:
                print(f"    {b['project']}/{b['task_id']}: missing {b['missing']}")
        if report["attention"]:
            print(f"  Needs attention ({len(report['attention'])}):")
            for a in report["attention"]:
                print(f"    {a['project']}/{a['task_id']} [{a['status']}] {a.get('last_error_code') or ''}")
        if report["disabled"]:
            print(f"  Disabled ({len(report['disabled'])}): {[d['task_id'] for d in report['disabled'][:5]]}")
    return 0 if report["ok"] else 1


def cmd_attention(home: Path, project: str | None, *, as_json: bool = False) -> int:
    """List tasks needing operator attention (errors, failed, interrupted)."""
    from taskqueue.db import open_db
    from taskqueue.queries import list_attention

    db = _db(home)
    if not db.exists():
        print("No queue DB.")
        return 1
    conn = open_db(db, migrate=False)
    items = list_attention(conn, project=project)
    conn.close()
    result = {"count": len(items), "items": items}
    if as_json:
        emit_json(result, indent=None)
    else:
        if not items:
            print("No tasks need attention.")
        for a in items:
            print(f"  {a['project']}/{a['task_id']} [{a['status']}] {a.get('last_error_code') or ''}")
    return 0 if not items else 1


_SKIP_SCAN_DIRS = frozenset({
    ".git", ".hg", ".venv", "venv", "env", "node_modules", "__pycache__",
    "site-packages", "build", "dist", ".tox", ".mypy_cache", ".pytest_cache",
})


def _iter_project_sources(root: Path, *, max_py: int = 500, max_cpp: int = 200):
    """Yield up to max_py *.py and max_cpp *.cpp files, pruning vendored/venv trees."""
    import os

    n_py = n_cpp = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_SCAN_DIRS]
        for fn in filenames:
            if fn.endswith(".py") and n_py < max_py:
                n_py += 1
                yield Path(dirpath) / fn
            elif fn.endswith(".cpp") and n_cpp < max_cpp:
                n_cpp += 1
                yield Path(dirpath) / fn
        if n_py >= max_py and n_cpp >= max_cpp:
            return


def cmd_audit_projects(home: Path) -> int:
    """W8.1: Scan registered project roots for client-library adoption metrics.

    Reports per-project:
    - tq_client / report_progress usage
    - notify_url presence
    - resource hints (ram_gib / gpu_vram_gib)
    - failure rate (last 90 days)
    """
    import re

    from taskqueue.config import load_projects
    from taskqueue.db import open_db

    projects = load_projects(home)
    db = _db(home)
    conn = open_db(db) if db.exists() else None

    TQ_CLIENT_RE = re.compile(r'tq_client|report_progress', re.IGNORECASE)
    NOTIFY_RE = re.compile(r'notify_url\s*:', re.IGNORECASE)
    RAM_RE = re.compile(r'ram_gib\s*:', re.IGNORECASE)
    GPU_RE = re.compile(r'gpu_vram_gib\s*:', re.IGNORECASE)

    results = []
    for proj_name, proj in projects.items():
        queue_dir = getattr(proj, "queue_dir", None) or (Path(proj.root) / "experiments" / "operator_queue")
        yamls = list(queue_dir.glob("*.yaml")) + list(queue_dir.glob("*.yml")) if queue_dir.is_dir() else []
        n_yamls = len(yamls)

        uses_client = 0
        uses_notify = 0
        uses_ram = 0
        uses_gpu = 0
        for yaml_path in yamls:
            try:
                text = yaml_path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if TQ_CLIENT_RE.search(text):
                uses_client += 1
            if NOTIFY_RE.search(text):
                uses_notify += 1
            if RAM_RE.search(text):
                uses_ram += 1
            if GPU_RE.search(text):
                uses_gpu += 1

        # Also scan project root Python/C++ sources for tq_client import
        src_root = Path(proj.root)
        src_uses_client = any(
            TQ_CLIENT_RE.search(f.read_text(encoding="utf-8", errors="replace"))
            for f in _iter_project_sources(src_root)
        )

        fail_rate: str = "n/a"
        if conn:
            try:
                row = conn.execute(
                    """SELECT
                        COUNT(*) as total,
                        SUM(CASE WHEN status IN ('failed','interrupted','stopped') THEN 1 ELSE 0 END) as failed
                       FROM runs
                       WHERE project=? AND started_at > datetime('now','-90 days')""",
                    (proj_name,),
                ).fetchone()
                if row and row["total"]:
                    fail_rate = f"{100 * row['failed'] / row['total']:.0f}% ({row['failed']}/{row['total']})"
                else:
                    fail_rate = "0 runs"
            except Exception:
                pass

        results.append({
            "project": proj_name,
            "yamls": n_yamls,
            "client_in_yaml": uses_client,
            "client_in_src": src_uses_client,
            "notify_url": uses_notify,
            "ram_hint": uses_ram,
            "gpu_hint": uses_gpu,
            "fail_rate_90d": fail_rate,
        })

    if conn:
        conn.close()

    # Print table
    hdr = f"{'Project':<20} {'YAMLs':>6} {'CLI%':>5} {'Src':>4} {'Ntf':>4} {'RAM':>4} {'GPU':>4}  {'FailRate90d'}"
    print(hdr)
    print("-" * len(hdr))
    for r in results:
        cli_pct = f"{100*r['client_in_yaml']//r['yamls']}%" if r["yamls"] else "—"
        print(
            f"{r['project']:<20} {r['yamls']:>6} {cli_pct:>5} {'Y' if r['client_in_src'] else 'N':>4}"
            f" {r['notify_url']:>4} {r['ram_hint']:>4} {r['gpu_hint']:>4}  {r['fail_rate_90d']}"
        )
    return 0



def cmd_mcp(home: Path) -> int:
    from taskqueue.mcp_server import run_stdio
    return run_stdio(home)



def cmd_export(home: Path, dest: str | None, *, with_logs: bool) -> int:
    from datetime import datetime, timezone

    from taskqueue.export_import import export_home

    if not dest:
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        dest = str(home / "exports" / f"taskqueue-{ts}.tar.gz")
    try:
        path = export_home(home, Path(dest), with_logs=with_logs)
    except RuntimeError as exc:
        print(f"Error: {exc}")
        return 1
    print(f"Exported -> {path}")
    return 0



def cmd_import(home: Path, archive: str, *, force: bool) -> int:
    from taskqueue.export_import import import_home

    if _daemon_running(home) and not force:
        print("Daemon is running; stop it first (or pass --force).")
        return 1
    archive_path = Path(archive)
    if archive_path.is_file():
        size = archive_path.stat().st_size
        _MAX_IMPORT = 512 * 1024 * 1024
        if size > _MAX_IMPORT:
            print(f"Archive too large ({size} bytes; max {_MAX_IMPORT}).")
            return 1
    info = import_home(home, archive_path, force=force)
    print(f"Imported into {info['home']}")
    print(f"  db: {info['db']}")
    if info.get("manifest"):
        print(f"  manifest files: {info['manifest'].get('files')}")
    return 0



def cmd_token_generate(home: Path) -> None:
    import secrets

    from taskqueue.db import open_db, set_config

    token = secrets.token_urlsafe(32)
    home.mkdir(parents=True, exist_ok=True)
    conn = open_db(_db(home))
    try:
        set_config(conn, "auth_token", token)
    finally:
        conn.close()
    print(token)
    print("(stored as auth_token; restart daemon; workers: TQ_AUTH_TOKEN or --token)")

def _doctor_version(
    home: Path,
    *,
    env_home: str | None,
    pid: int | None,
    daemon_log: Path,
    port: int,
    bind_host: str,
    auth_on: bool,
    schema_from_db: int | None,
) -> None:
    import time
    from datetime import datetime

    from taskqueue import __version__
    from taskqueue.config import PROTOCOL_VERSION
    from taskqueue.db import SCHEMA_VERSION

    print(f"version:           {__version__}")
    print(f"home:              {home}")
    home_env_display = env_home if env_home else "(unset - using default)"
    print(f"TASKQUEUE_HOME:    {home_env_display}")
    if pid:
        # Probe HTTP even when PID looks alive (stale pid / dead port).
        http_ok = False
        try:
            from taskqueue.master_client import api_request

            probe = api_request(home, "GET", "/api/status", timeout=2.0)
            http_ok = probe is not None
        except Exception:
            http_ok = False
        if http_ok:
            print(f"daemon:            running (pid {pid}), http: ok")
        else:
            print(f"daemon:            UNREACHABLE (pid {pid} live but http failed)")
        try:
            import psutil

            proc = psutil.Process(pid)
            started = datetime.fromtimestamp(proc.create_time())
            uptime = time.time() - proc.create_time()
            rss = proc.memory_info().rss
            print(f"started_at:        {started.strftime('%Y-%m-%d %H:%M:%S')} (uptime {_fmt_dur(uptime)})")
            print(f"daemon_rss:        {_fmt_bytes(rss)}")
        except Exception as e:
            print(f"started_at:        (unavailable: {e})")
    else:
        print("daemon:            not running")
    print(f"daemon_log:        {daemon_log}" + (f" ({_fmt_bytes(daemon_log.stat().st_size)})" if daemon_log.exists() else " (missing)"))
    print(f"web_port:          {port}")
    print(f"bind_host:         {bind_host}")
    print(f"auth_token:        {'set' if auth_on else 'off'}")
    print(f"ui:                http://{bind_host}:{port}")
    print(f"protocol:          {PROTOCOL_VERSION}")
    print(
        f"schema_version:    "
        f"{schema_from_db if schema_from_db is not None else SCHEMA_VERSION} (code={SCHEMA_VERSION})"
    )


def _doctor_db(
    home: Path,
    *,
    db: Path,
    lease_holder: str | None,
    lease_expires: int | None,
    projects: dict,
    task_counts: dict[str, int],
    workers_online: int,
    workers_total: int,
) -> None:
    import time

    from taskqueue.config import registry_path
    from taskqueue.db import open_db

    print(f"db:                {db} ({'exists' if db.exists() else 'missing'})")
    if db.exists():
        try:
            sz = db.stat().st_size
            print(f"db_size:           {_fmt_bytes(sz)} ({sz} bytes)")
        except OSError:
            pass
    if lease_holder is not None:
        now = int(time.time())
        rem = (lease_expires or 0) - now
        print(
            f"master_lease:      {lease_holder}"
            + (f" (expires in {_fmt_dur(rem)})" if rem > 0 else " (expired)")
        )
    # Daemon lifecycle (schema 15)
    try:
        from taskqueue.db import last_boot_kind, list_daemon_events

        if db.exists():
            _dconn = open_db(db)
            try:
                boot = last_boot_kind(_dconn)
                print(f"last_boot:         {boot}")
                events = list_daemon_events(_dconn, limit=8)
                if events:
                    print("daemon_events:")
                    for ev in events:
                        detail = ev.get("detail_json") or ""
                        if detail and len(detail) > 80:
                            detail = detail[:77] + "..."
                        extra = f" {detail}" if detail else ""
                        print(f"  {ev['ts']}  {ev['kind']}{extra}")
            finally:
                _dconn.close()
    except Exception as e:
        print(f"daemon_events:     (unavailable: {e})")
    print(f"projects.yaml:     {registry_path(home)} ({'exists' if registry_path(home).exists() else 'missing'})")
    print(
        f"projects:          {len(projects)}"
        + (f" ({', '.join(sorted(projects))})" if projects else "")
    )
    if task_counts:
        order = ("queued", "running", "failed", "unknown", "stopped", "suspended", "interrupted", "cancelled", "completed", "disabled")
        parts = [f"{k}={task_counts[k]}" for k in order if k in task_counts]
        extras = [f"{k}={n}" for k, n in sorted(task_counts.items()) if k not in order]
        print(f"tasks:             {', '.join(parts + extras)}")
    print(f"workers:           {workers_online} online / {workers_total} registered")
    try:
        import psutil

        vm = psutil.virtual_memory()
        print(
            f"host_ram:          {_fmt_bytes(vm.used)} used / {_fmt_bytes(vm.total)} "
            f"({vm.percent:.0f}%)"
        )
        print(f"host_cpu_pct:      {psutil.cpu_percent(interval=0.1):.0f}% ({psutil.cpu_count(logical=True) or 1} cores)")
    except Exception:
        pass


def _doctor_gpu(check_torch: bool = False) -> None:
    try:
        from taskqueue.resources import cuda_health
        gh = cuda_health(check_torch=check_torch)
        if gh["nvidia_smi_ok"]:
            for g in gh["gpus"]:
                util = f" util={g['util_pct']:.0f}%" if "util_pct" in g else ""
                temp = f" {g['temp_c']:.0f}°C" if "temp_c" in g else ""
                driver = f" drv={g['driver']}" if "driver" in g else ""
                print(
                    f"gpu[{g['index']}]:           {g['name']}{driver}{temp}"
                    f" | VRAM {g['vram_used_gib']:.1f}/{g['vram_total_gib']:.1f} GiB"
                    f" ({g['vram_free_gib']:.1f} free){util}"
                )
            if gh["torch_cuda_available"] is True:
                dev = gh.get("torch_device_name") or ""
                print(f"torch_cuda:        available ({dev})")
            elif gh["torch_cuda_available"] is False:
                print("torch_cuda:        NOT AVAILABLE (nvidia-smi ok but torch.cuda.is_available()=False — driver/toolkit mismatch?)")
            # else torch not in this venv — skip
        else:
            err = gh.get("error") or "no GPU"
            print(f"gpu:               {err}")
            if gh["torch_cuda_available"] is False:
                print("torch_cuda:        NOT AVAILABLE")
        from taskqueue.resources import probe_gpu_processes
        procs = probe_gpu_processes()
        if procs:
            print(f"gpu_processes:     {len(procs)} CUDA context(s):")
            for r in procs[:12]:
                print(
                    f"  pid={r['pid']:6d}  {r['vram_gib']:6.2f} GiB  {r['cmdline_short']}"
                )
        elif gh["nvidia_smi_ok"]:
            print("gpu_processes:     none (no CUDA compute contexts)")
    except Exception as _gpu_exc:
        print(f"gpu:               (check failed: {_gpu_exc})")


def _doctor_processes(pid: int | None) -> None:
    # Stray processes — top RAM consumers outside tq daemon
    try:
        import psutil as _ps
        from taskqueue.resources import top_ram_consumers
        daemon_pid = pid or 0
        tq_pids: set[int] = {daemon_pid} if daemon_pid else set()
        try:
            dp = _ps.Process(daemon_pid)
            tq_pids.update(c.pid for c in dp.children(recursive=True))
        except Exception:
            pass
        scan: dict = {}
        stray = top_ram_consumers(exclude_pids=tq_pids, n=5, budget_sec=5.0, stats=scan)
        partial = " (partial: scan stopped after 5 s)" if scan.get("partial") else ""
        if stray:
            total_stray = sum(r["rss_gib"] for r in stray)
            print(f"top_ram_consumers: {total_stray:.1f} GiB across {len(stray)} non-tq process(es){partial}:")
            for r in stray:
                print(f"  pid={r['pid']:6d}  {r['rss_gib']:5.1f} GiB  {r['cmdline_short']}")
        else:
            print("top_ram_consumers: none >0.1 GiB outside tq")
    except Exception as _stray_exc:
        print(f"top_ram_consumers: (check failed: {_stray_exc})")


def _doctor_config(cfg: dict, projects: dict) -> None:
    shadow = int(cfg.get("admission_shadow_mode", 0) or 0) if cfg else 0
    if cfg:
        print(
            f"admission:         shadow={'on' if shadow else 'off'} "
            f"ram_limit={cfg.get('ram_limit_pct')}% "
            f"ram_hard={cfg.get('ram_hard_limit_pct')}% "
            f"cpu_limit={cfg.get('cpu_limit_pct')}%"
        )
        print(f"config_keys:       {len(cfg)} ({', '.join(sorted(cfg.keys()))})")

    # W4.11: warn if any project's queue dir has an excessive number of YAMLs.
    _QUEUE_DIR_WARN_THRESHOLD = 1000
    _oversized: list[str] = []
    for pname, preg in projects.items():
        qdir_attr = getattr(preg, "queue_dir", None)
        if qdir_attr is None:
            try:
                from pathlib import Path as _P

                qdir_attr = _P(getattr(preg, "root", "")) / "experiments" / "operator_queue"
            except Exception:
                continue
        try:
            count = sum(1 for _ in qdir_attr.glob("*.yaml")) + sum(1 for _ in qdir_attr.glob("*.yml"))
            if count > _QUEUE_DIR_WARN_THRESHOLD:
                _oversized.append(f"{pname} ({count} YAMLs)")
        except OSError:
            pass
    if _oversized:
        print()
        print(
            f"WARNING: large queue dirs (>{_QUEUE_DIR_WARN_THRESHOLD} YAMLs) — "
            "consider tq prune or raising max_queued_tasks_per_project:"
        )
        for _msg in _oversized:
            print(f"  {_msg}")

    print()
    print("Dogfood tip: prod uses default ~/.taskqueue :8765; dev uses")
    print("  TASKQUEUE_HOME=%USERPROFILE%\\.taskqueue-dev and web_port 8766.")
    print("  Never point a WIP worker at the prod master.")
    print("  Scheduler soak: tq log --grep \"SHADOW|reservation|admission skipped\"")


def cmd_doctor(home: Path, *, check_torch: bool = False) -> None:
    """Print dogfood-critical facts: home, uptime, DB, projects, memory, port."""
    import os

    from taskqueue.config import ENV_HOME, load_projects
    from taskqueue.db import SCHEMA_VERSION, get_config, open_db

    env_home = os.environ.get(ENV_HOME)
    pid = _daemon_running(home)
    db = _db(home)
    port = 8765
    schema_from_db: int | None = None
    cfg: dict = {}
    bind_host = "127.0.0.1"
    auth_on = False
    task_counts: dict[str, int] = {}
    workers_online = 0
    workers_total = 0
    lease_holder: str | None = None
    lease_expires: int | None = None

    if db.exists():
        try:
            conn = open_db(db)
            try:
                cfg = get_config(conn)
                port = int(cfg.get("web_port", 8765))
                bind_host = str(cfg.get("bind_host") or "127.0.0.1")
                auth_on = bool(str(cfg.get("auth_token") or "").strip())
                schema_from_db = int(cfg.get("schema_version", SCHEMA_VERSION))
                for row in conn.execute(
                    "SELECT status, COUNT(*) AS n FROM tasks GROUP BY status"
                ).fetchall():
                    task_counts[str(row["status"])] = int(row["n"])
                workers_total = int(
                    conn.execute("SELECT COUNT(*) FROM workers").fetchone()[0]
                )
                workers_online = int(
                    conn.execute(
                        "SELECT COUNT(*) FROM workers WHERE status = 'online'"
                    ).fetchone()[0]
                )
                lease = conn.execute(
                    "SELECT holder, expires_at FROM master_lease WHERE singleton = 1"
                ).fetchone()
                if lease:
                    lease_holder = str(lease["holder"])
                    lease_expires = int(lease["expires_at"])
            finally:
                conn.close()
        except Exception as e:
            print(f"warning: could not read DB config: {e}", file=sys.stderr)

    projects = {}
    try:
        projects = load_projects(home)
    except Exception as e:
        print(f"warning: projects.yaml: {e}", file=sys.stderr)

    daemon_log = home / "daemon.log"
    _doctor_version(
        home,
        env_home=env_home,
        pid=pid,
        daemon_log=daemon_log,
        port=port,
        bind_host=bind_host,
        auth_on=auth_on,
        schema_from_db=schema_from_db,
    )
    _doctor_db(
        home,
        db=db,
        lease_holder=lease_holder,
        lease_expires=lease_expires,
        projects=projects,
        task_counts=task_counts,
        workers_online=workers_online,
        workers_total=workers_total,
    )
    _doctor_gpu(check_torch)
    _doctor_processes(pid)
    _doctor_config(cfg, projects)
    _doctor_system_alerts(home, db)


def _doctor_system_alerts(home: Path, db: Path) -> None:
    """Warn when Windows toasts are enabled but winotify is missing."""
    if not db.exists():
        return
    try:
        from taskqueue.db import open_db
        from taskqueue.notify import winotify_available

        if winotify_available():
            return
        conn = open_db(db)
        try:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM system_alerts "
                "WHERE enabled = 1 AND windows_toast = 1"
            ).fetchone()
            n = int(row["n"] or 0) if row else 0
        except Exception:
            n = 0
        finally:
            conn.close()
        if n > 0:
            print(
                f"WARNING: {n} system alert(s) have Windows popup on, but winotify "
                "is not installed — pip install winotify  (or: pip install 'taskqueue[toast]')"
            )
    except Exception as e:
        print(f"warning: system alerts check: {e}", file=sys.stderr)


def cmd_daemon_log(
    home: Path,
    *,
    max_bytes: int | None = None,
    grep: str | None = None,
) -> int:
    """Tail {home}/daemon.log (daemon stdout). Distinct from `tq logs` (per-task)."""
    import re

    path = home / "daemon.log"
    if not path.exists():
        print(f"No daemon.log at {path}")
        print("Start the daemon with `tq start` (stdout is appended here).")
        return 1
    limit = 256 * 1024 if max_bytes is None else max(0, int(max_bytes))
    try:
        size = path.stat().st_size
        with open(path, "rb") as fh:
            if size > limit:
                fh.seek(size - limit)
                data = fh.read()
                # Drop possibly truncated first line
                nl = data.find(b"\n")
                if nl >= 0:
                    data = data[nl + 1 :]
            else:
                data = fh.read()
        text = data.decode("utf-8", errors="replace")
    except OSError as e:
        print(f"Failed to read {path}: {e}", file=sys.stderr)
        return 1
    if grep:
        try:
            pat = re.compile(grep)
        except re.error as e:
            print(f"Invalid --grep pattern: {e}", file=sys.stderr)
            return 1
        for line in text.splitlines():
            if pat.search(line):
                print(line)
    else:
        sys.stdout.write(text)
        if text and not text.endswith("\n"):
            print()
    return 0


def cmd_master_status(home: Path) -> int:
    """R13: Print master_lease holder and expiry."""
    import time
    from taskqueue.db import open_db

    db = _db(home)
    if not db.exists():
        print("No queue DB.")
        return 1
    conn = open_db(db)
    row = conn.execute(
        "SELECT holder, expires_at FROM master_lease WHERE singleton=1"
    ).fetchone()
    conn.close()
    if row is None:
        print("No master lease recorded (daemon not started yet).")
        return 0
    expires = int(row["expires_at"])
    remaining = expires - int(time.time())
    print(f"Master holder:  {row['holder']}")
    print(f"Expires at:     {time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime(expires))} UTC")
    print(f"Expires in:     {remaining}s")
    if remaining <= 0:
        print("Status:         STALE (lease expired)")
    else:
        print("Status:         active")
    return 0



def cmd_master_migrate(home: Path, target_url: str) -> int:
    """W7.2: Migrate master state to a standby target daemon.

    Flow:
      1. Verify target is in standby mode (GET target/api/status).
      2. Export the live DB (WAL-safe snapshot, daemon may be running).
      3. POST the archive to target /api/import.
      4. Verify target schema version and task/run counts match source.
      5. Yield the local master lease (DELETE from DB; daemon halts scheduling).
      6. Promote the target (POST target/api/promote → standby exits, daemon claims lease).
    """
    import json as _json
    import sqlite3 as _sqlite3
    import tempfile as _tempfile
    import urllib.error as _urlerr
    import urllib.request as _urlreq

    target_url = target_url.rstrip("/")

    # F-062: send Bearer token on all target requests when configured.
    from taskqueue.db import open_db as _open_db_early, get_config as _get_cfg_early
    _early_conn = _open_db_early(home / "queue.db")
    try:
        _src_cfg = _get_cfg_early(_early_conn)
    finally:
        _early_conn.close()
    _auth_token = str(_src_cfg.get("auth_token") or "").strip()

    def _auth_headers(**extra: str) -> dict[str, str]:
        headers = dict(extra)
        if _auth_token:
            headers["Authorization"] = f"Bearer {_auth_token}"
        return headers

    # --- 1. Verify target is in standby ---
    print(f"Connecting to {target_url} ...")
    try:
        req0 = _urlreq.Request(
            f"{target_url}/api/status",
            headers=_auth_headers(),
            method="GET",
        )
        with _urlreq.urlopen(req0, timeout=10) as r:
            tgt_status = _json.loads(r.read())
    except _urlerr.URLError as exc:
        print(f"error: cannot reach target: {exc}")
        return 1
    if tgt_status.get("mode") != "standby":
        print(
            f"error: target must be in standby mode (got mode={tgt_status.get('mode')!r}).\n"
            f"Run on target machine:  tq daemon --standby --bind 0.0.0.0 --port <PORT>\n"
            f"(Non-loopback standby requires auth_token set — F-062.)"
        )
        return 1
    print("  target is in standby mode.")

    # --- 2. Export live DB ---
    from taskqueue.export_import import export_home

    with _tempfile.TemporaryDirectory(prefix="tq-migrate-") as tmp:
        archive = Path(tmp) / "migrate.tar.gz"
        try:
            archive = export_home(home, archive, allow_running_daemon=True)
        except Exception as exc:
            print(f"error: export failed: {exc}")
            return 1
        size_kb = archive.stat().st_size // 1024
        print(f"  exported {size_kb} KB → {archive}")

        # Row counts for verification
        db = home / "queue.db"
        src_conn = _sqlite3.connect(str(db))
        src_conn.row_factory = _sqlite3.Row
        src_tasks = src_conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
        src_runs = src_conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
        src_schema = src_conn.execute(
            "SELECT value FROM config WHERE key='schema_version'"
        ).fetchone()
        src_conn.close()
        src_schema_v = int(src_schema[0]) if src_schema else 0
        print(f"  source: {src_tasks} tasks, {src_runs} runs, schema v{src_schema_v}")

        # --- 3. POST archive to target ---
        data = archive.read_bytes()
        req = _urlreq.Request(
            f"{target_url}/api/import",
            data=data,
            headers=_auth_headers(
                **{
                    "Content-Type": "application/octet-stream",
                    "Content-Length": str(len(data)),
                }
            ),
            method="POST",
        )
        try:
            with _urlreq.urlopen(req, timeout=120) as r:
                result = _json.loads(r.read())
        except _urlerr.URLError as exc:
            print(f"error: import upload failed: {exc}")
            return 1
        if not result.get("ok"):
            print(f"error: target import error: {result}")
            return 1
        print("  target imported successfully.")

    # --- 4. Verify target row counts ---
    try:
        req_st = _urlreq.Request(
            f"{target_url}/api/status",
            headers=_auth_headers(),
            method="GET",
        )
        with _urlreq.urlopen(req_st, timeout=10) as r:
            tgt_status2 = _json.loads(r.read())
    except _urlerr.URLError as exc:
        print(f"warning: could not re-read target status: {exc}")
        tgt_status2 = {}
    tgt_tasks = tgt_status2.get("tasks_count")
    tgt_runs = tgt_status2.get("runs_count")
    tgt_schema = tgt_status2.get("schema_version")
    if tgt_tasks is not None and tgt_runs is not None:
        if tgt_tasks != src_tasks or tgt_runs != src_runs:
            print(
                f"WARNING: count mismatch — source tasks={src_tasks} runs={src_runs} "
                f"vs target tasks={tgt_tasks} runs={tgt_runs}. Proceeding anyway."
            )
        else:
            print(f"  counts verified: {tgt_tasks} tasks, {tgt_runs} runs.")
    if tgt_schema is not None and tgt_schema != src_schema_v:
        print(f"WARNING: schema version mismatch (source={src_schema_v}, target={tgt_schema}).")

    # --- 5. Yield local master lease ---
    from taskqueue.db import get_config as _get_config_, open_db as _open_db_
    _cfg = _get_config_(_open_db_(home / "queue.db"))
    _local_port = int(_cfg.get("web_port", 8765))
    _local_url = f"http://127.0.0.1:{_local_port}"
    try:
        req2 = _urlreq.Request(
            f"{_local_url}/api/yield-master",
            data=b"",
            headers=_auth_headers(),
            method="POST",
        )
        with _urlreq.urlopen(req2, timeout=10) as r:
            _json.loads(r.read())
        print("  source lease yielded via API.")
    except _urlerr.URLError:
        # Daemon may not be running; drop the row directly only if daemon is dead.
        if _daemon_running(home) is not None:
            print(
                "  error: /api/yield-master failed but daemon process is still alive.\n"
                "  Refusing to drop lease to avoid split-brain. Stop the daemon first."
            )
            return 1
        try:
            conn = _open_db_(home / "queue.db")
            conn.execute("DELETE FROM master_lease WHERE singleton=1")
            conn.commit()
            conn.close()
            print("  source lease dropped directly in DB (daemon not reachable).")
        except Exception as exc2:
            print(f"  warning: could not drop source lease: {exc2}")

    # --- 6. Promote target ---
    try:
        req3 = _urlreq.Request(
            f"{target_url}/api/promote",
            data=b"",
            headers=_auth_headers(),
            method="POST",
        )
        with _urlreq.urlopen(req3, timeout=10) as r:
            prom = _json.loads(r.read())
        print(f"  target promoted: {prom.get('message', 'ok')}")
    except _urlerr.URLError as exc:
        print(f"  warning: promote call failed ({exc}). Target will claim lease on next tq start.")

    print()
    print("Migration complete.")
    print(f"  → Source ({home}): stop-daemon if still running.  tq stop-daemon")
    print("  → Target: if --standby exits, run: tq start  (it will claim the lease)")
    return 0


def cmd_sleep(home: Path, key: str, project: str | None) -> int:
    """Sleep a running task (3-tier: cooperative suspend → OS freeze → last-resort kill).

    Tier 2 (NtSuspendProcess / SIGSTOP) leaves ``stop_requested`` set but the daemon
    will **not** SIGTERM while ``sleep_tier=2`` (F-126). Use ``tq wake`` to resume.
    """
    import ctypes
    from taskqueue.db import open_db, write_control_action

    if "/" in key:
        proj, task_id = key.split("/", 1)
    elif project:
        proj, task_id = project, key
    else:
        print("Usage: tq sleep <project/task_id> [--project P]")
        return 1

    db = _db(home)
    if not db.exists():
        print("No queue DB.")
        return 1
    conn = open_db(db)
    row = get_task_status(conn, proj, task_id)
    if row is None:
        print(f"Task not found: {proj}/{task_id}")
        conn.close()
        return 1
    if row["status"] != "running":
        print(f"Task is not running (status={row['status']})")
        conn.close()
        return 1

    # Get PID from most recent run
    run_row = conn.execute(
        "SELECT run_id, pid FROM runs WHERE project=? AND task_id=? AND status='running' ORDER BY run_id DESC LIMIT 1",
        (proj, task_id),
    ).fetchone()
    pid = int(run_row["pid"]) if run_row and run_row["pid"] else None
    run_id = int(run_row["run_id"]) if run_row else None

    # Tier 1: cooperative suspend (same as tq suspend)
    try:
        cfg = conn.execute("SELECT value FROM config WHERE key='suspend_grace_sec'").fetchone()
        grace = float(json.loads(cfg["value"])) if cfg else 10.0
    except Exception:
        grace = 10.0

    write_control_action(home, proj, task_id, "suspend")
    # Request cooperative stop via DB
    import time as _time
    conn.execute(
        "UPDATE tasks SET stop_requested=1 WHERE project=? AND task_id=?", (proj, task_id),
    )
    conn.commit()

    tier = 1
    deadline = _time.monotonic() + grace
    while _time.monotonic() < deadline:
        _time.sleep(0.5)
        status_now = get_task_status(conn, proj, task_id)
        if status_now and status_now["status"] in ("suspended", "interrupted", "queued"):
            break
    else:
        # Tier 1 didn't complete — try Tier 2 (NtSuspendProcess on Windows)
        if pid and sys.platform == "win32":
            try:
                import ctypes
                import ctypes.wintypes
                PROCESS_SUSPEND_RESUME = 0x0800
                _kernel32 = ctypes.windll.kernel32
                _ntdll = ctypes.windll.ntdll
                _kernel32.OpenProcess.argtypes = [ctypes.wintypes.DWORD, ctypes.wintypes.BOOL, ctypes.wintypes.DWORD]
                _kernel32.OpenProcess.restype = ctypes.wintypes.HANDLE
                _kernel32.CloseHandle.argtypes = [ctypes.wintypes.HANDLE]
                _kernel32.CloseHandle.restype = ctypes.wintypes.BOOL
                _ntdll.NtSuspendProcess.argtypes = [ctypes.wintypes.HANDLE]
                _ntdll.NtSuspendProcess.restype = ctypes.c_long
                handle = _kernel32.OpenProcess(PROCESS_SUSPEND_RESUME, False, pid)
                if handle:
                    ret = _ntdll.NtSuspendProcess(handle)
                    _kernel32.CloseHandle(handle)
                    if ret == 0:  # STATUS_SUCCESS
                        tier = 2
                        print(f"Tier 2: process {pid} frozen via NtSuspendProcess")
                    else:
                        tier = 3
            except Exception as exc:
                print(f"Tier 2 freeze failed: {exc}", file=sys.stderr)
                tier = 3
        else:
            # Linux/Unix: freeze with SIGSTOP instead of killing.
            tier = 3
            if pid:
                try:
                    import signal as _sig
                    os.kill(pid, _sig.SIGSTOP)
                    tier = 2
                    print(f"Tier 2: process {pid} stopped via SIGSTOP")
                except Exception as exc:
                    print(f"Tier 2 SIGSTOP failed: {exc}", file=sys.stderr)
                    try:
                        os.kill(pid, _sig.SIGTERM)
                    except Exception:
                        pass
                    tier = 3

    if run_id:
        conn.execute(
            "UPDATE runs SET sleep_tier=? WHERE run_id=?", (tier, run_id)
        )
        conn.commit()

    conn.close()
    print(f"Sleep tier {tier} applied to {proj}/{task_id}")
    return 0



def cmd_wake(home: Path, key: str, project: str | None) -> int:
    """R11: Wake a sleeping task."""
    if "/" in key:
        proj, task_id = key.split("/", 1)
    elif project:
        proj, task_id = project, key
    else:
        print("Usage: tq wake <project/task_id> [--project P]")
        return 1

    db = _db(home)
    if not db.exists():
        print("No queue DB.")
        return 1

    from taskqueue.db import open_db
    conn = open_db(db)

    run_row = conn.execute(
        "SELECT run_id, pid, sleep_tier FROM runs WHERE project=? AND task_id=? ORDER BY run_id DESC LIMIT 1",
        (proj, task_id),
    ).fetchone()

    tier = int(run_row["sleep_tier"]) if run_row and run_row["sleep_tier"] else None
    pid = int(run_row["pid"]) if run_row and run_row["pid"] else None

    if tier == 2 and pid and sys.platform == "win32":
        try:
            import ctypes
            import ctypes.wintypes
            PROCESS_SUSPEND_RESUME = 0x0800
            _kernel32 = ctypes.windll.kernel32
            _ntdll = ctypes.windll.ntdll
            _kernel32.OpenProcess.argtypes = [ctypes.wintypes.DWORD, ctypes.wintypes.BOOL, ctypes.wintypes.DWORD]
            _kernel32.OpenProcess.restype = ctypes.wintypes.HANDLE
            _kernel32.CloseHandle.argtypes = [ctypes.wintypes.HANDLE]
            _kernel32.CloseHandle.restype = ctypes.wintypes.BOOL
            _ntdll.NtResumeProcess.argtypes = [ctypes.wintypes.HANDLE]
            _ntdll.NtResumeProcess.restype = ctypes.c_long
            handle = _kernel32.OpenProcess(PROCESS_SUSPEND_RESUME, False, pid)
            if handle:
                _ntdll.NtResumeProcess(handle)
                _kernel32.CloseHandle(handle)
            if run_row:
                conn.execute(
                    "UPDATE runs SET sleep_tier=NULL WHERE run_id=?", (run_row["run_id"],)
                )
                conn.commit()
            conn.execute(
                "UPDATE tasks SET stop_requested=0 WHERE project=? AND task_id=?",
                (proj, task_id),
            )
            conn.commit()
            print(f"Woke {proj}/{task_id} (tier 2 resume via NtResumeProcess)")
        except Exception as exc:
            print(f"Wake failed: {exc}", file=sys.stderr)
            conn.close()
            return 1
    elif tier == 2 and pid and sys.platform != "win32":
        try:
            import signal as _sig
            os.kill(pid, _sig.SIGCONT)
            if run_row:
                conn.execute(
                    "UPDATE runs SET sleep_tier=NULL WHERE run_id=?", (run_row["run_id"],)
                )
                conn.commit()
            conn.execute(
                "UPDATE tasks SET stop_requested=0 WHERE project=? AND task_id=?",
                (proj, task_id),
            )
            conn.commit()
            print(f"Woke {proj}/{task_id} (tier 2 resume via SIGCONT)")
        except Exception as exc:
            print(f"Wake failed: {exc}", file=sys.stderr)
            conn.close()
            return 1
    else:
        # Tier 1 or 3: re-queue from checkpoint (F-105: only resume non-terminal tasks)
        cur = conn.execute(
            "UPDATE tasks SET status='queued', stop_requested=0"
            " WHERE project=? AND task_id=?"
            " AND status IN ('suspended','stopped','interrupted')",
            (proj, task_id),
        )
        conn.commit()
        if cur.rowcount:
            print(f"Woke {proj}/{task_id} (re-queued from checkpoint)")
        else:
            _row = get_task_status(conn, proj, task_id)
            status = _row["status"] if _row else "unknown"
            print(f"{proj}/{task_id} is {status}; nothing to re-queue")

    conn.close()
    return 0


