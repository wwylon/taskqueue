"""Shared task mutation helpers (CLI / MCP / web).

Prefer master HTTP via ``mutate.mutate`` so the daemon is the only ordinary
SQLite writer. Offline SQLite paths run only when ``offline_sqlite_write_blocked``
allows (F-245).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode


def retry_task_op(
    home: Path,
    project: str,
    task_id: str,
    *,
    args_override: list[str] | None = None,
    downstream: bool = False,
    enable_downstream: bool = False,
    force: bool = False,
) -> dict[str, Any]:
    """Re-queue a terminal task. Returns ``{ok, ...}`` matching the HTTP API.

    F-086: ``args_override`` is sent in the retry body (or written before
    offline retry) so the scheduler cannot claim before overrides land.
    """
    from taskqueue.mutate import mutate

    body: dict[str, Any] = {}
    if args_override:
        body["args_override"] = list(args_override)
    if downstream:
        body["downstream"] = True
    if enable_downstream:
        body["enable_downstream"] = True

    def _offline() -> dict[str, Any]:
        from taskqueue.config import db_path
        from taskqueue.db import (
            open_db,
            retry_task,
            retry_task_with_downstream,
            set_args_override,
        )

        conn = open_db(db_path(home), migrate=False)
        try:
            if args_override:
                set_args_override(conn, project, task_id, list(args_override))
            if downstream or enable_downstream:
                result = retry_task_with_downstream(conn, project, task_id, home=home)
                ok = bool(result.get("requeued"))
                return {"ok": ok, **result}
            ok = retry_task(conn, project, task_id, home=home)
            return {"ok": ok}
        finally:
            conn.close()

    resp, mode = mutate(
        home,
        f"/api/tasks/{project}/{task_id}/retry",
        body=body or None,
        offline_fn=_offline,
        force=force,
    )
    if mode == "unreachable" or resp is None:
        return resp if isinstance(resp, dict) else {"ok": False, "error": "unreachable", "unreachable": True}
    return resp if isinstance(resp, dict) else {"ok": bool(resp)}


def stop_op(
    home: Path,
    project: str,
    task_id: str,
    *,
    reason: str = "",
    force: bool = False,
) -> dict[str, Any]:
    """Stop a running task or mark a queued task interrupted (F-246 offline parity)."""
    from taskqueue.mutate import mutate

    reason = (reason or "").strip()[:200]
    path = f"/api/stop/{project}/{task_id}"
    if reason:
        path = f"{path}?reason={quote(reason, safe='')}"

    def _offline() -> dict[str, Any]:
        from taskqueue._db_tasks import transition_task
        from taskqueue._time import _now
        from taskqueue.config import db_path
        from taskqueue.db import (
            get_task,
            open_db,
            request_stop_with_control,
            set_run_stop_reason,
        )

        conn = open_db(db_path(home), migrate=False)
        try:
            row = get_task(conn, project, task_id)
            if row is None:
                return {"ok": False, "status": None, "message": f"task not found: {project}/{task_id}"}
            current_status = row["status"]
            if current_status == "queued":
                changed = transition_task(
                    conn, project, task_id, to="interrupted", from_="queued", finished=True, now=_now()
                )
                if not changed:
                    return {"ok": False, "status": current_status, "message": "could not interrupt queued task"}
                conn.commit()
                return {
                    "ok": True,
                    "status": "interrupted",
                    "final_status": "interrupted",
                    "message": "stopped before start",
                    **({"reason": reason} if reason else {}),
                }
            if current_status != "running":
                return {
                    "ok": False,
                    "status": current_status,
                    "message": f"task is not running or queued (status={current_status})",
                }
            if reason:
                run_row = conn.execute(
                    "SELECT run_id FROM runs WHERE project=? AND task_id=? ORDER BY run_id DESC LIMIT 1",
                    (project, task_id),
                ).fetchone()
                if run_row:
                    set_run_stop_reason(conn, run_row["run_id"], reason)
            from taskqueue.eta import task_eta

            eta = task_eta(conn, project, task_id)
            request_stop_with_control(conn, home, project, task_id)
            return {
                "ok": True,
                "status": "running",
                "message": "stop requested",
                **({"reason": reason} if reason else {}),
                **({"eta": eta} if eta and "remaining_sec" in eta else {}),
            }
        finally:
            conn.close()

    resp, mode = mutate(home, path, offline_fn=_offline, force=force)
    if mode == "unreachable" or resp is None:
        return resp if isinstance(resp, dict) else {"ok": False, "error": "unreachable", "unreachable": True}
    if mode == "master" and isinstance(resp, dict):
        ok = bool(resp.get("ok"))
        final = resp.get("final_status")
        return {
            "ok": ok,
            "status": final or resp.get("status"),
            "message": ("stopped before start" if final else "stop requested")
            if ok
            else (resp.get("error") or "stop failed"),
            **({"reason": reason} if reason and ok else {}),
            **({"eta": resp["eta"]} if ok and resp.get("eta") else {}),
            **({"final_status": final} if final else {}),
        }
    return resp if isinstance(resp, dict) else {"ok": bool(resp)}


def hold_op(
    home: Path,
    project: str,
    task_id: str,
    *,
    duration_sec: float = 600.0,
    reason: str | None = None,
    force: bool = False,
) -> dict[str, Any]:
    from taskqueue.mutate import mutate

    reason = ((reason or "").strip()[:200] or None)
    q: dict[str, Any] = {"duration_sec": duration_sec}
    if reason:
        q["reason"] = reason

    def _offline() -> dict[str, Any]:
        from taskqueue.config import db_path
        from taskqueue.db import get_task, open_db, write_control_action

        conn = open_db(db_path(home), migrate=False)
        try:
            row = get_task(conn, project, task_id)
            if row is None:
                return {"ok": False, "message": f"task not found: {project}/{task_id}"}
            if row["status"] != "running":
                return {"ok": False, "status": row["status"], "message": "task is not running"}
            write_control_action(home, project, task_id, "hold", duration_sec=duration_sec, reason=reason)
        finally:
            conn.close()
        return {"ok": True, "duration_sec": duration_sec, "message": "hold requested"}

    resp, mode = mutate(
        home,
        f"/api/hold/{project}/{task_id}?{urlencode(q)}",
        offline_fn=_offline,
        force=force,
    )
    if mode == "unreachable" or resp is None:
        return resp if isinstance(resp, dict) else {"ok": False, "error": "unreachable", "unreachable": True}
    return resp if isinstance(resp, dict) else {"ok": bool(resp)}


def unhold_op(home: Path, project: str, task_id: str, *, force: bool = False) -> dict[str, Any]:
    from taskqueue.mutate import mutate

    def _offline() -> dict[str, Any]:
        from taskqueue.config import db_path
        from taskqueue.db import clear_control_file, get_task, open_db

        conn = open_db(db_path(home), migrate=False)
        try:
            row = get_task(conn, project, task_id)
            if row is None:
                return {"ok": False, "message": f"task not found: {project}/{task_id}"}
            clear_control_file(home, project, task_id)
        finally:
            conn.close()
        return {"ok": True, "message": "hold cleared"}

    resp, mode = mutate(home, f"/api/unhold/{project}/{task_id}", offline_fn=_offline, force=force)
    if mode == "unreachable" or resp is None:
        return resp if isinstance(resp, dict) else {"ok": False, "error": "unreachable", "unreachable": True}
    return resp if isinstance(resp, dict) else {"ok": bool(resp)}


def patch_op(
    home: Path,
    project: str,
    task_id: str,
    updates: dict[str, Any],
    *,
    force: bool = False,
) -> dict[str, Any]:
    """Patch YAML fields via master or offline (gated)."""
    from taskqueue.mutate import mutate

    def _offline() -> dict[str, Any]:
        from pathlib import Path as _Path

        from taskqueue.config import db_path, load_projects
        from taskqueue.db import get_config, get_task, open_db, upsert_task
        from taskqueue.taskspec import parse_task_yaml
        from taskqueue.yaml_patch import patch_task_yaml

        conn = open_db(db_path(home), migrate=False)
        try:
            row = get_task(conn, project, task_id)
            if row is None or not row["yaml_source"]:
                return {
                    "ok": False,
                    "applied": [],
                    "message": f"task not found or has no yaml_source: {project}/{task_id}",
                }
            path = _Path(row["yaml_source"])
            try:
                applied = patch_task_yaml(path, updates, task_id=task_id)
            except ValueError as exc:
                return {"ok": False, "applied": [], "message": str(exc)}
            projects = load_projects(home)
            proj = projects.get(project)
            if proj is None:
                return {"ok": False, "applied": applied, "message": f"project not registered: {project}"}
            upsert_task(
                conn,
                parse_task_yaml(path, proj, allow_remote_notify=bool(get_config(conn).get("notify_allow_remote"))),
            )
            return {"ok": True, "applied": applied, "message": f"patched {', '.join(applied)}"}
        finally:
            conn.close()

    resp, mode = mutate(
        home,
        f"/api/tasks/{project}/{task_id}/patch",
        body={"updates": updates},
        offline_fn=_offline,
        force=force,
    )
    if mode == "unreachable" or resp is None:
        return resp if isinstance(resp, dict) else {"ok": False, "applied": [], "error": "unreachable", "unreachable": True}
    if mode == "master" and isinstance(resp, dict):
        return {
            "ok": bool(resp.get("ok")),
            "applied": resp.get("applied") or [],
            "message": resp.get("error") or f"patched {', '.join(resp.get('applied') or [])}",
            **({"warning": resp["warning"]} if resp.get("warning") else {}),
        }
    return resp if isinstance(resp, dict) else {"ok": bool(resp)}
