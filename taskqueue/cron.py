"""S3: Cron schedule matching and re-queue logic."""

from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime, timedelta, timezone


_CRON_RANGES = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 7))


def _parse_field(field: str, lo: int, hi: int) -> set[int]:
    """Expand one cron field (``*``, ``a``, ``a-b``, ``*/n``, ``a-b/n``, ``a/n``, comma lists)."""
    out: set[int] = set()
    for part in field.split(","):
        base, _, step_s = part.partition("/")
        step = int(step_s) if step_s else 1
        if step < 1:
            raise ValueError(f"bad step in {part!r}")
        if base == "*":
            a, b = lo, hi
        elif "-" in base:
            a_s, b_s = base.split("-", 1)
            a, b = int(a_s), int(b_s)
        else:
            a = int(base)
            b = hi if step_s else a
        if a < lo or b > hi or a > b:
            raise ValueError(f"out of range: {part!r}")
        out.update(range(a, b + 1, step))
    return out


def _parse_cron(expr: str) -> tuple[set[int], ...] | None:
    """Parse a 5-field cron expression into value sets; None when invalid."""
    fields = expr.strip().split()
    if len(fields) != 5:
        return None
    try:
        sets = [_parse_field(f, lo, hi) for f, (lo, hi) in zip(fields, _CRON_RANGES)]
    except (ValueError, TypeError):
        return None
    if 7 in sets[4]:  # dow 7 == Sunday
        sets[4].add(0)
    return tuple(sets)


def cron_valid(expr: object) -> bool:
    return isinstance(expr, str) and _parse_cron(expr) is not None


def cron_matches(expr: str, dt: "datetime") -> bool:  # type: ignore[name-defined]
    """Return True if dt matches a 5-field cron expression (MIN HOUR DOM MON DOW).

    Invalid expressions never match. When both DOM and DOW are restricted, either
    matching is enough (standard cron semantics).
    """
    parsed = _parse_cron(expr)
    if parsed is None:
        return False
    minute_s, hour_s, dom_s, month_s, dow_s = parsed
    if dt.minute not in minute_s or dt.hour not in hour_s or dt.month not in month_s:
        return False
    dom_ok = dt.day in dom_s
    # dt.isoweekday() % 7 → Sun=0, Mon=1, …, Sat=6 (cron convention)
    dow_ok = dt.isoweekday() % 7 in dow_s
    dom_star = len(dom_s) == 31
    dow_star = len(dow_s) >= 7
    if dom_star or dow_star:
        return dom_ok and dow_ok
    return dom_ok or dow_ok


def check_cron_schedule(conn: sqlite3.Connection, state: dict) -> None:
    """Re-queue terminal tasks whose schedule.cron fired since the last daemon tick."""
    from taskqueue._db_tasks import retry_task
    from taskqueue.models import task_key

    now = datetime.now(timezone.utc)
    last: datetime | None = state.get("ts")
    state["ts"] = now
    if last is None:
        return  # First tick — skip to avoid false-firing on startup

    try:
        rows = conn.execute(
            "SELECT project, task_id, status, schedule FROM tasks "
            "WHERE schedule IS NOT NULL AND schedule != '' AND schedule != 'null'"
        ).fetchall()
    except Exception:
        return

    for row in rows:
        if row["status"] not in ("completed", "failed", "interrupted", "suspended", "stopped"):
            continue
        try:
            sched = json.loads(row["schedule"] or "null")
            if not sched or not isinstance(sched, dict):
                continue
            cron_expr = sched.get("cron")
            if not cron_expr:
                continue
        except Exception:
            continue

        check = (last + timedelta(seconds=60)).replace(second=0, microsecond=0)
        now_trunc = now.replace(second=0, microsecond=0)
        fired = False
        while check <= now_trunc:
            if cron_matches(cron_expr, check):
                fired = True
                break
            check += timedelta(minutes=1)

        if fired:
            ok = retry_task(conn, row["project"], row["task_id"])
            if ok:
                print(
                    f"[queue] {time.strftime('%H:%M:%S')} cron fired "
                    f"{task_key(row['project'], row['task_id'])} "
                    f"({cron_expr}) → re-queued",
                    flush=True,
                )
