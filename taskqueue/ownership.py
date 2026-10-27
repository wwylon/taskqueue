"""Shared ownership gate for CLI and HTTP mutations."""

from __future__ import annotations

import sqlite3
from typing import Any


def enforce_ownership(
    conn: sqlite3.Connection,
    proj: str,
    task_id: str,
    *,
    operator: str | None,
    force: bool,
    log_prefix: str = "[queue]",
) -> str | None:
    """Return error message or None. Logs force override when applied."""
    from taskqueue.db import check_ownership, get_task

    row = get_task(conn, proj, task_id)
    started_by = row["started_by"] if row is not None else None
    err = check_ownership(started_by, operator, force=force)
    if err is None and force and started_by and started_by != operator:
        print(
            f"{log_prefix} [force] overriding ownership: {proj}/{task_id} "
            f"started_by='{started_by}', caller='{operator or ''}'"
        )
    return err


def ownership_from_qs(conn: sqlite3.Connection, proj: str, task_id: str, qs: dict[str, Any]) -> str | None:
    """HTTP query-string variant (?operator=&force=)."""
    operator = (qs.get("operator", [None])[0] or "").strip() or None
    force = qs.get("force", ["0"])[0] in ("1", "true", "yes")
    return enforce_ownership(
        conn, proj, task_id, operator=operator, force=force, log_prefix="[queue]"
    )
