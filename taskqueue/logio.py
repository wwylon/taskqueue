"""Safe log-file reads: byte-tail without loading multi-GB files into memory."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

# Default window for UI / CLI / diagnose scan. Enough for recent context;
# never load an unbounded log into RAM.
DEFAULT_LOG_TAIL_BYTES = 256 * 1024
# Hard cap even when callers pass a larger ?bytes= / --bytes value.
MAX_LOG_TAIL_BYTES = 8 * 1024 * 1024


@dataclass(frozen=True)
class LogTail:
    text: str
    truncated: bool
    file_size: int
    start_offset: int

    @property
    def bytes_returned(self) -> int:
        return len(self.text.encode("utf-8", errors="replace"))


def clamp_tail_bytes(n: int | None, *, default: int = DEFAULT_LOG_TAIL_BYTES) -> int:
    if n is None:
        return default
    try:
        n = int(n)
    except (TypeError, ValueError):
        return default
    if n < 1:
        return default
    return min(n, MAX_LOG_TAIL_BYTES)


def read_log_tail(path: Path | str, *, max_bytes: int = DEFAULT_LOG_TAIL_BYTES) -> LogTail:
    """Return the last ``max_bytes`` of ``path`` (UTF-8, errors replaced).

    When truncated, drops a leading partial line so the result starts on a
    newline boundary when possible. A single line larger than the window is
    returned as-is (still bounded by ``max_bytes``).
    """
    p = Path(path)
    max_bytes = clamp_tail_bytes(max_bytes)
    try:
        size = p.stat().st_size
    except OSError:
        raise
    if size == 0:
        return LogTail("", False, 0, 0)

    with open(p, "rb") as f:
        if size <= max_bytes:
            data = f.read()
            return LogTail(
                data.decode("utf-8", errors="replace"),
                False,
                size,
                0,
            )
        start = size - max_bytes
        f.seek(start)
        data = f.read(max_bytes)
        # Align to next newline so we do not start mid-line.
        nl = data.find(b"\n")
        if nl != -1:
            data = data[nl + 1 :]
            start = start + nl + 1
        return LogTail(
            data.decode("utf-8", errors="replace"),
            True,
            size,
            start,
        )


def format_log_tail(tail: LogTail) -> str:
    """Human-readable body: optional truncation banner + text."""
    if not tail.truncated:
        return tail.text
    shown = tail.file_size - tail.start_offset
    banner = (
        f"--- truncated: showing last {shown} of {tail.file_size} bytes ---\n"
    )
    return banner + tail.text


def tail_log_lines(
    path: Path | str | None,
    n: int = 80,
    *,
    max_scan_bytes: int = DEFAULT_LOG_TAIL_BYTES,
) -> list[str]:
    """Last ``n`` lines via byte-tail (never ``read_text`` of the whole file)."""
    if not path:
        return []
    p = Path(path)
    if not p.is_file():
        return [f"(missing log: {path})"]
    try:
        # Oversample: enough bytes for typical line lengths; still O(max_scan).
        scan = clamp_tail_bytes(
            max(max_scan_bytes, min(MAX_LOG_TAIL_BYTES, max(n, 1) * 512)),
            default=DEFAULT_LOG_TAIL_BYTES,
        )
        tail = read_log_tail(p, max_bytes=scan)
    except OSError as exc:
        return [f"(read error: {exc})"]
    lines = tail.text.splitlines()
    return lines[-n:] if n > 0 else []
