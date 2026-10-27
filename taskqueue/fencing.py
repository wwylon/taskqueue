"""Session / lease fencing and execution-semantics helpers (gpt.md P4)."""

from __future__ import annotations

from taskqueue.models import STATUS_QUEUED, STATUS_UNKNOWN

SEM_IDEMPOTENT = "idempotent"
SEM_CHECKPOINTABLE = "checkpointable"
SEM_NON_IDEMPOTENT = "non_idempotent"
SEM_AT_MOST_ONCE = "at_most_once"

VALID_EXECUTION_SEMANTICS = frozenset({
    SEM_IDEMPOTENT,
    SEM_CHECKPOINTABLE,
    SEM_NON_IDEMPOTENT,
    SEM_AT_MOST_ONCE,
})


def resolve_execution_semantics(
    raw: str | None = None,
    *,
    idempotent: bool = False,
) -> str:
    """Map YAML fields to a canonical execution_semantics value."""
    s = (raw or "").strip().lower()
    if s:
        if s not in VALID_EXECUTION_SEMANTICS:
            raise ValueError(
                f"execution_semantics must be one of "
                f"{sorted(VALID_EXECUTION_SEMANTICS)}, got {raw!r}"
            )
        return s
    return SEM_IDEMPOTENT if idempotent else SEM_NON_IDEMPOTENT


def semantics_implies_idempotent(semantics: str) -> bool:
    return semantics in (SEM_IDEMPOTENT, SEM_CHECKPOINTABLE)


def may_auto_requeue(semantics: str) -> bool:
    """True when a lost/hung attempt may be re-queued without operator action."""
    return semantics_implies_idempotent(semantics)


def lost_attempt_task_status(semantics: str) -> str:
    """Task status after lease reclaim / hung escalation for a lost attempt."""
    return STATUS_QUEUED if may_auto_requeue(semantics) else STATUS_UNKNOWN


def session_id_mismatch(stored: str | None, provided: str | None) -> bool:
    """True when the worker row/run has a session and the caller does not match.

    Empty stored session = legacy pre-P4 row; allow any caller.
    """
    if not stored:
        return False
    return (provided or "") != stored


def should_stop_for_lost_lease(
    now: float,
    lease_expires_at: float | None,
    *,
    heartbeat_ok: bool,
    margin_sec: float | None = None,
) -> bool:
    """Worker-side lease fence (R-012): stop before expiry if master is unreachable.

    When the last heartbeat succeeded, the master still renews leases — do not stop.
    When heartbeats fail, stop at ``lease_expires_at - margin`` so the child exits
    before the master reclaims the attempt.
    """
    if heartbeat_ok or lease_expires_at is None:
        return False
    if margin_sec is None:
        from taskqueue.config import LEASE_SAFETY_MARGIN_SEC

        margin_sec = float(LEASE_SAFETY_MARGIN_SEC)
    return now >= (float(lease_expires_at) - float(margin_sec))
