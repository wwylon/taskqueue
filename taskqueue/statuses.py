"""Terminal / retryable task-status policy (single import surface).

DB mutations still live in ``_db_tasks``; wait/notify/CLI import from here so
status sets do not drift.
"""
from __future__ import annotations

from taskqueue._db_tasks import RETRYABLE_STATUSES, _TERMINAL_STATUSES

TERMINAL_STATUSES = _TERMINAL_STATUSES

__all__ = ["TERMINAL_STATUSES", "RETRYABLE_STATUSES"]
