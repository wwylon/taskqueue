"""Single online/offline mutation helper for CLI and MCP (F-202).

Prefer master HTTP when a daemon owns the DB; fall back to a caller-supplied
offline function only when ``offline_sqlite_write_blocked`` allows it.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any


def mutate(
    home: Path,
    path: str,
    *,
    body: dict[str, Any] | None = None,
    offline_fn: Callable[[], dict[str, Any]] | None = None,
    force: bool = False,
    params: dict[str, Any] | None = None,
) -> tuple[dict[str, Any] | None, str]:
    """POST ``path`` to the master, or run ``offline_fn`` when truly offline.

    Returns ``(response, mode)`` where mode is ``master`` | ``offline`` |
    ``unreachable``. On ``unreachable`` the caller must not write SQLite.
    """
    import os
    from urllib.parse import urlencode

    from taskqueue._cli_helpers import offline_sqlite_write_blocked
    from taskqueue.master_client import via_master

    q: dict[str, str] = {}
    if os.environ.get("TQ_OPERATOR"):
        q["operator"] = os.environ["TQ_OPERATOR"]
    if force:
        q["force"] = "1"
    q.update({k: str(v) for k, v in (params or {}).items() if v is not None})
    url = path + (("?" + urlencode(q)) if q else "")
    resp = via_master(home, "POST", url, body=body)
    if resp is not None:
        if resp.get("unreachable"):
            return None, "unreachable"
        return resp, "master"
    block = offline_sqlite_write_blocked(home)
    if block:
        return {"ok": False, "error": block, "unreachable": True}, "unreachable"
    if offline_fn is None:
        return {"ok": False, "error": "offline and no offline_fn provided"}, "unreachable"
    return offline_fn(), "offline"
