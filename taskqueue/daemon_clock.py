"""Wall-clock jump detection helpers extracted from daemon.py (F-208).

Kept pure so tests can import without the full TickEnv machinery.
"""

from __future__ import annotations


def clock_jump_threshold(scheduler_interval_sec: float, cfg_override: float = 0.0) -> float:
    """Gap (seconds) that counts as host sleep / clock jump."""
    if cfg_override and float(cfg_override) > 0:
        return float(cfg_override)
    return max(2.0 * float(scheduler_interval_sec), 30.0)


def is_clock_jump(prev_wall: float, now_wall: float, threshold: float) -> bool:
    return prev_wall > 0.0 and (now_wall - prev_wall) > threshold
