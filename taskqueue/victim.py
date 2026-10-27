"""Victim selection under resource pressure (scheduler_resources.md D7).

When the host runs out of memory something has to be stopped. The three stop
paths in the daemon each used to pick differently and none of them consulted
whether the task could actually be restarted:

  * hard-RAM kill        -> biggest RSS
  * evict on limit drop  -> lowest priority
  * excess max_running   -> most recently started

That means a non-idempotent job at 95% progress with no checkpoint could be
killed while a trivially restartable one sat beside it. This module scores the
*cost of killing* each candidate so all three paths agree, and so that
restartability actually counts.

Lowest score is chosen.
"""

from __future__ import annotations

import math
import sqlite3
from typing import Any

# Relief dominates so a critical host still recovers: freeing memory is the
# point of the exercise. Among candidates that relieve comparable pressure,
# restartability and lost work decide.
DEFAULT_WEIGHTS = {
    "relief": 2.0,      # how much pressure this actually removes
    "progress": 1.5,    # work destroyed by killing it
    "restart": 1.0,     # cheap to kill if it can just be re-run
    "priority": 1.0,    # operator-declared importance
    "elapsed": 0.5,     # long uncheckpointed runs are expensive to redo
}

# Resume-eligible tasks are cheaper to kill than one-shot tasks but more
# expensive than fully idempotent ones: resuming still costs checkpoint reload.
_RESTART_IDEMPOTENT = 1.0
_RESTART_RESUMABLE = 0.6


def _restartability(task_row: sqlite3.Row | None) -> float:
    """How safe is it to stop this task and run it again later?"""
    if task_row is None:
        return 0.0
    try:
        if int(task_row["idempotent"] or 0):
            return _RESTART_IDEMPOTENT
        # A task declaring resume_args knows how to pick up from a checkpoint.
        if task_row["resume_args"] and task_row["resume_args"] != "[]":
            return _RESTART_RESUMABLE
    except (KeyError, IndexError, TypeError):
        pass
    return 0.0


def score_victim(
    *,
    freed_gib: float,
    needed_gib: float,
    progress_pct: float | None,
    priority: int | None,
    elapsed_sec: float | None,
    restartability: float,
    weights: dict[str, float] | None = None,
) -> float:
    """Cost of killing one task. Lower means cheaper to kill."""
    w = {**DEFAULT_WEIGHTS, **(weights or {})}
    # G4: log1p relief so a task freeing 10× more than needed still gets
    # meaningful credit over one that just barely covers the need.
    if needed_gib > 0:
        relief = math.log1p(freed_gib / needed_gib)
    elif freed_gib > 0:
        relief = 1.0
    else:
        relief = 0.0
    return (
        w["progress"] * (float(progress_pct or 0.0) / 100.0)
        + w["priority"] * (float(priority or 0) / 100.0)
        + w["elapsed"] * min(1.0, float(elapsed_sec or 0.0) / 3600.0)
        - w["restart"] * restartability
        - w["relief"] * relief
    )


def pick_victim(
    conn: sqlite3.Connection,
    candidates: list[tuple[tuple, Any]],
    *,
    needed_gib: float = 0.0,
    resource: str = "ram",
    weights: dict[str, float] | None = None,
) -> tuple[tuple | None, list[tuple[tuple, float]]]:
    """Choose which running task to stop.

    *candidates* is [((project, task_id), ActiveRun), ...]. *resource* selects
    which measurement counts as relief ("ram" or "vram").

    Returns (chosen_key, scored) where *scored* is every candidate with its
    score, ascending — the caller should log the runner-up, since without it an
    operator cannot tell a good decision from a lucky one.
    """
    from taskqueue.db import get_last_run_for_task, get_task

    scored: list[tuple[tuple, float]] = []
    for key, run in candidates:
        project, task_id = key
        try:
            task_row = get_task(conn, project, task_id)
        except Exception:
            task_row = None

        if resource == "vram":
            freed = float(getattr(run, "current_vram_gib", 0.0) or 0.0)
        else:
            freed = float(getattr(run, "current_ram_gib", 0.0) or 0.0)

        try:
            elapsed = float(run.elapsed_sec())
        except Exception:
            elapsed = 0.0

        scored.append((
            key,
            score_victim(
                freed_gib=freed,
                needed_gib=needed_gib,
                progress_pct=getattr(run, "progress_pct", None),
                priority=(task_row["priority"] if task_row is not None else None),
                elapsed_sec=elapsed,
                restartability=_restartability(task_row),
                weights=weights,
            ),
        ))

    if not scored:
        return None, []
    scored.sort(key=lambda kv: kv[1])
    return scored[0][0], scored
