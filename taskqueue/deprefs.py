"""Dependency reference parsing shared by the scheduler, queries and DB layers."""

from __future__ import annotations

from typing import Any

from taskqueue.jsonutil import json_list


def resolve_dep_ref(owner_project: str, dep_ref: str) -> tuple[str, str, str]:
    """Normalize a depends_on entry to (dep_project, dep_task_id, label).

    Bare ids resolve in ``owner_project``; ``project/task_id`` is cross-project.
    Label matches scheduler messaging (cross-project keeps the slash form).
    """
    s = str(dep_ref)
    if "/" in s:
        dep_proj, dep_id = s.split("/", 1)
        return dep_proj, dep_id, s
    return owner_project, s, s


def row_dep_refs(row: Any, col: str, owner_project: str) -> list[tuple[str, str, str]]:
    """Resolved ``(project, task_id, label)`` refs of a ``depends_on``-style JSON column.

    A missing column, NULL, corrupt JSON or a non-list value yields ``[]`` so one bad row
    can never abort a scheduler pass.
    """
    try:
        if col not in row.keys():
            return []
        refs = json_list(row[col])
    except (ValueError, TypeError, KeyError, IndexError):
        return []
    if not isinstance(refs, list):
        return []
    return [resolve_dep_ref(owner_project, str(r)) for r in refs]
