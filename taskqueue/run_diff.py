"""Field-by-field comparison of two runs (``tq diff``)."""
from __future__ import annotations

import sqlite3
from typing import Any

from taskqueue._db_runs import get_run
from taskqueue._time import parse_iso

# Compared columns, in display order. Identity / bookkeeping columns are left out.
_FIELDS = (
    "status", "exit_code", "last_error_code", "error_category", "stopped_by",
    "progress_pct", "peak_ram_gib", "peak_vram_gib", "peak_cpu_pct", "worker_id",
)


def _duration(row: sqlite3.Row) -> float | None:
    try:
        if not row["started_at"] or not row["finished_at"]:
            return None
        return (parse_iso(row["finished_at"]) - parse_iso(row["started_at"])).total_seconds()
    except (TypeError, ValueError):
        return None


def _values(row: sqlite3.Row) -> dict[str, Any]:
    out: dict[str, Any] = {f: row[f] for f in _FIELDS}
    out["duration_sec"] = _duration(row)
    return out


def diff_runs(conn: sqlite3.Connection, run_a: int, run_b: int) -> dict[str, Any]:
    """Return ``{a, b, changes}``; each change has ``field``, ``a``, ``b`` and a numeric ``delta`` when both are numbers."""
    rows = []
    for rid in (run_a, run_b):
        r = get_run(conn, int(rid))
        if r is None:
            raise LookupError(f"run {rid} not found")
        rows.append(r)
    va, vb = _values(rows[0]), _values(rows[1])
    changes = []
    for field in (*_FIELDS, "duration_sec"):
        a, b = va[field], vb[field]
        if a == b:
            continue
        ch: dict[str, Any] = {"field": field, "a": a, "b": b}
        if isinstance(a, (int, float)) and isinstance(b, (int, float)):
            ch["delta"] = round(b - a, 6)
        changes.append(ch)

    def head(r: sqlite3.Row) -> dict[str, Any]:
        return {k: r[k] for k in ("run_id", "project", "task_id", "started_at", "finished_at")}

    return {"a": head(rows[0]), "b": head(rows[1]), "changes": changes}


def render_diff(d: dict[str, Any]) -> str:
    a, b = d["a"], d["b"]
    lines = [f"run {a['run_id']} ({a['project']}/{a['task_id']})  vs  run {b['run_id']} ({b['project']}/{b['task_id']})"]
    if not d["changes"]:
        lines.append("(no differences)")
    for c in d["changes"]:
        delta = f"  ({c['delta']:+g})" if "delta" in c else ""
        lines.append(f"  {c['field']:<16} {c['a']!s:>12}  ->  {c['b']!s:<12}{delta}")
    return "\n".join(lines)
