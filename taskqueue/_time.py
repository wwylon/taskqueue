"""Canonical timestamp helper shared across taskqueue modules.

Use ``_now()`` instead of inlining ``datetime.now(UTC).isoformat()`` so that
all ISO timestamps in the DB have consistent microsecond-precision format.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime


def _now() -> str:
    """Return the current UTC time as an ISO 8601 string with microseconds."""
    return datetime.now(UTC).isoformat()


def _ts() -> str:
    """Return the current local time as HH:MM:SS for log prefixes."""
    return time.strftime("%H:%M:%S")


def parse_iso(ts: str) -> datetime:
    """Parse an ISO-8601 timestamp (a trailing 'Z' is accepted) into a datetime."""
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))
