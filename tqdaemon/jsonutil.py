"""Small JSON helpers shared across the package."""

from __future__ import annotations

import json
from typing import Any


def json_list(raw: str | None) -> list[Any]:
    """Decode a JSON array column; NULL or empty means an empty list."""
    return json.loads(raw or "[]")
