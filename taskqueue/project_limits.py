"""Per-project limit rows in the config table (leaf module: db and _db_tasks both import it)."""

from __future__ import annotations

import json
import sqlite3

_PROJECT_LIMIT_FIELDS = (
    "max_tasks", "min_tasks", "max_cpu_cores", "max_ram_gib", "max_gpu_vram_gib",
    "max_gpu_hours_day",
)


def get_all_project_limits(conn: sqlite3.Connection) -> dict[str, dict[str, float]]:
    """Return {project: {field: value}} for all projects that have any limit set."""
    rows = conn.execute(
        "SELECT key, value FROM config WHERE key LIKE 'project_limit_%'"
    ).fetchall()
    result: dict[str, dict[str, float]] = {}
    for r in rows:
        # key = project_limit_<project>_<field>
        suffix = r["key"][len("project_limit_"):]
        for field in _PROJECT_LIMIT_FIELDS:
            if suffix.endswith(f"_{field}"):
                project = suffix[: -(len(field) + 1)]
                if project not in result:
                    result[project] = {f: 0.0 for f in _PROJECT_LIMIT_FIELDS}
                try:
                    result[project][field] = float(json.loads(r["value"]))
                except Exception:
                    pass
                break
    return result
