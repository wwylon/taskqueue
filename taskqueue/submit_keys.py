"""C-002: idempotent task submission with a client key.

Agents retry ``submit_task`` / ``POST /api/cli/add`` after a timeout. With an optional ``client_key``
the first submission reserves ``(project, client_key)`` before writing anything; a retry with the same
key and the same body gets the original ``task_ids`` back instead of a second YAML file, a retry while
the first is still being written gets 409 (retry shortly), and the same key with a different body is a
409 conflict. Keys expire after ``submit_key_ttl_sec`` (default 24 h).
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time

MAX_KEY_LEN = 128
DEFAULT_TTL_SEC = 86400
PENDING_STALE_SEC = 120.0  # a reservation this old without a result was abandoned (crash mid-submit)


class SubmitKeyConflict(Exception):
    """The key cannot be used for this submission (HTTP 409)."""


def body_hash(yaml_text: str, filename: str = "") -> str:
    return hashlib.sha256(f"{filename}\0{yaml_text}".encode()).hexdigest()


def key_error(key: object) -> str | None:
    """Why ``key`` is not an acceptable client_key, or None."""
    if not isinstance(key, str) or not key.strip():
        return "client_key must be a non-empty string"
    if len(key) > MAX_KEY_LEN:
        return f"client_key longer than {MAX_KEY_LEN} characters"
    if any(ord(c) < 32 for c in key):
        return "client_key contains control characters"
    return None


def claim(
    conn: sqlite3.Connection,
    project: str,
    key: str,
    digest: str,
    *,
    ttl_sec: float = DEFAULT_TTL_SEC,
    now: float | None = None,
) -> dict | None:
    """Reserve the key: None means go ahead (then :func:`complete` or :func:`release`).

    A dict is the earlier result to return as-is; :class:`SubmitKeyConflict` means refuse.
    """
    now = time.time() if now is None else now
    conn.execute("DELETE FROM submit_keys WHERE created_at < ?", (now - ttl_sec,))
    try:
        conn.execute(
            "INSERT INTO submit_keys (project, client_key, body_hash, created_at) VALUES (?, ?, ?, ?)",
            (project, key, digest, now),
        )
        conn.commit()
        return None
    except sqlite3.IntegrityError:
        pass
    row = conn.execute(
        "SELECT body_hash, task_ids, path, created_at FROM submit_keys WHERE project = ? AND client_key = ?",
        (project, key),
    ).fetchone()
    conn.commit()
    if row is None:  # expired between the DELETE and the INSERT race; try once more
        return claim(conn, project, key, digest, ttl_sec=ttl_sec, now=now)
    if row[0] != digest:
        raise SubmitKeyConflict("client_key was already used for a different submission")
    if row[1] is None:
        if now - float(row[3]) < PENDING_STALE_SEC:
            raise SubmitKeyConflict("a submission with this client_key is still in progress; retry shortly")
        conn.execute("UPDATE submit_keys SET created_at = ? WHERE project = ? AND client_key = ?", (now, project, key))
        conn.commit()
        return None
    return {"task_ids": json.loads(row[1]), "path": row[2]}


def complete(conn: sqlite3.Connection, project: str, key: str, task_ids: list[str], path: str) -> None:
    conn.execute(
        "UPDATE submit_keys SET task_ids = ?, path = ? WHERE project = ? AND client_key = ?",
        (json.dumps(list(task_ids)), path, project, key),
    )
    conn.commit()


def release(conn: sqlite3.Connection, project: str, key: str) -> None:
    """Drop a reservation whose submission failed, so a corrected retry can reuse the key."""
    conn.execute("DELETE FROM submit_keys WHERE project = ? AND client_key = ? AND task_ids IS NULL", (project, key))
    conn.commit()
