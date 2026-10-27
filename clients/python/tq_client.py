"""Minimal helpers for taskqueue task scripts (cooperative control + progress).

Zero external dependencies — copy this single file into any project and import it:

    import tq_client as tq

    # Report progress
    tq.progress(50, "halfway")

    # Progress context manager over an iterable
    items = list(range(100))
    for item in tq.progress_loop(items, msg="processing"):
        ...  # tq.maybe_suspend() called automatically each iteration

    # Manual suspend check
    tq.maybe_suspend()

    # Checkpoint cadence: write when work units OR ~10 min due; reset both after save
    cadence = tq.CheckpointCadence(every_n_units=1000)  # every_sec=600 default
    if cadence.note_work():
        tq.save_checkpoint("run.json", {"step": step})
        cadence.reset()
    state = tq.load_checkpoint("run.json") or {}
"""

from __future__ import annotations

import json
import os
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, Generator, Iterable, TypeVar

# Match taskqueue.models.EXIT_SUSPENDED
EXIT_SUSPENDED = 75

_T = TypeVar("_T")


def progress(pct: float, msg: str = "") -> None:
    """Print a progress line the daemon can parse: (N%)."""
    line = f"{msg} ({pct:.1f}%)" if msg else f"({pct:.1f}%)"
    print(line, flush=True)


def metric(**fields: Any) -> None:
    """Emit structured metrics the daemon merges into ``runs.metrics_json`` (last value per key wins).

    Example: ``tq.metric(epoch=3, loss=0.124)`` prints ``TQ_METRIC {"epoch": 3, "loss": 0.124}``.
    """
    if fields:
        print("TQ_METRIC " + json.dumps(fields, default=str), flush=True)


def control_file() -> Path | None:
    raw = os.environ.get("TQ_CONTROL_FILE")
    return Path(raw) if raw else None


def poll_control() -> str | None:
    """Return control action (`suspend`, …) or None if idle/missing."""
    path = control_file()
    if path is None or not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if isinstance(data, dict):
        action = data.get("action")
        return str(action) if action else None
    return None


def request_suspend_exit() -> None:
    """Exit with EXIT_SUSPENDED after optional message."""
    print(f"cooperative suspend requested; exiting {EXIT_SUSPENDED}", flush=True)
    raise SystemExit(EXIT_SUSPENDED)


def save_checkpoint(path: str | Path, payload: dict) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(payload, indent=2))
        fh.flush()
        os.fsync(fh.fileno())  # durable before the rename: a crash must not leave a torn checkpoint
    os.replace(tmp, p)


def load_checkpoint(path: str | Path) -> dict | None:
    p = Path(path)
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


class CheckpointCadence:
    """Dual-trigger checkpoint gate: work units OR wall time since last reset.

    Call ``save_checkpoint`` then ``reset()`` when due. Force-saves (suspend /
    SIGTERM) always write, then ``reset()`` — they do not go through ``due()``.
    """

    def __init__(
        self,
        every_n_units: int,
        *,
        every_sec: float = 600.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if every_n_units < 1:
            raise ValueError("every_n_units must be >= 1")
        if every_sec <= 0:
            raise ValueError("every_sec must be > 0")
        self.every_n_units = every_n_units
        self.every_sec = every_sec
        self._clock = clock
        self._units = 0
        self._t0 = clock()

    def note_work(self, n: int = 1) -> bool:
        """Add work units and return whether a checkpoint is due (does not reset)."""
        if n < 0:
            raise ValueError("n must be >= 0")
        self._units += n
        return self.due()

    def due(self) -> bool:
        """True if work or time threshold is met since last ``reset()``."""
        if self._units >= self.every_n_units:
            return True
        return (self._clock() - self._t0) >= self.every_sec

    def reset(self) -> None:
        """Clear work counter and restart the timer (call after a successful save)."""
        self._units = 0
        self._t0 = self._clock()


def maybe_suspend() -> None:
    """If control file asks for suspend, exit 75."""
    if poll_control() == "suspend":
        request_suspend_exit()


def progress_loop(
    iterable: Iterable[_T],
    *,
    msg: str = "",
    total: int | None = None,
    suspend_check: bool = True,
    min_interval_sec: float = 1.0,
) -> Generator[_T, None, None]:
    """W8.2: Yield items while reporting progress and checking for cooperative suspend.

    Progress lines and the control-file read happen at most every ``min_interval_sec``
    (plus the first item), not per item. ``total`` defaults to ``len(iterable)`` when the
    iterable has one; a plain iterator is not materialised (progress then has no percent).

    Example::

        for batch in tq.progress_loop(batches, msg="training"):
            train(batch)
    """
    n = total
    if n is None:
        try:
            n = len(iterable)  # type: ignore[arg-type]
        except TypeError:
            n = None
    last = float("-inf")
    for i, item in enumerate(iterable):
        now = time.monotonic()
        if now - last >= min_interval_sec:
            last = now
            if suspend_check:
                maybe_suspend()
            if n:
                progress(i / n * 100, msg)
        yield item
    if n:
        progress(100.0, msg)


if __name__ == "__main__":
    # Smoke: print env + current control action
    print("TQ_CONTROL_FILE=", os.environ.get("TQ_CONTROL_FILE"), file=sys.stderr)
    print("action=", poll_control())
