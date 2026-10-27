"""Shared runtime context for the HTTP Handler."""
from __future__ import annotations

import sqlite3
import threading
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable


@dataclass
class HandlerContext:
    """Values formerly closed over by ``make_handler``.

    ``make_handler`` mirrors these onto BoundHandler class attributes
    (``_db_path``, ``_projects_ref``, …) for minimal method churn.
    """

    conn: sqlite3.Connection
    active_runs: dict
    system_info: dict[str, Any]
    projects_ref: Callable[[], dict]
    db_path: Path
    bind_host: str = "127.0.0.1"
    pending_sync: set[str] = field(default_factory=set)
    pending_file_sync: set[str] = field(default_factory=set)
    sse_semaphore: threading.Semaphore | None = None
    tls: threading.local = field(default_factory=threading.local)
    # G1: per-worker command queues for low-latency command delivery.
    pending_commands: dict[str, deque] = field(default_factory=dict)
    commands_condition: threading.Condition = field(
        default_factory=threading.Condition
    )
