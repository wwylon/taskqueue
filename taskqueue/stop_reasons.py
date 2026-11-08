"""Machine ``stop_reason`` codes and what they imply (one definition for local + remote finalize).

Operator-facing labels live in ``stop_summary``; this module only holds policy sets.
"""

from __future__ import annotations

# tq forced the stop (cap, pressure, frozen, evict): an exit 0 afterwards is not a finished run.
FORCED_STOP_REASONS = frozenset({"wall_clock_cap", "ram_hard", "vram_hard", "frozen", "frozen_restart", "evicted"})

# Every stop tq itself initiated — finalize records ``stopped_by = "tq"``.
TQ_STOP_REASONS = FORCED_STOP_REASONS | {"deps_invalidated", "scavenger_preempt"}

# Host memory pressure victims (land ``stopped``, auto-restart).
PRESSURE_STOP_REASONS = frozenset({"ram_hard", "vram_hard"})


def stopped_by_for(stop_reason: str | None) -> str | None:
    """``"tq"`` for a tq-initiated stop, ``"host-sleep"`` for a sleep drain, else None."""
    if stop_reason in TQ_STOP_REASONS:
        return "tq"
    if stop_reason == "host-sleep":
        return "host-sleep"
    return None
