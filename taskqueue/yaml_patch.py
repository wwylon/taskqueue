"""Safe YAML field patches for operator_queue task files."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from taskqueue.fsutil import atomic_write_text

ALLOWED_PATCH_KEYS = frozenset({
    "priority",
    "enabled",
    "wall_clock_cap_sec",
    "max_attempts",
})

_INT_KEYS = frozenset({"priority", "wall_clock_cap_sec", "max_attempts"})

_MATRIX_KEY = re.compile(r"^matrix\s*:", re.MULTILINE)
_BASE_ID = re.compile(r"^task_id\s*:\s*[\"']?([^\"'#\s]+)", re.MULTILINE)
_DOC_SEP = re.compile(r"^---[ \t]*\r?$", re.MULTILINE)


def _target_span(text: str, task_id: str | None) -> tuple[int, int]:
    """Return the (start, end) of the YAML document holding ``task_id``.

    A single-document file (or no ``task_id``) is the whole text. In a
    multi-document file only the document whose top-level ``task_id:`` matches
    is edited; no match raises ``ValueError`` so other documents are never touched.
    """
    cuts = [m.span() for m in _DOC_SEP.finditer(text)]
    if not task_id:
        return 0, len(text)
    if not cuts:
        # A matrix file expands to many tasks sharing one document: patching it for a single
        # variant id would silently change every variant, so only the base task_id may edit it.
        if _MATRIX_KEY.search(text):
            m = _BASE_ID.search(text)
            if not m or m.group(1) != task_id:
                raise ValueError(
                    f"task_id {task_id!r} is a matrix variant; patch the base task "
                    f"{m.group(1) if m else '(file stem)'!r} to change all variants"
                )
        return 0, len(text)
    starts = [0] + [end for _, end in cuts]
    ends = [begin for begin, _ in cuts] + [len(text)]
    pat = re.compile(
        rf"^task_id\s*:\s*[\"']?{re.escape(task_id)}[\"']?\s*(#.*)?$", re.MULTILINE
    )
    for start, end in zip(starts, ends):
        if pat.search(text, start, end):
            return start, end
    raise ValueError(f"task_id {task_id!r} not found as a document in this file")


def patch_task_yaml(path: Path, updates: dict[str, Any], task_id: str | None = None) -> list[str]:
    """Apply allowed key updates to a YAML file. Returns list of applied keys.

    ``task_id`` selects the document in a multi-document file. The write is atomic.
    """
    unknown = sorted(set(updates) - ALLOWED_PATCH_KEYS)
    if unknown:
        raise ValueError(f"unsupported patch keys: {', '.join(unknown)}")
    full = path.read_text(encoding="utf-8")
    lo, hi = _target_span(full, task_id)
    text = full[lo:hi]
    applied: list[str] = []
    for key, value in updates.items():
        if isinstance(value, str) and ("\n" in value or "\r" in value):
            raise ValueError(f"invalid {key}: newlines not allowed")
        if key == "enabled":
            if not isinstance(value, bool):
                raise ValueError("enabled must be a boolean")
            val_str = "true" if value else "false"
        elif key in _INT_KEYS:
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError(f"{key} must be an integer")
            val_str = str(value)
        else:
            val_str = str(value)
        pattern = re.compile(rf"^{re.escape(key)}\s*:\s*.*$", re.MULTILINE)
        if pattern.search(text):
            text = pattern.sub(f"{key}: {val_str}", text, count=1)
        else:
            if not text.endswith("\n"):
                text += "\n"
            text += f"{key}: {val_str}\n"
        applied.append(key)
    atomic_write_text(path, full[:lo] + text + full[hi:])
    return applied
