"""Shared UTC datetime helpers for the analytics / alerting / forecast modules."""

from __future__ import annotations

from datetime import datetime, timezone

from taskqueue.db import parse_iso


def now_utc(now: datetime | None = None) -> datetime:
    """Current UTC time, or ``now`` coerced to aware UTC (naive is assumed UTC)."""
    if now is None:
        return datetime.now(timezone.utc)
    if now.tzinfo is None:
        return now.replace(tzinfo=timezone.utc)
    return now


def iso_utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def parse_utc(value: str | None) -> datetime | None:
    """Parse a stored ISO timestamp to aware UTC; None for empty or malformed input."""
    if not value:
        return None
    try:
        dt = parse_iso(str(value))
    except Exception:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def within_window(last: str | None, window_sec: int, now: datetime) -> bool:
    """True when stored ISO time ``last`` is less than ``window_sec`` seconds before ``now``.

    A non-positive window, or a missing or malformed timestamp, is never inside the window.
    """
    if window_sec <= 0:
        return False
    last_dt = parse_utc(last)
    if last_dt is None:
        return False
    return (now - last_dt).total_seconds() < window_sec
