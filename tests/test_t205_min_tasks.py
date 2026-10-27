"""T-205: per-project ``min_tasks`` reserved slots."""

from __future__ import annotations

from pathlib import Path

import pytest

from taskqueue.db import get_all_project_limits, get_project_limits, open_db, set_project_limit
from taskqueue.scheduler import (
    FilterContext,
    filter_project_min,
    reserved_slots_for_others,
)


def test_min_tasks_is_a_project_limit_field(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "q.db")
    set_project_limit(conn, "b", "min_tasks", 2)
    assert get_project_limits(conn, "b")["min_tasks"] == 2.0
    assert get_all_project_limits(conn)["b"]["min_tasks"] == 2.0


def test_reserved_counts_only_projects_with_queued_work() -> None:
    limits = {"b": {"min_tasks": 2}, "c": {"min_tasks": 3}}
    queued = {"b": 4}  # c idle -> reserves nothing
    assert reserved_slots_for_others(limits, queued, {}, "a") == 2


def test_reserved_subtracts_running_and_excludes_self() -> None:
    limits = {"b": {"min_tasks": 2}}
    assert reserved_slots_for_others(limits, {"b": 3}, {"b": 1}, "a") == 1
    assert reserved_slots_for_others(limits, {"b": 3}, {"b": 2}, "a") == 0
    assert reserved_slots_for_others(limits, {"b": 3}, {}, "b") == 0


def test_filter_blocks_when_cap_would_starve_other_project() -> None:
    ctx = FilterContext(n_running=2, g_max_tasks=3, min_reserved_others=1)
    fr = filter_project_min(ctx)
    assert not fr.ok and "min_tasks" in fr.reason


def test_filter_admits_when_slack_remains() -> None:
    assert filter_project_min(FilterContext(n_running=1, g_max_tasks=3, min_reserved_others=1)).ok


def test_filter_noop_without_global_cap_or_with_force() -> None:
    assert filter_project_min(FilterContext(n_running=9, g_max_tasks=0, min_reserved_others=2)).ok
    assert filter_project_min(
        FilterContext(n_running=2, g_max_tasks=3, min_reserved_others=1, force=True)
    ).ok
