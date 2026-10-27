"""Unit tests for taskqueue.cron — cron_matches and check_cron_schedule."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from taskqueue.cron import check_cron_schedule, cron_matches


# ---------------------------------------------------------------------------
# cron_matches — field parsing
# ---------------------------------------------------------------------------

def _dt(minute=0, hour=0, day=1, month=1, weekday=1):
    """Build a UTC datetime for testing. weekday: Mon=1 … Sun=7 (isoweekday)."""
    return datetime(2025, month, day, hour, minute, tzinfo=timezone.utc).replace(
        # Override isoweekday via a known date: 2025-01-06 is Mon(1)
    )


def _dt_raw(year=2025, month=1, day=6, hour=0, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=timezone.utc)


def test_star_matches_any():
    dt = _dt_raw(minute=37, hour=14)
    assert cron_matches("* * * * *", dt)


def test_exact_minute_match():
    dt = _dt_raw(minute=30)
    assert cron_matches("30 * * * *", dt)
    assert not cron_matches("31 * * * *", dt)


def test_exact_hour_match():
    dt = _dt_raw(hour=3, minute=0)
    assert cron_matches("0 3 * * *", dt)
    assert not cron_matches("0 4 * * *", dt)


def test_step_expression():
    # Every 15 minutes
    for m in (0, 15, 30, 45):
        assert cron_matches("*/15 * * * *", _dt_raw(minute=m))
    assert not cron_matches("*/15 * * * *", _dt_raw(minute=7))


def test_range_expression():
    # Hours 9-17
    for h in range(9, 18):
        assert cron_matches("0 9-17 * * *", _dt_raw(hour=h, minute=0))
    assert not cron_matches("0 9-17 * * *", _dt_raw(hour=8, minute=0))
    assert not cron_matches("0 9-17 * * *", _dt_raw(hour=18, minute=0))


def test_comma_list():
    for m in (0, 15, 45):
        assert cron_matches("0,15,45 * * * *", _dt_raw(minute=m))
    assert not cron_matches("0,15,45 * * * *", _dt_raw(minute=30))


def test_day_of_week():
    # 2025-01-06 = Monday = isoweekday 1 → cron DOW 1
    mon = _dt_raw(year=2025, month=1, day=6)
    assert cron_matches("0 0 * * 1", mon)
    assert not cron_matches("0 0 * * 0", mon)   # Sunday
    assert not cron_matches("0 0 * * 2", mon)   # Tuesday


def test_sunday_is_zero():
    # 2025-01-05 = Sunday = isoweekday 7 → cron DOW 0
    sun = _dt_raw(year=2025, month=1, day=5)
    assert cron_matches("0 0 * * 0", sun)
    assert not cron_matches("0 0 * * 1", sun)


def test_dom_and_month():
    dt = _dt_raw(year=2025, month=3, day=15)
    assert cron_matches("0 0 15 3 *", dt)
    assert not cron_matches("0 0 15 4 *", dt)
    assert not cron_matches("0 0 14 3 *", dt)


def test_invalid_field_count_returns_false():
    dt = _dt_raw()
    assert not cron_matches("* * * *", dt)       # 4 fields
    assert not cron_matches("* * * * * *", dt)   # 6 fields
    assert not cron_matches("", dt)


def test_step_with_base():
    # 2/5 means: starting at minute 2, every 5 → 2,7,12,17,...
    assert cron_matches("2/5 * * * *", _dt_raw(minute=2))
    assert cron_matches("2/5 * * * *", _dt_raw(minute=7))
    assert not cron_matches("2/5 * * * *", _dt_raw(minute=3))


# ---------------------------------------------------------------------------
# check_cron_schedule — integration with DB
# ---------------------------------------------------------------------------

def _seed_scheduled_task(db, project="proj", task_id="t1", status="completed"):
    import json
    now = "2025-01-06T00:00:00+00:00"
    db.execute(
        "INSERT INTO tasks (project, task_id, status, command, enqueued_at, updated_at, enabled, schedule) "
        "VALUES (?, ?, ?, 'true', ?, ?, 1, ?)",
        (project, task_id, status, now, now, json.dumps({"cron": "0 2 * * *"})),
    )
    db.commit()


def test_first_tick_skips(tmp_path: Path):
    from taskqueue.db import open_db
    db = open_db(tmp_path / "q.db")
    _seed_scheduled_task(db)
    state: dict = {}
    check_cron_schedule(db, state)  # first tick — should not re-queue
    row = db.execute("SELECT status FROM tasks WHERE task_id='t1'").fetchone()
    assert row["status"] == "completed"   # unchanged


def test_cron_fires_on_match(tmp_path: Path):
    from taskqueue.db import open_db
    from unittest.mock import patch
    db = open_db(tmp_path / "q.db")
    _seed_scheduled_task(db, status="completed")

    # Prime state with last tick = 01:59 so the 02:00 cron fires
    last = datetime(2025, 1, 6, 1, 59, tzinfo=timezone.utc)
    state = {"ts": last}
    fake_now = datetime(2025, 1, 6, 2, 1, tzinfo=timezone.utc)

    with patch("taskqueue.cron.datetime") as mock_dt:
        mock_dt.now.return_value = fake_now
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
        check_cron_schedule(db, state)

    row = db.execute("SELECT status FROM tasks WHERE task_id='t1'").fetchone()
    assert row["status"] == "queued"


def test_cron_does_not_fire_before_match(tmp_path: Path):
    from taskqueue.db import open_db
    from unittest.mock import patch
    db = open_db(tmp_path / "q.db")
    _seed_scheduled_task(db, status="completed")

    # Last tick at 01:30, now at 01:45 — "0 2 * * *" hasn't fired yet
    last = datetime(2025, 1, 6, 1, 30, tzinfo=timezone.utc)
    state = {"ts": last}
    fake_now = datetime(2025, 1, 6, 1, 45, tzinfo=timezone.utc)

    with patch("taskqueue.cron.datetime") as mock_dt:
        mock_dt.now.return_value = fake_now
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
        check_cron_schedule(db, state)

    row = db.execute("SELECT status FROM tasks WHERE task_id='t1'").fetchone()
    assert row["status"] == "completed"   # unchanged


def test_cron_only_fires_for_terminal_status(tmp_path: Path):
    from taskqueue.db import open_db
    from unittest.mock import patch
    db = open_db(tmp_path / "q.db")
    _seed_scheduled_task(db, status="running")  # running — should not re-queue

    last = datetime(2025, 1, 6, 1, 59, tzinfo=timezone.utc)
    state = {"ts": last}
    fake_now = datetime(2025, 1, 6, 2, 1, tzinfo=timezone.utc)

    with patch("taskqueue.cron.datetime") as mock_dt:
        mock_dt.now.return_value = fake_now
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
        check_cron_schedule(db, state)

    row = db.execute("SELECT status FROM tasks WHERE task_id='t1'").fetchone()
    assert row["status"] == "running"   # unchanged


# ---------------------------------------------------------------------------
# Phase 9: robustness / semantics
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("expr", ["*/0 * * * *", "MON * * * *", "5-30/5 * * * *x", "61 * * * *", "a b c d e", "* * *"])
def test_invalid_cron_never_matches_and_never_raises(expr):
    assert cron_matches(expr, _dt_raw(minute=5)) is False


def test_range_with_step_and_dow_seven():
    assert cron_matches("5-30/5 * * * *", _dt_raw(minute=10))
    assert not cron_matches("5-30/5 * * * *", _dt_raw(minute=11))
    # 2025-01-05 is a Sunday: both 0 and 7 mean Sunday
    assert cron_matches("0 0 * * 7", _dt_raw(day=5))
    assert cron_matches("0 0 * * 0", _dt_raw(day=5))


def test_dom_or_dow_when_both_restricted():
    # 2025-01-06 is Monday (dow 1), day 6. "15th OR Monday" should match.
    assert cron_matches("0 0 15 * 1", _dt_raw(day=6))
    assert not cron_matches("0 0 15 * 2", _dt_raw(day=6))


def test_bad_cron_rejected_at_parse():
    from taskqueue.taskspec import validate_task_spec

    with pytest.raises(ValueError, match="schedule.cron"):
        validate_task_spec({"task_id": "t", "command": "x", "schedule": {"cron": "*/0 * * * *"}}, Path("t.yaml"))
    validate_task_spec({"task_id": "t", "command": "x", "schedule": {"cron": "*/5 * * * *"}}, Path("t.yaml"))
