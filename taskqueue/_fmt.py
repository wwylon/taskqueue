"""Shared formatting helpers for durations and byte sizes."""

from __future__ import annotations


def fmt_duration(secs: float) -> str:
    """Human-readable duration: Xd HH:MM:SS, HhMMmSSs, MmSSs, or Ss."""
    s = max(0, int(secs))
    d, rem = divmod(s, 86400)
    h, rem = divmod(rem, 3600)
    m, sec = divmod(rem, 60)
    if d:
        return f"{d}d{h:02d}h{m:02d}m"
    if h:
        return f"{h}h{m:02d}m{sec:02d}s"
    if m:
        return f"{m}m{sec:02d}s"
    return f"{sec}s"


def fmt_bytes(n: int) -> str:
    """Human-readable byte size: 1.2 GiB, 345 MiB, etc."""
    x = float(n)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if x < 1024.0 or unit == "TiB":
            if unit == "B":
                return f"{int(x)} {unit}"
            return f"{x:.1f} {unit}"
        x /= 1024.0
    return f"{n} B"


# Status colours for terminal output, matching the health levels (taskqueue.health):
# red = failed, amber = needs a look, green = done, cyan = running.
_STATUS_ANSI = {
    "failed": "31", "unknown": "31", "cancelled": "31",
    "interrupted": "33", "stopped": "33", "suspended": "33",
    "completed": "32",
    "running": "36",
}


def use_color(stream=None) -> bool:
    """ANSI colour only for an interactive terminal; a non-empty ``NO_COLOR`` turns it off."""
    import os
    import sys

    stream = stream or sys.stdout
    if os.environ.get("NO_COLOR"):
        return False
    try:
        return stream.isatty()
    except (AttributeError, ValueError):
        return False


def color_status(text: str, status: str, *, enabled: bool | None = None) -> str:
    """Wrap already-padded ``text`` in the colour for ``status`` (padding stays aligned)."""
    code = _STATUS_ANSI.get(status)
    if not code or not (use_color() if enabled is None else enabled):
        return text
    return f"\033[{code}m{text}\033[0m"


def shown_progress(status: str | None, pct: float | None) -> float | None:
    """Progress worth showing (same rule as the web UI's ``_progressPct``), or None.

    Running: live value. Failed / interrupted / suspended / stopped / unknown: where the last
    run got to. Queued / disabled: only a partial last run (the resume point). Completed: never.
    """
    if pct is None or status == "completed":
        return None
    try:
        pct = float(pct)
    except (TypeError, ValueError):
        return None
    if status in ("queued", "disabled") and not 0 < pct < 100:
        return None
    return pct
