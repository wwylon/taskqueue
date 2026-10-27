"""Tolerant column access for sqlite rows (columns added by later migrations)."""

from __future__ import annotations

import functools
import json
import math
from typing import Any, Mapping


def row_get(row: Any, key: str, default: Any = None) -> Any:
    """Value of ``row[key]``, or ``default`` if the column is absent from the row."""
    if key not in row.keys():
        return default
    return row[key]


def row_value(row: Any, key: str, default: Any = None) -> Any:
    """Like :func:`row_get` but also tolerant of dict rows, and NULL columns yield ``default``."""
    try:
        keys = row.keys() if hasattr(row, "keys") else None
        if keys is not None and key not in keys:
            return default
        val = row[key]
        return default if val is None else val
    except (KeyError, IndexError, TypeError):
        return default


@functools.lru_cache(maxsize=1024)
def _parse_resources_str(raw: str) -> dict[str, Any]:
    try:
        val = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return val if isinstance(val, dict) else {}


def row_resources(row: Any) -> dict[str, Any]:
    """Parse a task ``resources`` JSON column (F-127). Callers must not mutate the result."""
    raw = row_value(row, "resources", "{}")
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str):
        return {}
    return _parse_resources_str(raw)


def resources_dict_from_mapping(res_raw: Mapping[str, Any] | None) -> dict[str, Any]:
    """Normalize a YAML/JSON ``resources:`` mapping (shared by taskspec + admit)."""
    if not isinstance(res_raw, dict):
        return {
            "cpu_cores": 1,
            "ram_gib": 0.0,
            "gpu_vram_gib": 0.0,
            "locks": (),
        }
    locks = res_raw.get("locks") or []
    return {
        "cpu_cores": math.ceil(float(res_raw.get("cpu_cores", 1))),
        "ram_gib": float(res_raw.get("ram_gib", 0.0)),
        "gpu_vram_gib": float(res_raw.get("gpu_vram_gib", 0.0)),
        "locks": tuple(str(x) for x in locks),
    }


# Columns omitted from admit/occupancy scans (secrets / large unused blobs). F-128.
_ADMIT_TASK_EXCLUDE = frozenset({"env", "yaml_source", "args_override", "metadata"})
_admit_cols_cache: str | None = None


def admit_task_select_sql(conn: Any) -> str:
    """``SELECT … FROM tasks`` without secret/large columns for hot admit paths."""
    global _admit_cols_cache
    if _admit_cols_cache is None:
        names = [
            str(r[1])
            for r in conn.execute("PRAGMA table_info(tasks)").fetchall()
            if str(r[1]) not in _ADMIT_TASK_EXCLUDE
        ]
        _admit_cols_cache = ", ".join(names) if names else "*"
    return f"SELECT {_admit_cols_cache} FROM tasks"
