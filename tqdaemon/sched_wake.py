"""Wake the daemon scheduler early (run finished, enqueue, YAML sync)."""

from __future__ import annotations

import threading

_wake = threading.Event()


def wake_scheduler() -> None:
    """T-200: signal the daemon tick to leave ``wait_or_timeout`` early."""
    _wake.set()


def signal_run_finished() -> None:
    """Called from ActiveRun reader/stop paths when _done is set."""
    wake_scheduler()


def wait_or_timeout(sec: float) -> bool:
    """Block up to sec seconds. Returns True if woken early."""
    if sec <= 0:
        return _wake.is_set()
    return _wake.wait(timeout=sec)


def clear() -> None:
    _wake.clear()


def is_set() -> bool:
    """Test helper."""
    return _wake.is_set()
