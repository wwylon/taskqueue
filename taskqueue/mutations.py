"""Shared task mutation helpers (CLI / MCP / web).

Prefer master HTTP via ``via_master`` so the daemon is the only ordinary
SQLite writer. Offline SQLite paths are for repair when the daemon is down.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any


def retry_task_op(
    home: Path,
    project: str,
    task_id: str,
    *,
    args_override: list[str] | None = None,
    downstream: bool = False,
    enable_downstream: bool = False,
) -> dict[str, Any]:
    """Re-queue a terminal task. Returns ``{ok, ...}`` matching the HTTP API.

    F-086: ``args_override`` is sent in the retry body (or written before
    offline retry) so the scheduler cannot claim before overrides land.
    """
    from taskqueue.master_client import via_master

    body: dict[str, Any] = {}
    if args_override:
        body["args_override"] = list(args_override)
    if downstream:
        body["downstream"] = True
    if enable_downstream:
        body["enable_downstream"] = True
    api = via_master(
        home,
        "POST",
        f"/api/tasks/{project}/{task_id}/retry",
        body=body or None,
    )
    if api is not None:
        return api if isinstance(api, dict) else {"ok": bool(api)}

    from taskqueue.db import (
        open_db,
        retry_task,
        retry_task_with_downstream,
        set_args_override,
    )
    from taskqueue.config import db_path

    conn = open_db(db_path(home))
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
