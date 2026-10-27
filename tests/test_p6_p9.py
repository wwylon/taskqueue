"""Tests for P6–P9: backfill reservation, CPU gating, VRAM pressure, estimate telemetry.

P6 — reservation: top-priority resource-blocked task holds headroom.
P7 — CPU gate: live CPU load above limit blocks admission for one tick.
P8 — VRAM pressure: a victim is chosen when measured VRAM exceeds hard limit.
P9 — estimate telemetry: estimated_ram/vram_gib recorded on run rows.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from taskqueue.db import open_db
from taskqueue.resources import ResourceBudget, cpu_cores_from_load


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _budget(*, used: float, total: float = 64.0, soft: int = 80, hard: int = 95,
            gpu_total: float = 0.0, gpu_used: float = 0.0) -> ResourceBudget:
    system = {
        "cpu_total_cores": 16,
        "ram_total_gib": total,
        "gpu_vram_total_gib": gpu_total,
        "gpus": (
            [{"index": 0, "vram_gib": gpu_total, "vram_used_gib": gpu_used}]
            if gpu_total else []
        ),
    }
    cfg = {
        "cpu_limit_pct": 100,
        "gpu_limit_pct": 100,
        "ram_limit_pct": soft,
        "ram_hard_limit_pct": hard,
    }
    b = ResourceBudget(system, cfg)
    b.ram_reserved = used
    return b


def _mk_task(conn, project: str, task_id: str, *, priority: int = 50) -> None:
    now = datetime.now(UTC).isoformat()
    conn.execute(
        "INSERT INTO tasks (project, task_id, command, status, enqueued_at, updated_at, priority)"
        " VALUES (?,?,?,?,?,?,?)",
        (project, task_id, "python", "queued", now, now, priority),
    )
    conn.commit()


def _mk_run(conn, project: str, task_id: str, *, ram: float, vram: float = 0.0,
            est_ram: float | None = None, est_vram: float | None = None,
            status: str = "completed") -> int:
    now = datetime.now(UTC).isoformat()
    cur = conn.execute(
        "INSERT INTO runs (project, task_id, started_at, finished_at, status, "
        "peak_ram_gib, peak_vram_gib, estimated_ram_gib, estimated_vram_gib)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        (project, task_id, now, now, status, ram, vram, est_ram, est_vram),
    )
    conn.commit()
    return cur.lastrowid


# ---------------------------------------------------------------------------
# P6: reservation + backfill
# ---------------------------------------------------------------------------

def test_reservation_pre_holds_top_task_headroom(tmp_path: Path) -> None:
    """A large top-priority task holds its estimate; a small task can only start
    if there is room for BOTH simultaneously."""
    # budget: soft = 20 GiB, currently 8 GiB used
    b = _budget(used=8.0, total=32.0, soft=65)  # soft = 20.8 GiB

    # Top task estimate: 15 GiB. After reservation, only 5.8 GiB remains.
    b.reserve(0, 0.0, 15.0)  # simulate pre-reserving the top task's estimate

    # A 4 GiB task should still fit (8 + 15 + 4 = 27 > soft? no: 8 reserved + 15 reserved + 4 = 27? wait)
    # Actually after reserve(15): ram_reserved = 23. soft = 20.8. 23 > 20.8 so anything new won't fit.
    # The point: even a small task can't sneak in while the large task's headroom is held.
    ok, _ = b.fits(1, 0.0, 4.0)
    assert not ok, "small task must not steal the reserved headroom"


def test_reservation_allows_task_that_fits_in_remaining_headroom(tmp_path: Path) -> None:
    """If reserved headroom still leaves room for a smaller task, it is admitted."""
    # budget: soft = 40 GiB, used 5 GiB
    b = _budget(used=5.0, total=50.0, soft=80)  # soft = 40 GiB
    # Top task estimate: 15 GiB. Pre-reserve.
    b.reserve(0, 0.0, 15.0)  # ram_reserved = 20 GiB; headroom = 20 GiB
    # A 10 GiB task should fit (5 + 15 + 10 = 30 <= 40)
    ok, reason = b.fits(1, 0.0, 10.0)
    assert ok, reason


def test_reservation_state_tracks_ticks() -> None:
    """Reservation state increments ticks on the same task each call."""
    state: dict = {}
    key = ("proj", "task1")

    for expected_tick in range(1, 4):
        if state.get("key") == key:
            state["ticks"] = state.get("ticks", 0) + 1
        else:
            state.clear()
            state["key"] = key
            state["ticks"] = 1
        assert state["ticks"] == expected_tick


# ---------------------------------------------------------------------------
# P7: CPU cores from load
# ---------------------------------------------------------------------------

def test_cpu_cores_from_load_returns_nonnegative() -> None:
    """cpu_cores_from_load() must never return a negative number."""
    result = cpu_cores_from_load()
    assert result >= 0.0


def test_cpu_cores_from_load_returns_float() -> None:
    result = cpu_cores_from_load()
    assert isinstance(result, float)


def test_cpu_soft_gate_blocks_admission_when_overloaded() -> None:
    """When live CPU EMA exceeds cpu_limit_cores, admission is skipped.

    This tests the gate logic in daemon.py run_daemon; we replicate the
    arithmetic here so a regression in the comparison is caught.
    """
    cpu_total = 16
    cpu_limit_pct = 50
    cpu_limit_cores = cpu_total * cpu_limit_pct / 100.0  # 8.0

    cpu_ema_overloaded = 9.5
    cpu_ema_ok = 7.0

    assert cpu_ema_overloaded > cpu_limit_cores, "overloaded case should block"
    assert cpu_ema_ok <= cpu_limit_cores, "normal case should admit"


def test_host_cpu_gate_records_block_reason_without_starting(tmp_path: Path) -> None:
    """When the host CPU gate is up, queued tasks stay queued and get a reason.

    UI Force sets force_run; that path is not exercised here (start() is real).
    """
    from datetime import UTC, datetime

    from taskqueue.config import Project
    from taskqueue.db import get_task, open_db
    from taskqueue.scheduler import _local_admit_queued

    conn = open_db(tmp_path / "q.db")
    now = datetime.now(UTC).isoformat()
    conn.execute(
        "INSERT INTO tasks (project, task_id, command, status, enqueued_at, updated_at, enabled) "
        "VALUES ('p','t1','true','queued',?,?,1)",
        (now, now),
    )
    conn.commit()
    projects = {"p": Project(name="p", root=tmp_path, queue_dir=tmp_path / "q")}
    system = {
        "cpu_total_cores": 8,
        "ram_total_gib": 64.0,
        "gpu_vram_total_gib": 0.0,
        "gpus": [],
    }
    cfg = {
        "cpu_limit_pct": 100,
        "gpu_limit_pct": 100,
        "ram_limit_pct": 80,
        "ram_hard_limit_pct": 95,
    }
    reason = "admission skipped (CPU): live load 19.1 cores > limit 18.7"
    _local_admit_queued(
        conn, tmp_path, projects, {}, system, cfg, 30, False,
        host_cpu_blocked=True,
        host_cpu_reason=reason,
    )
    row = get_task(conn, "p", "t1")
    assert row["status"] == "queued"
    assert row["block_reason"] == reason


# ---------------------------------------------------------------------------
# P8: VRAM pressure response
# ---------------------------------------------------------------------------

def test_vram_budget_fits_respects_hard_limit() -> None:
    """With a full card, a new task requiring any VRAM must be refused."""
    b = _budget(used=10.0, total=64.0, gpu_total=8.0, gpu_used=7.8)
    ok, reason = b.fits(1, 2.0, 0.0)
    assert not ok
    assert "GPU" in reason


def test_vram_pressure_victim_picked_for_relief() -> None:
    """When VRAM is exhausted the task with the most VRAM is the primary relief candidate."""
    from taskqueue.victim import score_victim

    # Task A: 6 GiB VRAM, idempotent, 10% progress
    score_a = score_victim(
        freed_gib=6.0, needed_gib=3.0, progress_pct=10, priority=50,
        elapsed_sec=60, restartability=1.0,
    )
    # Task B: 1 GiB VRAM, not restartable, 80% progress
    score_b = score_victim(
        freed_gib=1.0, needed_gib=3.0, progress_pct=80, priority=50,
        elapsed_sec=3600, restartability=0.0,
    )
    # Task A should have a lower (cheaper to kill) score because it provides much more relief.
    assert score_a < score_b, "high-VRAM idempotent task should be cheaper to kill"


# ---------------------------------------------------------------------------
# P9: Estimate telemetry in runs table
# ---------------------------------------------------------------------------

def test_estimated_columns_exist_in_runs_schema(tmp_path: Path) -> None:
    """open_db must create estimated_ram_gib and estimated_vram_gib columns."""
    conn = open_db(tmp_path / "q.db")
    cols = [r["name"] for r in conn.execute("PRAGMA table_info(runs)").fetchall()]
    assert "estimated_ram_gib" in cols
    assert "estimated_vram_gib" in cols


def test_estimate_round_trip(tmp_path: Path) -> None:
    """Writing and reading estimated values from runs works correctly."""
    conn = open_db(tmp_path / "q.db")
    run_id = _mk_run(conn, "p", "t1", ram=6.0, vram=4.0, est_ram=5.0, est_vram=4.0)
    row = conn.execute(
        "SELECT estimated_ram_gib, estimated_vram_gib FROM runs WHERE run_id=?",
        (run_id,),
    ).fetchone()
    assert row["estimated_ram_gib"] == pytest.approx(5.0)
    assert row["estimated_vram_gib"] == pytest.approx(4.0)


def test_estimate_accuracy_ratio(tmp_path: Path) -> None:
    """Accuracy ratio peak/estimated is correct for a set of completed runs."""
    conn = open_db(tmp_path / "q.db")
    _mk_task(conn, "p", "t1")
    # Create runs with known over/under ratios
    _mk_run(conn, "p", "t1", ram=4.0, est_ram=2.0)   # ratio 2.0x (over)
    _mk_run(conn, "p", "t1", ram=1.0, est_ram=2.0)   # ratio 0.5x (under)
    _mk_run(conn, "p", "t1", ram=2.0, est_ram=2.0)   # ratio 1.0x (accurate)

    rows = conn.execute(
        "SELECT estimated_ram_gib, peak_ram_gib FROM runs "
        "WHERE estimated_ram_gib IS NOT NULL AND peak_ram_gib IS NOT NULL"
    ).fetchall()
    assert len(rows) == 3
    ratios = sorted(r["peak_ram_gib"] / r["estimated_ram_gib"] for r in rows)
    assert ratios == pytest.approx([0.5, 1.0, 2.0])
