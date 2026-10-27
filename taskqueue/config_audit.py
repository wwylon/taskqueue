"""Before/after capture for config changes in the operator audit, and one-click rollback."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

_SECRET_KEYS = frozenset({"auth_token"})
_REDACTED = "[redacted]"


def build_config_detail(before: dict[str, Any], changes: list[tuple[str, Any]]) -> str:
    """JSON audit detail ``{"changes": {key: {"old", "new"}}}``; secrets are redacted (not rollbackable)."""
    out: dict[str, dict[str, Any]] = {}
    for key, new in changes:
        old = before.get(key)
        if key in _SECRET_KEYS:
            out[key] = {"old": _REDACTED, "new": _REDACTED}
        else:
            out[key] = {"old": old, "new": new}
    return json.dumps({"changes": out}, separators=(",", ":"), default=str)


def rollback_config_audit(conn: sqlite3.Connection, audit_id: int) -> tuple[int, dict[str, Any]]:
    """Restore the ``old`` values recorded by audit row *audit_id*. Returns ``(http_status, body)``."""
    from taskqueue.config import VALID_CONFIG_KEYS, validate_config
    from taskqueue.db import get_config, set_config

    row = conn.execute("SELECT action, detail FROM operator_audit WHERE id=?", (audit_id,)).fetchone()
    if row is None:
        return 404, {"ok": False, "error": "audit event not found"}
    if row["action"] not in ("config", "config_rollback"):
        return 409, {"ok": False, "error": "only config events can be rolled back"}
    try:
        changes = json.loads(row["detail"] or "")["changes"]
    except (ValueError, KeyError, TypeError):
        return 409, {"ok": False, "error": "event has no recorded previous values (recorded before diffs existed)"}
    current = get_config(conn)
    restored: list[tuple[str, Any, Any]] = []
    skipped: dict[str, str] = {}
    for key, ch in changes.items():
        old = ch.get("old") if isinstance(ch, dict) else None
        if key in _SECRET_KEYS or old == _REDACTED:
            skipped[key] = "secret"
        elif key not in VALID_CONFIG_KEYS:
            skipped[key] = "unknown key"
        elif old is None:
            skipped[key] = "no previous value"
        elif validate_config(key, old):
            skipped[key] = "previous value no longer valid"
        else:
            restored.append((key, VALID_CONFIG_KEYS[key][0](old), current.get(key)))
    for key, val, _cur in restored:
        set_config(conn, key, val)
    return 200, {
        "ok": True,
        "restored": [k for k, _, _ in restored],
        "skipped": skipped,
        "changes": [(k, cur, val) for k, val, cur in restored],
    }
