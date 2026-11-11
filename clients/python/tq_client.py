"""Minimal helpers for tqdaemon task scripts (cooperative control + progress).

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

# Match tqdaemon.models.EXIT_SUSPENDED
EXIT_SUSPENDED = 75

_T = TypeVar("_T")


def progress(pct: float, msg: str = "") -> None:
    """Print a progress line the daemon can parse: (N%)."""
    line = f"{msg} ({pct:.1f}%)" if msg else f"({pct:.1f}%)"
    print(line, flush=True)


def env_info(*, print_it: bool = False) -> dict[str, str | None]:
    """Return runner-injected ``TQ_*`` identity vars (and optionally print them)."""
    keys = ("TQ_HOME", "TQ_PROJECT", "TQ_TASK_ID", "TQ_CONTROL_FILE", "TQ_OPERATOR", "TQ_DRY_RUN")
    info = {k: os.environ.get(k) for k in keys}
    if print_it:
        for k, v in info.items():
            print(f"{k}={v}", flush=True)
    return info


def require_gpu(*, exit_code: int = 2) -> None:
    """Fail fast when no usable GPU is present (YAML ``--require-gpu`` convention).

    Tries ``torch.cuda.is_available()``, then ``nvidia-smi -L``. On failure prints a
    clear message and raises ``SystemExit(exit_code)``.
    """
    try:
        import torch  # type: ignore[import-untyped]

        if bool(torch.cuda.is_available()):
            return
        print("require_gpu: torch.cuda.is_available() is False", flush=True)
        raise SystemExit(exit_code)
    except SystemExit:
        raise
    except Exception:
        pass
    import shutil
    import subprocess

    smi = shutil.which("nvidia-smi")
    if smi:
        try:
            r = subprocess.run(
                [smi, "-L"],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
            if r.returncode == 0 and (r.stdout or "").strip():
                return
            print(
                f"require_gpu: nvidia-smi -L failed (rc={r.returncode}): {(r.stderr or r.stdout or '').strip()}",
                flush=True,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            print(f"require_gpu: nvidia-smi error: {exc}", flush=True)
    else:
        print("require_gpu: neither torch.cuda nor nvidia-smi available", flush=True)
    raise SystemExit(exit_code)


def metric(**fields: Any) -> None:
    """Emit structured metrics the daemon merges into ``runs.metrics_json`` (last value per key wins).

    Example: ``tq.metric(epoch=3, loss=0.124)`` prints ``TQ_METRIC {"epoch": 3, "loss": 0.124}``.
    """
    if fields:
        print("TQ_METRIC " + json.dumps(fields, default=str), flush=True)


def control_file() -> Path | None:
    raw = os.environ.get("TQ_CONTROL_FILE")
    return Path(raw) if raw else None


def get_control() -> dict[str, Any] | None:
    """Return the full control-file payload, or None if idle/missing."""
    path = control_file()
    if path is None or not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def poll_control() -> str | None:
    """Return control action (`suspend`, `hold`, …) or None if idle/missing."""
    data = get_control()
    if not data:
        return None
    action = data.get("action")
    return str(action) if action else None


def request_suspend_exit() -> None:
    """Exit with EXIT_SUSPENDED after optional message."""
    print(f"cooperative suspend requested; exiting {EXIT_SUSPENDED}", flush=True)
    raise SystemExit(EXIT_SUSPENDED)


def hold_until() -> float | None:
    """Unix epoch until which a hold is active, or None if not holding."""
    data = get_control()
    if not data or data.get("action") != "hold":
        return None
    until = data.get("until")
    try:
        return float(until) if until is not None else None
    except (TypeError, ValueError):
        return None


def maybe_hold(*, sleep_sec: float = 1.0, clock: Callable[[], float] = time.time) -> bool:
    """If control asks for hold and ``until`` is in the future, sleep and return True.

    Suspend always wins: if action is ``suspend``, exit 75 instead.
    Returns False when not holding (caller should do useful work).
    """
    data = get_control()
    if not data:
        return False
    action = data.get("action")
    if action == "suspend":
        request_suspend_exit()
    if action != "hold":
        return False
    until = hold_until()
    if until is None or clock() >= until:
        return False
    print(f"cooperative hold until {until:.0f}; idling", flush=True)
    while True:
        data = get_control()
        if not data:
            return True
        action = data.get("action")
        if action == "suspend":
            request_suspend_exit()
        if action != "hold":
            return True
        try:
            u = float(data.get("until"))
        except (TypeError, ValueError):
            return True
        now = clock()
        if now >= u:
            return True
        time.sleep(min(sleep_sec, max(0.05, u - now)))


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
    """If control file asks for suspend, exit 75. Hold is handled by ``maybe_hold``."""
    if poll_control() == "suspend":
        request_suspend_exit()
    maybe_hold()


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
