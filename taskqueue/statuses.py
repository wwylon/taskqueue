"""Terminal / retryable / settle / finalize task-status policy (F-201).

DB mutations still live in ``_db_tasks``; wait/notify/CLI import from here so
status sets do not drift.
"""

from __future__ import annotations

from taskqueue._db_tasks import _TERMINAL_STATUSES, RETRYABLE_STATUSES

TERMINAL_STATUSES = _TERMINAL_STATUSES

# F-154: statuses a worker may send on finalize (plus suspended for coop stop).
FINALIZE_STATUSES = frozenset({"completed", "failed", "interrupted", "stopped", "cancelled", "suspended"})

# Will not change without an operator/agent action (wait returns).
# ``stopped`` is auto-restartable — exclude it from settled (F-201 / F-192).
# ``suspended`` still settles wait (needs explicit resume, not silent restart).
STALLED_STATUSES = frozenset({"disabled"})
SETTLED_STATUSES = frozenset(s for s in TERMINAL_STATUSES if s != "stopped") | STALLED_STATUSES

# Peers that cannot satisfy depends_on (UI paints these as dead).
# ``stopped``/``interrupted`` are auto-restartable — they must NOT cascade-cancel
# dependents (see CANCEL_TRIGGER_STATUSES).
DEAD_DEP_STATUSES = frozenset({"failed", "stopped", "interrupted", "disabled", "cancelled"})
# Deliberately excludes stopped/interrupted (auto-restart) and disabled (operator).
CANCEL_TRIGGER_STATUSES = frozenset({"failed", "cancelled"})

# Auto-restartable without ``tq retry`` (admit treats like queued).
AUTO_RESTARTABLE_STATUSES = frozenset({"stopped"})

__all__ = [
    "TERMINAL_STATUSES",
    "RETRYABLE_STATUSES",
    "FINALIZE_STATUSES",
    "STALLED_STATUSES",
    "SETTLED_STATUSES",
    "DEAD_DEP_STATUSES",
    "CANCEL_TRIGGER_STATUSES",
    "AUTO_RESTARTABLE_STATUSES",
]
