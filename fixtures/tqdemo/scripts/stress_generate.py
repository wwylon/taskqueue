"""Populate the live daemon DB with a large number of tasks across many projects.

Creates:
  - 40 fake projects  (stress-proj-000 … stress-proj-039)
  - 25 tasks each     (1 000 tasks total), mix of finished statuses
  - All inserted with enabled=0 so the daemon never tries to run them

All created entries are prefixed "stress-" so stress_cleanup.py can find them.
Run this as a tqdemo task; the daemon sees the new rows immediately.
"""

from __future__ import annotations

import json
import random
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

_REPO_ROOT = Path(__file__).parent.parent.parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from taskqueue.config import db_path, state_home
from taskqueue.db import open_db

# Realistic historical snapshot. All enabled=0 — the daemon never runs these.
# "running" is intentionally excluded: the daemon's zombie detector flips any
# running row with no live process to interrupted within one scheduler tick.
_STATUSES = [
    ("completed",   0, 55),
    ("failed",      0, 20),
    ("interrupted", 0, 15),
    ("stopped",     0, 10),
]

_COMMANDS = ["python", "python", "bash", "uv"]
_TAGS = ["stress", "benchmark", "load-test", "perf", "synthetic"]


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _past(minutes: int) -> str:
    return (datetime.now(UTC) - timedelta(minutes=minutes)).isoformat()


def _weighted() -> tuple[str, int]:
    pool = [(s, e) for s, e, w in _STATUSES for _ in range(w)]
    return random.choice(pool)


def _resources() -> str:
    return json.dumps({
        "cpu_cores":    random.choice([1, 2, 4]),
        "ram_gib":      round(random.choice([0.25, 0.5, 1.0, 2.0]), 2),
        "gpu_vram_gib": random.choice([0.0, 0.0, 0.0, 4.0, 8.0]),
        "locks": [],
    })


def main() -> int:
    home = state_home()
    conn = open_db(db_path(home))

    n_projects = 40
    tasks_per_project = 25
    total = n_projects * tasks_per_project
    inserted = 0

    random.seed(42)

    for proj_i in range(n_projects):
        proj = f"stress-proj-{proj_i:03d}"
        for task_i in range(tasks_per_project):
            task_id = f"stress-task-{task_i:03d}"
            status, enabled = _weighted()
            prio = random.randint(10, 90)
            tag = random.choice(_TAGS)
            cmd = random.choice(_COMMANDS)
            res = _resources()
            desc = f"synthetic load task {proj}/{task_id}"
            enqueued = _past(random.randint(10, 1440))  # 10 min – 24 h ago

            conn.execute(
                """
                INSERT OR IGNORE INTO tasks
                    (project, task_id, priority, status, command, args,
                     resources, description, tags, enabled,
                     enqueued_at, updated_at, dispatch)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'local')
                """,
                (proj, task_id, prio, status, cmd,
                 json.dumps(["--stress"]),
                 res, desc, json.dumps([tag]),
                 enabled, enqueued, enqueued),
            )
            inserted += 1

            if inserted % 100 == 0:
                conn.commit()
                pct = 100.0 * inserted / total
                print(f"inserted {inserted}/{total} tasks ({pct:.1f}%)", flush=True)

    conn.commit()
    conn.close()

    print(f"(100.0%)", flush=True)
    print(f"done — {inserted} tasks across {n_projects} stress projects (all enabled=0)", flush=True)
    print(f"open http://127.0.0.1:8765 — use filter 'all' status to see them all", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
