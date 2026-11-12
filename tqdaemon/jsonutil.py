"""Small JSON helpers shared across the package."""

from __future__ import annotations

import json
from typing import Any


def json_list(raw: str | None) -> list[Any]:
    """Decode a JSON array column; NULL or empty means an empty list."""
    return json.loads(raw or "[]")


def parse_json_obj(val: Any) -> dict[str, Any]:
    """Tolerant JSON object column: dict passes through; NULL, junk or non-objects -> {}."""
    if isinstance(val, dict):
        return val
    if not val or not isinstance(val, str):
        return {}
    try:
        parsed = json.loads(val)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def parse_json_list(val: Any) -> list[Any]:
    """Tolerant JSON array column: list/tuple pass through; NULL, junk or non-arrays -> []."""
    if isinstance(val, list):
        return val
    if isinstance(val, tuple):
        return list(val)
    if not val or not isinstance(val, str):
        return []
    try:
        parsed = json.loads(val)
    except (TypeError, ValueError):
        return []
    return parsed if isinstance(parsed, list) else []
