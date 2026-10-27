"""Shared task/run DTO helpers for CLI, MCP, and web.

Canonical serializers live in ``taskqueue.web.responses``; this module is the
stable import path for non-web callers.
"""

from __future__ import annotations

from typing import Any


def task_dict(row: Any, **kwargs: Any) -> dict[str, Any]:
    from taskqueue.web.responses import _task_dict

    return _task_dict(row, **kwargs)


def queue_row_dict(row: Any, **kwargs: Any) -> dict[str, Any]:
    from taskqueue.web.responses import _queue_row_dict

    return _queue_row_dict(row, **kwargs)


def live_run_dict(row: Any, **kwargs: Any) -> dict[str, Any]:
    from taskqueue.web.responses import _live_run_dict

    return _live_run_dict(row, **kwargs)
