"""`tq clone`: start a new task from an existing one (a template) by copying its YAML.

The source task's YAML document is copied to ``<queue_dir>/<new_id>.yaml`` with the new
``task_id``; ``--set key=value`` overrides top-level fields (values parsed as YAML). The file
is validated with the normal parser before it is written, then the next sync picks it up.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any


def clone_task_yaml(
    conn: sqlite3.Connection,
    projects: dict,
    project: str,
    task_id: str,
    new_id: str,
    overrides: dict[str, Any] | None = None,
) -> Path:
    """Write the clone and return its path. Raises ValueError with a readable message."""
    import yaml

    from taskqueue.db import get_task
    from taskqueue.taskspec import validate_task_spec

    row = get_task(conn, project, task_id)
    if row is None:
        raise ValueError(f"task not found: {project}/{task_id}")
    proj = projects.get(project)
    if proj is None:
        raise ValueError(f"project '{project}' is not registered")
    src = Path(str(row["yaml_source"] or ""))
    if not src.is_file():
        raise ValueError(f"source YAML not found: {src}")
    docs = [d for d in yaml.safe_load_all(src.read_text(encoding="utf-8")) if d]
    doc = next((d for d in docs if str(d.get("task_id") or d.get("id")) == task_id), None)
    if doc is None:
        if len(docs) == 1 and docs[0].get("matrix"):
            raise ValueError("source is a matrix: clone the matrix file by hand instead")
        raise ValueError(f"no document with task_id {task_id!r} in {src.name}")
    new = {k: v for k, v in doc.items() if k not in ("id", "matrix")}
    new["task_id"] = new_id
    new.update(overrides or {})
    dest = Path(proj.queue_dir) / f"{new_id}.yaml"
    if dest.exists():
        raise ValueError(f"{dest} already exists")
    validate_task_spec(new, dest)
    if get_task(conn, project, new_id) is not None:
        raise ValueError(f"task {project}/{new_id} already exists")
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".tmp")
    tmp.write_text(yaml.safe_dump(new, sort_keys=False, allow_unicode=True), encoding="utf-8")
    tmp.replace(dest)
    return dest


def parse_set_args(pairs: list[str]) -> dict[str, Any]:
    """`key=value` pairs; values are YAML scalars/lists (e.g. priority=80, args=[--lr, 0.1])."""
    import yaml

    out: dict[str, Any] = {}
    for p in pairs:
        key, sep, val = p.partition("=")
        if not sep or not key.strip():
            raise ValueError(f"--set expects key=value, got {p!r}")
        out[key.strip()] = yaml.safe_load(val) if val.strip() else ""
    return out
