"""Post-finalize wake hooks shared by local runner and remote worker finalize.

``on_complete`` / ``on_fail`` re-queue listed task IDs and stamp ``spawned_by``.
``rearm_on: upstream_success`` re-queues same-project dependents.
``teardown`` runs an optional post-exit shell command (best-effort by default).
"""

from __future__ import annotations
from taskqueue.jsonutil import json_list

import json
import sqlite3
import subprocess
import threading
from typing import Any


def _run_teardown_async(td_cmd: list[str], td_timeout: float, td_best_effort: bool) -> None:
    try:
        subprocess.run(td_cmd, timeout=td_timeout, check=not td_best_effort)
    except Exception as te:
        if not td_best_effort:
            print(f"[runner] teardown failed: {te}", flush=True)
            return
        print(
            f"[runner] teardown failed (best_effort=true, ignoring): {te}",
            flush=True,
        )


def _retry_and_stamp_spawned(
    conn: sqlite3.Connection,
    project: str,
    child_task_id: str,
    parent_ref: str,
) -> None:
    from taskqueue.db import retry_task, set_spawned_by

    if retry_task(conn, project, str(child_task_id)):
        set_spawned_by(conn, project, str(child_task_id), parent_ref)


def apply_finalize_wake_hooks(
    conn: sqlite3.Connection,
    project: str,
    task_id: str,
    status: str,
    *,
    task_row: Any | None = None,
    run_teardown: bool = True,
    teardown_async: bool = True,
) -> None:
    """Run on_complete/on_fail, rearm_on, and optional teardown after a terminal finalize."""
    parent_ref = f"{project}/{task_id}"

    # on_complete / on_fail
    try:
        hook_key = "on_complete" if status == "completed" else "on_fail"
        hook_ids_raw = None
        if task_row is not None:
            try:
                hook_ids_raw = task_row[hook_key]
            except (KeyError, IndexError, TypeError):
                pass
        if hook_ids_raw is None:
            fresh = conn.execute(
                f"SELECT {hook_key} FROM tasks WHERE project = ? AND task_id = ?",
                (project, task_id),
            ).fetchone()
            if fresh:
                hook_ids_raw = fresh[0]
        hook_ids = json_list(hook_ids_raw)
        for tid in hook_ids:
            _retry_and_stamp_spawned(conn, project, str(tid), parent_ref)
    except Exception:
        pass

    # rearm_on = upstream_success (AND or OR peers)
    if status == "completed":
        try:
            rearm_rows = conn.execute(
                "SELECT task_id, depends_on, depends_on_any FROM tasks "
                "WHERE project = ? AND rearm_on = 'upstream_success' "
                "AND status IN ('queued','failed','interrupted','stopped')",
                (project,),
            ).fetchall()
            for rrow in rearm_rows:
                dep_ids: list[str] = []
                for col in ("depends_on", "depends_on_any"):
                    try:
                        raw = rrow[col] if col in rrow.keys() else "[]"
                        deps = json_list(raw)
                    except Exception:
                        continue
                    for d in deps:
                        d = str(d)
                        if "/" in d:
                            dp, di = d.split("/", 1)
                            if dp == project:
                                dep_ids.append(di)
                        else:
                            dep_ids.append(d)
                if task_id in dep_ids:
                    _retry_and_stamp_spawned(conn, project, rrow["task_id"], parent_ref)
        except Exception:
            pass

    if not run_teardown:
        return

    try:
        teardown_raw = None
        if task_row is not None:
            try:
                teardown_raw = task_row["teardown"]
            except (KeyError, IndexError, TypeError):
                pass
        if teardown_raw is None:
            td_row = conn.execute(
                "SELECT teardown FROM tasks WHERE project = ? AND task_id = ?",
                (project, task_id),
            ).fetchone()
            if td_row:
                teardown_raw = td_row["teardown"]
        if teardown_raw:
            td = json.loads(teardown_raw) if isinstance(teardown_raw, str) else teardown_raw
            if isinstance(td, dict) and td.get("command"):
                td_cmd = [str(td["command"])] + [str(a) for a in (td.get("args") or [])]
                td_timeout = float(td.get("timeout_sec") or 60)
                td_best_effort = bool(td.get("best_effort", True))
                if teardown_async:
                    threading.Thread(
                        target=_run_teardown_async,
                        args=(td_cmd, td_timeout, td_best_effort),
                        name=f"tq-teardown-{project}/{task_id}",
                        daemon=True,
                    ).start()
                else:
                    _run_teardown_async(td_cmd, td_timeout, td_best_effort)
    except Exception:
        pass
