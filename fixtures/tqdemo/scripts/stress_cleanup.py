"""Remove all stress-* tasks and projects from the live daemon DB.

Deletes every row in `tasks` and `runs` where project LIKE 'stress-%'.
Safe to run multiple times (idempotent). Does not touch non-stress data.
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).parent.parent.parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from taskqueue.config import db_path, state_home
from taskqueue.db import open_db


def main() -> int:
    home = state_home()
    conn = open_db(db_path(home))

    task_count = conn.execute(
        "SELECT COUNT(*) FROM tasks WHERE project LIKE 'stress-%'"
    ).fetchone()[0]
    run_count = conn.execute(
        "SELECT COUNT(*) FROM runs WHERE project LIKE 'stress-%'"
    ).fetchone()[0]

    print(f"found {task_count} tasks and {run_count} runs to remove (50.0%)", flush=True)

    conn.execute("DELETE FROM runs  WHERE project LIKE 'stress-%'")
    conn.execute("DELETE FROM tasks WHERE project LIKE 'stress-%'")
    conn.commit()
    conn.close()

    print(f"(100.0%)", flush=True)
    print(f"removed {task_count} tasks, {run_count} runs — stress projects gone", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
