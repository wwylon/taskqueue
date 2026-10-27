from datetime import datetime, timedelta, timezone

import pytest

from taskqueue.cron import cron_matches
from taskqueue.timeline_forecast import _next_cron_fire


def _brute(expr, now, hours):
    cur = (now + timedelta(minutes=1)).replace(second=0, microsecond=0)
    end = now + timedelta(hours=hours)
    while cur <= end:
        if cron_matches(expr, cur):
            return cur
        cur += timedelta(minutes=1)
    return None


@pytest.mark.parametrize(
    "expr",
    ["*/15 * * * *", "30 3 * * *", "0 0 * * 1", "5 4 15 * *", "0 9-17 * * 1-5", "0 0 31 2 *", "bad", ""],
)
def test_matches_bruteforce(expr) -> None:
    now = datetime(2026, 10, 18, 12, 7, 33, tzinfo=timezone.utc)
    for hours in (1, 24, 48):
        exp = None if expr in ("bad", "") else _brute(expr, now, hours)
        assert _next_cron_fire(expr, now, hours=hours) == exp
