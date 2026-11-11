"""F-251: shared YAML submit (HTTP / MCP / CLI offline)."""

from __future__ import annotations

import os
import time
import uuid
from pathlib import Path
from typing import Any


class SubmitError(Exception):
    """Validation / size / project errors with optional HTTP status."""

    def __init__(self, message: str, *, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


def submit_yaml_to_queue(
    conn: Any,
    proj: Any,
    yaml_text: str,
    *,
    filename: str | None = None,
    client_key: str | None = None,
    via: str = "api",
    operator: str | None = None,
    client: str | None = None,
    max_bytes: int = 1024 * 1024,
) -> dict[str, Any]:
    """Validate YAML, write into ``proj.queue_dir``, upsert tasks.

    Returns ``{ok, task_ids, path, ...}``. Raises ``SubmitError`` on failure.
    Caller owns the connection commit.
    """
    from tqdaemon.db import get_config, parse_task_yaml_many, stamp_provenance, upsert_task

    if not isinstance(yaml_text, str) or not yaml_text.strip():
        raise SubmitError("project and yaml_text required")
    if len(yaml_text.encode("utf-8")) > max_bytes:
        raise SubmitError("YAML too large (max 1 MiB)", status=413)

    explicit_name = (filename or "").strip()
    if explicit_name:
        fname = Path(explicit_name).name
        if not fname.endswith((".yaml", ".yml")):
            fname = fname + ".yaml"
        if ".." in fname or "/" in fname or "\\" in fname:
            raise SubmitError("invalid filename")
    else:
        fname = f"submitted_{int(time.time() * 1000)}_{uuid.uuid4().hex[:6]}.yaml"

    cfg = get_config(conn)
    allow_remote = bool(cfg.get("notify_allow_remote"))
    project_name = proj.name

    if client_key is not None:
        from tqdaemon import submit_keys

        bad = submit_keys.key_error(client_key)
        if bad:
            raise SubmitError(bad)
        try:
            earlier = submit_keys.claim(
                conn,
                project_name,
                client_key,
                submit_keys.body_hash(yaml_text, explicit_name),
                ttl_sec=float(cfg.get("submit_key_ttl_sec", submit_keys.DEFAULT_TTL_SEC)),
            )
        except submit_keys.SubmitKeyConflict as exc:
            raise SubmitError(str(exc), status=409) from exc
        if earlier is not None:
            return {
                "ok": True,
                "task_ids": earlier["task_ids"],
                "path": earlier["path"],
                "deduplicated": True,
            }

    dest = proj.queue_dir / fname
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(f".{fname}.{uuid.uuid4().hex[:8]}.tmp")
    try:
        tmp.write_text(yaml_text, encoding="utf-8")
        parse_task_yaml_many(tmp, proj, allow_remote_notify=allow_remote)
        os.replace(tmp, dest)
        tasks = parse_task_yaml_many(dest, proj, allow_remote_notify=allow_remote)
    except SubmitError:
        raise
    except Exception as exc:
        tmp.unlink(missing_ok=True)
        if client_key is not None:
            from tqdaemon import submit_keys

            submit_keys.release(conn, project_name, client_key)
        raise SubmitError(str(exc)) from exc

    ids = []
    for task in tasks:
        upsert_task(conn, task)
        ids.append(task.task_id)
    stamp_provenance(conn, project_name, ids, via, operator, client)
    if client_key is not None:
        from tqdaemon import submit_keys

        submit_keys.complete(conn, project_name, client_key, ids, str(dest))
    return {"ok": True, "task_ids": ids, "path": str(dest)}
