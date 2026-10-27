"""``tq archive``: take old finished tasks out of the live queue without losing them.

Each archived task (row + its runs) is appended to ``<home>/archive/<project>.jsonl``, its YAML
moves to ``<queue_dir>/archive/`` (a subdirectory, so YAML sync never globs it), and the DB rows
are deleted. Nothing is archived while running, and the YAML move happens before the DB delete so
a later sync cannot resurrect or delete-by-absence anything inconsistently.
"""

from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

DEFAULT_STATUSES = ("completed",)
_NEVER = frozenset({"running", "queued", "suspended"})


def archive_tasks(
    conn,
    projects: dict[str, Any],
    *,
    home: Path,
    older_than_days: float = 30.0,
    statuses: tuple[str, ...] = DEFAULT_STATUSES,
    only_projects: list[str] | None = None,
    dry_run: bool = False,
    now: str | None = None,
) -> dict[str, Any]:
    """Archive tasks in ``statuses`` whose ``updated_at`` is older than ``older_than_days``."""
    statuses = tuple(s for s in statuses if s not in _NEVER)
    if not statuses:
        return {"archived": 0, "tasks": [], "dry_run": dry_run}
    ref = datetime.fromisoformat(now) if now else datetime.now(UTC).replace(tzinfo=None)
    cutoff = (ref - timedelta(days=older_than_days)).isoformat()
    marks = ",".join("?" * len(statuses))
    sql = f"SELECT * FROM tasks WHERE status IN ({marks}) AND updated_at < ?"
    params: list[Any] = [*statuses, cutoff]
    if only_projects:
        sql += f" AND project IN ({','.join('?' * len(only_projects))})"
        params += only_projects
    rows = conn.execute(sql + " ORDER BY project, task_id", params).fetchall()
    done: list[dict[str, str]] = []
    for row in rows:
        proj, tid = row["project"], row["task_id"]
        src = row["yaml_source"] if "yaml_source" in row.keys() else None
        if not _movable(src):
            continue  # shares a YAML with other tasks: sync would resurrect it
        done.append({"project": proj, "task_id": tid})
        if dry_run:
            continue
        runs = [dict(r) for r in conn.execute("SELECT * FROM runs WHERE project = ? AND task_id = ?", (proj, tid))]
        out = home / "archive"
        out.mkdir(parents=True, exist_ok=True)
        with open(out / f"{proj}.jsonl", "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"task": dict(row), "runs": runs}, default=str) + "\n")
        _move_yaml(src, projects.get(proj))
        for r in runs:
            conn.execute("DELETE FROM resource_history WHERE run_id = ?", (r["run_id"],))
        conn.execute("DELETE FROM runs WHERE project = ? AND task_id = ?", (proj, tid))
        conn.execute("DELETE FROM tasks WHERE project = ? AND task_id = ?", (proj, tid))
        conn.commit()
    return {"archived": len(done), "tasks": done, "dry_run": dry_run}


def _movable(src: str | None) -> bool:
    """True when the task's YAML is absent/untracked or holds only this task."""
    if not src or not Path(src).is_file():
        return True
    try:
        from taskqueue.taskspec import parse_task_yaml_many

        return len(parse_task_yaml_many(Path(src))) <= 1
    except Exception:
        return True  # unparseable file holds no other live task


def _move_yaml(src: str | None, project: Any) -> None:
    """Move the task's YAML to ``<queue_dir>/archive/``."""
    if not src or project is None or not Path(src).is_file():
        return
    dest_dir = Path(project.queue_dir) / "archive"
    dest_dir.mkdir(parents=True, exist_ok=True)
    shutil.move(src, str(dest_dir / Path(src).name))
