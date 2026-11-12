"""Tolerant column access for sqlite rows (columns added by later migrations)."""

from __future__ import annotations

import functools
import math
from collections.abc import Mapping
from typing import Any

_MISSING = (KeyError, IndexError, TypeError)  # dict / sqlite3.Row / non-mapping


def has_col(row: Any, key: str) -> bool:
    """Whether ``row`` (sqlite3.Row or dict) has column ``key`` — O(1), unlike ``key in row.keys()``,
    which builds the full column list on every call (hot in admission loops)."""
    try:
        row[key]
    except _MISSING:
        return False
    return True


def row_get(row: Any, key: str, default: Any = None) -> Any:
    """Value of ``row[key]``, or ``default`` if the column is absent from the row."""
    try:
        return row[key]
    except _MISSING:
        return default


def row_value(row: Any, key: str, default: Any = None) -> Any:
    """Like :func:`row_get` but also tolerant of dict rows, and NULL columns yield ``default``."""
    try:
        val = row[key]
    except _MISSING:
        return default
    return default if val is None else val


@functools.lru_cache(maxsize=1024)
def _parse_resources_str(raw: str) -> dict[str, Any]:
    from tqdaemon.jsonutil import parse_json_obj

    return parse_json_obj(raw or "{}")


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
