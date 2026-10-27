"""Cheap "has the database changed?" counter for conditional GETs.

``PRAGMA data_version`` only changes when *another* connection commits, and the web layer
uses per-thread read connections, so a dedicated long-lived connection per database file
watches it and bumps an in-process counter. The connection never writes.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

_MIN_INTERVAL_SEC = 0.25


class ChangeTracker:
    def __init__(self, db_path: str | Path, *, min_interval: float = _MIN_INTERVAL_SEC) -> None:
        self._db_path = str(db_path)
        self._min_interval = min_interval
        self._lock = threading.Lock()
        self._conn: sqlite3.Connection | None = None
        self._last_dv: int | None = None
        self._counter = 0
        self._checked_at = 0.0

    def token(self) -> int | None:
        """Monotonic counter that changes after any commit; ``None`` if unavailable."""
        with self._lock:
            now = time.monotonic()
            if self._conn is not None and now - self._checked_at < self._min_interval:
                return self._counter
            try:
                if self._conn is None:
                    self._conn = sqlite3.connect(self._db_path, timeout=1.0, check_same_thread=False)
                dv = int(self._conn.execute("PRAGMA data_version").fetchone()[0])
            except sqlite3.Error:
                self._conn = None
                return None
            if self._last_dv is not None and dv != self._last_dv:
                self._counter += 1
            self._last_dv = dv
            self._checked_at = now
            return self._counter


_TRACKERS: dict[str, ChangeTracker] = {}
_TRACKERS_LOCK = threading.Lock()


def tracker_for(db_path: str | Path) -> ChangeTracker:
    key = str(db_path)
    with _TRACKERS_LOCK:
        t = _TRACKERS.get(key)
        if t is None:
            t = _TRACKERS[key] = ChangeTracker(key)
        return t
