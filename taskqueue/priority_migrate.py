"""One-shot priority invert: higher number = sooner.

Remaps ``new = clamp(0, 100, 100 - old)`` in the DB and in registered
project operator_queue YAML files so relative urgency is preserved when
switching from ASC (lower-sooner) to DESC (higher-sooner) scheduling.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from taskqueue.config import load_projects
from taskqueue.db import get_config, open_db, set_config
from taskqueue._db_tasks import set_task_priority
from taskqueue.yaml_patch import patch_task_yaml

PRIORITY_HIGHER_WINS_KEY = "priority_higher_wins"
INVERT_BASE = 100

_PRIORITY_LINE = re.compile(r"^priority\s*:\s*(-?\d+)\s*(?:#.*)?$", re.MULTILINE)


def invert_priority(old: int) -> int:
    return max(0, min(INVERT_BASE, INVERT_BASE - int(old)))


def migrate_priority_invert(home: Path, *, dry_run: bool = False) -> dict[str, Any]:
    """Invert priorities in DB + YAML. Returns a summary dict."""
    db_path = home / "queue.db"
    conn = open_db(db_path)
    cfg = get_config(conn)
    if cfg.get(PRIORITY_HIGHER_WINS_KEY):
        conn.close()
        return {
            "ok": False,
            "error": f"already migrated ({PRIORITY_HIGHER_WINS_KEY}=1); refusing to double-invert",
            "dry_run": dry_run,
        }

    rows = conn.execute("SELECT project, task_id, priority FROM tasks").fetchall()
    db_changes: list[tuple[str, str, int, int]] = []
    for r in rows:
        old = int(r["priority"])
        new = invert_priority(old)
        if new != old:
            db_changes.append((r["project"], r["task_id"], old, new))

    yaml_changes: list[tuple[str, str, int, int]] = []
    projects = load_projects(home)
    for pname, proj in projects.items():
        qdir = proj.queue_dir
        if not qdir.is_dir():
            continue
        for path in sorted(qdir.glob("*.yaml")) + sorted(qdir.glob("*.yml")):
            text = path.read_text(encoding="utf-8")
            m = _PRIORITY_LINE.search(text)
            if not m:
                continue
            old = int(m.group(1))
            new = invert_priority(old)
            if new == old:
                continue
            yaml_changes.append((pname, str(path), old, new))

    if not dry_run:
        # F-102: commit DB changes first, then patch YAMLs, then write the done-marker.
        # If the process dies mid-YAML, the marker is absent so re-running will re-scan
        # and patch only the files whose on-disk value still needs inversion (idempotent
        # because patch_task_yaml only writes when the regex matches the old value).
        for project, task_id, _old, new in db_changes:
            set_task_priority(conn, project, task_id, new)
        conn.commit()
        for _pname, path_str, old, new in yaml_changes:
            # Re-check the file's current priority before patching so a re-run after
            # partial failure skips files already updated in a previous attempt.
            text = Path(path_str).read_text(encoding="utf-8")
            m = _PRIORITY_LINE.search(text)
            if m and int(m.group(1)) == old:
                patch_task_yaml(Path(path_str), {"priority": new})
        set_config(conn, PRIORITY_HIGHER_WINS_KEY, 1)
        conn.commit()

    conn.close()
    return {
        "ok": True,
        "dry_run": dry_run,
        "db_updated": len(db_changes),
        "yaml_updated": len(yaml_changes),
        "db_samples": db_changes[:10],
        "yaml_samples": yaml_changes[:10],
        "marker": PRIORITY_HIGHER_WINS_KEY,
    }
