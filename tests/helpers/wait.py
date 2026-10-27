"""Poll-until helper: replaces fixed ``time.sleep`` waits in tests."""

from __future__ import annotations

import time
from collections.abc import Callable


def wait_until(cond: Callable[[], object], timeout: float = 10.0, interval: float = 0.02, msg: str = "") -> None:
    """Return as soon as *cond()* is truthy; fail with *msg* after *timeout* seconds."""
    deadline = time.monotonic() + timeout
    while True:
        if cond():
            return
        if time.monotonic() >= deadline:
            raise AssertionError(msg or f"condition not met within {timeout}s")
        time.sleep(interval)
